#!/usr/bin/env python3
"""Extract Knowledge IR from segmented conversation episodes.

Input is a *.conversations.jsonl file. Each line is ONE conversation episode.
This script feeds episodes to the LLM one-by-one using extract_knowledge_ir_prompt.md
and writes one Knowledge IR JSON object per episode.

Usage:
  python extract_knowledge_ir.py --file conversations_output/foo.conversations.jsonl
  python extract_knowledge_ir.py --file foo.conversations.jsonl --out foo.knowledge.jsonl
  python extract_knowledge_ir.py --dir conversations_output --out-dir knowledge_ir --limit 3
  python extract_knowledge_ir.py --self-check
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import analysis_harness

PROMPT_FILE = Path(__file__).with_name("extract_knowledge_ir_prompt.md")

ARRAY_FIELDS = (
    "findings",
    "decisions",
    "ideas",
    "open_questions",
    "tasks",
    "constraints",
    "artifacts",
    "lessons",
    "entities",
    "retrieval_cues",
)
OUTCOME_STATUSES = {"completed", "partial", "blocked", "abandoned", "unknown"}
IMPORTANCE = {"high", "medium", "low"}
OUTPUT_HINT = """
## Extra output constraint for this request

Return exactly ONE JSON object and nothing else.
No Markdown fences, no commentary, no thinking process.
""".strip()


def load_prompt(path: Path = PROMPT_FILE) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit(f"empty prompt file: {path}")
    return text


def load_conversations(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"{path}:{i}: invalid JSON: {e}") from e
            if not isinstance(obj, dict):
                raise SystemExit(f"{path}:{i}: expected object")
            if "messages" not in obj:
                raise SystemExit(f"{path}:{i}: missing messages (not a conversation episode?)")
            rows.append(obj)
    if not rows:
        raise SystemExit(f"empty conversations file: {path}")
    return rows


def slim_conversation(conv: dict) -> dict:
    """Send only fields the extractor needs; drop noul / extras."""
    msgs = []
    for m in conv.get("messages") or []:
        if not isinstance(m, dict):
            continue
        msgs.append({
            "role": m.get("role"),
            "ts": m.get("ts"),
            "text": m.get("text"),
            "line_no": m.get("line_no"),
        })
    return {
        "conversation_id": conv.get("conversation_id"),
        "session_id": conv.get("session_id"),
        "title": conv.get("title"),
        "start_line_no": conv.get("start_line_no"),
        "end_line_no": conv.get("end_line_no"),
        "messages": msgs,
    }


def strip_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            return "\n".join(lines[1:-1]).strip()
        return "\n".join(lines[1:]).strip()
    return s


def parse_json_object(raw: str) -> dict:
    text = strip_fences(raw)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"model returned no JSON object\n---\n{text[:1000]}")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("model JSON root is not an object")
    return obj


def _as_list(v):
    return v if isinstance(v, list) else []


def normalize_ir(obj: dict, conv: dict) -> dict:
    """Fill identity fields from source conversation; coerce empty categories to []."""
    out = dict(obj)
    out["conversation_id"] = (
        out.get("conversation_id")
        or conv.get("conversation_id")
        or ""
    )
    out["session_id"] = out.get("session_id") or conv.get("session_id") or ""
    if not out.get("title"):
        out["title"] = conv.get("title") or "conversation"
    out["goal"] = str(out.get("goal") or "").strip()
    out["summary"] = str(out.get("summary") or "").strip()

    outcome = out.get("outcome") if isinstance(out.get("outcome"), dict) else {}
    status = str(outcome.get("status") or "unknown").strip().lower()
    if status not in OUTCOME_STATUSES:
        status = "unknown"
    out["outcome"] = {
        "status": status,
        "result": str(outcome.get("result") or "").strip(),
    }

    imp = str(out.get("importance") or "medium").strip().lower()
    out["importance"] = imp if imp in IMPORTANCE else "medium"

    for key in ARRAY_FIELDS:
        out[key] = _as_list(out.get(key))

    return out


def extract_one(
    conv: dict,
    *,
    prompt_path: Path = PROMPT_FILE,
    max_tokens: int = 4096,
    timeout: float = 180.0,
) -> dict:
    system = load_prompt(prompt_path) + "\n\n" + OUTPUT_HINT
    payload = slim_conversation(conv)
    user = (
        "Extract Knowledge IR from the following single conversation JSON.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    raw = analysis_harness.complete(
        user,
        max_tokens=max_tokens,
        system=system,
        timeout=timeout,
    )
    return normalize_ir(parse_json_object(raw), conv)


def default_out_path(src: Path, out_dir: Path | None = None) -> Path:
    name = src.name
    if name.endswith(".conversations.jsonl"):
        name = name[: -len(".conversations.jsonl")] + ".knowledge.jsonl"
    else:
        name = src.stem + ".knowledge.jsonl"
    return (out_dir or src.parent) / name


def process_file(
    path: Path,
    *,
    out_path: Path | None = None,
    out_dir: Path | None = None,
    prompt_path: Path = PROMPT_FILE,
    max_tokens: int = 4096,
    timeout: float = 180.0,
    resume: bool = True,
) -> dict:
    conversations = load_conversations(path)
    out_path = out_path or default_out_path(path, out_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    done_ids: set[str] = set()
    existing: list[dict] = []
    if resume and out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                existing.append(obj)
                cid = obj.get("conversation_id")
                if isinstance(cid, str) and cid:
                    done_ids.add(cid)

    written = list(existing)
    errors = []
    skipped = 0
    for i, conv in enumerate(conversations):
        cid = str(conv.get("conversation_id") or f"{path.stem}::idx{i}")
        if cid in done_ids:
            skipped += 1
            continue
        try:
            ir = extract_one(
                conv,
                prompt_path=prompt_path,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            if not ir.get("conversation_id"):
                ir["conversation_id"] = cid
            written.append(ir)
            done_ids.add(ir["conversation_id"])
            # checkpoint after each conversation
            with out_path.open("w", encoding="utf-8", newline="\n") as fo:
                for row in written:
                    fo.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                f"  [{i+1}/{len(conversations)}] {cid}: ok importance={ir.get('importance')}",
                file=sys.stderr,
                flush=True,
            )
        except Exception as e:
            errors.append({"conversation_id": cid, "error": str(e)[:300]})
            print(f"  [{i+1}/{len(conversations)}] {cid}: ERROR {e}", file=sys.stderr, flush=True)

    return {
        "source": str(path),
        "output": str(out_path),
        "conversations": len(conversations),
        "knowledge_records": len(written),
        "skipped_existing": skipped,
        "errors": errors,
    }


def self_check() -> int:
    sample = {
        "conversation_id": "s1::c001",
        "session_id": "s1",
        "title": "demo",
        "messages": [{"role": "user", "text": "hi", "line_no": 1}],
    }
    raw = """```json
{"conversation_id":"s1::c001","session_id":"s1","title":"demo","goal":"g","summary":"s",
 "outcome":{"status":"completed","result":"done"},"importance":"low",
 "findings":[],"decisions":[],"ideas":[],"open_questions":[],"tasks":[],
 "constraints":[],"artifacts":[],"lessons":[],"entities":[],"retrieval_cues":["demo"]}
```"""
    ir = normalize_ir(parse_json_object(raw), sample)
    assert ir["conversation_id"] == "s1::c001"
    assert ir["outcome"]["status"] == "completed"
    assert ir["findings"] == []
    assert ir["retrieval_cues"] == ["demo"]
    slim = slim_conversation({
        **sample,
        "messages": [{"role": "user", "text": "x", "line_no": 3, "noul": 0.9, "agent": "pi"}],
    })
    assert "noul" not in slim["messages"][0]
    print(json.dumps({"ok": True, "title": ir["title"]}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file", type=Path, help="one *.conversations.jsonl file")
    p.add_argument("--dir", type=Path, help="directory of *.conversations.jsonl files")
    p.add_argument("--out", type=Path, default=None, help="output path for --file")
    p.add_argument("--out-dir", type=Path, default=None, help="output directory")
    p.add_argument("--prompt", type=Path, default=PROMPT_FILE)
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--limit", type=int, default=None, help="max conversation files to process")
    p.add_argument("--no-resume", action="store_true", help="ignore existing knowledge outputs")
    p.add_argument("--self-check", action="store_true")
    args = p.parse_args(argv)

    if args.self_check:
        return self_check()

    files: list[Path] = []
    if args.file:
        files.append(args.file)
    if args.dir:
        files.extend(sorted(args.dir.glob("*.conversations.jsonl")))
    # de-dupe while preserving order
    seen = set()
    uniq = []
    for f in files:
        rp = f.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        uniq.append(f)
    files = uniq
    if args.limit is not None:
        files = files[: args.limit]

    if not files:
        p.print_help()
        return 2

    if args.out and (len(files) != 1 or args.file is None):
        raise SystemExit("--out is only valid with a single --file")

    summaries = []
    t0 = time.time()
    for f in files:
        print(f"FILE {f}", file=sys.stderr, flush=True)
        summary = process_file(
            f,
            out_path=args.out if args.file and args.out else None,
            out_dir=args.out_dir,
            prompt_path=args.prompt,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            resume=not args.no_resume,
        )
        summaries.append(summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)

    if len(summaries) > 1:
        total_err = sum(len(s.get("errors") or []) for s in summaries)
        print(
            json.dumps(
                {
                    "files": len(summaries),
                    "knowledge_records": sum(s.get("knowledge_records", 0) for s in summaries),
                    "errors": total_err,
                    "elapsed_sec": round(time.time() - t0, 1),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return 0 if all(not s.get("errors") for s in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
