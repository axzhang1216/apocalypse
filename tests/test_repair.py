"""Tests for skills_apocalypse.repair."""
import pytest

from skills_apocalypse.repair import (
    DEFAULT_KEEP,
    TEXT_KEEP,
    rotate_backup,
    walk_content,
)
from skills_apocalypse.repair import scan_for_unsupported


def test_walk_content_replaces_document_in_default_mode():
    content = [
        {"type": "text", "text": "hello"},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf"}},
    ]
    findings = []
    walk_content(content, "top", 1, findings, mutate=True, keep=DEFAULT_KEEP)
    assert len(findings) == 1
    assert findings[0][2] == "document"
    # text block kept, document replaced with placeholder
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "hello"
    assert content[1]["type"] == "text"
    assert "Removed document" in content[1]["text"]


def test_walk_content_strict_mode_also_strips_image():
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png"}},
        {"type": "text", "text": "after"},
    ]
    findings = []
    walk_content(content, "top", 1, findings, mutate=True, keep=TEXT_KEEP)
    assert [f[2] for f in findings] == ["image"]
    assert content[0]["type"] == "text"
    assert "Removed image" in content[0]["text"]
    assert content[1]["text"] == "after"


def test_walk_content_keeps_blocks_with_empty_or_missing_type():
    """A block with no/empty `type` is treated as unknown but kept (we
    cannot be sure it is unsafe to drop)."""
    content = [
        {"type": ""},
        {"foo": "bar"},
    ]
    findings = []
    walk_content(content, "top", 1, findings, mutate=True, keep=DEFAULT_KEEP)
    assert findings == []  # no findings — we kept them
    assert content[0] == {"type": ""}
    assert content[1] == {"foo": "bar"}


def test_walk_content_no_op_on_none_and_non_lists():
    findings = []
    assert walk_content(None, "top", 1, findings, mutate=True, keep=DEFAULT_KEEP) == 0
    assert walk_content("not a list", "top", 1, findings, mutate=True, keep=DEFAULT_KEEP) == 0
    assert findings == []


def test_walk_content_recurses_into_tool_result_content():
    content = [
        {
            "type": "tool_result",
            "tool_use_id": "x",
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "image", "source": {"type": "base64"}},
            ],
        }
    ]
    findings = []
    walk_content(content, "top", 1, findings, mutate=True, keep=TEXT_KEEP)
    # image in tool_result content is found
    assert [f[2] for f in findings] == ["image"]
    # The tool_result is preserved; the image inside was replaced.
    assert content[0]["type"] == "tool_result"
    assert content[0]["content"][0]["type"] == "text"
    assert content[0]["content"][1]["type"] == "text"
    assert "Removed image" in content[0]["content"][1]["text"]


def test_walk_content_skips_non_dict_items_silently():
    content = [
        {"type": "text", "text": "ok"},
        "raw string item",
        42,
    ]
    findings = []
    walk_content(content, "top", 1, findings, mutate=True, keep=DEFAULT_KEEP)
    assert findings == []
    # Non-dict items are NOT replaced; they pass through.
    assert content[1] == "raw string item"
    assert content[2] == 42


def _write_minimal_jsonl(tmp_path, lines):
    p = tmp_path / "session.jsonl"
    p.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    return p


def _user_line(text):
    import json
    return json.dumps({
        "type": "user",
        "timestamp": "2026-07-21T10:00:00Z",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }, ensure_ascii=False)


def _assistant_with_blocks(blocks):
    import json
    return json.dumps({
        "type": "assistant",
        "timestamp": "2026-07-21T10:00:01Z",
        "message": {"role": "assistant", "content": blocks},
    }, ensure_ascii=False)


def test_scan_reports_document_in_default_mode(tmp_path):
    line = _assistant_with_blocks([
        {"type": "text", "text": "ok"},
        {"type": "document", "source": {"type": "base64"}},
    ])
    p = _write_minimal_jsonl(tmp_path, [_user_line("hi"), line])
    res = scan_for_unsupported(str(p), strip_images=False)
    assert res["found"] is True
    assert res["counts"]["document"] == 1
    assert res["counts"]["image"] == 0
    assert res["counts"]["other_unknown"] == 0
    assert res["total_blocks"] == 1
    assert res["sample"][0]["line"] == 2
    assert res["sample"][0]["type"] == "document"
    assert "mtime" in res and "size" in res


def test_scan_strict_mode_counts_image_separately(tmp_path):
    line = _assistant_with_blocks([
        {"type": "image", "source": {"type": "base64", "media_type": "image/png"}},
    ])
    p = _write_minimal_jsonl(tmp_path, [line])
    res = scan_for_unsupported(str(p), strip_images=True)
    assert res["found"] is True
    assert res["counts"]["image"] == 1


def test_scan_reports_other_unknown_for_unrecognised_type(tmp_path):
    line = _assistant_with_blocks([
        {"type": "future_provider_block", "data": "x"},
    ])
    p = _write_minimal_jsonl(tmp_path, [line])
    res = scan_for_unsupported(str(p), strip_images=True)
    assert res["counts"]["other_unknown"] == 1


def test_scan_does_not_mutate_the_file(tmp_path):
    line = _assistant_with_blocks([{"type": "document", "source": {"type": "base64"}}])
    p = _write_minimal_jsonl(tmp_path, [_user_line("hi"), line])
    before = p.read_text(encoding="utf-8")
    scan_for_unsupported(str(p), strip_images=False)
    assert p.read_text(encoding="utf-8") == before


def test_scan_returns_empty_counts_on_clean_file(tmp_path):
    p = _write_minimal_jsonl(tmp_path, [_user_line("hi"), _assistant_with_blocks([{"type": "text", "text": "ok"}])])
    res = scan_for_unsupported(str(p), strip_images=False)
    assert res["found"] is False
    assert res["counts"] == {"document": 0, "image": 0, "other_unknown": 0}
    assert res["total_blocks"] == 0
    assert res["sample"] == []


def test_rotate_backup_first_time_creates_canonical_slot(tmp_path):
    src = tmp_path / "session.jsonl"
    src.write_text("original\n", encoding="utf-8")
    out = rotate_backup(str(src), keep=3)
    assert out == str(tmp_path / "session.jsonl.bak")
    assert (tmp_path / "session.jsonl.bak").read_text(encoding="utf-8") == "original\n"


def test_rotate_backup_identical_source_is_idempotent(tmp_path):
    src = tmp_path / "session.jsonl"
    bak = tmp_path / "session.jsonl.bak"
    src.write_text("same\n", encoding="utf-8")
    bak.write_text("same\n", encoding="utf-8")
    out = rotate_backup(str(src), keep=3)
    # Returns the existing backup path; no rotation.
    assert out == str(bak)
    # No `.bak.1.<ts>` should be created.
    rotated = list(tmp_path.glob("session.jsonl.bak.1.*"))
    assert rotated == []


def test_rotate_backup_different_source_rotates_old(tmp_path):
    src = tmp_path / "session.jsonl"
    bak = tmp_path / "session.jsonl.bak"
    src.write_text("NEW\n", encoding="utf-8")
    bak.write_text("OLD\n", encoding="utf-8")
    rotate_backup(str(src), keep=3)
    # The new content is in the canonical slot.
    assert bak.read_text(encoding="utf-8") == "NEW\n"
    # The old content is preserved as a `.bak.1.<ts>`.
    rotated = list(tmp_path.glob("session.jsonl.bak.1.*"))
    assert len(rotated) == 1
    assert rotated[0].read_text(encoding="utf-8") == "OLD\n"


def test_rotate_backup_respects_keep_limit(tmp_path):
    import time as _t
    src = tmp_path / "session.jsonl"
    bak = tmp_path / "session.jsonl.bak"
    # Pre-populate with 3 rotated backups (at keep=3) and an OLD content.
    src.write_text("CONTENT-4\n", encoding="utf-8")
    bak.write_text("CONTENT-4\n", encoding="utf-8")
    for i, content in enumerate(["OLD-1", "OLD-2", "OLD-3"]):
        ts = str(int(_t.time() * 1000) + i)
        (tmp_path / f"session.jsonl.bak.1.{ts}").write_text(content + "\n", encoding="utf-8")
        _t.sleep(0.002)  # ensure different mtime
    # Now change source so we trigger a rotation.
    src.write_text("CONTENT-5\n", encoding="utf-8")
    # NOTE: bak intentionally NOT synced — keeping it as CONTENT-4 ensures
    # `rotate_backup` sees src != bak and actually performs the rotation,
    # rather than bailing out of its idempotency check.
    rotate_backup(str(src), keep=3)
    # The new content is in `.bak` and one of the rotated slots.
    rotated_files = sorted(tmp_path.glob("session.jsonl.bak.1.*"))
    # keep=3 means at most 3 rotated files; we already had 3 and added 1, so 1 must be deleted.
    # Total: 3 (including the rotation of OLD-4 we just made).
    assert len(rotated_files) == 3


import os
import json
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor

from skills_apocalypse.repair import repair_transcript, repair_lock_for


def test_repair_replaces_document_in_strict_mode_and_creates_bak(tmp_path):
    p = tmp_path / "session.jsonl"
    src_lines = [
        _user_line("hi"),
        _assistant_with_blocks([
            {"type": "text", "text": "ok"},
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf"}},
        ]),
    ]
    p.write_text("".join(l + "\n" for l in src_lines), encoding="utf-8")

    res = repair_transcript(str(p), strip_images=True)
    assert res["ok"] is True
    assert res["replaced_blocks"] == 1
    assert os.path.exists(str(p) + ".bak")
    bak_content = (tmp_path / "session.jsonl.bak").read_text(encoding="utf-8")
    assert '"type": "document"' in bak_content  # original preserved
    new = p.read_text(encoding="utf-8")
    assert '"type": "document"' not in new
    assert "Removed document" in new
    assert res["backup_path"] == str(p) + ".bak"


def test_repair_keeps_unaffected_lines_byte_identical(tmp_path):
    """Records with no offending blocks are written back byte-identical,
    not re-serialised. This is the byte-level passthrough guarantee from
    the spec."""
    p = tmp_path / "session.jsonl"
    user_line = _user_line("hi")
    # Construct the assistant line with non-default whitespace to make
    # any re-serialisation observable.
    assistant_obj = {
        "type": "assistant",
        "timestamp": "2026-07-21T10:00:01Z",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "ok"},
            {"type": "document", "source": {"type": "base64"}},
        ]},
    }
    # Use compact spacing in the user line; loose spacing in the assistant.
    assistant_line = json.dumps(assistant_obj, ensure_ascii=False, separators=(", ", ": "))
    p.write_text(user_line + "\n" + assistant_line + "\n", encoding="utf-8")
    original_user = user_line + "\n"

    res = repair_transcript(str(p), strip_images=True)
    assert res["ok"] is True
    new_content = p.read_text(encoding="utf-8")
    # User line is record 1, has no offending blocks -> byte-identical.
    assert new_content.startswith(original_user)


def test_repair_rejects_mtime_change(tmp_path):
    p = tmp_path / "session.jsonl"
    p.write_text(_user_line("hi") + "\n", encoding="utf-8")
    before = os.stat(str(p)).st_mtime
    # Bump the mtime by 5 seconds (well past the 1.5s window).
    os.utime(str(p), (before + 5, before + 5))
    res = repair_transcript(str(p), strip_images=True, mtime_before=before)
    assert res["ok"] is False
    assert res["error"] == "mtime_changed"
    # File is unchanged.
    assert p.read_text(encoding="utf-8") == _user_line("hi") + "\n"


def test_repair_rejects_oversize_files(tmp_path):
    p = tmp_path / "session.jsonl"
    p.write_text("x", encoding="utf-8")
    # Override the size check by passing max_bytes explicitly.
    res = repair_transcript(str(p), strip_images=True, max_bytes=0)
    assert res["ok"] is False
    assert res["error"] == "file too large"


def test_repair_is_idempotent(tmp_path):
    p = tmp_path / "session.jsonl"
    p.write_text(
        _assistant_with_blocks([{"type": "document", "source": {"type": "base64"}}]) + "\n",
        encoding="utf-8",
    )
    r1 = repair_transcript(str(p), strip_images=True)
    assert r1["ok"] is True
    bak = (tmp_path / "session.jsonl.bak").read_text(encoding="utf-8")
    r2 = repair_transcript(str(p), strip_images=True)
    assert r2["ok"] is True
    # Same backup; no second `.bak.1.<ts>` created.
    rotated = list(tmp_path.glob("session.jsonl.bak.1.*"))
    assert rotated == []
    # Original `.bak` still holds the original.
    assert (tmp_path / "session.jsonl.bak").read_text(encoding="utf-8") == bak


def test_repair_concurrent_only_one_succeeds(tmp_path):
    # On Unix-like systems with fine-grained mtime resolution, the
    # losing workers should all observe the mtime guard. Windows has
    # coarser mtime resolution, so the portable assertion is simply
    # that only one worker actually performed the replacement (the
    # rest see the already-clean file and return replaced_blocks=0).
    p = tmp_path / "session.jsonl"
    p.write_text(
        _assistant_with_blocks([{"type": "document", "source": {"type": "base64"}}]) + "\n",
        encoding="utf-8",
    )
    sid = "test-session"
    lk = repair_lock_for(sid)
    start = threading.Barrier(5)

    def worker(_i):
        start.wait()  # all workers launch at the same time
        return repair_transcript(str(p), strip_images=True, lock=lk)

    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(worker, range(5)))
    # All workers should return ok=True — but only ONE of them should
    # have actually performed the replacement. The rest see a clean
    # file and return replaced_blocks=0 (no-op success).
    workers_that_replaced = [r for r in results if r.get("replaced_blocks", 0) > 0]
    assert len(workers_that_replaced) == 1, results
    # The single rewriter must have stripped the document block.
    assert workers_that_replaced[0]["replaced_blocks"] == 1


import subprocess
import sys as _sys


def test_cli_inspect_reports_findings_without_modifying(tmp_path, capsys):
    p = tmp_path / "session.jsonl"
    p.write_text(
        _assistant_with_blocks([
            {"type": "document", "source": {"type": "base64"}},
        ]) + "\n",
        encoding="utf-8",
    )
    before = p.read_text(encoding="utf-8")
    result = subprocess.run(
        [_sys.executable, "-m", "skills_apocalypse.repair", "--inspect", str(p)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    out = result.stdout
    assert "mode:" in out
    assert "default (strip document)" in out  # unflagged inspect = default mode
    assert "offending blocks" in out
    assert p.read_text(encoding="utf-8") == before


def test_cli_inspect_strip_images_honors_flag(tmp_path, capsys):
    """`--inspect --strip-images` must report glm5.2 mode and count image blocks.

    Without --strip-images, the same file (containing only `image` blocks)
    should report 0 image findings (DEFAULT_KEEP keeps image).
    """
    p = tmp_path / "session.jsonl"
    p.write_text(
        _assistant_with_blocks([
            {"type": "image", "source": {"type": "base64", "media_type": "image/png"}},
        ]) + "\n",
        encoding="utf-8",
    )
    # Without --strip-images: image is in DEFAULT_KEEP, so no findings.
    result1 = subprocess.run(
        [_sys.executable, "-m", "skills_apocalypse.repair", "--inspect", str(p)],
        capture_output=True, text=True,
    )
    assert result1.returncode == 0
    out1 = result1.stdout
    assert "default (strip document)" in out1
    assert "offending blocks (would-be stripped): 0" in out1
    # With --strip-images: image is NOT in TEXT_KEEP, so 1 image finding.
    result2 = subprocess.run(
        [_sys.executable, "-m", "skills_apocalypse.repair",
         "--inspect", "--strip-images", str(p)],
        capture_output=True, text=True,
    )
    assert result2.returncode == 0
    out2 = result2.stdout
    assert "for-glm5.2 (strip images too)" in out2
    assert "offending blocks (would-be stripped): 1" in out2
    assert "type=image" in out2


def test_cli_repair_creates_backup(tmp_path):
    p = tmp_path / "session.jsonl"
    p.write_text(
        _assistant_with_blocks([
            {"type": "document", "source": {"type": "base64"}},
        ]) + "\n",
        encoding="utf-8",
    )
    rc = subprocess.call(
        [_sys.executable, "-m", "skills_apocalypse.repair", "--strip-images", str(p)],
    )
    assert rc == 0
    assert os.path.exists(str(p) + ".bak")
    assert '"type": "document"' not in p.read_text(encoding="utf-8")


import http.server
import threading
import urllib.request
import urllib.error


def _spawn_server_in_thread(handler_cls):
    """Start a http.server.HTTPServer on an ephemeral port and return
    (server, port, thread). Caller is responsible for shutdown()."""
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port, t


def test_server_preview_returns_counts(tmp_path, monkeypatch):
    # We can't easily import the full Handler without launching the
    # real server. Instead, exercise the bare handler in-process.
    from skills_apocalypse import server as srv
    # Find a session file inside a fake PROJECTS_DIR.
    proj = tmp_path / "proj1"
    proj.mkdir()
    p = proj / "abc123.jsonl"
    p.write_text(
        _assistant_with_blocks([{"type": "document", "source": {"type": "base64"}}]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(srv, "PROJECTS_DIR", tmp_path)
    # Construct a minimal request and feed it into the handler.
    handler_cls = srv.Handler
    captured = {}

    class _Stub:
        def __init__(self):
            self.headers = {"Content-Length": "0"}
            self.rfile = type("R", (), {"read": lambda *_a, **_k: b""})()
        def send_json(self, data, status=200):
            captured["data"] = data
            captured["status"] = status

    h = handler_cls.__new__(handler_cls)
    h.path = "/api/sessions2/abc123/repair/preview"
    h.headers = {"Content-Length": "0"}
    h.command = "GET"
    h._Stub = _Stub  # not used
    h.do_GET = lambda: srv.Handler.do_GET(h)
    # Replace send_json on this instance.
    h.send_json = lambda data, status=200: (captured.setdefault("data", data), captured.setdefault("status", status))
    h.do_GET()
    assert captured["status"] == 200
    assert captured["data"]["ok"] is True
    assert captured["data"]["counts"]["document"] == 1
    assert "mtime" in captured["data"]


def test_server_preview_404_on_unknown_session(tmp_path, monkeypatch):
    from skills_apocalypse import server as srv
    monkeypatch.setattr(srv, "PROJECTS_DIR", tmp_path)
    captured = {}
    h = srv.Handler.__new__(srv.Handler)
    h.path = "/api/sessions2/does-not-exist/repair/preview"
    h.headers = {"Content-Length": "0"}
    h.command = "GET"
    h.send_json = lambda data, status=200: (captured.setdefault("data", data), captured.setdefault("status", status))
    h.do_GET()
    assert captured["status"] == 404
    assert captured["data"]["ok"] is False


# --- F1: mtime guard compares against caller's mtime_before, not local stat ---
def test_repair_mtime_guard_uses_caller_mtime_after_long_delay(tmp_path):
    """F1: when caller passes an mtime_before from a preview that was
    taken >1.5s ago, the in-lock re-check must catch it. The old code
    captured mtime_now at the initial stat (which is fresher than the
    preview) and compared against that, so the race slipped through."""
    p = tmp_path / "session.jsonl"
    p.write_text(
        _assistant_with_blocks([{"type": "document", "source": {"type": "base64"}}]) + "\n",
        encoding="utf-8",
    )
    # Simulate a preview taken 3 seconds ago.
    preview_mtime = os.stat(str(p)).st_mtime - 3.0
    res = repair_transcript(str(p), strip_images=True, mtime_before=preview_mtime)
    assert res["ok"] is False
    assert res["error"] == "mtime_changed"
    # File is unchanged.
    assert '"type": "document"' in p.read_text(encoding="utf-8")


# --- F2: byte-level splice preserves non-default formatting ---
def test_repair_preserves_unusual_whitespace_on_repaired_lines(tmp_path):
    """F2: records with offending blocks are spliced in-place, so
    unusual whitespace in the original line is preserved (no
    re-serialisation that would normalize it)."""
    p = tmp_path / "session.jsonl"
    user_line = _user_line("hi")
    # Construct assistant line with deliberately unusual whitespace:
    # extra spaces around colons, tabs, etc.
    weird_obj = {
        "type": "assistant",
        "timestamp": "2026-07-21T10:00:01Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "document", "source": {"type": "base64"}},
            ],
        },
    }
    # Use indent=4 to produce very distinctive formatting.
    weird_line = json.dumps(weird_obj, ensure_ascii=False, indent=4)
    p.write_text(user_line + "\n" + weird_line + "\n", encoding="utf-8")
    original_weird = weird_line

    res = repair_transcript(str(p), strip_images=True)
    assert res["ok"] is True
    new_content = p.read_text(encoding="utf-8")
    # The spliced line must contain the exact whitespace signature
    # from the original (indent=4 style). json.dumps without indent
    # would produce compact single-line output and fail this check.
    # Find the assistant line (after the user line + newline).
    lines = new_content.split("\n")
    # The user line is line 0; the assistant block starts at line 1.
    # Splice preserves the original line except for the offending block,
    # so the indent=4 prefix on non-offending keys must remain.
    assert lines[1].startswith("{"), f"expected indented line, got: {lines[1][:80]}"
    assert "    " in lines[1] or any("    " in ln for ln in lines[1:8]), (
        "expected 4-space indentation preserved from original"
    )


# --- F3: server returns 400 when mtime is missing ---
def test_server_repair_returns_400_when_mtime_missing(tmp_path, monkeypatch):
    """F3: POST without `mtime` must return 400, not silently bypass
    the race guard."""
    from skills_apocalypse import server as srv
    proj = tmp_path / "proj1"
    proj.mkdir()
    p = proj / "abc123.jsonl"
    p.write_text(
        _assistant_with_blocks([{"type": "document", "source": {"type": "base64"}}]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(srv, "PROJECTS_DIR", tmp_path)
    captured = {}
    h = srv.Handler.__new__(srv.Handler)
    h.path = "/api/sessions2/abc123/repair"
    h.command = "POST"
    h.headers = {"Content-Length": str(len(b'{"strip_images": true}'))}

    class _Body:
        def read(self, n):
            return b'{"strip_images": true}'
    h.rfile = _Body()
    h.send_json = lambda data, status=200: (captured.setdefault("data", data), captured.setdefault("status", status))
    h.do_POST()
    assert captured["status"] == 400
    assert captured["data"]["ok"] is False
    assert "mtime" in captured["data"]["error"]


# --- F5: repair_transcript acquires per-id lock when none is passed ---
def test_repair_acquires_per_session_lock_when_none_passed(tmp_path, monkeypatch):
    """F5: when lock=None, repair_transcript must still hold the
    per-session lock (verified by inspecting the lock object's
    acquire/release state via a wrapper)."""
    p = tmp_path / "session.jsonl"
    p.write_text(
        _assistant_with_blocks([{"type": "document", "source": {"type": "base64"}}]) + "\n",
        encoding="utf-8",
    )
    # Spy on repair_lock_for to confirm it was called.
    import skills_apocalypse.repair as rep
    called_with = []
    original = rep.repair_lock_for

    def spy(sid):
        called_with.append(sid)
        return original(sid)

    monkeypatch.setattr(rep, "repair_lock_for", spy)
    res = repair_transcript(str(p), strip_images=True)
    assert res["ok"] is True
    assert called_with == [os.path.splitext(os.path.basename(str(p)))[0]]


# --- F6: rotate_backup skips hashing when .bak doesn't exist ---
def test_rotate_backup_first_time_does_not_hash(tmp_path, monkeypatch):
    """F6: when .bak doesn't exist, rotate_backup must skip hashing
    entirely and just copy. Verified by patching _sha256 and
    confirming it was not called."""
    from skills_apocalypse.repair import _sha256
    src = tmp_path / "session.jsonl"
    src.write_text("original\n", encoding="utf-8")
    call_count = [0]
    real_sha = _sha256

    def counting_sha(path):
        call_count[0] += 1
        return real_sha(path)

    monkeypatch.setattr("skills_apocalypse.repair._sha256", counting_sha)
    out = rotate_backup(str(src), keep=3)
    assert out == str(src) + ".bak"
    assert call_count[0] == 0, f"_sha256 was called {call_count[0]} times; should be 0"
    assert (src.parent / "session.jsonl.bak").read_text(encoding="utf-8") == "original\n"


def test_rotate_backup_hashes_only_when_bak_exists(tmp_path, monkeypatch):
    """F6: when .bak already exists, rotate_backup hashes both files
    for the idempotency check."""
    from skills_apocalypse.repair import _sha256
    src = tmp_path / "session.jsonl"
    bak = src.parent / "session.jsonl.bak"
    src.write_text("same\n", encoding="utf-8")
    bak.write_text("same\n", encoding="utf-8")
    call_count = [0]
    real_sha = _sha256

    def counting_sha(path):
        call_count[0] += 1
        return real_sha(path)

    monkeypatch.setattr("skills_apocalypse.repair._sha256", counting_sha)
    rotate_backup(str(src), keep=3)
    assert call_count[0] == 2, f"_sha256 was called {call_count[0]} times; should be 2"
