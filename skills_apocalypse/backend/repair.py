#!/usr/bin/env python3
"""Repair a Claude Code session .jsonl so it resumes cleanly on
text-only endpoints that reject non-text content block types.

Scan-only (`scan_for_unsupported`) and atomic rewrite
(`repair_transcript`) live here. Also exposes a CLI.

Keep-sets:
  default      {text, thinking, image, tool_use, tool_result}
  strict       {text, thinking, tool_use, tool_result}
               = ARK / z.ai / glm / deepseek / minimax-family whitelists.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time

DEFAULT_KEEP = {"text", "thinking", "image", "tool_use", "tool_result"}
TEXT_KEEP = {"text", "thinking", "tool_use", "tool_result"}

PLACEHOLDER = (
    "[Removed {orig} content block — not supported by current endpoint/model. "
    "Session repaired. If you need this content, re-read the source file on a "
    "compatible model.]"
)

MAX_BYTES = 200 * 1024 * 1024  # 200 MB
BACKUP_KEEP_DEFAULT = 3


def walk_content(content, where, line_no, findings, mutate, keep):
    """Iterate a content-block list; record and optionally replace
    offenders. Returns the number of blocks replaced.

    A block whose `type` is empty or missing is treated as "unknown but
    keepable" — we don't drop it, since it may be a tool-specific
    extension. The audit distinguishes "other_unknown" only when a real
    non-empty type was found in the keep-set complement.
    """
    changed = 0
    if not isinstance(content, list):
        return 0
    for idx, b in enumerate(content):
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        # Empty/missing `type` -> keep as-is, do not record.
        if not t or not isinstance(t, str):
            continue
        if t not in keep:
            try:
                size = len(json.dumps(b, ensure_ascii=False))
            except Exception:
                size = 0
            findings.append((line_no, where, t, size))
            if mutate:
                content[idx] = {"type": "text", "text": PLACEHOLDER.format(orig=t)}
                changed += 1
        if t == "tool_result" and isinstance(b.get("content"), list):
            changed += walk_content(b["content"], "nested", line_no, findings, mutate, keep)
    return changed


def scan_for_unsupported(path, *, strip_images):
    """Read-only scan of a Claude Code session jsonl. Returns a
    summary dict; never modifies the file."""
    p = os.path.abspath(path)
    keep = TEXT_KEEP if strip_images else DEFAULT_KEEP
    counts = {"document": 0, "image": 0, "other_unknown": 0}
    total_blocks = 0
    total_bytes = 0
    sample = []
    found = False

    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for line_no, raw in enumerate(fh, 1):
                raw = raw.rstrip("\n")
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                line_findings = []
                walk_content(content, "top", line_no, line_findings,
                             mutate=False, keep=keep)
                for fln, where, t, size in line_findings:
                    found = True
                    total_blocks += 1
                    total_bytes += size
                    if t == "document":
                        counts["document"] += 1
                    elif t == "image":
                        counts["image"] += 1
                    else:
                        counts["other_unknown"] += 1
                    if len(sample) < 20:
                        sample.append({
                            "line": fln,
                            "where": where,
                            "type": t,
                            "size": size,
                        })
    except FileNotFoundError:
        raise

    try:
        st = os.stat(p)
        mtime = st.st_mtime
        size = st.st_size
    except OSError:
        mtime = 0.0
        size = 0

    return {
        "path": p,
        "found": found,
        "counts": counts,
        "total_blocks": total_blocks,
        "total_bytes": total_bytes,
        "sample": sample,
        "mtime": mtime,
        "size": size,
    }


def _sha256(path):
    h = __import__("hashlib").sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rotate_backup(path, *, keep=BACKUP_KEEP_DEFAULT):
    """Ensure `<path>.bak` holds a copy of `path`. Idempotent: if the
    existing `.bak` already matches the source, no rotation. If they
    differ, the existing `.bak` is moved to a timestamped sibling and
    the new copy becomes the canonical slot. Returns the absolute path
    of the canonical `.bak`."""
    p = os.path.abspath(path)
    bak = p + ".bak"
    if not os.path.exists(p):
        raise FileNotFoundError(p)
    # Fast path: no existing .bak — skip hashing entirely.
    if not os.path.exists(bak):
        shutil.copy2(p, bak)
        return bak
    # Slow path: compare hashes to decide whether to rotate.
    if _sha256(p) == _sha256(bak):
        return bak
    # Move the old `.bak` aside to make room.
    import time as _t
    ts = str(int(_t.time() * 1000))
    rotated = f"{bak}.1.{ts}"
    # Handle same-ms collisions (rare but possible on Windows).
    n = 1
    while os.path.exists(rotated):
        n += 1
        rotated = f"{bak}.1.{ts}.{n}"
    os.replace(bak, rotated)
    # Write the new content into the canonical slot.
    shutil.copy2(p, bak)
    # Trim rotated backups to `keep`.
    rotated_files = []
    for entry in os.listdir(os.path.dirname(p) or "."):
        if entry.startswith(os.path.basename(bak) + ".1."):
            rotated_files.append(os.path.join(os.path.dirname(p) or ".", entry))
    if len(rotated_files) > keep:
        rotated_files.sort(key=lambda f: os.stat(f).st_mtime)
        for old in rotated_files[: len(rotated_files) - keep]:
            try:
                os.unlink(old)
            except OSError:
                pass
    return bak


# Per-session lock registry. Each session id maps to its own lock so
# concurrent repairs of DIFFERENT sessions don't block each other.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_META = threading.Lock()


def repair_lock_for(session_id: str) -> threading.Lock:
    with _LOCKS_META:
        lk = _LOCKS.get(session_id)
        if lk is None:
            lk = threading.Lock()
            _LOCKS[session_id] = lk
        return lk


def _emit_repair_event(session_id, replaced_blocks, replaced_bytes, backup_path, cwd, strip_images):
    """Append one audit line to events.jsonl. Best-effort — failure is
    logged but does not fail the repair."""
    try:
        from pathlib import Path
        from datetime import datetime, timezone
        events_file = Path.home() / ".claude" / "apocalypse" / "events.jsonl"
        events_file.parent.mkdir(parents=True, exist_ok=True)
        evt = {
            "type": "repair",
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session_id": session_id,
            "replaced_blocks": replaced_blocks,
            "replaced_bytes": replaced_bytes,
            "backup_path": backup_path,
            "cwd": cwd,
            "strip_images": strip_images,
        }
        with open(events_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(evt, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        import sys
        print(f"[repair] could not write audit event: {exc}", file=sys.stderr)


def _extract_cwd_from_jsonl(path: str) -> str:
    """Read the first `cwd` field from a Claude session jsonl. Returns
    empty string if none is found."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                cwd = obj.get("cwd")
                if isinstance(cwd, str):
                    return cwd
    except OSError:
        pass
    return ""


def repair_transcript(path, *, strip_images, mtime_before=None, max_bytes=MAX_BYTES, lock=None):
    """Atomically strip non-text blocks from a Claude session jsonl.

    Returns a dict. See the spec for the full schema. `mtime_before` is
    a safety check: if the file's mtime has changed by more than 1.5s
    since the caller's snapshot, the operation aborts with
    `error="mtime_changed"`. The caller passes `lock` (from
    `repair_lock_for`) for concurrent-exclusion; if not provided, no
    lock is acquired (caller is responsible).
    """
    started = time.monotonic()
    p = os.path.abspath(path)
    sid = os.path.splitext(os.path.basename(p))[0]
    keep = TEXT_KEEP if strip_images else DEFAULT_KEEP

    def fail(error, **extra):
        return {
            "ok": False,
            "error": error,
            "replaced_blocks": 0,
            "replaced_bytes": 0,
            "backup_path": None,
            "mtime_before": 0.0,
            "mtime_after": 0.0,
            "size_before": 0,
            "size_after": 0,
            "duration_ms": int((time.monotonic() - started) * 1000),
            **extra,
        }

    try:
        st = os.stat(p)
    except OSError as exc:
        return fail(f"stat failed: {exc}")

    mtime_now = st.st_mtime
    size_before = st.st_size
    if size_before > max_bytes:
        return fail("file too large", size=size_before)
    if mtime_before is not None and abs(mtime_now - mtime_before) >= 1.5:
        return fail("mtime_changed", mtime_before=mtime_before, mtime_now=mtime_now)

    # Determine if there is anything to repair (cheap pre-check).
    pre = scan_for_unsupported(p, strip_images=strip_images)
    if not pre["found"]:
        # Nothing to do, but still emit a no-op result and skip the
        # atomic-write dance.
        return {
            "ok": True,
            "replaced_blocks": 0,
            "replaced_bytes": 0,
            "backup_path": str(p) + ".bak" if os.path.exists(p + ".bak") else None,
            "mtime_before": mtime_now,
            "mtime_after": mtime_now,
            "size_before": size_before,
            "size_after": size_before,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    # Acquire the lock. If the caller didn't pass one, default to the
    # per-session lock so the CLI path is also concurrency-safe. The
    # server already passes its own (same-object), so this is idempotent.
    effective_lock = lock if lock is not None else repair_lock_for(sid)
    with effective_lock:
        # Re-check mtime inside the lock to catch a writer that landed
        # between the outer check and the lock acquisition.
        try:
            st2 = os.stat(p)
        except OSError as exc:
            return fail(f"stat failed: {exc}")
        # Use the caller's mtime_before (or our initial stat as fallback)
        # for the comparison — not the local `mtime_now` that may have
        # been refreshed between the outer check and the lock acquisition.
        reference = mtime_before if mtime_before is not None else mtime_now
        if abs(st2.st_mtime - reference) >= 1.5:
            return fail("mtime_changed", mtime_before=reference, mtime_now=st2.st_mtime)
        mtime_now = st2.st_mtime

        # Back up first (still inside the lock so the backup name is
        # unique and stable).
        try:
            backup_path = rotate_backup(p)
        except Exception as exc:  # noqa: BLE001
            return fail(f"backup failed: {exc}")

        # Atomic rewrite: write to a tmp file in the same directory,
        # then `os.replace`.
        tmp_dir = os.path.dirname(p) or "."
        fd, tmp_path = tempfile.mkstemp(
            prefix=os.path.basename(p) + ".repair.",
            suffix=".tmp",
            dir=tmp_dir,
        )
        # Close the fd immediately - we'll open it as a text stream next.
        os.close(fd)
        replaced_blocks = 0
        replaced_bytes = 0
        try:
            with open(tmp_path, "w", encoding="utf-8", errors="replace") as out:
                with open(p, "r", encoding="utf-8", errors="replace") as inp:
                    for raw in inp:
                        # Preserve trailing newline state byte-identical.
                        has_nl = raw.endswith("\n")
                        line = raw[:-1] if has_nl else raw
                        if not line.strip():
                            out.write(raw)
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            out.write(raw)
                            continue
                        msg = obj.get("message")
                        content = msg.get("content") if isinstance(msg, dict) else None
                        if not isinstance(content, list):
                            out.write(raw)
                            continue
                        # Detect offending blocks; if any, splice them out
                        # in-place to preserve formatting on the rest of the line.
                        local_findings = []
                        walk_content(content, "top", 0, local_findings, mutate=True, keep=keep)
                        if local_findings:
                            # Build a placeholder JSON literal for the splice.
                            placeholder = json.dumps(
                                {"type": "text", "text": PLACEHOLDER.format(orig=local_findings[0][2])},
                                ensure_ascii=False,
                            )
                            spliced = _splice_block_in_line(line, content, local_findings, placeholder)
                            if spliced is not None:
                                for _fln, _where, t, size in local_findings:
                                    replaced_blocks += 1
                                    replaced_bytes += size
                                out.write(spliced)
                                if has_nl:
                                    out.write("\n")
                                continue
                            # Splice failed — fall back to re-serialise the
                            # whole record and log to stderr.
                            print(
                                "[repair] splice failed for line; falling back to re-serialise",
                                file=sys.stderr,
                            )
                            for _fln, _where, t, size in local_findings:
                                replaced_blocks += 1
                                replaced_bytes += size
                            out.write(json.dumps(obj, ensure_ascii=False))
                            if has_nl:
                                out.write("\n")
                        else:
                            # No offending blocks -> write original line byte-identical.
                            out.write(raw)
                out.flush()
                # Fsync via the file's fd while we still have it open.
                try:
                    os.fsync(out.fileno())
                except (OSError, AttributeError):
                    pass  # Some streams may not support fsync
        except Exception as exc:  # noqa: BLE001
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return fail(f"rewrite failed: {exc}")
        # Close + atomic replace. No need to fsync again - we already
        # did it above while the file was open. os.replace is atomic.
        try:
            os.replace(tmp_path, p)
        except Exception as exc:  # noqa: BLE001
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return fail(f"atomic replace failed: {exc}")

    # Post-repair stats.
    try:
        st3 = os.stat(p)
        mtime_after = st3.st_mtime
        size_after = st3.st_size
    except OSError:
        mtime_after = 0.0
        size_after = 0

    cwd = _extract_cwd_from_jsonl(p)
    _emit_repair_event(
        session_id=sid,
        replaced_blocks=replaced_blocks,
        replaced_bytes=replaced_bytes,
        backup_path=backup_path,
        cwd=cwd,
        strip_images=strip_images,
    )

    return {
        "ok": True,
        "replaced_blocks": replaced_blocks,
        "replaced_bytes": replaced_bytes,
        "backup_path": backup_path,
        "mtime_before": mtime_now,
        "mtime_after": mtime_after,
        "size_before": size_before,
        "size_after": size_after,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


class _NullContext:
    """A context manager that does nothing. Used when the caller does
    not pass a lock so `with ctx:` still works."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _find_block_spans(content_start, line, content):
    """Walk the raw line starting at `content_start` and yield
    (start_offset, end_offset) pairs for each top-level JSON object
    in the content array. Uses `json.JSONDecoder().raw_decode`
    repeatedly to handle whitespace.

    Returns None if the walk fails (mismatch between parsed tree and
    raw line)."""
    spans = []
    decoder = json.JSONDecoder()
    i = content_start
    n = len(line)
    # Skip whitespace and expect '['.
    while i < n and line[i] in " \t\r\n":
        i += 1
    if i >= n or line[i] != "[":
        return None
    i += 1
    while i < n:
        # Skip whitespace and commas.
        while i < n and line[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        if line[i] == "]":
            break
        # Try to decode one JSON object at position i.
        try:
            obj, end = decoder.raw_decode(line, i)
        except json.JSONDecodeError:
            return None
        spans.append((i, end))
        i = end
    # Sanity: the count of top-level spans must match the parsed content.
    if len(spans) != len(content):
        return None
    return spans


def _splice_block_in_line(line, content, findings, replacement):
    """Replace the offending block(s) in the raw line with `replacement`.

    `content` is the parsed content array (already mutated in-place).
    `findings` is the list of (line_no, where, type, size) tuples from
    walk_content — only the `where` ("top" or "nested") and the
    index into content matter.

    Returns the spliced line on success, or None if the splice walk
    fails (caller should fall back to re-serialising)."""
    # Locate the content array in the raw line by scanning for
    # '"content":' then '['.
    needle = '"content":'
    idx = line.find(needle)
    if idx < 0:
        return None
    bracket = line.find("[", idx + len(needle))
    if bracket < 0:
        return None
    # Determine which top-level indices in `content` were replaced.
    # All findings from a single line correspond to the same top-level
    # indices (walk_content mutates in place), so we collect them here.
    replaced_indices = set()
    for _fln, where, _t, _size in findings:
        if where == "top":
            # We need to know which index in the top-level `content`
            # array was mutated. Since walk_content mutates content[idx]
            # in place and the findings are returned in order, we can
            # walk `content` and detect which entries are now placeholders.
            pass  # detection happens below
    # Simpler: compare current content entries against original by
    # looking for the placeholder text. But we don't have the original.
    # Instead, use the fact that walk_content set content[idx] to
    # {"type": "text", "text": PLACEHOLDER.format(orig=t)} for offenders.
    # Find indices whose `text` starts with "[Removed ".
    replaced_indices = {
        i
        for i, b in enumerate(content)
        if isinstance(b, dict)
        and b.get("type") == "text"
        and isinstance(b.get("text"), str)
        and b["text"].startswith("[Removed ")
    }
    if not replaced_indices:
        return None
    spans = _find_block_spans(bracket, line, content)
    if spans is None:
        return None
    # Splice from right to left so offsets remain valid.
    pieces = []
    cursor = len(line)
    for idx in sorted(replaced_indices, reverse=True):
        start, end = spans[idx]
        pieces.append(line[end:cursor])
        pieces.append(replacement)
        cursor = start
    pieces.append(line[:cursor])
    pieces.reverse()
    return "".join(pieces)


def _find_session_jsonl(session_id: str) -> list[str]:
    """Search ~/.claude/projects/*/ for jsonl files whose name contains
    session_id. Prefer exact `<id>.jsonl` matches, then prefix matches."""
    import os as _os
    projects = _os.path.expanduser("~/.claude/projects")
    if not _os.path.isdir(projects):
        return []
    matches = []
    for entry in _os.listdir(projects):
        proj = _os.path.join(projects, entry)
        if not _os.path.isdir(proj):
            continue
        try:
            for name in _os.listdir(proj):
                if name.endswith(".jsonl") and session_id in name:
                    matches.append(_os.path.join(proj, name))
        except OSError:
            continue
    base = session_id + ".jsonl"

    def rank(p):
        b = _os.path.basename(p)
        if b == base:
            return (0, p)
        if b.startswith(session_id):
            return (1, p)
        return (2, p)

    matches.sort(key=rank)
    return matches


def _parse_cli_args(argv):
    inspect_only = False
    strip_images = False
    session_arg = None
    file_arg = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--inspect":
            inspect_only = True
        elif a in ("--strip-images", "--for-glm5.2", "--for-glb5.2"):
            strip_images = True
        elif a == "--session":
            i += 1
            if i >= len(argv):
                raise SystemExit("--session requires a value")
            session_arg = argv[i]
        elif a.startswith("-"):
            raise SystemExit(f"unknown flag: {a}")
        else:
            if file_arg is not None:
                raise SystemExit(f"unexpected positional arg: {a}")
            file_arg = a
        i += 1
    return inspect_only, strip_images, session_arg, file_arg


def _resolve_path(session_arg, file_arg):
    if session_arg and file_arg:
        raise SystemExit("specify either a file path or --session, not both")
    if session_arg:
        matches = _find_session_jsonl(session_arg)
        if not matches:
            raise SystemExit(f"no jsonl found matching session: {session_arg}")
        if len(matches) > 1:
            print(f"multiple matches for '{session_arg}':")
            for m in matches:
                print(f"  {m}")
            raise SystemExit("refine --session or pass the file path directly")
        return matches[0]
    if file_arg:
        return file_arg
    return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 0
    inspect_only, strip_images, session_arg, file_arg = _parse_cli_args(argv)
    p = _resolve_path(session_arg, file_arg)
    if not p:
        print(__doc__)
        return 1
    if not os.path.isfile(p):
        print(f"not a file: {p}", file=sys.stderr)
        return 1
    if inspect_only:
        res = scan_for_unsupported(p, strip_images=strip_images)
        mode = "for-glm5.2 (strip images too)" if strip_images else "default (strip document)"
        print(f"mode: {mode}")
        print(f"keep-set: {sorted(TEXT_KEEP if strip_images else DEFAULT_KEEP)}")
        print(f"offending blocks (would-be stripped): {res['total_blocks']}")
        for item in res["sample"]:
            print(f"  line {item['line']:>5}  {item['where']:<6}  "
                  f"type={item['type']:<10}  {item['size']}B")
        print("(inspect only — no changes written)")
        return 0
    res = repair_transcript(p, strip_images=strip_images)
    if not res["ok"]:
        print(f"repair failed: {res.get('error')}", file=sys.stderr)
        return 1
    print(f"repaired -> {p}")
    print(f"backup   -> {res['backup_path']}")
    print(f"replaced {res['replaced_blocks']} blocks ({res['replaced_bytes']} bytes)")
    return 0


if __name__ == "__main__":
    import sys as _sys
    raise SystemExit(main(_sys.argv[1:]))
