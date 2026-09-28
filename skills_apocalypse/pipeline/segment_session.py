#!/usr/bin/env python3
"""Segment one cleaned session JSONL into contiguous conversation episodes.

Uses Apocalypse analysis_harness (MiniMax) + segment_conversations_prompt.md.

The LLM is asked only for boundaries/titles. Messages are always reattached from
the original cleaned session by start/end line_no, so text cannot drift.

Usage:
  python segment_session.py --session cleaned_sessions/pi_....jsonl
  python segment_session.py --session cleaned.jsonl --out episodes.jsonl
  python segment_session.py --self-check
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import analysis_harness

PROMPT_FILE = Path(__file__).with_name("segment_conversations_prompt.md")
BOUNDARY_HINT = """
## Extra output constraint for this request

Return JSONL only. Do NOT output thinking process, explanations, or any non-JSONL text.

Each line MUST include:
  conversation_id, session_id, title, start_line_no, end_line_no

IMPORTANT: Use the `line_no` field from each message. The `line_no` values are sequential (1, 2, 3, ...).

You MUST omit the `messages` array to save tokens. Output only the metadata above.

Episodes must be contiguous (no gaps) and cover all input messages.
""".strip()


def load_prompt(path: Path = PROMPT_FILE) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"empty prompt file: {path}")
    return text


def load_session(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{i}: invalid JSON: {e}") from e
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{i}: expected object")
            rows.append(obj)
    if not rows:
        raise ValueError(f"empty session: {path}")
    return rows


def session_id_of(rows: list[dict], fallback: str) -> str:
    for r in rows:
        sid = r.get("session_id")
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
    return fallback


def build_user_payload(rows: list[dict]) -> str:
    # Keep original objects; one JSONL line per message.
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)


def strip_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            return "\n".join(lines[1:-1]).strip()
        return "\n".join(lines[1:]).strip()
    return s


def parse_episode_lines(raw: str) -> list[dict]:
    # Remove <think> tags that MiniMax may include
    text = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL)
    text = re.sub(r'<mm:think>.*?</mm:think>', '', text, flags=re.DOTALL)
    text = strip_fences(text)

    episodes = []
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        # tolerate accidental prose between JSONL rows
        if not (line.startswith("{") and line.endswith("}")):
            m = re.search(r"\{.*\}", line)
            if not m:
                continue
            line = m.group(0)
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"episode line {i}: invalid JSON: {e}\n{line[:200]}") from e
        if not isinstance(obj, dict):
            raise ValueError(f"episode line {i}: expected object")
        episodes.append(obj)
    if not episodes:
        raise ValueError(f"model returned no episode JSONL rows\n---\n{text[:1000]}")
    return episodes


def index_by_line_no(rows: list[dict]) -> dict[int, dict]:
    """Index messages by their line_no field.

    The line_no field contains sequential numbers (1, 2, 3, ...) added during cleaning.
    """
    by: dict[int, dict] = {}
    for r in rows:
        ln = r.get("line_no")
        if ln is None:
            continue
        try:
            by[int(ln)] = r
        except (TypeError, ValueError):
            continue
    if by:
        return by
    # fallback: if no line_no field, use 1-based positions
    return {i + 1: r for i, r in enumerate(rows)}


def ordered_line_nos(rows: list[dict]) -> list[int]:
    """Return ordered line numbers from the line_no field.

    The line_no field contains sequential numbers (1, 2, 3, ...) added during cleaning.
    """
    by = index_by_line_no(rows)
    # preserve session order, not numeric sort
    out = []
    seen = set()
    for r in rows:
        ln = r.get("line_no")
        try:
            key = int(ln)
        except (TypeError, ValueError):
            key = None
        if key is None or key in seen or key not in by:
            continue
        out.append(key)
        seen.add(key)
    if out:
        return out
    # fallback: if no line_no field, return [1, 2, 3, ...]
    return list(range(1, len(rows) + 1))


def rebuild_episodes(raw_episodes: list[dict], rows: list[dict], session_id: str) -> list[dict]:
    line_nos = ordered_line_nos(rows)
    by = index_by_line_no(rows)
    pos = {ln: i for i, ln in enumerate(line_nos)}

    # normalize boundaries
    bounds = []
    for ep in raw_episodes:
        try:
            start = int(ep.get("start_line_no"))
            end = int(ep.get("end_line_no"))
        except (TypeError, ValueError) as e:
            raise ValueError(f"episode missing numeric start/end: {ep!r}") from e
        if start not in pos or end not in pos:
            raise ValueError(
                f"episode boundary not in session line_nos: start={start} end={end} "
                f"known={line_nos[:8]}{'...' if len(line_nos)>8 else ''}"
            )
        if pos[start] > pos[end]:
            raise ValueError(f"episode start after end: {start} > {end}")
        title = str(ep.get("title") or "").strip() or "conversation"
        bounds.append((start, end, title))

    # sort by appearance order; fill gaps / overlaps by forcing contiguous cover
    bounds.sort(key=lambda x: pos[x[0]])

    # If model under-covered, expand first/last and fill holes by merging into previous.
    covered = [None] * len(line_nos)  # type: ignore[var-annotated]
    for ep_i, (start, end, title) in enumerate(bounds):
        for j in range(pos[start], pos[end] + 1):
            if covered[j] is None:
                covered[j] = ep_i
            # overlaps: keep earlier episode (merge policy)

    # assign uncovered positions to nearest previous episode, else next
    for j, owner in enumerate(covered):
        if owner is not None:
            continue
        prev = next((covered[k] for k in range(j - 1, -1, -1) if covered[k] is not None), None)
        nxt = next((covered[k] for k in range(j + 1, len(covered)) if covered[k] is not None), None)
        covered[j] = prev if prev is not None else nxt
    if any(c is None for c in covered):
        # no usable episodes — one big conversation
        covered = [0] * len(line_nos)
        bounds = [(line_nos[0], line_nos[-1], "conversation")]

    # rebuild contiguous groups in order
    groups: list[tuple[int, int, str]] = []
    cur = covered[0]
    start_i = 0
    title_of = {i: t for i, (_s, _e, t) in enumerate(bounds)}
    for j in range(1, len(covered) + 1):
        if j == len(covered) or covered[j] != cur:
            groups.append((line_nos[start_i], line_nos[j - 1], title_of.get(cur, "conversation")))
            if j < len(covered):
                cur = covered[j]
                start_i = j

    out = []
    for i, (start, end, title) in enumerate(groups, 1):
        msgs = []
        for ln in line_nos[pos[start] : pos[end] + 1]:
            msgs.append(by[ln])
        out.append({
            "conversation_id": f"{session_id}::c{i:03d}",
            "session_id": session_id,
            "title": title,
            "start_line_no": start,
            "end_line_no": end,
            "messages": msgs,
        })
    return out


def tag_conclusions(episodes: list[dict]) -> list[dict]:
    """Tag every message with `conclusion` (bool) in place.

    True only for an assistant message that is:
      1. immediately followed by a user message, or
      2. the last message of the episode.
    """
    for ep in episodes:
        msgs = ep.get("messages") or []
        n = len(msgs)
        for i, m in enumerate(msgs):
            if str(m.get("role") or "") != "assistant":
                m["conclusion"] = False
                continue
            next_role = str(msgs[i + 1].get("role") or "") if i + 1 < n else ""
            m["conclusion"] = (next_role == "user") or (i == n - 1)
    return episodes


def segment_session(
    session_path: Path,
    *,
    prompt_path: Path = PROMPT_FILE,
    out_path: Path | None = None,
    max_tokens: int = 4096,
    timeout: float = 180.0,
) -> Path:
    rows = load_session(session_path)
    sid = session_id_of(rows, fallback=session_path.stem)
    system = load_prompt(prompt_path) + "\n\n" + BOUNDARY_HINT
    user = (
        "Segment the following cleaned session JSONL into conversation episodes.\n\n"
        + build_user_payload(rows)
    )

    # Tiny sessions: skip the LLM and emit one episode.
    if len(rows) <= 2:
        episodes = [{
            "conversation_id": f"{sid}::c001",
            "session_id": sid,
            "title": "conversation",
            "start_line_no": rows[0].get("line_no", 1),
            "end_line_no": rows[-1].get("line_no", len(rows)),
            "messages": rows,
        }]
    else:
        raw = analysis_harness.complete(
            user,
            max_tokens=max_tokens,
            system=system,
            timeout=timeout,
        )
        episodes = rebuild_episodes(parse_episode_lines(raw), rows, sid)

    episodes = tag_conclusions(episodes)

    if out_path is None:
        out_path = session_path.with_name(session_path.stem + ".conversations.jsonl")

    with out_path.open("w", encoding="utf-8", newline="\n") as fo:
        for ep in episodes:
            fo.write(json.dumps(ep, ensure_ascii=False) + "\n")
    return out_path


def self_check() -> int:
    # offline: rebuild covers all messages, contiguous, no drift
    rows = [
        {"session_id": "s1", "role": "user", "text": "A start", "line_no": 10},
        {"session_id": "s1", "role": "assistant", "text": "A reply", "line_no": 11},
        {"session_id": "s1", "role": "user", "text": "B start", "line_no": 20},
        {"session_id": "s1", "role": "assistant", "text": "B reply", "line_no": 21},
    ]
    raw = [
        {"title": "topic A", "start_line_no": 10, "end_line_no": 11},
        {"title": "topic B", "start_line_no": 20, "end_line_no": 21},
    ]
    eps = rebuild_episodes(raw, rows, "s1")
    assert len(eps) == 2, eps
    assert eps[0]["conversation_id"] == "s1::c001"
    assert eps[1]["conversation_id"] == "s1::c002"
    assert [m["text"] for m in eps[0]["messages"]] == ["A start", "A reply"]
    assert [m["text"] for m in eps[1]["messages"]] == ["B start", "B reply"]
    # hole filling: model skips middle → still cover
    rows2 = rows + [{"session_id": "s1", "role": "user", "text": "A again", "line_no": 30}]
    raw2 = [
        {"title": "A", "start_line_no": 10, "end_line_no": 11},
        {"title": "A2", "start_line_no": 30, "end_line_no": 30},
    ]
    eps2 = rebuild_episodes(raw2, rows2, "s1")
    assert sum(len(e["messages"]) for e in eps2) == 5
    # conclusion tagging: assistant before user (case 1) + last assistant (case 2)
    rows3 = [
        {"session_id": "s1", "role": "user", "text": "q1", "line_no": 1},
        {"session_id": "s1", "role": "assistant", "text": "a1。结论一。", "line_no": 2},
        {"session_id": "s1", "role": "user", "text": "q2", "line_no": 3},
        {"session_id": "s1", "role": "assistant", "text": "a2。结论二。", "line_no": 4},
    ]
    ep4 = [{"messages": rows3}]
    tag_conclusions(ep4)
    flags = [m.get("conclusion") for m in ep4[0]["messages"]]
    assert flags == [False, True, False, True], flags
    print(json.dumps({"ok": True, "episodes": len(eps), "filled": len(eps2), "conclusions": flags}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--session", type=Path, help="cleaned session JSONL")
    p.add_argument("--prompt", type=Path, default=PROMPT_FILE)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--self-check", action="store_true")
    args = p.parse_args(argv)

    if args.self_check:
        return self_check()
    if args.session is None:
        p.print_help()
        return 2

    out = segment_session(
        args.session,
        prompt_path=args.prompt,
        out_path=args.out,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
    )
    # print a tiny summary
    eps = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
    summary = {
        "output": str(out),
        "episodes": len(eps),
        "titles": [e.get("title") for e in eps],
        "spans": [(e.get("start_line_no"), e.get("end_line_no"), len(e.get("messages") or [])) for e in eps],
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
