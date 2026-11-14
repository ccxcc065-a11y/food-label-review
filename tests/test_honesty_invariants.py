"""Honesty invariants: without an authorized corpus the workbench must abstain.

This repository ships rule *metadata* only (clause numbers, page numbers, status),
never standards text. That is a legal constraint, but it is also a useful test:
the program has to degrade to 未检查／未覆盖 instead of guessing, and it must
never turn silence into a pass.
"""

from __future__ import annotations

import json
import unittest
from collections import Counter

import audit
import extract


ROWS = [
    ("品名：示例原味饼干", [0.05, 0.02, 0.5, 0.04]),
    ("配料：小麦粉、白砂糖、植物油", [0.05, 0.10, 0.7, 0.05]),
    ("净含量：100克", [0.05, 0.20, 0.3, 0.03]),
    ("生产日期：2026年05月01日", [0.05, 0.25, 0.4, 0.03]),
    ("贮存条件：阴凉干燥处保存", [0.05, 0.30, 0.5, 0.03]),
    ("生产者：示例食品有限公司", [0.05, 0.35, 0.5, 0.03]),
    ("地址：某省某市某路1号", [0.05, 0.40, 0.5, 0.03]),
    ("联系方式：400-000-0000", [0.05, 0.45, 0.4, 0.03]),
    ("食品生产许可证编号：SC10000000000000", [0.05, 0.50, 0.5, 0.03]),
    ("产品标准代号：GB/T 20980", [0.05, 0.55, 0.4, 0.03]),
]

_RESULT = {}


def synthetic_payload():
    blocks = [{"id": "b%d" % index, "text": text, "confidence": 1, "box": box}
              for index, (text, box) in enumerate(ROWS, 1)]
    pages = [{"id": "p1", "name": "example-label.jpg", "blocks": blocks}]
    return {
        "draft": extract.extract({"pages": pages}),
        "pages": pages,
        "context": {
            "scope": "regular", "sampleKind": "physical", "scenario": "auto",
            "productionDate": "2026-05-01", "nutritionRequired": "required",
            "verified": True, "preview": True, "complete": True,
        },
    }


def audit_result():
    if not _RESULT:
        _RESULT["value"] = audit.audit(synthetic_payload())
    return _RESULT["value"]


class AbstentionTests(unittest.TestCase):
    def test_audit_runs_without_any_standards_text(self):
        result = audit_result()
        self.assertEqual(result["version"], "2025")
        self.assertTrue(result["items"])
        self.assertEqual((result["versions"] or {}).get("bundleId"), "bundle-bootstrap")

    def test_no_item_claims_a_pass_without_a_corpus(self):
        outcomes = Counter(item.get("outcome") for item in audit_result()["items"])
        self.assertEqual(outcomes.get("pass", 0), 0, outcomes)
        self.assertGreater(outcomes.get("gap", 0), 0, "缺少依据时必须以未覆盖呈现")

    def test_clause_dependent_checks_state_why_they_abstain(self):
        gaps = [item for item in audit_result()["items"] if item.get("outcome") == "gap"]
        self.assertTrue(gaps)
        for item in gaps:
            detail = str(item.get("detail") or "")
            self.assertTrue("条款" in detail or "依据表" in detail, (item.get("id"), detail))

    def test_r14_without_its_basis_table_is_uncovered_not_passed(self):
        items = [item for item in audit_result()["items"] if item.get("id") == "R14"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["outcome"], "gap")
        self.assertEqual(items[0].get("uncoveredReason"), "warning-book-missing")
        self.assertIn("不得视为通过", items[0]["detail"])

    def test_checkpoint_ledger_has_91_rows_and_never_passes(self):
        ledger = (audit_result().get("checklist") or {}).get("ledger") or []
        self.assertEqual(len(ledger), 91)
        labels = Counter(row.get("statusLabel") for row in ledger)
        self.assertEqual(labels.get("通过", 0), 0, labels)

    def test_classification_is_consistent_with_items(self):
        result = audit_result()
        counts = result.get("classification") or {}
        self.assertGreaterEqual(counts.get("pending", 0), 0)
        pending = [item for item in result["items"]
                   if item.get("group") not in ("issue", "pass", "internal")
                   and item.get("track") != "standard2025"
                   or item.get("track") == "standard2025"
                   and item.get("outcome") not in ("issue", "pass")]
        self.assertEqual(counts.get("pending"), len(pending))
        self.assertEqual(counts.get("mustChange", 0), 0)
        self.assertEqual(counts.get("recheckRequired", 0), 0)


class DisclosureTests(unittest.TestCase):
    def test_candidate_config_is_declared_unpublished(self):
        candidate = audit_result().get("candidateConfig") or {}
        self.assertFalse(candidate.get("published"))
        self.assertTrue(candidate.get("note"))
        self.assertTrue(candidate.get("codeModules"), "判定模块摘要必须列出，才能冻结一次审核")
        self.assertIn("audit.py", candidate["codeModules"])
        # 没有依据表时摘要必须是 None，而不是编一个值。
        self.assertIsNone((candidate.get("warningBasis") or {}).get("sha256"))

    def test_running_code_identity_is_reported(self):
        running = audit_result().get("runningProgram") or {}
        self.assertTrue(running.get("auditSha256"))
        self.assertEqual(running.get("programVersion"), running.get("pinnedProgramVersion"))
        self.assertIsNone(running.get("warningBasisSha256"))


if __name__ == "__main__":
    unittest.main()
