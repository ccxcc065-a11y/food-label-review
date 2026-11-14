"""Catch missing public assets and prompts without requiring OCR or a model key."""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PublicDistributionTests(unittest.TestCase):
    def test_html_and_css_references_exist(self):
        for name in ["index.html", "admin.html", "style.css"]:
            text = (ROOT / name).read_text(encoding="utf-8")
            paths = re.findall(r'(?:src|href)=["\'](/[^"\']+)["\']|url\(["\']?(/[^)"\']+)', text)
            for html_path, css_path in paths:
                path = html_path or css_path
                if path == "/admin":
                    continue  # An application route, not a file.
                self.assertTrue((ROOT / path.lstrip("/")).is_file(), (name, path))

    def test_active_role_prompts_are_distributed(self):
        for name in ["ai-review.md", "remediation.md"]:
            self.assertGreater(len((ROOT / "agents" / name).read_text(encoding="utf-8")), 100)

    def test_demo_is_explicit_and_has_all_seven_nutrient_rows(self):
        fixture = json.loads((ROOT / "demos" / "synthetic-ocr.json").read_text(encoding="utf-8"))
        self.assertIs(fixture["synthetic"], True)
        self.assertEqual(fixture["recognitionMode"], "pre-recorded-not-live-ocr")
        from labelreview.extract import extract
        draft = extract({"pages": [{"id": "demo", "blocks": fixture["blocks"]}]})
        self.assertEqual(draft["fields"]["name"]["value"], "虚构原味饼干")
        self.assertEqual(draft["fields"]["producer"]["value"], "演示食品有限公司")
        self.assertEqual(draft["fields"]["contact"]["value"], "000-000-0000")
        self.assertEqual(draft["fields"]["basis"]["value"], "每100克")
        expected = {"energy": ("2000", "kJ", "24"), "protein": ("6.0", "g", "10"),
                    "fat": ("20.0", "g", "33"), "saturated": ("5.0", "g", "25"),
                    "carb": ("65.0", "g", "22"), "sugar": ("15.0", "g", ""),
                    "sodium": ("400", "mg", "20")}
        for key, values in expected.items():
            row = draft["nutrition"][key]
            self.assertEqual(tuple(row.get(field, "") for field in ["value", "unit", "nrv"]), values, key)

    def test_ai_check_count_is_not_the_number_of_rule_findings(self):
        from labelreview.report_api import _ai_stats
        stats = _ai_stats({"items": [{"id": "rule-1"}]}, {"status": "skipped"}, {})
        self.assertEqual(stats["checked"], 0)

    def test_ai_check_count_uses_completed_coverage_only(self):
        from labelreview.report_api import _ai_stats
        review = {"coverage": [{"checklistId": "a", "coverage": "已查"},
                               {"checklistId": "a", "coverage": "已查"},
                               {"checklistId": "b", "coverage": "未查"},
                               {"checklistId": "c", "coverage": "未检查／未覆盖"}]}
        self.assertEqual(_ai_stats({}, review, {})["checked"], 1)


if __name__ == "__main__":
    unittest.main()
