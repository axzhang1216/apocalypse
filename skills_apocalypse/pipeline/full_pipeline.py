#!/usr/bin/env python3
"""Full pipeline: Raw sessions → L0 parse → Clean (v3) → Segment → Conversations

Optimized pipeline with persistent httpx client for fast Jev calls.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import transcripts
from clean_session import clean_session
from segment_session import segment_session


def find_sessions(archive_root: Path, agents: list[str]) -> list[Path]:
    """Find all session files for specified agents."""
    sessions = []
    for agent in agents:
        agent_dir = archive_root / "agent-chats" / agent
        if not agent_dir.exists():
            continue
        # Find all .jsonl files recursively in agent directory
        for session_file in agent_dir.rglob("*.jsonl"):
            if session_file.is_file():
                sessions.append(session_file)
    return sorted(sessions)


def process_one_session(
    session_path: Path,
    agent: str,
    clean_dir: Path,
    conv_dir: Path,
    threshold: float,
    min_keep: int,
) -> dict:
    """Process single session: parse → clean → segment"""
    session_id = f"{agent}::{session_path.stem}"

    try:
        # Step 1: Clean (includes L0 parsing internally)
        clean_result = clean_session(
            session_path,
            agent=agent,
            out_dir=clean_dir,
            workers=4,  # Per-session workers
            threshold=threshold,
            min_keep=min_keep,
        )

        if clean_result.get("skipped_reason"):
            return {
                "session_id": session_id,
                "status": "skipped",
                "reason": clean_result["skipped_reason"],
                "kept": clean_result["kept"],
                "total": clean_result["total"],
            }

        # Step 2: Segment
        cleaned_path = Path(clean_result["output"])
        conv_path = conv_dir / cleaned_path.name.replace(".jsonl", ".conversations.jsonl")
        segment_session(
            cleaned_path,
            out_path=conv_path,
        )

        # Read the segmented result to get episode count
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
            "kept": clean_result["kept"],
            "total": clean_result["total"],
            "prefiltered": clean_result.get("prefiltered", 0),
            "jev_calls": clean_result.get("jev_calls", 0),
            "errors": clean_result.get("errors", 0),
            "episodes": len(episodes),
            "episode_titles": [ep.get("title", "conversation") for ep in episodes[:3]],
        }

    except Exception as e:
        return {
            "session_id": session_id,
            "status": "error",
            "error": str(e)[:200],
        }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", type=Path, required=True, help="Archive root directory")
    p.add_argument("--agents", type=str, required=True, help="Comma-separated agent names")
    p.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "full_pipeline_output")
    p.add_argument("--workers", type=int, default=8, help="Concurrent sessions")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--min-keep", type=int, default=2)
    args = p.parse_args(argv)

    archive_root = args.archive.resolve()
    agents = [a.strip() for a in args.agents.split(",")]
    out_dir = args.out_dir.resolve()

    clean_dir = out_dir / "cleaned"
    conv_dir = out_dir / "conversations"
    clean_dir.mkdir(parents=True, exist_ok=True)
    conv_dir.mkdir(parents=True, exist_ok=True)

    # Find all sessions
    print(f"[pipeline] Scanning archive: {archive_root}")
    sessions = find_sessions(archive_root, agents)
    print(f"[pipeline] Found {len(sessions)} sessions")

    if not sessions:
        print("[pipeline] No sessions found")
        return 1

    # Process sessions
    start_time = time.time()
    results = []
    ok_count = 0
    skip_count = 0
    error_count = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for session_path in sessions:
            agent = None
            for a in agents:
                if a in str(session_path):
                    agent = a
                    break
            if not agent:
                agent = agents[0]

            fut = executor.submit(
                process_one_session,
                session_path,
                agent,
                clean_dir,
                conv_dir,
                args.threshold,
                args.min_keep,
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
                print(f"[{i}/{len(sessions)}] {session_id}: ok")
                print(f"  → {episodes} episodes: {titles}")
            elif status == "skipped":
                skip_count += 1
                print(f"[{i}/{len(sessions)}] {session_id}: skipped - {result.get('reason')}")
            else:
                error_count += 1
                print(f"[{i}/{len(sessions)}] {session_id}: error - {result.get('error')}")

    elapsed = time.time() - start_time

    # Summary
    print(f"\n=== Pipeline Complete ===")
    print(f"Total sessions: {len(sessions)}")
    print(f"  Success: {ok_count}")
    print(f"  Skipped: {skip_count}")
    print(f"  Errors: {error_count}")
    print(f"Time: {elapsed/60:.1f} minutes ({elapsed:.0f}s)")
    print(f"\nOutput:")
    print(f"  Cleaned: {clean_dir}")
    print(f"  Conversations: {conv_dir}")

    # Statistics
    total_kept = sum(r.get("kept", 0) for r in results if r["status"] == "ok")
    total_messages = sum(r.get("total", 0) for r in results if r["status"] == "ok")
    total_episodes = sum(r.get("episodes", 0) for r in results if r["status"] == "ok")
    total_jev_calls = sum(r.get("jev_calls", 0) for r in results if r["status"] == "ok")

    print(f"\nStatistics:")
    print(f"  Total messages: {total_messages}")
    print(f"  Kept after cleaning: {total_kept} ({total_kept*100/total_messages:.1f}%)")
    print(f"  Total episodes: {total_episodes}")
    print(f"  Jev API calls: {total_jev_calls}")
    print(f"  Avg Jev calls/session: {total_jev_calls/ok_count:.1f}")

    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
