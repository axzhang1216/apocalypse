#!/usr/bin/env python3
"""Segment pipeline: Cleaned sessions → Conversations (episodes)

Reads cleaned .jsonl files and segments them into conversation episodes using LLM.
Uses Anthropic API configured via environment variables.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from segment_session import segment_session


def find_cleaned_sessions(clean_dir: Path) -> list[Path]:
    """Find all cleaned session files."""
    return sorted(clean_dir.glob("*.jsonl"))


def process_one_session(
    cleaned_path: Path,
    conv_dir: Path,
    max_tokens: int,
    timeout: float,
) -> dict:
    """Segment one cleaned session into conversations."""
    session_id = cleaned_path.stem
    conv_path = conv_dir / cleaned_path.name.replace(".jsonl", ".conversations.jsonl")

    # Skip if output already exists
    if conv_path.exists() and conv_path.stat().st_size > 100:
        # Read existing result
        episodes = []
        try:
            with open(conv_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        episodes.append(json.loads(line))
            return {
                "session_id": session_id,
                "status": "skipped",
                "reason": "output already exists",
                "episodes": len(episodes),
            }
        except Exception:
            # If reading fails, re-process
            pass

    try:
        segment_session(
            cleaned_path,
            out_path=conv_path,
            max_tokens=max_tokens,
            timeout=timeout,
        )

        # Read result to count episodes
        episodes = []
        if conv_path.exists():
            with open(conv_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        episodes.append(json.loads(line))

        return {
            "session_id": session_id,
            "status": "ok",
            "episodes": len(episodes),
            "episode_titles": [ep.get("title", "conversation") for ep in episodes[:3]],
        }

    except Exception as e:
        error_msg = str(e)[:200]
        return {
            "session_id": session_id,
            "status": "error",
            "error": error_msg,
        }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--clean-dir", type=Path, required=True, help="Directory with cleaned sessions")
    p.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "conversations_output")
    p.add_argument("--workers", type=int, default=4, help="Concurrent sessions")
    p.add_argument("--max-tokens", type=int, default=4096, help="Max tokens for segmentation prompt")
    p.add_argument("--timeout", type=float, default=300.0, help="Timeout per session (seconds)")
    p.add_argument("--limit", type=int, default=None, help="Limit number of sessions to process")
    args = p.parse_args(argv)

    clean_dir = args.clean_dir.resolve()
    conv_dir = args.out_dir.resolve()
    conv_dir.mkdir(parents=True, exist_ok=True)

    # Note: segment_session uses analysis_harness which reads config from
    # ~/.claude/apocalypse/harness.json (MiniMax API configured there)

    # Find cleaned sessions
    print(f"[segment-pipeline] Scanning: {clean_dir}")
    sessions = find_cleaned_sessions(clean_dir)

    if args.limit:
        sessions = sessions[:args.limit]

    print(f"[segment-pipeline] Found {len(sessions)} cleaned sessions")

    if not sessions:
        print("[segment-pipeline] No sessions found")
        return 1

    # Process sessions
    start_time = time.time()
    results = []
    ok_count = 0
    skip_count = 0
    error_count = 0

    print(f"[segment-pipeline] Starting segmentation with {args.workers} workers...")
    print(f"[segment-pipeline] Timeout per session: {args.timeout}s")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for session_path in sessions:
            fut = executor.submit(
                process_one_session,
                session_path,
                conv_dir,
                args.max_tokens,
                args.timeout,
            )
            futures[fut] = session_path

        for i, fut in enumerate(as_completed(futures), 1):
            result = fut.result()
            results.append(result)

            status = result["status"]
            session_id = result["session_id"]

            if status == "ok":
                ok_count += 1
                episodes = result.get("episodes", 0)
                titles = ", ".join(result.get("episode_titles", []))
                print(f"[{i}/{len(sessions)}] {session_id}: ok ({episodes} episodes)")
                if titles:
                    print(f"  → {titles}")
            elif status == "skipped":
                skip_count += 1
                episodes = result.get("episodes", 0)
                print(f"[{i}/{len(sessions)}] {session_id}: skipped (already exists, {episodes} episodes)")
            else:
                error_count += 1
                error_msg = result.get("error", "unknown error")
                print(f"[{i}/{len(sessions)}] {session_id}: error - {error_msg}")

    elapsed = time.time() - start_time

    # Summary
    print(f"\n=== Segment Pipeline Complete ===")
    print(f"Total sessions: {len(sessions)}")
    print(f"  Success: {ok_count}")
    print(f"  Skipped: {skip_count}")
    print(f"  Errors: {error_count}")
    print(f"Time: {elapsed/60:.1f} minutes ({elapsed:.0f}s)")
    print(f"\nOutput: {conv_dir}")

    # Statistics
    total_episodes = sum(r.get("episodes", 0) for r in results if r["status"] == "ok")
    if ok_count > 0:
        print(f"\nStatistics:")
        print(f"  Total episodes: {total_episodes}")
        print(f"  Avg episodes/session: {total_episodes/ok_count:.1f}")
        print(f"  Avg time/session: {elapsed/len(sessions):.1f}s")

    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
