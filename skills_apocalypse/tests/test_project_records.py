import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills_apocalypse.backend import project_records as pr


class ProjectRecordsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.source = self.root / "session.jsonl"
        self.data = self.root / "state"
        self.data.mkdir()

        self.old_paths = (pr.DATA_DIR, pr.SETTINGS_FILE, pr.STATE_FILE, pr.WORKSPACE_FILE)
        pr.DATA_DIR = self.data
        pr.SETTINGS_FILE = self.data / "settings.json"
        pr.STATE_FILE = self.data / "project_records_state.json"
        pr.WORKSPACE_FILE = self.data / "workspace.json"
        pr.WORKSPACE_FILE.write_text(json.dumps({
            "projects": {str(self.project): {"cwd": str(self.project), "name": "project"}}
        }), encoding="utf-8")
        pr._state = {"version": 1, "sources": {}, "stats": {}}
        pr._runtime.update({
            "running": False, "last_sync_at": None, "last_error": None,
            "monitored_projects": 0, "tracked_sessions": 0,
            "messages_written": 0, "pending_messages": 0,
        })

    def tearDown(self):
        pr.DATA_DIR, pr.SETTINGS_FILE, pr.STATE_FILE, pr.WORKSPACE_FILE = self.old_paths
        self.tmp.cleanup()

    def _write(self, rows, mode="w"):
        with open(self.source, mode, encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _scan(self):
        return {
            "claude:test": {
                "agent": "claude", "cwd": str(self.project), "name": "project",
                "sessions": [{"id": "session-1", "path": self.source, "agent": "claude"}],
            }
        }

    def test_backfill_incremental_dedup_and_disable(self):
        self._write([
            {"type": "user", "timestamp": "2026-10-15T01:00:00Z", "message": {"content": [{"type": "text", "text": "hello"}]}},
            {"type": "assistant", "timestamp": "2026-10-15T01:00:01Z", "message": {"content": [{"type": "text", "text": "world"}]}},
        ])
        with mock.patch.object(pr.transcripts, "scan", side_effect=lambda include_live=True: self._scan()):
            first = pr.sync_once()
            self.assertEqual(first["messages_written"], 2)
            record = self.project / "chat_history" / "record.md"
            text = record.read_text(encoding="utf-8")
            self.assertIn("**User:** hello", text)
            self.assertIn("**Claude:** world", text)
            self.assertEqual(text.count("**User:** hello"), 1)

            second = pr.sync_once()
            self.assertEqual(second["messages_written"], 2)
            self.assertEqual(record.read_text(encoding="utf-8").count("**User:** hello"), 1)

            self._write([
                {"type": "user", "timestamp": "2026-10-15T01:01:00Z", "message": {"content": [{"type": "text", "text": "again"}]}},
            ], mode="a")
            third = pr.sync_once()
            self.assertEqual(third["messages_written"], 3)
            self.assertIn("**User:** again", record.read_text(encoding="utf-8"))

            pr.set_enabled(False)
            self._write([
                {"type": "assistant", "timestamp": "2026-10-15T01:01:01Z", "message": {"content": [{"type": "text", "text": "should not appear"}]}},
            ], mode="a")
            pr.sync_once()
            self.assertNotIn("should not appear", record.read_text(encoding="utf-8"))

    def test_workspace_root_wins_for_nested_cwd(self):
        nested = self.project / "src" / "deep"
        nested.mkdir(parents=True)
        root = pr._project_root(str(nested), [self.project.resolve()])
        self.assertEqual(root, self.project.resolve())


if __name__ == "__main__":
    unittest.main()
