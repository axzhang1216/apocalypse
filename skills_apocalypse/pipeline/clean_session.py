#!/usr/bin/env python3
"""Clean session with persistent httpx.Client for fast Jev calls.

Rules:
1. User messages go through Jev too; only pure "继续"/"test" pings are dropped
2. Aggressive prefiltering for non-user roles (system/tool/metadata)
3. Persistent httpx.Client with HTTP/2 connection pooling
4. Concurrent processing with shared client

Performance: ~4.6x faster than urllib per-call approach
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import transcripts

SAFE_NAME = re.compile(r"[^\w.\-]+", re.UNICODE)

INSTRUCTIONS = {
    "question": "Does this transcript record contain meaningful conversational content?",
    "yes_means": [
        "A human request, question, instruction, correction, or decision",
        "An agent reply that explains, answers, proposes, or advances a real task",
    ],
    "no_means": [
        "Connectivity / ping text such as 'test' or 'ok'",
        "Slash-command UI chrome or local-command stdout",
        "Raw tool / terminal dumps, paths, JSON blobs, base64, or logs with no conversational substance",
        "System notices that only report model/provider switches or waiting status",
        "Empty or purely mechanical acknowledgements",
    ],
    "note": (
        "Judge this single record alone. role=tool or toolResult is almost never "
        "meaningful conversation unless the text itself is a human/agent utterance."
    ),
}


def _safe(s: str, fallback: str = "unknown") -> str:
    return SAFE_NAME.sub("_", s).strip("_")[:64] or fallback


def output_name(meta: dict) -> str:
    agent = meta.get("agent", "unknown")
    workspace = _safe(meta.get("workspace", "unknown"))
    session_id = _safe(meta.get("session_id", "unknown"))
    return f"{agent}_{workspace}_{session_id}.jsonl"


def detect_agent(path: Path, hint: str | None) -> str:
    if hint:
        return hint
    name_lower = path.name.lower()
    if "rollout-" in name_lower or path.parts and "codex" in str(path.parts):
        return "codex"
    if "chat_history" in name_lower:
        return "grok"
    if "messages.jsonl" in name_lower:
        return "hermes"
    if path.parts and ("openclaw" in str(path.parts) or "agents" in str(path.parts)):
        return "openclaw"
    if path.suffix == ".jsonl":
        return "pi"
    return "claude"


def session_meta(path: Path, agent: str) -> dict:
    data = transcripts.parse_transcript(path, agent)
    return {
        "agent": agent,
        "workspace": data.get("workspace", "unknown"),
        "session_id": data.get("session_id", path.stem),
        "cwd": data.get("cwd", ""),
    }


def prefilter(role: str, text: str) -> str | None:
    """Return reason string if should skip Jev, None otherwise."""
    if not text or len(text) < 3:
        return "empty"
    if role in ("tool", "toolResult", "function", "tool_call"):
        return "tool"
    if role in ("system", "developer"):
        return "system"
    if text.startswith("# AGENTS.md") or text.startswith("<environment_context>"):
        return "metadata"
    if text.startswith("# Context from my IDE setup:") and len(text) < 200:
        return "ide_context"
    return None


def classify_user(text: str) -> str | None:
    """Return 'ping' if a user message is a pure ping, else None (→ Jev)."""
    if text.strip().lower() in ("test", "继续"):
        return "ping"
    return None


def load_session_records(path: Path, agent: str, meta: dict) -> list[dict]:
    user_msgs, transcript = transcripts.parse_transcript_points(path, agent)
    records = []
    for entry in transcript:
        records.append({
            "agent": agent,
            "session_id": meta["session_id"],
            "project": meta["workspace"],
            "role": entry.get("role"),
            "text": entry.get("text") or "",
            "ts": entry.get("ts") or "",
            "line_no": entry.get("line_no"),
        })
    return records


def judge_with_jev(
    client: httpx.Client,
    api_key: str,
    model: str,
    text: str,
    threshold: float,
) -> dict:
    """Judge single message with Jev using persistent client."""
    url = "https://api.typesafe.ai/v1/systemone"
    body = {
        "model": model,
        "state": {"text": text},
        "questions": {"meaningful": {"type": "noul", "instructions": INSTRUCTIONS}},
    }

    try:
        resp = client.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        result = resp.json()
        noul = float(result.get("answers", {}).get("meaningful", {}).get("noul", 0))
        return {
            "meaningful": noul >= threshold,
            "noul": noul,
        }
    except Exception as e:
        return {
            "meaningful": False,
            "error": str(e),
        }


def clean_session(
    session_path: Path | str,
    *,
    agent: str | None = None,
    out_dir: Path | str | None = None,
    workers: int = 8,
    threshold: float = 0.5,
    min_keep: int = 2,
    api_key: str | None = None,
    model: str = "jev-latest",
) -> dict:
    """Clean one session with persistent httpx client."""
    session_path = Path(session_path)
    if not session_path.is_file():
        raise FileNotFoundError(session_path)

    # Load API key
    if api_key is None:
        import os
        api_key = os.environ.get("TYPESAFE_API_KEY")
        if not api_key:
            secrets_path = Path.home() / ".claude" / "apocalypse" / "secrets.json"
            if secrets_path.exists():
                secrets = json.loads(secrets_path.read_text(encoding="utf-8"))
                api_key = secrets.get("typesafe_api_key")
        if not api_key:
            raise ValueError("TYPESAFE_API_KEY not found in env or config")

    agent = detect_agent(session_path, agent)
    meta = session_meta(session_path, agent)
    records = load_session_records(session_path, agent, meta)

    # Apply prefiltering, then queue everything else for Jev
    results: dict[int, dict] = {}
    to_judge = []  # (index, text)

    for i, rec in enumerate(records):
        role = rec.get("role", "")
        text = rec.get("text", "")

        # User messages: only pure "继续"/"test" pings are dropped; the rest goes to Jev.
        if role == "user":
            if classify_user(text):
                results[i] = {"meaningful": False, "noul": 0.0, "prefilter": "ping"}
                continue
            to_judge.append((i, text))
            continue

        # Aggressive prefiltering for non-user roles
        reason = prefilter(role, text)
        if reason:
            results[i] = {"meaningful": False, "noul": 0.0, "prefilter": reason}
            continue

        # Queue for Jev
        to_judge.append((i, text))

    # Batch judge with persistent httpx client
    errors = 0
    if to_judge:
        with httpx.Client(
            http2=True,
            timeout=30.0,
            limits=httpx.Limits(
                max_connections=workers,
                max_keepalive_connections=workers,
            ),
        ) as client:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        judge_with_jev, client, api_key, model, text, threshold
                    ): idx
                    for idx, text in to_judge
                }
                for fut in as_completed(futures):
                    idx = futures[fut]
                    try:
                        results[idx] = fut.result()
                        if results[idx].get("error"):
                            errors += 1
                    except Exception as e:
                        results[idx] = {"meaningful": False, "error": str(e)}
                        errors += 1

    # Build output
    kept_rows = []
    for i, rec in enumerate(records):
        r = results.get(i) or {}
        if r.get("meaningful") is True:
            row = dict(rec)
            # Add sequential line_no as a reference for segmentation
            # This is NOT the original line number, just 1, 2, 3, ...
            row["line_no"] = len(kept_rows) + 1
            # Remove noul - not needed in cleaned output
            row.pop("noul", None)
            kept_rows.append(row)

    out_dir = Path(out_dir) if out_dir else session_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / output_name(meta)

    result = {
        "output": None,
        "kept": len(kept_rows),
        "total": len(records),
        "errors": errors,
        "skipped_reason": None,
        "meta": meta,
        "prefiltered": sum(1 for r in results.values() if r.get("prefilter")),
        "jev_calls": len(to_judge),
    }

    if len(kept_rows) < min_keep:
        result["skipped_reason"] = f"kept {len(kept_rows)} < min_keep {min_keep}"
        return result

    with out_path.open("w", encoding="utf-8", newline="\n") as fo:
        for row in kept_rows:
            fo.write(json.dumps(row, ensure_ascii=False) + "\n")
    result["output"] = str(out_path)
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--session", type=Path, help="Session JSONL file")
    p.add_argument("--agent", type=str)
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--min-keep", type=int, default=2)
    p.add_argument("--api-key", type=str, help="TypeSafe API key")
    p.add_argument("--model", type=str, default="jev-latest")
    p.add_argument("--self-check", action="store_true")
    args = p.parse_args(argv)

    if args.self_check:
        # ponytail: offline check of the user ping-vs-Jev rule (no API call)
        assert classify_user("继续") == "ping"
        assert classify_user("TEST") == "ping"
        assert classify_user("帮我修一下这个 bug") is None
        print(json.dumps({"ok": True}, ensure_ascii=False))
        return 0

    if args.session is None:
        p.print_help()
        return 2

    res = clean_session(
        args.session,
        agent=args.agent,
        out_dir=args.out_dir,
        workers=args.workers,
        threshold=args.threshold,
        min_keep=args.min_keep,
        api_key=args.api_key,
        model=args.model,
    )
    print(json.dumps(res, ensure_ascii=False, default=str))
    return 1 if res.get("errors") or res.get("skipped_reason") else 0


if __name__ == "__main__":
    raise SystemExit(main())
