"""Field extraction and report safety checks.

Both are pure functions: no standards text, no OCR binary, no server. They cover
the two places where a quiet mistake would be worst — reading the label wrong,
and letting a broken report reach the customer.
"""

from __future__ import annotations

import unittest

import labelreview.extract as extract
import labelreview.html_check as html_check
def page(rows):
    blocks = [{"id": "b%d" % index, "text": text, "confidence": 1, "box": box}
              for index, (text, box) in enumerate(rows, 1)]
    return {"pages": [{"id": "p1", "name": "example.jpg", "blocks": blocks}]}


class FieldExtractionTests(unittest.TestCase):
    def test_core_fields_are_labelled_from_text(self):
        draft = extract.extract(page([
            ("品名：示例原味饼干", [0.05, 0.02, 0.5, 0.04]),
            ("配料：小麦粉、白砂糖、植物油", [0.05, 0.10, 0.7, 0.05]),
            ("净含量：100克", [0.05, 0.20, 0.3, 0.03]),
            ("生产商：示例食品有限公司", [0.05, 0.30, 0.5, 0.03]),
            ("食品生产许可证编号：SC10000000000000", [0.05, 0.40, 0.5, 0.03]),
            ("产品标准代号：GB/T 20980", [0.05, 0.50, 0.4, 0.03]),
            ("贮存条件：阴凉干燥处保存", [0.05, 0.60, 0.5, 0.03]),
        ]))
        fields = {key: row["value"] for key, row in draft["fields"].items()}
        self.assertEqual(fields["name"], "示例原味饼干")
        self.assertIn("小麦粉", fields["ingredients"])
        self.assertEqual(fields["net"], "100克")
        self.assertIn("示例食品有限公司", fields["producer"])
        self.assertIn("SC10000000000000", fields["license"])
        self.assertIn("GB/T 20980", fields["standard"])
        self.assertIn("阴凉干燥处", fields["storage"])

    def test_fields_keep_their_evidence_refs(self):
        draft = extract.extract(page([("品名：示例饼干", [0.05, 0.02, 0.5, 0.04])]))
        self.assertEqual(draft["fields"]["name"]["refs"], ["b1"])

    def test_out_of_range_boxes_are_rejected(self):
        with self.assertRaises(ValueError):
            extract.read_pages({"pages": [{"id": "p", "blocks": [
                {"id": "a", "text": "示例", "box": [2.0, 0.0, 1.0, 1.0]}]}]})

    def test_duplicate_block_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            extract.read_pages({"pages": [{"id": "p", "blocks": [
                {"id": "a", "text": "一", "box": [0.0, 0.0, 0.1, 0.1]},
                {"id": "a", "text": "二", "box": [0.2, 0.2, 0.1, 0.1]}]}]})

    def test_too_many_pages_are_rejected(self):
        with self.assertRaises(ValueError):
            extract.read_pages({"pages": [{"id": "p%d" % i, "blocks": []} for i in range(25)]})


class ReportSafetyTests(unittest.TestCase):
    """`html_check` is the gate that stops a broken report from being saved."""

    def run_check(self, html):
        return html_check.run(html, {}, {"items": []}, {})

    def test_residual_punctuation_is_blocked(self):
        result = self.run_check("<html><body>报告含残留 。； 片段</body></html>")
        codes = [error["code"] for error in result["errors"]]
        self.assertIn("residual-punctuation", codes)
        self.assertFalse(result["ok"])

    def test_unreplaced_placeholders_are_blocked(self):
        result = self.run_check("<html><body>待替换 {name} 占位</body></html>")
        codes = [error["code"] for error in result["errors"]]
        self.assertIn("residual-punctuation", codes)

    def test_clean_report_has_no_punctuation_complaint(self):
        result = self.run_check("<html><body><p>共列示 0 项须整改后复审。</p></body></html>")
        codes = [error["code"] for error in result["errors"]]
        self.assertNotIn("residual-punctuation", codes)


if __name__ == "__main__":
    unittest.main()
