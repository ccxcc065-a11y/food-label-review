"""Formal RuleRegistry for the food-label review workbench (P3).

list_rules / build_checklist / evaluate wrap the already-published rule book
and the existing audit.py, consistency.py and interpretations.py algorithms.
They do not rewrite nutrition, NRV or claim math. The 应查项对照表 markdown
is the source of truth for the 91 checkpoints; checkpoints.json is a
machine-readable copy generated from that table.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import registry as registry_mod


ROOT = Path(__file__).resolve().parent
CHECKPOINTS_JSON = ROOT / "checkpoints.json"


def checkpoint_markdown_path():
    """Prefer the in-repo copy so a clean checkout does not need sibling p0/.

    The box work tree may still have the table under ../p0/truth/; that remains
    a fallback. LABEL_REVIEW_CHECKPOINTS_MD overrides both when set.
    """
    candidates = [
        ROOT / "docs" / "应查项对照表.md",
        ROOT.parent / "p0" / "truth" / "应查项对照表.md",
    ]
    override = os.environ.get("LABEL_REVIEW_CHECKPOINTS_MD", "").strip()
    if override:
        candidates.insert(0, Path(override))
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


CHECKPOINTS_MARKDOWN = checkpoint_markdown_path()

UNCOVERED = "未检查／未覆盖"
RESULT_PASS = "pass"
RESULT_ISSUE = "issue"
RESULT_REVIEW = "review"
RESULT_NA = "not_applicable"
RESULT_NE = "not_evaluated"

# Consistency / allergen leftovers that already have code but were not in the
# published catalog. Registration lives in RULE_CATALOG; a bundle only lists
# them after the next publish.
CONSISTENCY_RULE_IDS = ("I01", "I02", "I03", "I04", "I06", "I07")
ALLERGEN_BRANCH_IDS = ("GB7718AL-gluten",)
# R14（4.14 警示语）2026-11-14 起由 audit.py:warning_statements 实现，
# 但只覆盖 knowledge/warnings.json 登记的依据，其余规定仍按未检查／未覆盖处理。
# 保留本元组是为了继续表达「哪些规则可以声称未实现」，目前为空。
UNIMPLEMENTED_RULE_IDS = ()

NUTRITION_RULE_IDS = {"N00", "N01", "N02", "N03", "F01"}
NUTRITION_EVIDENCE = ("value", "unit", "basis", "printedNrv", "reference", "rounding")

# Outcome groups used by audit.Report.add / consistency.note.
_ITEM_RESULT = {
    "pass": RESULT_PASS,
    "issue": RESULT_ISSUE,
    "review": RESULT_REVIEW,
    "gap": RESULT_NE,
    "uncovered": RESULT_NE,
    "blocked": RESULT_NE,
    "internal": RESULT_REVIEW,
}

_CHECKPOINT_CACHE = None


def _read_json(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_checkpoints(refresh=False):
    """Load the 91-checkpoint table. Prefer regenerating from the markdown."""
    global _CHECKPOINT_CACHE
    if _CHECKPOINT_CACHE is not None and not refresh:
        return _CHECKPOINT_CACHE
    markdown = checkpoint_markdown_path()
    data = None
    if markdown.is_file():
        try:
            data = parse_checkpoint_markdown(markdown)
        except Exception:
            data = None
    if data is None:
        data = _read_json(CHECKPOINTS_JSON, {}) or {}
    checkpoints = list(data.get("checkpoints") or [])
    if len(checkpoints) != 91:
        raise RuntimeError("应查项对照表未能读出 91 个检查点。")
    _CHECKPOINT_CACHE = {
        "source": data.get("source") or str(markdown),
        "themes": list(data.get("themes") or []),
        "checkpoints": checkpoints,
        "count": len(checkpoints),
    }
    return _CHECKPOINT_CACHE


def parse_checkpoint_markdown(path):
    """Parse 应查项对照表.md into the same shape as checkpoints.json."""
    text = Path(path).read_text(encoding="utf-8")
    known = set(registry_mod.RULE_CATALOG) | {
        "I01", "I02", "I03", "I04", "I06", "I07", "N00", "R14",
        "GB7718AL-gluten",
        "job-ticket-not-label", "category-name-allowed", "additive-two-forms",
        "name-mention-no-emphasis", "no-quantity-claim",
    }
    themes, rows = [], []
    theme_id, theme_title, theme_label = None, None, None
    for line in text.splitlines():
        heading = re.match(r"^## (版本适用|主题 (\d+))\s*[　\s]*(.*)$", line)
        if heading:
            if heading.group(1).startswith("主题"):
                theme_id = int(heading.group(2))
                theme_title = (heading.group(3) or "").strip()
                theme_label = f"主题 {theme_id}"
            else:
                theme_id = 0
                theme_title = "版本适用（先行条件）"
                theme_label = "版本适用"
            themes.append({"themeId": theme_id, "theme": theme_label, "title": theme_title})
            continue
        if not re.match(r"^\|\s*CP-\d{2}-\d{2}\s", line):
            continue
        cells = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(cells) < 7:
            continue
        cp_cell, basis, applies, execution, rule_cell, coverage, tests = cells[:7]
        match = re.match(r"(CP-\d{2}-\d{2})\s+(.*)$", cp_cell.strip())
        cid, title = (match.group(1), match.group(2).strip()) if match else (cp_cell.strip(), "")
        rule_ids = _extract_rule_ids(rule_cell, known)
        cov_status, cov_label = _coverage_status(coverage)
        primary, methods = _execution_methods(execution, cov_label, rule_cell)
        rows.append({
            "checkpointId": cid,
            "title": title,
            "themeId": theme_id,
            "theme": theme_title,
            "themeLabel": theme_label,
            "authorizedBasis": basis,
            "appliesWhen": applies,
            "executionText": execution,
            "ruleCell": rule_cell,
            "ruleIds": rule_ids,
            "executionMethod": primary,
            "executionMethods": methods,
            "coverageStatus": cov_status,
            "coverageStatusLabel": cov_label,
            "tests": tests,
            "evidenceRequirements": _table_evidence(theme_id, rule_ids),
        })
    return {
        "source": str(Path(path)),
        "themes": themes,
        "checkpoints": rows,
        "count": len(rows),
    }


def _extract_rule_ids(cell, known):
    ids = []
    for token in re.findall(r"`([^`]+)`", cell or ""):
        token = token.strip()
        if token in known and token not in ids:
            ids.append(token)
    for token in re.findall(r"[A-Za-z][A-Za-z0-9-]*", cell or ""):
        if token in known and token not in ids:
            ids.append(token)
    return ids


def _coverage_status(label):
    text = label or ""
    if text.startswith("已有"):
        return "present", text
    if text.startswith("部分"):
        return "partial", text
    if text.startswith("缺失"):
        return "missing", text
    return "unknown", text


def _execution_methods(exec_cell, coverage_label, rule_cell):
    exec_cell = exec_cell or ""
    rule_cell = rule_cell or ""
    methods = []
    unimplemented = "未实现" in exec_cell
    program = _has_program(exec_cell) or _has_program(rule_cell)
    if re.search(r"程序未见|程序尚未|程序没有", exec_cell) and not re.search(
            r"程序(?:检查|核对|做|有|记录|识别|列出|检索|尝试|按)", exec_cell):
        program = False
    if any(token in rule_cell for token in ("确认绑定", "归属逻辑", "fact_qa")):
        program = True
    if "AI" in exec_cell or "模型" in exec_cell:
        methods_ai = True
    else:
        methods_ai = False
    if program:
        methods.append("program")
    if methods_ai:
        methods.append("AI-assist")
    if "人工" in exec_cell:
        methods.append("human")
    if unimplemented and not program:
        methods.append("unimplemented")
    if not methods:
        rules = _extract_rule_ids(rule_cell, set(registry_mod.RULE_CATALOG) | {
            "I01", "I02", "I03", "I04", "I06", "I07", "N00", "R14", "GB7718AL-gluten",
        })
        if coverage_label.startswith("缺失") and (not rules or "无" in rule_cell or unimplemented):
            methods = ["unimplemented"]
        else:
            methods = ["human"]
    if "unimplemented" in methods and "program" not in methods:
        primary = "unimplemented"
    elif "program" in methods:
        primary = "program"
    elif "AI-assist" in methods:
        primary = "AI-assist"
    else:
        primary = methods[0]
    return primary, methods


def _has_program(text):
    if not text:
        return False
    keywords = ("程序", "质检", "解释模板", "取条款", "结构失败", "分层阻断",
                "证据清单", "确认绑定", "归属结果", "归属逻辑")
    return any(key in text for key in keywords)


def _table_evidence(theme_id, rule_ids):
    if theme_id in {11, 12} or any(rule in NUTRITION_RULE_IDS for rule in rule_ids):
        return {
            "pass": list(NUTRITION_EVIDENCE),
            "issue": list(NUTRITION_EVIDENCE),
            "review": ["value", "unit", "basis"],
        }
    if "G03" in rule_ids or theme_id == 6:
        return {"pass": ["value", "unit"], "issue": ["value", "unit"], "review": ["value"]}
    return {"pass": ["requiredFacts"], "issue": ["requiredFacts"], "review": ["requiredFacts"]}


def evidence_needs_for(rule_id, catalog_entry=None):
    """Declared evidence needs for different conclusions."""
    entry = catalog_entry if isinstance(catalog_entry, dict) else (registry_mod.RULE_CATALOG.get(rule_id) or {})
    declared = entry.get("evidenceNeeds")
    if isinstance(declared, dict) and declared:
        return declared
    if rule_id in NUTRITION_RULE_IDS:
        return {
            "pass": list(NUTRITION_EVIDENCE),
            "issue": list(NUTRITION_EVIDENCE),
            "review": ["value", "unit", "basis"],
        }
    if rule_id in {"G03", "METRO", "M04"}:
        return {"pass": ["value", "unit"], "issue": ["value", "unit"], "review": ["value"]}
    required = entry.get("requiredEvidence") or "requiredFacts"
    return {"pass": [required] if isinstance(required, str) else list(required),
            "issue": [required] if isinstance(required, str) else list(required),
            "review": [required] if isinstance(required, str) else list(required)}


def _versions_of(registry, bundle_id=None):
    return dict(registry.current_versions(bundle_id) or {})


def resolve_release(rule_release_id, registry=None):
    """Map a rulesVersion, bundleId, or auditProfile fragment to a bundle id."""
    registry = registry or registry_mod.default_registry()
    current = registry.current() or {}
    current_id = current.get("bundleId")
    if not rule_release_id:
        return current_id, _versions_of(registry, current_id)
    if isinstance(rule_release_id, dict):
        bundle_id = (rule_release_id.get("bundleId") or rule_release_id.get("bundle")
                     or rule_release_id.get("auditProfileId"))
        rules_version = (rule_release_id.get("ruleReleaseId") or rule_release_id.get("rulesVersion")
                         or rule_release_id.get("ruleRelease"))
        if bundle_id:
            return bundle_id, _versions_of(registry, bundle_id)
        rule_release_id = rules_version
    text = str(rule_release_id or "")
    if text.startswith("bundle-") and registry.bundle_meta(text):
        return text, _versions_of(registry, text)
    # rulesVersion may be shared by more than one bundle (P2 leftover).
    # Prefer the current bundle when it matches; otherwise the latest history row.
    current_versions = _versions_of(registry, current_id)
    if current_versions.get("rulesVersion") == text or current_id == text:
        return current_id, current_versions
    history = list(registry.history() or [])
    for row in reversed(history):
        versions = row.get("versions") or {}
        if versions.get("rulesVersion") == text or row.get("bundleId") == text:
            bundle_id = row.get("bundleId")
            return bundle_id, _versions_of(registry, bundle_id)
    return current_id, current_versions


def _coverage_book(registry, bundle_id):
    folder = registry._bundle_dir(bundle_id)
    path = folder / "knowledge" / "coverage.json"
    if path.is_file():
        return _read_json(path, {}) or {}
    fallback = registry.knowledge_dir / "coverage.json"
    return _read_json(fallback, {}) or {}


def _clause_parts(field):
    text = str(field or "").replace("附录", " ")
    parts = []
    for part in re.split(r"[、，,;；]+", text):
        part = part.strip()
        if part:
            parts.append(part)
    return parts


def _citations_of(record):
    clauses = []
    for row in record.get("citations") or []:
        document = row.get("document") or ""
        for number in _clause_parts(row.get("clause")):
            clauses.append({"edition": row.get("edition"), "document": document, "clause": number,
                            "pages": row.get("pages")})
    return clauses


class RuleRegistry:
    def __init__(self, registry=None):
        self.registry = registry or registry_mod.default_registry()

    def list_rules(self, ruleReleaseId=None):
        """Published rules only, including disabled ones with their status."""
        bundle_id, versions = resolve_release(ruleReleaseId, self.registry)
        book = self.registry.rules_bundle(bundle_id) or {}
        published = dict(book.get("rules") or {})
        coverage = _coverage_book(self.registry, bundle_id)
        unregistered = []
        coverage_by_rule = {}
        for number, row in (coverage.get("clauses") or {}).items():
            rule_ids = row.get("rule") or []
            if isinstance(rule_ids, str):
                rule_ids = [rule_ids]
            for rule_id in rule_ids:
                coverage_by_rule.setdefault(rule_id, []).append({
                    "standard": coverage.get("standard") or "",
                    "clause": number,
                    "title": row.get("title"),
                    "status": row.get("status"),
                    "note": row.get("note"),
                })
                if rule_id not in published:
                    unregistered.append({
                        "ruleId": rule_id,
                        "clause": number,
                        "standard": coverage.get("standard") or "",
                        # The clause row may say "implemented" because other registered
                        # rules share it; the unregistered id itself is not implemented.
                        "clauseCoverageStatus": row.get("status"),
                        "coverageStatus": "unregistered",
                        "flag": "unregistered",
                        "implemented": False,
                        "note": "覆盖表引用了未在已发布目录登记的规则编号，不能当作已实现。",
                    })
        already = {row["ruleId"] for row in unregistered}
        # Catalog consistency / allergen ids can be writable in the program even when
        # the coverage table omits them (I03 was missing from that list). They stay
        # unlabeled as published unless a formal republish adds them to the 37-rule book.
        for rule_id in (*CONSISTENCY_RULE_IDS, *ALLERGEN_BRANCH_IDS, "N00"):
            if rule_id in published or rule_id in already:
                continue
            catalog = registry_mod.RULE_CATALOG.get(rule_id) or {}
            unregistered.append({
                "ruleId": rule_id,
                "clause": "",
                "standard": "",
                "clauseCoverageStatus": "absent",
                "coverageStatus": "unregistered",
                "flag": "unregistered",
                "implemented": False,
                "note": "程序可写入，但未在本包已发布的 37 个规则目录中登记，覆盖表也未列示。非本包已发布规则 ID。不得当作已发布规则。",
                "title": catalog.get("title") or "",
            })
        rules = []
        for rule_id, record in published.items():
            catalog = registry_mod.RULE_CATALOG.get(rule_id) or {}
            enabled = record.get("enabled", True) is not False
            item = dict(record)
            item["id"] = rule_id
            item["enabled"] = enabled
            item["status"] = "enabled" if enabled else "disabled"
            item["statusLabel"] = "启用" if enabled else "已停用"
            item["evidenceNeeds"] = evidence_needs_for(rule_id, {**catalog, **record})
            if catalog.get("methods") and not item.get("methods"):
                item["methods"] = list(catalog.get("methods") or [])
            # Overlay catalog implementation only when the published record has none.
            if not (record.get("implementationVersion") or "").strip():
                if rule_id in UNIMPLEMENTED_RULE_IDS or catalog.get("implementationKind") == "unimplemented":
                    item["implementationKind"] = "unimplemented"
                    item["implementationVersion"] = catalog.get("implementationVersion") or ""
                elif catalog.get("implementationVersion"):
                    item["implementationVersion"] = catalog["implementationVersion"]
            item["citations"] = record.get("citations") or []
            item["citedClauses"] = _citations_of(item)
            item["coverage"] = coverage_by_rule.get(rule_id) or []
            rules.append(item)
        return {
            "bundleId": bundle_id,
            "ruleReleaseId": versions.get("rulesVersion") or bundle_id,
            "versions": versions,
            "rules": rules,
            "unregisteredCoverageIds": unregistered,
            "source": "published-bundle",
        }

    def build_checklist(self, productProfile=None, auditProfile=None):
        """Emit all 91 checkpoints from the 应查项 table for this audit profile."""
        productProfile = productProfile if isinstance(productProfile, dict) else {}
        auditProfile = auditProfile if isinstance(auditProfile, dict) else {}
        bundle_id, versions = resolve_release(
            auditProfile.get("bundleId") or auditProfile.get("ruleReleaseId")
            or auditProfile.get("rulesVersion") or auditProfile,
            self.registry)
        listed = self.list_rules(auditProfile or bundle_id)
        published_ids = {item["id"] for item in listed["rules"]}
        disabled_ids = {item["id"] for item in listed["rules"] if not item.get("enabled")}
        table = load_checkpoints()
        checkpoints = []
        for row in table["checkpoints"]:
            applicability = infer_applicability(row, productProfile)
            rule_ids = list(row.get("ruleIds") or [])
            unregistered = [rule_id for rule_id in rule_ids
                            if _is_catalog_rule(rule_id) and rule_id not in published_ids]
            item = {
                "checkpointId": row["checkpointId"],
                "theme": row.get("theme"),
                "themeId": row.get("themeId"),
                "themeLabel": row.get("themeLabel"),
                "title": row.get("title"),
                "ruleIds": rule_ids,
                "applicability": applicability,
                "executionMethod": row.get("executionMethod"),
                "executionMethods": list(row.get("executionMethods") or []),
                "evidenceRequirements": dict(row.get("evidenceRequirements") or {}),
                "coverageStatus": row.get("coverageStatus"),
                "coverageStatusLabel": row.get("coverageStatusLabel"),
                "authorizedBasis": row.get("authorizedBasis"),
                "appliesWhen": row.get("appliesWhen"),
                "unregisteredRuleIds": unregistered,
                "disabledRuleIds": [rule_id for rule_id in rule_ids if rule_id in disabled_ids],
                "releases": dict(versions),
            }
            checkpoints.append(item)
        theme_ids = sorted({row["themeId"] for row in checkpoints if row.get("themeId")})
        return {
            "source": table.get("source"),
            "count": len(checkpoints),
            "themes": table.get("themes") or [],
            "themeIds": theme_ids,
            "auditProfile": dict(versions),
            "bundleId": bundle_id,
            "ruleReleaseId": versions.get("rulesVersion") or bundle_id,
            "unregisteredCoverageIds": listed.get("unregisteredCoverageIds") or [],
            "checkpoints": checkpoints,
        }

    def evaluate(self, factSnapshot, checklist, releases):
        """Wrap existing audit/consistency/interpretation algorithms.

        Disabled, failed or unknown applicability never become pass.
        Agent silence is not pass. Missing required evidence downgrades a
        definite pass/issue to review or not_evaluated in the ledger only;
        raw audit item outcomes are left unchanged.
        """
        releases = releases if isinstance(releases, dict) else {}
        bundle_id, versions = resolve_release(
            releases.get("bundleId") or releases.get("ruleReleaseId") or releases.get("rulesVersion")
            or releases, self.registry)
        pinned = dict(versions)
        if isinstance(checklist, dict) and not checklist.get("checkpoints"):
            checklist = self.build_checklist(_product_from_snapshot(factSnapshot), releases)
        elif not isinstance(checklist, dict):
            checklist = self.build_checklist(_product_from_snapshot(factSnapshot), releases)
        payload = _as_audit_payload(factSnapshot, pinned)
        execution_error = None
        report = None
        if _looks_like_audit_result(factSnapshot) and not payload:
            report = factSnapshot
        else:
            try:
                import audit as audit_mod
                report = audit_mod.audit(payload)
            except Exception as exc:
                execution_error = exc
                report = {"items": [], "versions": pinned, "executionError": str(exc),
                          "executionFailed": True}
        return self.map_results(report, checklist, releases, execution_error)

    def map_results(self, report, checklist, releases=None, execution_error=None):
        """Map an existing audit result onto the 91-checkpoint ledger."""
        releases = releases if isinstance(releases, dict) else {}
        versions = ((report or {}).get("versions") if isinstance(report, dict) else None) or {}
        if not versions:
            _, versions = resolve_release(releases or None, self.registry)
        listed = self.list_rules(versions or releases)
        published = {item["id"]: item for item in listed["rules"]}
        disabled_ids = {item["id"] for item in listed["rules"] if not item.get("enabled")}
        items = list((report or {}).get("items") or []) if isinstance(report, dict) else []
        by_rule = {}
        for item in items:
            for key in (item.get("ruleId"), item.get("id"), item.get("citationId")):
                if key:
                    by_rule.setdefault(str(key), []).append(item)
        fact_evidence = _fact_evidence(report if isinstance(report, dict) else {}, releases)
        ledger = []
        if not isinstance(checklist, dict) or not checklist.get("checkpoints"):
            checklist = self.build_checklist(_product_from_snapshot(report), versions)
        failed = bool(execution_error or (isinstance(report, dict) and report.get("executionFailed")))
        failure_note = ""
        if execution_error:
            failure_note = str(execution_error)
        elif failed:
            failure_note = str((report or {}).get("executionError") or "执行失败")
        for row in checklist["checkpoints"]:
            entry = _evaluate_checkpoint(
                row, by_rule, published, disabled_ids, fact_evidence, failed, failure_note)
            ledger.append(entry)
        counts = {key: 0 for key in (RESULT_PASS, RESULT_ISSUE, RESULT_REVIEW, RESULT_NA, RESULT_NE)}
        for entry in ledger:
            counts[entry["result"]] = counts.get(entry["result"], 0) + 1
        # T13: these buckets must never be counted as pass.
        pass_ids = [entry["checkpointId"] for entry in ledger if entry["result"] == RESULT_PASS]
        for entry in ledger:
            if entry["result"] == RESULT_PASS and (
                    entry.get("applicability") == "unknown"
                    or entry.get("disabledRuleIds")
                    or entry.get("execution") == "failed"
                    or entry.get("statusLabel") == UNCOVERED):
                raise AssertionError("disabled / failed / unknown applicability must not be pass")
        return {
            "bundleId": versions.get("bundleId"),
            "ruleReleaseId": versions.get("rulesVersion") or versions.get("bundleId"),
            "versions": dict(versions),
            "checklist": checklist,
            "ledger": ledger,
            "counts": counts,
            "passCheckpointIds": pass_ids,
            "unregisteredCoverageIds": listed.get("unregisteredCoverageIds") or [],
            "executionFailed": failed,
            "executionError": failure_note or None,
            "auditItems": items,
            "sourceAuditVersions": dict(versions),
        }


def _is_catalog_rule(rule_id):
    if not rule_id:
        return False
    if rule_id in registry_mod.RULE_CATALOG:
        return True
    if rule_id in {"I01", "I02", "I03", "I04", "I06", "I07", "N00", "R14", "GB7718AL-gluten"}:
        return True
    return bool(re.fullmatch(r"[A-Z]{1,10}\d{0,4}(?:-[A-Za-z0-9]+)?", str(rule_id))) and "fact" not in str(rule_id)


def infer_applicability(checkpoint, productProfile):
    """Default unknown unless a deterministic precondition can be evaluated."""
    profile = productProfile if isinstance(productProfile, dict) else {}
    cid = checkpoint.get("checkpointId") if isinstance(checkpoint, dict) else ""
    theme_id = checkpoint.get("themeId") if isinstance(checkpoint, dict) else None
    scope = str(profile.get("scope") or profile.get("foodScope") or "").strip()
    if cid == "CP-00-01":
        return "applicable"
    if cid in {"CP-14-01", "CP-14-02"}:
        if scope == "imported":
            return "applicable"
        if scope in {"regular", "workshop"}:
            return "not_applicable"
        return "unknown"
    if cid == "CP-14-03":
        if scope == "workshop":
            return "applicable"
        if scope in {"regular", "imported"}:
            return "not_applicable"
        return "unknown"
    if theme_id in {11, 12} or (isinstance(cid, str) and cid.startswith(("CP-11-", "CP-12-"))):
        required = profile.get("nutritionRequired")
        if required in {True, "required", "yes", "true"}:
            return "applicable"
        if required in {False, "exempt", "not_required", "no", "false"}:
            return "not_applicable"
        return "unknown"
    if cid == "CP-13-01":
        required = profile.get("nutritionRequired")
        if required in {True, "required", "yes", "true"}:
            return "applicable"
        if required in {False, "exempt", "not_required", "no", "false"}:
            return "not_applicable"
        return "unknown"
    if scope in {"regular", "imported", "workshop"} and cid in {"CP-06-01", "CP-02-01", "CP-03-01"}:
        return "applicable"
    return "unknown"


def _looks_like_audit_result(value):
    return isinstance(value, dict) and "items" in value and "versions" in value and "draft" not in value


def _as_audit_payload(factSnapshot, versions):
    if not isinstance(factSnapshot, dict):
        return {"draft": {}, "pages": [], "context": {"versions": versions}}
    if "draft" in factSnapshot or "pages" in factSnapshot:
        context = dict(factSnapshot.get("context") or {})
        context["versions"] = dict(versions)
        payload = dict(factSnapshot)
        payload["context"] = context
        if "draft" not in payload:
            payload["draft"] = {}
        return payload
    if "manifest" in factSnapshot and "payload" in factSnapshot:
        return _as_audit_payload(factSnapshot["payload"], versions)
    return None


def _product_from_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        return {}
    context = snapshot.get("context") if isinstance(snapshot.get("context"), dict) else {}
    if not context and isinstance(snapshot.get("payload"), dict):
        context = snapshot["payload"].get("context") or {}
    return {
        "scope": context.get("scope"),
        "nutritionRequired": context.get("nutritionRequired"),
        "sampleKind": context.get("sampleKind"),
        "productionDate": context.get("productionDate"),
    }


def _fact_evidence(report, releases):
    """Best-effort presence flags for nutrition/NRV-style evidence. No math."""
    draft = {}
    if isinstance(releases, dict) and isinstance(releases.get("draft"), dict):
        draft = releases["draft"]
    if isinstance(report, dict):
        if isinstance(report.get("draft"), dict):
            draft = report.get("draft") or draft
        # report_api / audit result may expose fields / nutritionRows
    nutrients = draft.get("nutrition") if isinstance(draft.get("nutrition"), dict) else {}
    fields = draft.get("fields") if isinstance(draft.get("fields"), dict) else {}
    if not nutrients and isinstance(report, dict):
        rows = report.get("nutritionRows") or []
        if rows:
            nutrients = {str(row.get("key") or row.get("id") or row.get("name") or i): row
                         for i, row in enumerate(rows) if isinstance(row, dict)}
    def _has_value(mapping, keys):
        for key in keys:
            row = mapping.get(key) if isinstance(mapping, dict) else None
            if isinstance(row, dict) and str(row.get("value") or "").strip():
                return True
            if row not in (None, "", [], {}):
                if not isinstance(row, dict):
                    return True
        return False

    value = False
    unit = False
    printed = False
    for row in (nutrients or {}).values():
        if not isinstance(row, dict):
            continue
        if str(row.get("value") or "").strip():
            value = True
        if str(row.get("unit") or "").strip():
            unit = True
        if str(row.get("nrv") or row.get("printedNrv") or "").strip():
            printed = True
    basis = False
    if isinstance(fields, dict):
        basis_row = fields.get("basis") or fields.get("计量基准")
        if isinstance(basis_row, dict):
            basis = bool(str(basis_row.get("value") or "").strip())
        elif basis_row:
            basis = True
    if not basis and isinstance(report, dict):
        for row in report.get("fields") or []:
            if isinstance(row, dict) and row.get("id") in {"basis", "计量基准"} and row.get("value"):
                basis = True
    # Reference and rounding come from the published standard, not the label.
    # They are present when the published bundle can be resolved, not assumed
    # from a successful calculation.
    versions = (report or {}).get("versions") if isinstance(report, dict) else {}
    reference = bool(isinstance(versions, dict) and versions.get("knowledgeVersion"))
    rounding = reference
    net_value = False
    net_unit = False
    if isinstance(fields, dict):
        net = fields.get("net") or fields.get("netContent") or fields.get("净含量")
        if isinstance(net, dict) and str(net.get("value") or "").strip():
            net_value = True
            text = str(net.get("value") or "")
            net_unit = bool(re.search(r"(?:g|kg|ml|mL|L|克|千克|毫升|升|毫升)", text, re.I))
    return {
        "value": value or net_value,
        "unit": unit or net_unit,
        "basis": basis,
        "printedNrv": printed,
        "reference": bool(reference),
        "rounding": bool(rounding),
        "requiredFacts": True,
        "nutritionPresent": value,
        "netPresent": net_value,
    }


def _evaluate_checkpoint(row, by_rule, published, disabled_ids, fact_evidence, failed, failure_note):
    rule_ids = list(row.get("ruleIds") or [])
    registered = [rule_id for rule_id in rule_ids if rule_id in published]
    disabled = [rule_id for rule_id in registered if rule_id in disabled_ids]
    unregistered = list(row.get("unregisteredRuleIds") or [])
    applicability = row.get("applicability") or "unknown"
    execution_method = row.get("executionMethod") or "unimplemented"
    related = []
    for rule_id in rule_ids:
        related.extend(by_rule.get(rule_id) or [])
    # Deduplicate by object identity / id+title
    seen = set()
    unique = []
    for item in related:
        key = (item.get("id"), item.get("title"), item.get("outcome") or item.get("group"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    related = unique
    clause_ids = []
    for rule_id in registered:
        for cite in (published[rule_id].get("citedClauses") or []):
            if cite.get("clause"):
                clause_ids.append(cite)
    evidence_status = "missing"
    result = RESULT_NE
    status_label = UNCOVERED
    execution = "not_started"
    reason = ""
    # 限依据的转致条款（如 R14）：没命中已登记依据时不能说「没跑」，也不能说通过。
    scope_note = ""
    for rule_id in registered:
        entry = registry_mod.RULE_CATALOG.get(rule_id) or {}
        scope = entry.get("coverageScope") if isinstance(entry, dict) else None
        if isinstance(scope, dict) and str(scope.get("note") or "").strip():
            scope_note = str(scope["note"]).strip()
            break

    if applicability == "not_applicable":
        result = RESULT_NA
        status_label = "不适用"
        execution = "completed"
        reason = "根据产品范围，本检查点不适用。"
        evidence_status = "sufficient"
    elif failed and execution_method == "program":
        result = RESULT_NE
        status_label = UNCOVERED
        execution = "failed"
        reason = "执行失败，不能视为通过。" + ((" " + failure_note) if failure_note else "")
        evidence_status = "missing"
    elif applicability == "unknown":
        # Must not become pass; do not drop related clauses.
        result = RESULT_NE
        status_label = "适用性未知"
        execution = "completed" if related else "not_started"
        reason = "适用性未知，相关条款未排除，不能视为通过。"
        if scope_note and not related:
            status_label = "未覆盖（未命中已登记依据）"
            execution = "completed"
            reason = scope_note + " 适用性未知，不能视为通过。"
        if disabled:
            status_label = UNCOVERED
            reason = "规则已停用，且适用性未知，不能视为通过。"
            execution = "not_started"
        elif related:
            mapped = [_map_item_result(item) for item in related]
            if RESULT_ISSUE in mapped:
                result = RESULT_REVIEW
                status_label = "待核实"
                reason = "适用性未知，已有程序线索只能待核实，不能升为确定通过或确定问题。"
                evidence_status = "missing"
            elif RESULT_REVIEW in mapped:
                result = RESULT_REVIEW
                status_label = "待核实"
                reason = "适用性未知，已有程序线索只能待核实。"
                evidence_status = "missing"
        clause_ids = clause_ids  # keep
    elif disabled and (not registered or set(registered) <= set(disabled)):
        result = RESULT_NE
        status_label = UNCOVERED
        execution = "not_started"
        reason = "规则已停用，显示未检查／未覆盖，不能视为通过。"
    elif disabled:
        # Some of the checkpoint's rules are disabled: never pass.
        result = RESULT_NE
        status_label = UNCOVERED
        execution = "not_started"
        reason = "部分规则已停用，本检查点不能视为通过。"
        if related:
            mapped = [_map_item_result(item) for item in related]
            if RESULT_ISSUE in mapped:
                result = RESULT_ISSUE
                status_label = "须整改（同时有规则停用，未覆盖部分不得计为通过）"
                execution = "completed"
                reason = "已触发问题，但停用规则的部分仍为未覆盖。"
            elif RESULT_REVIEW in mapped:
                result = RESULT_REVIEW
                status_label = "待核实"
                execution = "completed"
                reason = "已有待核实记录，停用规则的部分仍为未覆盖，不能视为通过。"
    elif execution_method == "unimplemented" or (
            row.get("coverageStatus") == "missing" and not related and not registered):
        result = RESULT_NE
        status_label = UNCOVERED
        execution = "not_started"
        reason = "尚未实现或未覆盖，不能视为通过。"
    elif unregistered and not registered and not related:
        result = RESULT_NE
        status_label = UNCOVERED
        execution = "not_started"
        reason = "覆盖表引用了未在已发布目录登记的规则编号，不能当作已实现。"
    elif not related:
        # Program ran but produced no item, or there is no execution record.
        # Agent / program silence is not pass.
        result = RESULT_NE
        if scope_note:
            # 限依据的转致条款：本样本没有触发任何已登记依据，属于「未覆盖」，
            # 不是「程序没跑」。这样既不判通过，也不在客户版伪造一条待办。
            status_label = "未覆盖（未命中已登记依据）"
            execution = "completed"
            reason = scope_note
        else:
            status_label = UNCOVERED if execution_method in {"unimplemented", "AI-assist"} else "未产生执行记录"
            execution = "not_started"
            reason = "没有执行记录，不能视为通过。"
            if execution_method == "program" and registered:
                status_label = "未产生执行记录"
                reason = "已登记程序规则未产生本项记录，不能视为通过。"
    else:
        mapped = [_map_item_result(item) for item in related]
        execution = "completed"
        if any((item.get("outcome") or item.get("group")) == "uncovered" for item in related) and all(
                (item.get("outcome") or item.get("group")) in {"uncovered", "gap"} for item in related):
            result = RESULT_NE
            status_label = UNCOVERED
            reason = "规则已停用或未覆盖，不能视为通过。"
            execution = "not_started"
        elif RESULT_ISSUE in mapped:
            result = RESULT_ISSUE
            status_label = "须整改"
            reason = "程序检查给出确定问题。"
            evidence_status = "sufficient"
        elif RESULT_REVIEW in mapped or "gap" in {(item.get("outcome") or item.get("group")) for item in related}:
            result = RESULT_REVIEW
            status_label = "待核实"
            reason = "程序检查给出待核实，不能视为通过。"
            evidence_status = "missing"
        elif mapped and all(value == RESULT_PASS for value in mapped):
            result = RESULT_PASS
            status_label = "通过"
            reason = "程序检查给出通过。"
            evidence_status = "sufficient"
        else:
            result = RESULT_NE
            status_label = "未产生明确结论"
            reason = "没有明确的通过或问题记录，不能视为通过。"
            execution = "completed"

    # Missing required evidence cannot stay a definite pass or issue.
    if result in {RESULT_PASS, RESULT_ISSUE} and applicability == "applicable":
        needed = (row.get("evidenceRequirements") or {}).get(result) or []
        if _nutrition_related(row, rule_ids):
            missing = [key for key in needed if key in NUTRITION_EVIDENCE and not fact_evidence.get(key)]
            if missing:
                result = RESULT_REVIEW
                status_label = "待核实"
                evidence_status = "missing"
                reason = "所需证据不完整（缺少 " + "、".join(missing) + "），不能给出确定通过或确定问题。"
                execution = "completed"

    return {
        "checkpointId": row.get("checkpointId"),
        "theme": row.get("theme"),
        "themeId": row.get("themeId"),
        "title": row.get("title"),
        "ruleIds": rule_ids,
        "ruleResultIds": [item.get("id") for item in related if item.get("id")],
        "clauseIds": clause_ids,
        "applicability": applicability,
        "execution": execution,
        "executionMethod": execution_method,
        "evidenceStatus": evidence_status,
        "result": result,
        "statusLabel": status_label,
        "reason": reason,
        "disabledRuleIds": disabled,
        "unregisteredRuleIds": unregistered,
        "coverageStatus": row.get("coverageStatus"),
    }


def _nutrition_related(row, rule_ids):
    if row.get("themeId") in {11, 12}:
        return True
    return any(rule_id in NUTRITION_RULE_IDS for rule_id in rule_ids)


def _map_item_result(item):
    outcome = item.get("outcome") or item.get("group") or ""
    if item.get("status") == UNCOVERED or outcome == "uncovered":
        return RESULT_NE
    return _ITEM_RESULT.get(outcome, RESULT_NE)


def record_audit(report, product_profile, audit_profile, registry=None):
    """Attach a checklist ledger to an already-computed audit result (no re-entry)."""
    engine = RuleRegistry(registry)
    checklist = engine.build_checklist(product_profile, audit_profile)
    mapped = engine.map_results(report, checklist, audit_profile)
    return {
        "source": checklist.get("source"),
        "count": checklist.get("count"),
        "themes": checklist.get("themes"),
        "themeIds": checklist.get("themeIds"),
        "auditProfile": checklist.get("auditProfile"),
        "bundleId": checklist.get("bundleId"),
        "ruleReleaseId": checklist.get("ruleReleaseId"),
        "checkpoints": checklist.get("checkpoints"),
        "ledger": mapped.get("ledger"),
        "counts": mapped.get("counts"),
        "unregisteredCoverageIds": mapped.get("unregisteredCoverageIds"),
    }


def default_rule_registry():
    return RuleRegistry()


def list_rules(ruleReleaseId=None, registry=None):
    return RuleRegistry(registry).list_rules(ruleReleaseId)


def build_checklist(productProfile=None, auditProfile=None, registry=None):
    return RuleRegistry(registry).build_checklist(productProfile, auditProfile)


def evaluate(factSnapshot, checklist, releases, registry=None):
    return RuleRegistry(registry).evaluate(factSnapshot, checklist, releases)
