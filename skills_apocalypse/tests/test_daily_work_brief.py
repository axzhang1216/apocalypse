"""Tests for the daily work brief prompt, schema, and display split."""
import sys
import unittest
from datetime import date
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1] / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

import daily_work_brief as dwb  # noqa: E402


class BriefTest(unittest.TestCase):
    def test_prompt_uses_done_and_pending_lists(self):
        prompt = dwb.brief_prompt({"date": "2026-10-19"})
        self.assertIn('"done"', prompt)
        self.assertIn('"pending"', prompt)
        self.assertIn('"user_action"', prompt)
        self.assertIn('Refer to the brief\'s day as "当日"', prompt)
        self.assertNotIn('"leftovers"', prompt)

    def test_split_lines_one_bullet_per_clause(self):
        self.assertEqual(dwb.split_lines("甲；乙。丙"), ["甲", "乙。", "丙"])

    def test_finished_items_never_appear_under_pending(self):
        raw = {"projects": [
            {"project": "桌面便签", "done": ["Linear UI 改版", "按钮 Logo 替换"],
             "pending": [], "user_action": ""},
            {"project": "Climate", "done": ["合并图已推送"],
             "pending": ["投影脚本待跑"], "user_action": "在本机跑脚本"},
        ]}
        brief = dwb.normalize_brief(raw, date(2026, 10, 19), {})
        self.assertEqual(
            brief["projects"][0]["done"], ["Linear UI 改版", "按钮 Logo 替换"]
        )
        done, pending = dwb.render_markdown(brief).split("## 潜在问题")
        self.assertIn("- Linear UI 改版", done)
        self.assertIn("- 合并图已推送", done)
        self.assertNotIn("合并图已推送", pending)
        self.assertIn("- 投影脚本待跑", pending)
        self.assertIn("需要你做：在本机跑脚本", pending)

    def test_old_progress_shape_maps_to_done_or_pending(self):
        raw = {"projects": [
            {"project": "A", "progress": "A 已完成", "status": "done"},
            {"project": "B", "progress": "B 未完成；B 仍缺数据", "status": "running"},
        ]}
        brief = dwb.normalize_brief(raw, date(2026, 10, 19), {})
        self.assertEqual(brief["projects"][0]["done"], ["A 已完成"])
        self.assertEqual(brief["projects"][1]["pending"], ["B 未完成", "B 仍缺数据"])


if __name__ == "__main__":
    unittest.main()
