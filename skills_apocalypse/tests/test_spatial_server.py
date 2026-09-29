"""Smoke tests for the Spatial OS compatibility bridge."""
import sys
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import spatial_server

import feishu_sync


WORKSPACE = {
    "last_full_init": "2026-09-04T00:00:00Z",
    "projects": {
        "/tmp/apocalypse": {
            "name": "apocalypse",
            "title": "Apocalypse",
            "tags": ["AI工具开发", "前端开发"],
            "cwd": "/tmp/apocalypse",
            "last_active": "2026-09-04T09:10:00Z",
            "analyzed_sessions": {
                "sess-a": {
                    "user_goal": "Integrate the Spatial OS",
                    "summary": "Wired the new UI to the existing server.",
                    "outcome": "completed",
                    "category": "ai_tools",
                    "ts": "2026-09-04T09:10:00Z",
                }
            },
            "points": [
                {
                    "id": "point-a",
                    "topic": "Backend compatibility",
                    "decision": "Subclass the existing HTTP handler.",
                    "related_to": ["point-b"],
                    "session_id": "sess-a",
                },
                {
                    "id": "point-b",
                    "topic": "Legacy fallback",
                    "decision": "Keep the old dashboard reachable.",
                    "related_to": ["point-a"],
                    "session_id": "sess-a",
                },
            ],
        }
    },
}

LIVE = [
    {
        "session_id": "sess-a",
        "cwd": "/tmp/apocalypse",
        "project_name": "apocalypse",
        "last_ts": "2026-09-04T09:10:00Z",
        "status": "green",
        "resume_id": "sess-a",
    }
]


class SpatialWorldTests(unittest.TestCase):
    def test_world_preserves_project_session_and_decision_objects(self):
        with mock.patch.object(spatial_server.legacy, "_load_workspace", return_value=WORKSPACE), \
             mock.patch.object(spatial_server.legacy, "scan_transcripts", return_value=LIVE):
            payload = spatial_server.world()

        kinds = [obj["type"] for obj in payload["objects"]]
        self.assertEqual(kinds.count("project"), 1)
        self.assertEqual(kinds.count("session"), 1)
        self.assertEqual(kinds.count("decision"), 2)

        project = next(obj for obj in payload["objects"] if obj["type"] == "project")
        session = next(obj for obj in payload["objects"] if obj["type"] == "session")
        decisions = [obj for obj in payload["objects"] if obj["type"] == "decision"]
        self.assertEqual(project["status"], "active")
        self.assertEqual(session["session_id"], "sess-a")
        self.assertTrue(all(d["project_id"] == project["id"] for d in decisions))
        self.assertTrue(all(d["related_to"] for d in decisions))

    def test_hook_event_is_normalized_for_motion_layer(self):
        event = {
            "type": "tool_start",
            "session_id": "abcdef123456",
            "project_name": "apocalypse",
            "tool": "Edit",
            "ts": "2026-09-04T09:10:00Z",
        }
        result = spatial_server.normalize(event)
        self.assertEqual(result["type"], "tool_call")
        self.assertEqual(result["project"], "apocalypse")
        self.assertEqual(result["text"], "Edit")
        self.assertTrue(result["agent"].startswith("CLAUDE-"))

    def test_ops_keeps_all_agents_in_one_session_surface(self):
        codex = [{
            "session_id": "codex-a",
            "cwd": "/tmp/codex-proj",
            "project_name": "codex-proj",
            "last_ts": "2026-09-04T08:00:00Z",
            "thread_name": "Review integration",
        }]
        others = [{
            "session_id": "agent-a",
            "cwd": "/tmp/agent-proj",
            "project_name": "agent-proj",
            "last_ts": "2026-09-04T07:00:00Z",
            "thread_name": "Pi/openclaw/hermes row",
        }]
        empty_activity = {"window_days": 84, "active_hours": 0, "days": []}
        with mock.patch.object(spatial_server, "activity", return_value=empty_activity), \
             mock.patch.object(spatial_server, "ws_lookup", return_value=({}, {})), \
             mock.patch.object(spatial_server, "agents", return_value=[]), \
             mock.patch.object(spatial_server, "flow", return_value={"current": {"load": 0}}), \
             mock.patch.object(spatial_server, "quotas", return_value=[]), \
             mock.patch.object(spatial_server, "schedule", return_value={"events": [], "tasks": [], "suggested": []}), \
             mock.patch.object(spatial_server.legacy, "read_events", return_value=[]), \
             mock.patch.object(spatial_server.legacy, "scan_transcripts", return_value=LIVE), \
             mock.patch.object(spatial_server.legacy, "scan_codex_transcripts", return_value=codex), \
             mock.patch.object(spatial_server.legacy, "scan_pi_transcripts", return_value=others), \
             mock.patch.object(spatial_server.legacy, "scan_openclaw_transcripts", return_value=others), \
             mock.patch.object(spatial_server.legacy, "scan_hermes_transcripts", return_value=others):
            payload = spatial_server.ops()

        providers = {row["provider"] for row in payload["sessions"]}
        self.assertEqual(providers, {"claude", "codex", "pi", "openclaw", "hermes"})


    def test_schedule_uses_local_status_and_cached_snapshot_without_network(self):
        # After a successful sync, opening the app renders the cached snapshot
        # plus local status; feishu_sync.fetch_schedule (the network path) is
        # never called.
        cached = {"events": [{"start": "10:00", "end": "11:00", "title": "Sync", "project": "FEISHU CALENDAR"}],
                  "tasks": [], "suggested": [], "source": "feishu"}
        with mock.patch.object(spatial_server.feishu_sync, "status", return_value={
                "configured": True, "authorized": True, "connected": True,
                "last_sync_at": "2026-09-04T10:00:00Z", "last_error": None,
                "message": "FEISHU LIVE"}), \
             mock.patch.object(spatial_server.feishu_sync, "cached_schedule", return_value=cached), \
             mock.patch.object(spatial_server.feishu_sync, "fetch_schedule") as fetch:
            payload = spatial_server.schedule()
        self.assertIsNone(fetch.call_args)
        self.assertEqual(payload["source"], "feishu")
        self.assertEqual(payload["events"][0]["title"], "Sync")
        self.assertTrue(payload["feishu"]["connected"])

    def test_schedule_surfaces_feishu_failure_locally(self):
        # When the last sync failed there is no cached snapshot, but the local
        # status must carry the failure so the UI can show it in the agenda.
        with mock.patch.object(spatial_server.feishu_sync, "status", return_value={
                "configured": True, "authorized": True, "connected": False,
                "last_sync_at": "2026-09-04T10:00:00Z",
                "last_error": "FEISHU API ERROR · nope", "code": 99991672,
                "message": "FEISHU API ERROR · nope"}), \
             mock.patch.object(spatial_server.feishu_sync, "cached_schedule", return_value=None):
            payload = spatial_server.schedule()
        self.assertFalse(payload["feishu"]["connected"])
        self.assertIn("nope", payload["feishu"]["last_error"])
        self.assertEqual(payload["source"], "demo")  # local file fallback

    def test_sync_now_persists_successful_snapshot(self):
        sched = {"events": [], "tasks": [], "suggested": [], "source": "feishu", "calendar_id": "cal"}
        info = {"configured": True, "authorized": True, "connected": True, "message": "FEISHU LIVE"}
        with mock.patch.object(feishu_sync, "fetch_schedule", return_value=(sched, info)), \
             mock.patch.object(feishu_sync, "_load_state", return_value={}), \
             mock.patch.object(feishu_sync, "_save_state") as save:
            live, out = feishu_sync.sync_now()
        self.assertEqual(live["source"], "feishu")
        self.assertTrue(out["connected"])
        saved = save.call_args[0][0]
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["schedule"]["calendar_id"], "cal")
        self.assertIsNone(saved["last_error"])

    def test_sync_now_persists_failure(self):
        info = {"configured": True, "authorized": True, "connected": False,
                "message": "FEISHU API ERROR · boom", "code": 99991672}
        with mock.patch.object(feishu_sync, "fetch_schedule", return_value=(None, info)), \
             mock.patch.object(feishu_sync, "_load_state", return_value={}), \
             mock.patch.object(feishu_sync, "_save_state") as save:
            live, out = feishu_sync.sync_now()
        self.assertIsNone(live)
        self.assertFalse(out["connected"])
        saved = save.call_args[0][0]
        self.assertFalse(saved["ok"])
        self.assertIn("boom", saved["last_error"])
        self.assertEqual(saved["code"], 99991672)


