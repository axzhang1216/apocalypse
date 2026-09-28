#!/usr/bin/env python3
"""Judge whether one unified_transcripts JSONL record has meaningful dialog via Jev.

Credentials come from Apocalypse's local config:
  ~/.claude/apocalypse/harness.json  -> typesafe.model / typesafe.auth
  ~/.claude/apocalypse/secrets.json  -> typesafe_api_key

Fallback order for the API key: --api-key > TYPESAFE_API_KEY env > Apocalypse secrets.

Usage:
  python has_meaningful_dialog.py '{"agent":"claude","role":"user","text":"test",...}'
  python has_meaningful_dialog.py --file unified_transcripts_sample.jsonl --index 100
  echo '<jsonl-line>' | python has_meaningful_dialog.py
  python has_meaningful_dialog.py --file all.jsonl --batch --out results.csv
  python has_meaningful_dialog.py --self-check
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

APOCALYPSE_DIR = Path.home() / ".claude" / "apocalypse"
HARNESS_FILE = APOCALYPSE_DIR / "harness.json"
SECRETS_FILE = APOCALYPSE_DIR / "secrets.json"
DEFAULT_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_THRESHOLD = 0.5
DEFAULT_MAX_TEXT = 3000
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


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _secret_from_auth(auth: dict | None) -> str | None:
    """Same auth kinds as analysis_harness: env / file / literal."""
    if not isinstance(auth, dict):
        return None
    kind = auth.get("kind")
    if kind == "env":
        return os.environ.get(str(auth.get("name") or "")) or None
    if kind == "file":
        try:
            path = Path(os.path.expandvars(os.path.expanduser(str(auth.get("path") or ""))))
            cur: object = json.loads(path.read_text(encoding="utf-8"))
            for part in str(auth.get("json_path") or "").split("."):
                if part:
                    cur = cur[part]  # type: ignore[index]
            return str(cur) if cur is not None else None
        except Exception:
            return None
    if kind == "literal":
        return str(auth.get("value") or "") or None
    return None


def load_typesafe_config() -> dict:
    """Return {api_key, model, api_url} from Apocalypse harness + secrets."""
    harness = _read_json(HARNESS_FILE)
    cfg = harness.get("typesafe") if isinstance(harness.get("typesafe"), dict) else {}
    secrets = _read_json(SECRETS_FILE)

    api_key = (
        _secret_from_auth(cfg.get("auth") if isinstance(cfg.get("auth"), dict) else None)
        or secrets.get("typesafe_api_key")
        or os.environ.get("TYPESAFE_API_KEY")
        or ""
    )
    model = (
        (cfg.get("model") if isinstance(cfg.get("model"), str) else None)
        or os.environ.get("TYPESAFE_MODEL")
        or "jev-latest"
    )
    base = str(cfg.get("base_url") or "https://api.typesafe.ai/v1").rstrip("/")
    api_url = base + ("/systemone" if not base.endswith("/systemone") else "")
    return {"api_key": str(api_key).strip(), "model": model, "api_url": api_url}


def pack_state(record: dict, max_text: int = DEFAULT_MAX_TEXT) -> dict:
    text = record.get("text") or ""
    if not isinstance(text, str):
        text = str(text)
    return {
        "agent": record.get("agent"),
        "role": record.get("role"),
        "project": record.get("project"),
        "text_len": len(text),
        "truncated": len(text) > max_text,
        "text": text[:max_text],
    }


def prefilter(record: dict) -> str | None:
    """Cheap deterministic skips. Returns a reason string, or None to call Jev."""
    role = str(record.get("role") or "")
    if role in ("tool", "toolResult"):
        return "tool"
    if role == "user":
        text = record.get("text") or ""
        if not isinstance(text, str):
            text = str(text)
        if text.strip().lower() in ("test", "继续"):
            return "ping"
    return None


RETRYABLE_HTTP = {429, 500, 502, 503, 504}


def ask_jev(
    state: dict,
    *,
    model: str,
    api_key: str,
    api_url: str = DEFAULT_API_URL,
    timeout: float = 60.0,
    retries: int = 2,
) -> float:
    body = {
        "model": model,
        "state": state,
        "questions": {"meaningful": {"type": "noul", "instructions": INSTRUCTIONS}},
    }
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            api_url,
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            last = RuntimeError(f"TypeSafe HTTP {e.code}: {detail or e.reason}")
            if e.code in RETRYABLE_HTTP and attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise last from e
        except Exception as e:
            last = RuntimeError(f"TypeSafe request failed: {e}")
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise last from e

        try:
            return float(payload["answers"]["meaningful"]["noul"])
        except Exception as e:
            raise RuntimeError(f"Unexpected TypeSafe response: {payload!r}") from e
    raise RuntimeError(f"TypeSafe request failed: {last}")


def judge_record(
    record: dict,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_text: int = DEFAULT_MAX_TEXT,
    model: str | None = None,
    api_key: str | None = None,
) -> dict:
    out = {
        "meaningful": None,
        "noul": None,
        "threshold": threshold,
        "agent": record.get("agent"),
        "role": record.get("role"),
        "text_len": len(record.get("text") or ""),
        "model": model,
        "prefilter": None,
        "error": None,
    }
    reason = prefilter(record)
    if reason:
        out["noul"] = 0.0
        out["meaningful"] = False
        out["prefilter"] = reason
        out["model"] = "prefilter"
        return out

    cfg = load_typesafe_config()
    key = (api_key or cfg["api_key"] or "").strip()
    if not key:
        raise SystemExit(
            "TypeSafe API key missing. Add typesafe_api_key to "
            f"{SECRETS_FILE}, or set TYPESAFE_API_KEY."
        )
    model = model or cfg["model"]
    out["model"] = model
    try:
        noul = ask_jev(
            pack_state(record, max_text=max_text),
            model=model,
            api_key=key,
            api_url=cfg["api_url"],
        )
    except RuntimeError as e:
        out["error"] = str(e)
        return out
    out["noul"] = round(noul, 4)
    out["meaningful"] = noul >= threshold
    return out


CSV_PREVIEW_CHARS = 500
CSV_FIELDS = [
    "index", "line_no", "agent", "role", "text_len",
    "noul", "meaningful", "model", "prefilter", "error",
    "text_preview", "text_truncated",
]


def run_batch(
    jsonl_path: Path,
    out_path: Path | None,
    *,
    workers: int,
    emit_jsonl: Path | None = None,
    source_tag: str | None = None,
    **kwargs,
) -> int:
    records: list[tuple[int, dict]] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict):
                    rec = {"text": ""}
            except json.JSONDecodeError:
                rec = {"text": ""}
            records.append((i, rec))

    results: dict[int, dict] = {}
    error_count = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(judge_record, rec, **kwargs): i for i, rec in records}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as e:  # defensive: judge_record records its own errors
                results[i] = {"error": str(e)}
            done += 1
            if results[i].get("error"):
                error_count += 1
            if done % 50 == 0 or done == len(records):
                print(
                    f"batch {done}/{len(records)} done, {error_count} errors",
                    file=sys.stderr,
                    flush=True,
                )

    # ponytail: no resume/checkpoint — a killed batch just re-runs the whole file
    if out_path is not None:
        with out_path.open("w", newline="", encoding="utf-8-sig") as fo:
            w = csv.DictWriter(fo, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            for i, rec in records:
                r = results[i]
                text = rec.get("text") or ""
                if not isinstance(text, str):
                    text = str(text)
                w.writerow({
                    "index": i,
                    "line_no": rec.get("line_no"),
                    "agent": r.get("agent"),
                    "role": r.get("role"),
                    "text_len": r.get("text_len"),
                    "noul": r.get("noul"),
                    "meaningful": r.get("meaningful"),
                    "model": r.get("model"),
                    "prefilter": r.get("prefilter"),
                    "error": r.get("error"),
                    "text_preview": text[:CSV_PREVIEW_CHARS],
                    "text_truncated": str(len(text) > CSV_PREVIEW_CHARS).lower(),
                })
        print(f"wrote {out_path} ({len(records)} rows, {error_count} errors)", file=sys.stderr)

    if emit_jsonl is not None:
        kept = 0
        with emit_jsonl.open("w", encoding="utf-8", newline="\n") as fo:
            for i, rec in records:
                r = results[i]
                if r.get("meaningful") is not True:
                    continue
                row = dict(rec)
                row["source_file"] = source_tag or jsonl_path.name
                row["source_index"] = i
                row["noul"] = r.get("noul")
                fo.write(json.dumps(row, ensure_ascii=False) + "\n")
                kept += 1
        print(f"wrote {emit_jsonl} ({kept} meaningful / {len(records)})", file=sys.stderr)

    return 1 if error_count else 0


def parse_line(raw: str) -> dict:
    raw = raw.strip()
    if not raw:
        raise SystemExit("empty input line")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"input is not JSON: {e}") from e
    if not isinstance(obj, dict):
        raise SystemExit("JSONL record must be an object")
    return obj


def self_check() -> int:
    # ponytail: smallest runnable check that fails if the judgment contract drifts
    local_cases = [
        ({"role": "tool", "text": '{"output": "huge dump"}'}, False, "tool"),
        ({"role": "toolResult", "text": "# diary"}, False, "tool"),
        ({"role": "user", "text": "test"}, False, "ping"),
        ({"role": "user", "text": "继续"}, False, "ping"),
    ]
    failed = 0
    for record, expect, reason in local_cases:
        out = judge_record(record)
        ok = out["meaningful"] is expect and out.get("prefilter") == reason
        print(json.dumps({"expect": expect, "ok": ok, **out}, ensure_ascii=False))
        failed += not ok

    cfg = load_typesafe_config()
    if not cfg["api_key"]:
        print("self-check: no TypeSafe key; skipped live Jev case", file=sys.stderr)
        return 1 if failed else 0
    live = {
        "agent": "codex",
        "role": "assistant",
        "text": (
            "对，你的理解是对的。\n\n"
            "1. 如果你走 Codex/ChatGPT 套餐，那是按套餐额度计费；\n"
            "2. 如果你自己的服务器直接调 OpenAI API，那是 API 按 token 计费。"
        ),
    }
    out = judge_record(live)
    ok = out["meaningful"] is True and not out.get("prefilter")
    print(json.dumps({"expect": True, "ok": ok, **out}, ensure_ascii=False))
    failed += not ok
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("line", nargs="?", help="one JSONL record as a JSON string")
    p.add_argument("--file", type=Path, help="JSONL file to read a line from")
    p.add_argument("--index", type=int, help="0-based line index into --file")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--max-text", type=int, default=DEFAULT_MAX_TEXT)
    p.add_argument("--model", default=None)
    p.add_argument("--api-key", default=None, help="override Apocalypse/env key")
    p.add_argument("--batch", action="store_true", help="judge every line in --file concurrently")
    p.add_argument("--out", type=Path, default=None, help="CSV output path for --batch")
    p.add_argument("--emit-jsonl", type=Path, default=None, help="write meaningful records as JSONL")
    p.add_argument("--source-tag", default=None, help="source_file label written into --emit-jsonl")
    p.add_argument("--workers", type=int, default=8, help="concurrent requests for --batch")
    p.add_argument("--self-check", action="store_true")
    args = p.parse_args(argv)

    if args.self_check:
        return self_check()

    if args.batch:
        if args.file is None:
            raise SystemExit("--batch requires --file")
        if args.out is None and args.emit_jsonl is None:
            raise SystemExit("--batch requires --out and/or --emit-jsonl")
        return run_batch(
            args.file,
            args.out,
            workers=args.workers,
            emit_jsonl=args.emit_jsonl,
            source_tag=args.source_tag,
            threshold=args.threshold,
            max_text=args.max_text,
            model=args.model,
            api_key=args.api_key,
        )

    if args.file is not None:
        if args.index is None:
            raise SystemExit("--file requires --index")
        with args.file.open(encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i == args.index:
                    record = parse_line(line)
                    break
            else:
                raise SystemExit(f"index {args.index} out of range for {args.file}")
    elif args.line is not None:
        record = parse_line(args.line)
    elif not sys.stdin.isatty():
        record = parse_line(sys.stdin.read())
    else:
        p.print_help()
        return 2

    out = judge_record(
        record,
        threshold=args.threshold,
        max_text=args.max_text,
        model=args.model,
        api_key=args.api_key,
    )
    if out.get("error"):
        raise SystemExit(out["error"])
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["meaningful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
