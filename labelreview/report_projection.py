"""T18: project report cards, sections, materials, and 07 actions from one DecisionSet.

Counts are unique decisions (or checkpoints), not rule-entry counts and not
wording-overlap merges. A material request attached to an already-counted
decision is listed once and is not also counted as a second issue.
Conditional remedies are separated from formal 07 edits.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

import labelreview.decision_set as decision_set
from labelreview.decision_set import DecisionSet, FAIL_CONCLUSION, PENDING_CONCLUSION

SECTIONS = ("02", "03", "04", "06")


def _item_map(items: Optional[Sequence[Mapping[str, Any]]]) -> dict:
    rows = {}
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        item_id = str(item.get("id") or item.get("itemId") or "")
        if item_id:
            rows[item_id] = item
    return rows


def _has_human_ruling(decision: Mapping[str, Any]) -> bool:
    """True only when a named operator recorded a ruling (not auto-seeded locks)."""
    for row in decision.get("rulings") or []:
        if isinstance(row, Mapping) and str(row.get("operator") or "").strip():
            return True
    return False


def _section_for(decision: Mapping[str, Any], item: Mapping[str, Any]) -> Optional[str]:
    conclusion = str(decision.get("conclusion") or "")
    if conclusion == PENDING_CONCLUSION:
        return "06"
    if conclusion == "不适用":
        # Coverage N/A is not a customer 待核实 card; it stays in checkpoint counts.
        return None
    if conclusion == "证据不足":
        # Insufficient evidence belongs in 06 only when there is a renderable row.
        return "06" if item else None
    if conclusion and conclusion not in {FAIL_CONCLUSION, PENDING_CONCLUSION}:
        # Other non-issue conclusions stay out of 02–04.
        if not item:
            return None
    if conclusion != FAIL_CONCLUSION and not item:
        return None
    track = item.get("track")
    outcome = item.get("outcome") or item.get("group")
    group = item.get("group")
    severity = item.get("severity")
    if conclusion == FAIL_CONCLUSION or outcome == "issue" or group in {"issue", "internal"}:
        if track == "standard2025":
            return "04"
        if severity in {"critical", "redline", "draft-hold"}:
            return "02"
        return "03"
    if outcome in {"review", "gap"} or group not in {"issue", "pass", "internal", None, ""}:
        return "06"
    return None


def _material_key(row: Mapping[str, Any]) -> tuple:
    return (
        str(row.get("decisionId") or ""),
        str(row.get("what") or "").strip(),
        str(row.get("fromWhom") or "").strip(),
        str(row.get("affects") or "").strip(),
    )


def _merge_date_anchor(section_ids: list, items_by_id: dict) -> list:
    if "date-anchor" not in section_ids:
        return section_ids
    anchor = items_by_id.get("date-anchor") or {}
    refs = set(anchor.get("refs") or [])
    if not refs:
        return section_ids
    if any(
        item_id in {"F04", "G02"} and refs & set((items_by_id.get(item_id) or {}).get("refs") or [])
        for item_id in section_ids
    ):
        return [item_id for item_id in section_ids if item_id != "date-anchor"]
    return section_ids


def project(
    decisions: Optional[DecisionSet],
    items: Optional[Sequence[Mapping[str, Any]]] = None,
    *,
    plan: Optional[Mapping[str, Any]] = None,
    coverage: Optional[Sequence[Mapping[str, Any]]] = None,
) -> dict:
    """Return the T18 projection bound to one DecisionSet (fallback: items only)."""
    items = [row for row in (items or []) if isinstance(row, Mapping)]
    items_by_id = _item_map(items)
    sections = {key: [] for key in SECTIONS}
    seen = set()
    rows = []

    decision_rows = list(decisions.decisions) if decisions is not None else []
    for decision in decision_rows:
        item_id = str(decision.get("itemId") or decision.get("decisionId") or "")
        if not item_id or item_id in seen:
            continue
        item = items_by_id.get(item_id) or {}
        # Auto-seeded coverage / unconfirmed AI candidates often use a DEC-* id
        # that is not in report.items. Counting them inflates 03/06 titles while
        # report.js drops the missing rows (title-mismatch, no HTML save).
        # They stay out of official customer cards until a human ruling exists.
        if not item and not _has_human_ruling(decision):
            continue
        section = _section_for(decision, item)
        if not section:
            continue
        seen.add(item_id)
        sections[section].append(item_id)
        rows.append({"decision": decision, "item": item, "section": section})

    # Items not yet in the DecisionSet are not invented as passes, but a
    # projection used before seeding should still be able to describe them
    # when no DecisionSet is supplied (legacy classify path).
    if decisions is None:
        for item in items:
            item_id = str(item.get("id") or item.get("itemId") or "")
            if not item_id or item_id in seen:
                continue
            fake = {"itemId": item_id, "conclusion": decision_set.conclusion_for_item(item) or "",
                    "decisionId": item_id, "status": "legacy"}
            section = _section_for(fake, item)
            if not section:
                continue
            seen.add(item_id)
            sections[section].append(item_id)
            rows.append({"decision": fake, "item": item, "section": section})

    sections["06"] = _merge_date_anchor(sections["06"], items_by_id)

    classification = {
        "recheckRequired": len(sections["02"]),
        "confirmedRedline": sum(
            1 for item_id in sections["02"]
            if (items_by_id.get(item_id) or {}).get("severity") != "draft-hold"
        ),
        "draftHold": sum(
            1 for item_id in sections["02"]
            if (items_by_id.get(item_id) or {}).get("severity") == "draft-hold"
        ),
        "normative": len(sections["03"]),
        "mustChange": len(sections["04"]),
        "pending": len(sections["06"]),
    }

    materials, seen_materials = [], set()
    if decisions is not None:
        for row in decisions.pendingMaterials:
            key = _material_key(row)
            if not key[1] or key in seen_materials:
                continue
            # A material request belongs to its decision; it is not a second
            # issue count. It is listed once, even if the decision sits in 02–04.
            seen_materials.add(key)
            materials.append({
                "decisionId": row.get("decisionId") or "",
                "itemId": row.get("itemId") or "",
                "what": row.get("what") or "",
                "fromWhom": row.get("fromWhom") or "",
                "affects": row.get("affects") or "",
            })
    for item_id in sections["06"]:
        item = items_by_id.get(item_id) or {}
        analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        missing = item.get("missingMaterial") if isinstance(item.get("missingMaterial"), dict) else {}
        texts = list(analysis.get("missingEvidence") or [])
        if missing.get("what"):
            texts.append("%s（%s）；影响：%s" % (
                missing.get("what"), missing.get("fromWhom") or "", missing.get("affects") or ""))
        for text in texts:
            blob = str(text).strip()
            key = ("text", blob)
            if blob and key not in seen_materials:
                seen_materials.add(key)
                materials.append({"decisionId": "", "itemId": item_id, "what": blob, "fromWhom": "", "affects": ""})

    formal, conditional = split_revision_actions(plan, decisions)
    checkpoint_counts = _checkpoint_counts(coverage)

    counts = [
        classification["recheckRequired"],
        classification["normative"],
        classification["mustChange"],
        classification["pending"],
    ]
    return {
        "source": "decisionSet" if decisions is not None else "items",
        "decisionVersion": decisions.decisionVersion if decisions is not None else "",
        "classification": classification,
        "sections": sections,
        "counts": counts,
        "materials": materials,
        "materialSummaries": [row["what"] for row in materials if row.get("what")][:12],
        "formalEdits": formal,
        "conditionalEdits": conditional,
        "checkpointCounts": checkpoint_counts,
    }


def split_revision_actions(
    plan: Optional[Mapping[str, Any]],
    decisions: Optional[DecisionSet] = None,
) -> tuple[list, list]:
    """Split plan actions into formal 07 edits vs 核实后选择的改法."""
    formal, conditional = [], []
    for item in (plan or {}).get("items") or []:
        if not isinstance(item, Mapping):
            continue
        state = str(item.get("decisionState") or "")
        item_id = str(item.get("itemId") or item.get("id") or "")
        frozen = None
        if decisions is not None and item_id:
            frozen = decisions._find(item_id)
        if frozen and frozen.get("conclusion") == PENDING_CONCLUSION:
            state = "needs_material"
        if item.get("options") or item.get("pendingEdit") or state in {"conditional", "needs_material"}:
            if item.get("pendingEdit") or item.get("options") or item.get("edit"):
                conditional.append({
                    "itemId": item_id,
                    "actionId": item.get("actionId"),
                    "decisionState": state or "conditional",
                    "title": item.get("title") or "",
                    "options": list(item.get("options") or []),
                    "pendingEdit": item.get("pendingEdit"),
                    "decisionId": (frozen or {}).get("decisionId") or "",
                })
            continue
        if state == "confirmed" and item.get("edit"):
            if (item.get("edit") or {}).get("operation") == "suggest_reorder":
                conditional.append({
                    "itemId": item_id,
                    "actionId": item.get("actionId"),
                    "decisionState": "conditional",
                    "title": item.get("title") or "",
                    "options": item.get("options") or [],
                    "pendingEdit": item.get("edit"),
                    "decisionId": (frozen or {}).get("decisionId") or "",
                })
                continue
            formal.append({
                "itemId": item_id,
                "actionId": item.get("actionId"),
                "decisionState": "confirmed",
                "edit": item.get("edit"),
                "decisionId": (frozen or {}).get("decisionId") or "",
            })
    return formal, conditional


def _checkpoint_counts(coverage: Optional[Sequence[Mapping[str, Any]]]) -> dict:
    rows = [row for row in (coverage or []) if isinstance(row, Mapping)]
    seen = set()
    unique_rows = []
    for row in rows:
        key = str(row.get("checklistId") or row.get("checkpointId") or row.get("id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        unique_rows.append(row)
    counts = {
        "checkpoints": len(unique_rows),
        "checked": 0,
        "unchecked": 0,
        "uncovered": 0,
        "applicable": 0,
        "notApplicable": 0,
        "insufficient": 0,
    }
    for row in unique_rows:
        coverage_value = str(row.get("coverage") or row.get("status") or "")
        applicability = str(row.get("applicability") or "")
        if coverage_value in {"已查", "checked"}:
            counts["checked"] += 1
        elif coverage_value in {"未检查／未覆盖", "uncovered"}:
            counts["uncovered"] += 1
        elif coverage_value in {"未查", "unchecked"}:
            counts["unchecked"] += 1
        if applicability in {"适用", "applicable"}:
            counts["applicable"] += 1
        elif applicability in {"不适用", "not_applicable"}:
            counts["notApplicable"] += 1
        elif applicability in {"证据不足", "insufficient"}:
            counts["insufficient"] += 1
    return counts


def view_from_projection(projection: Mapping[str, Any]) -> dict:
    """Shape used by report.js / report_api._report_view."""
    return {
        "source": projection.get("source"),
        "decisionVersion": projection.get("decisionVersion") or "",
        "classification": dict(projection.get("classification") or {}),
        "sections": dict(projection.get("sections") or {}),
        "materials": list(projection.get("materialSummaries") or []),
        "counts": list(projection.get("counts") or []),
        "formalEdits": list(projection.get("formalEdits") or []),
        "conditionalEdits": list(projection.get("conditionalEdits") or []),
        "checkpointCounts": dict(projection.get("checkpointCounts") or {}),
    }
