"""Tests for the live conversation stream watcher (backend/stream_watcher.py).

All agent roots, the data dir, and batch conversations dirs are redirected
into a temp directory so nothing on this machine is read or written.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import stream_watcher as sw  # noqa: E402


def _claude_user(text, ts, cwd=None):
    d = {"type": "user", "timestamp": ts, "message": {"content": text}}
    if cwd:
        d["cwd"] = cwd
    return d


def _claude_assistant(text, ts):
    return {"type": "assistant", "timestamp": ts,
            "message": {"content": [{"type": "text", "text": text}]}}


class WatcherTestCase(unittest.TestCase):
    """Redirects every path the watcher touches into a temp dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sw_test_"))
        # raw session roots, one per agent (env vars are read at poll time)
        self.roots = {
            "CLAUDE_CONFIG_DIR": self.tmp / "claude",
            "CODEX_HOME": self.tmp / "codex",
            "PI_CODING_AGENT_SESSION_DIR": self.tmp / "pi",
            "OPENCLAW_STATE_DIR": self.tmp / "openclaw",
            "GROK_HOME": self.tmp / "grok",
            "HERMES_HOME": self.tmp / "hermes",
            "APOCALYPSE_BATCH_CONVERSATIONS_DIR": self.tmp / "batch",
        }
        for p in self.roots.values():
            p.mkdir(parents=True, exist_ok=True)
        self.env = mock.patch.dict(os.environ, {k: str(v) for k, v in self.roots.items()})
        self.env.start()
        # watcher data layout
        sw.DATA_DIR = self.tmp / "apocalypse"
        sw.LIVE_CONV_DIR = sw.DATA_DIR / "conversations"
        sw.STATE_FILE = sw.DATA_DIR / "stream_state.json"
        sw.EVENTS_FILE = sw.DATA_DIR / "events.jsonl"
        sw._state = {"version": 1, "files": {}, "hermes": {}}
        sw._sessions.clear()
        sw._sid_info.clear()
        sw._meta_cache.clear()
        sw._hermes_meta_cache.clear()

    def tearDown(self):
        self.env.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────

    def claude_file(self, name="sess-abc", project_dir="E--t-proj"):
        d = self.roots["CLAUDE_CONFIG_DIR"] / "projects" / project_dir
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{name}.jsonl"

    def append(self, path, *records):
        with open(path, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def poll(self, judge_side=None, title_side=None):
        """One poll cycle with Jev/title mocked; returns the jev mock."""
        m = mock.patch.object(sw, "_ask_jev",
                              side_effect=judge_side or (lambda *a, **k: {"meaningful": True, "new_topic": False, "via": "jev"}))
        t = mock.patch.object(sw, "_make_title", side_effect=title_side or (lambda s: "T:" + s[:6]))
        with m as jm, t:
            sw._poll_files()
        return jm

    def live_records(self, sid):
        f = sw.LIVE_CONV_DIR / f"claude_proj_{sid}.conversations.jsonl"
        self.assertTrue(f.exists(), f"live file missing: {f}")
        return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]

    def events(self):
        if not sw.EVENTS_FILE.exists():
            return []
        return [json.loads(l) for l in sw.EVENTS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]


class FirstSightRules(WatcherTestCase):
    def test_stale_history_skipped(self):
        """A file that existed before the watcher starts is history (cursor=EOF)."""
        p = self.claude_file()
        self.append(p, _claude_user("历史消息不该被处理", "2026-01-01T00:00:00Z", cwd="E:/t/proj"))
        old = time.time() - 7200
        os.utime(p, (old, old))
        sw._poll_files()
        cur = sw._state["files"][f"claude|{p}"]
        self.assertEqual(cur["offset"], p.stat().st_size)
        self.assertEqual(sw.stats()["conversations"], 0)

    def test_brand_new_file_processed_from_start(self):
        p = self.claude_file()
        self.append(p, _claude_user("新会话的第一条消息", "2026-01-01T00:00:00Z", cwd="E:/t/proj"))
        self.poll()
        self.assertEqual(sw.stats()["conversations"], 1)
        recs = self.live_records("sess-abc")
        self.assertEqual(len(recs[0]["messages"]), 1)

    def test_adopted_session_tail_scan_skips_covered(self):
        """Fresh conversations → 256 KB tail scan, but watermark-covered
        messages are skipped; only the new tail is attached."""
        p = self.claude_file()
        self.append(p,
                    _claude_user("旧消息一", "2026-01-01T00:00:00Z", cwd="E:/t/proj"),
                    _claude_assistant("旧回复一", "2026-01-01T00:01:00Z"),
                    _claude_user("新消息二", "2026-01-02T00:00:00Z", cwd="E:/t/proj"))
        # batch conversations covering the first two messages
        batch = self.roots["APOCALYPSE_BATCH_CONVERSATIONS_DIR"] / "claude_proj_sess-abc.conversations.jsonl"
        batch.write_text(json.dumps({
            "conversation_id": "sess-abc::c001", "session_id": "sess-abc",
            "title": "旧对话", "start_line_no": 1, "end_line_no": 2,
            "messages": [
                {"agent": "claude", "session_id": "sess-abc", "project": "unknown",
                 "role": "user", "text": "旧消息一", "ts": "2026-01-01T00:00:00Z", "line_no": 1},
                {"agent": "claude", "session_id": "sess-abc", "project": "unknown",
                 "role": "assistant", "text": "旧回复一", "ts": "2026-01-01T00:01:00Z",
                 "line_no": 2, "conclusion": True},
            ],
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        sw._startup_index()
        judge_calls = []

        def jev(text, prev, with_topic):
            judge_calls.append(text)
            return {"meaningful": True, "new_topic": False, "via": "jev"}

        self.poll(judge_side=jev)
        # only the post-watermark message went through judgment
        self.assertEqual(judge_calls, ["新消息二"])
        # batch file adopted into the live dir, message appended to its open conv
        recs = self.live_records("sess-abc")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["conversation_id"], "sess-abc::c001")
        self.assertEqual(len(recs[0]["messages"]), 3)
        self.assertEqual(recs[0]["messages"][-1]["line_no"], 3)
        self.assertEqual(recs[0]["messages"][-1]["text"], "新消息二")
        # batch dir untouched
        self.assertEqual(len(json.loads(batch.read_text(encoding="utf-8"))["messages"]), 2)


class RoutingTests(WatcherTestCase):
    def setUp(self):
        super().setUp()
        self.p = self.claude_file()
        self.append(self.p, _claude_user("第一条请求", "2026-01-01T00:00:00Z", cwd="E:/t/proj"))

    def test_new_conversation_then_appends(self):
        self.poll()  # first message → new conversation
        self.append(self.p, _claude_assistant("这是回复", "2026-01-01T00:01:00Z"))
        self.poll()
        recs = self.live_records("sess-abc")
        self.assertEqual(len(recs), 1)
        msgs = recs[0]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual([m["line_no"] for m in msgs], [1, 2])
        # last assistant message is the (provisional) conclusion
        self.assertTrue(msgs[1].get("conclusion"))
        self.assertFalse(msgs[0].get("conclusion", False))

    def test_followup_same_topic_assistant_conclusion(self):
        self.append(self.p,
                    _claude_assistant("回复一", "2026-01-01T00:01:00Z"),
                    _claude_user("追问一下", "2026-01-01T00:02:00Z", cwd="E:/t/proj"))
        self.poll()  # all three lines in one poll
        msgs = self.live_records("sess-abc")[0]["messages"]
        self.assertEqual(len(msgs), 3)
        # assistant reply followed by user question = conclusion
        self.assertTrue(msgs[1].get("conclusion"))
        self.assertEqual(msgs[2]["line_no"], 3)

    def test_new_topic_closes_and_opens(self):
        def jev(text, prev, with_topic):
            return {"meaningful": True, "new_topic": text.startswith("换个话题"), "via": "jev"}
        self.poll(judge_side=jev)
        self.append(self.p, _claude_assistant("回复一", "2026-01-01T00:01:00Z"))
        self.poll(judge_side=jev)
        self.append(self.p, _claude_user("换个话题：天气如何", "2026-01-01T00:02:00Z", cwd="E:/t/proj"))
        self.poll(judge_side=jev)
        recs = self.live_records("sess-abc")
        self.assertEqual(len(recs), 2)
        self.assertEqual([r["conversation_id"] for r in recs],
                         ["sess-abc::c001", "sess-abc::c002"])
        self.assertEqual(recs[1]["start_line_no"], 3)
        # first conversation's assistant reply became a conclusion
        self.assertTrue(recs[0]["messages"][-1].get("conclusion"))
        evs = [e["type"] for e in self.events()]
        self.assertEqual(evs.count("conversation"), 2)
        self.assertEqual(evs.count("message"), 1)

    def test_noise_and_ping_skipped_without_jev(self):
        self.append(self.p,
                    _claude_user("test", "2026-01-01T00:01:00Z", cwd="E:/t/proj"),
                    _claude_user("<command-message>foo</command-message>", "2026-01-01T00:02:00Z", cwd="E:/t/proj"),
                    _claude_user("<local-command-stdout>ok</local-command-stdout>", "2026-01-01T00:03:00Z", cwd="E:/t/proj"))
        jm = self.poll()  # first real message judged; noise never reaches jev
        judged = [c.args[0] for c in jm.call_args_list]
        self.assertEqual(judged, ["第一条请求"])
        msgs = self.live_records("sess-abc")[0]["messages"]
        self.assertEqual(len(msgs), 1)

    def test_not_meaningful_dropped_but_prev_text_updates(self):
        calls = []

        def jev(text, prev, with_topic):
            calls.append((text, prev))
            return {"meaningful": False, "new_topic": None, "via": "jev"}
        self.poll(judge_side=jev)
        self.append(self.p, _claude_user("嗯嗯好的收到", "2026-01-01T00:01:00Z", cwd="E:/t/proj"))
        self.poll(judge_side=jev)
        # the dropped message still becomes prev_text for the next judgment
        self.assertEqual(calls[1][0], "嗯嗯好的收到")
        self.assertEqual(sw._sessions["sess-abc"]["open"], None)
        self.assertEqual(sw._sessions["sess-abc"]["prev_text"], "嗯嗯好的收到")

    def test_jev_failure_falls_back_to_analysis_model(self):
        self.append(self.p, _claude_user("再来一条", "2026-01-01T00:01:00Z", cwd="E:/t/proj"))
        with mock.patch.object(sw, "_ask_jev", side_effect=RuntimeError("jev down")), \
             mock.patch.object(sw, "_make_title", side_effect=lambda s: "T:" + s[:6]), \
             mock.patch("analysis_harness.complete_json",
                        return_value={"meaningful": True, "new_topic": False}) as cj:
            sw._poll_files()
            self.assertEqual(cj.call_count, 2)

    def test_jev_and_analysis_failure_keeps_message(self):
        self.append(self.p, _claude_user("再来一条", "2026-01-01T00:01:00Z", cwd="E:/t/proj"))
        with mock.patch.object(sw, "_ask_jev", side_effect=RuntimeError("jev down")), \
             mock.patch.object(sw, "_make_title", side_effect=lambda s: "T:" + s[:6]), \
             mock.patch("analysis_harness.complete_json", side_effect=RuntimeError("down")):
            sw._poll_files()
        # default verdict: keep the message, same topic
        self.assertEqual(len(self.live_records("sess-abc")[0]["messages"]), 2)


class PersistenceTests(WatcherTestCase):
    def test_cursor_and_state_persisted(self):
        p = self.claude_file()
        self.append(p, _claude_user("第一条请求", "2026-01-01T00:00:00Z", cwd="E:/t/proj"))
        self.poll()
        self.assertTrue(sw.STATE_FILE.exists())
        saved = json.loads(sw.STATE_FILE.read_text(encoding="utf-8"))
        self.assertIn(f"claude|{p}", saved["files"])
        self.assertEqual(saved["files"][f"claude|{p}"]["offset"], p.stat().st_size)

    def test_replay_after_reload_does_not_duplicate(self):
        """Conversations are the truth on disk; state rebuild restores routing."""
        p = self.claude_file()
        self.append(p, _claude_user("第一条请求", "2026-01-01T00:00:00Z", cwd="E:/t/proj"))
        self.poll()
        self.append(p, _claude_assistant("回复一", "2026-01-01T00:01:00Z"))
        self.poll()
        # simulate restart: wipe memory, reload state + index
        sw._state = {"version": 1, "files": {}, "hermes": {}}
        sw._sessions.clear()
        sw._load_state()
        sw._startup_index()
        sess = sw._sessions["sess-abc"]
        self.assertEqual(sess["next_line_no"], 3)
        self.assertEqual(sess["next_cid"], 2)
        # lazy open load on next message
        self.append(p, _claude_user("再追问一下细节", "2026-01-01T00:02:00Z", cwd="E:/t/proj"))
        self.poll()
        recs = self.live_records("sess-abc")
        self.assertEqual(len(recs), 1)
        msgs = recs[0]["messages"]
        self.assertEqual([m["line_no"] for m in msgs], [1, 2, 3])
        # assistant reply got its conclusion flag on the follow-up
        self.assertTrue(msgs[1].get("conclusion"))

    def test_partial_line_waits(self):
        p = self.claude_file()
        self.append(p, _claude_user("第一条请求", "2026-01-01T00:00:00Z", cwd="E:/t/proj"))
        self.poll()
        with open(p, "a", encoding="utf-8") as f:
            f.write('{"type":"user","timestamp":"2026-01-01T00:01:00Z","mes')
        before = sw._state["files"][f"claude|{p}"]["offset"]
        self.poll()
        self.assertEqual(sw._state["files"][f"claude|{p}"]["offset"], before)
        # complete the line
        with open(p, "a", encoding="utf-8") as f:
            f.write('sage":{"content":"补全后的消息"}}\n')
        self.poll()
        self.assertEqual(len(self.live_records("sess-abc")[0]["messages"]), 2)


class HermesTests(WatcherTestCase):
    def _make_db(self, n_history=3):
        db = self.roots["HERMES_HOME"] / "state.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT, "
                     "tool_calls TEXT, tool_name TEXT, effect_disposition TEXT, "
                     "timestamp REAL, token_count INTEGER, finish_reason TEXT)")
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('hsess', ?)", (str(self.tmp / "hproj"),))
        for i in range(n_history):
            conn.execute("INSERT INTO messages (session_id, role, content, timestamp) "
                         "VALUES ('hsess', ?, ?, ?)",
                         ("user" if i % 2 == 0 else "assistant", f"历史消息{i}", 1767225600.0 + i))
        conn.commit()
        conn.close()

    def test_first_sight_skips_history_then_processes_new(self):
        self._make_db(n_history=3)
        judged = []

        def jev(text, prev, with_topic):
            judged.append(text)
            return {"meaningful": True, "new_topic": False, "via": "jev"}

        with mock.patch.object(sw, "_ask_jev", side_effect=jev), \
             mock.patch.object(sw, "_make_title", side_effect=lambda s: "T:" + s[:6]):
            sw._poll_hermes()   # first sight: history skipped
            self.assertEqual(judged, [])
            conn = sqlite3.connect(self.roots["HERMES_HOME"] / "state.db")
            conn.execute("INSERT INTO messages (session_id, role, content, timestamp) "
                         "VALUES ('hsess', 'user', '新消息', 1767225700.0)")
            conn.commit()
            conn.close()
            sw._poll_hermes()   # new row processed
        self.assertEqual(judged, ["新消息"])
        f = sw.LIVE_CONV_DIR / "hermes_hproj_hsess.conversations.jsonl"
        self.assertTrue(f.exists())
        rec = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["messages"][0]["text"], "新消息")
        self.assertEqual(rec["messages"][0]["project"], "hproj")


class ReadApiTests(WatcherTestCase):
    def test_list_and_get(self):
        p = self.claude_file()
        self.append(p, _claude_user("第一条请求", "2026-01-01T00:00:00Z", cwd="E:/t/proj"))
        self.poll()
        rows = sw.list_conversations()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["cid"], "sess-abc::c001")
        self.assertEqual(r["project"], "proj")
        self.assertEqual(r["status"], "open")
        full = sw.get_conversation("sess-abc::c001")
        self.assertEqual(full["messages"][0]["text"], "第一条请求")
        self.assertIsNone(sw.get_conversation("nope"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
