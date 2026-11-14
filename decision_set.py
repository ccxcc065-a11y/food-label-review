"""DecisionSet for P4 (T15 / T17), now used by the live audit/report path.

The content-bound candidate and writer contracts live here. report_api and
runtime_agents import this module for promotion and writer gating; they
should call these APIs rather than reimplementing approval identity.

Contracts this file encodes:

- candidateId is a digest of assertion + evidence + clause refs + versions.
  A round-sequence label such as AI-1 is only a display id and is never
  used as the approval key.
- decisionVersion is a digest of frozen facts, rules, and human rulings.
  AI call order and unconfirmed candidate order do not enter the digest.
- Human confirmations are rulings bound to a specific candidate and the
  current snapshot / clause / evidence versions. They do not mutate facts.
- A writer cannot overwrite a frozen conclusion without new evidence.
  A registered exception flows back as a dispute and a new decisionVersion,
  never as a silent overwrite.

The no-arg constructor, lock(), add(), and reject_writer_revision() hooks
match the intended T17 contract already described in
tests/test_p0_known_defects.py. That file is not edited here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union


SCHEMA = "p4-decision-set-1"

PASS_CONCLUSION = "可以通过"
FAIL_CONCLUSION = "须整改"
PENDING_CONCLUSION = "待核实"

_PASS_TEXT = re.compile(r"可以通过|无需整改|不必整改|无须整改|无需修改")
_FAIL_TEXT = re.compile(r"须整改|需要整改|应整改|应当整改")
_PENDING_TEXT = re.compile(r"待核实|待补充|待核验|待查")

_FACT_PATCH_KEYS = {
    "facts", "factPatch", "adoptedValues", "adopted_values",
    "fieldValues", "field_values", "mutateFacts", "newFacts",
}


class DecisionError(ValueError):
    """Base error for DecisionSet operations."""


class UnboundApprovalError(DecisionError):
    """Approval was a round label or otherwise not bound to a candidate."""


class ApprovalTransferError(DecisionError):
    """An approval for one candidate was applied to a different one."""


class WriterOverwriteError(DecisionError):
    """Writer tried to change a conclusion without a legitimate path."""


class RulingValidationError(DecisionError):
    """A human ruling was missing bindings or tried to patch facts."""


class StaleRevisionError(DecisionError):
    """expectedDecisionRevision did not match the current decisionVersion."""


class UnknownDecisionError(DecisionError):
    """No decision or candidate matched the requested id."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _short(value: Any, prefix: str, size: int = 16) -> str:
    return "%s%s" % (prefix, _digest(value)[:size])


def _clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _sort_key(row: Mapping, *keys: str) -> tuple:
    return tuple(str(row.get(key) or "") for key in keys)


def infer_conclusion_from_text(text: str) -> Optional[str]:
    """Map writer prose to a conclusion token.

    '无需整改' is checked before '须整改' so the latter is not a substring hit.
    """
    blob = str(text or "")
    if not blob.strip():
        return None
    if _PASS_TEXT.search(blob):
        return PASS_CONCLUSION
    if _FAIL_TEXT.search(blob):
        return FAIL_CONCLUSION
    if _PENDING_TEXT.search(blob):
        return PENDING_CONCLUSION
    return None


def normalize_candidate(raw: Mapping[str, Any]) -> dict:
    """Stable content payload used to compute candidateId.

    Display / round identifiers (id, displayId, roundId, sequence) are
    deliberately excluded. Field aliases from the older AI-1 fixtures
    (claim/fact/reason, evidenceIds, clause) are accepted and folded in.
    """
    if not isinstance(raw, Mapping):
        raise DecisionError("candidate must be a mapping")

    assertion = raw.get("assertion")
    if assertion is None:
        assertion = {
            "claim": raw.get("claim") or raw.get("title") or "",
            "fact": raw.get("fact") or "",
            "reason": raw.get("reason") or "",
        }
    assertion = _clean(assertion)

    evidence = _normalize_evidence(raw)
    clause_refs = _normalize_clause_refs(raw)
    versions = _normalize_versions(raw)
    exceptions = _normalize_exceptions(raw.get("consideredExceptions"))

    return {
        "assertion": assertion,
        "auditProfileId": str(raw.get("auditProfileId") or raw.get("audit_profile_id") or ""),
        "checkpointId": str(raw.get("checkpointId") or raw.get("checkpoint_id") or ""),
        "clauseRefs": clause_refs,
        "consideredExceptions": exceptions,
        "evidence": evidence,
        "snapshotId": str(raw.get("snapshotId") or raw.get("snapshot_id") or ""),
        "versions": versions,
    }


def _normalize_evidence(raw: Mapping[str, Any]) -> list:
    rows = []
    if raw.get("evidence"):
        for item in _as_list(raw.get("evidence")):
            if isinstance(item, Mapping):
                rows.append({
                    "evidenceId": str(item.get("evidenceId") or item.get("id") or ""),
                    "quoteOrValue": str(item.get("quoteOrValue") or item.get("text") or item.get("value") or ""),
                    "role": str(item.get("role") or "supports"),
                    "version": str(item.get("version") or item.get("textHash") or item.get("digest") or ""),
                })
            else:
                rows.append({
                    "evidenceId": str(item),
                    "quoteOrValue": "",
                    "role": "supports",
                    "version": "",
                })
    else:
        quotes = raw.get("evidenceQuotes") or {}
        for item in _as_list(raw.get("evidenceIds") or raw.get("refs")):
            evidence_id = str(item)
            quote = ""
            if isinstance(quotes, Mapping):
                quote = str(quotes.get(evidence_id) or "")
            rows.append({
                "evidenceId": evidence_id,
                "quoteOrValue": quote,
                "role": "supports",
                "version": "",
            })
    rows.sort(key=lambda row: _sort_key(row, "evidenceId", "role", "quoteOrValue", "version"))
    return rows


def _normalize_clause_refs(raw: Mapping[str, Any]) -> list:
    rows = []
    for item in _as_list(raw.get("clauseRefs") or raw.get("clause_refs")):
        if not isinstance(item, Mapping):
            rows.append({
                "clauseId": str(item),
                "document": "",
                "textHash": "",
                "version": "",
                "reason": "",
            })
            continue
        rows.append({
            "clauseId": str(item.get("clauseId") or item.get("clause") or item.get("id") or ""),
            "document": str(item.get("document") or item.get("documentId") or item.get("standard") or ""),
            "textHash": str(item.get("textHash") or item.get("text_hash") or item.get("digest") or ""),
            "version": str(item.get("version") or item.get("edition") or item.get("knowledgeVersion") or ""),
            "reason": str(item.get("reason") or item.get("applicabilityReason") or item.get("适用理由") or ""),
        })
    rows.sort(key=lambda row: _sort_key(row, "document", "clauseId", "textHash", "version", "reason"))
    return rows


def _normalize_versions(raw: Mapping[str, Any]) -> dict:
    versions = raw.get("versions") if isinstance(raw.get("versions"), Mapping) else {}
    return {
        "knowledgeVersion": str(
            raw.get("knowledgeVersion") or versions.get("knowledgeVersion") or versions.get("knowledge") or ""
        ),
        "rulesVersion": str(
            raw.get("rulesVersion") or versions.get("rulesVersion") or versions.get("rules") or ""
        ),
        "evidenceVersion": str(
            raw.get("evidenceVersion") or versions.get("evidenceVersion") or versions.get("evidence") or ""
        ),
    }


def _normalize_exceptions(value: Any) -> list:
    rows = []
    for item in _as_list(value):
        if isinstance(item, Mapping):
            rows.append({
                "exceptionId": str(item.get("exceptionId") or item.get("id") or ""),
                "clauseId": str(item.get("clauseId") or item.get("clause") or ""),
                "document": str(item.get("document") or item.get("documentId") or ""),
                "textHash": str(item.get("textHash") or item.get("digest") or ""),
            })
        else:
            rows.append({
                "exceptionId": str(item),
                "clauseId": "",
                "document": "",
                "textHash": "",
            })
    rows.sort(key=lambda row: _sort_key(row, "document", "clauseId", "exceptionId", "textHash"))
    return rows


def candidate_id(raw: Mapping[str, Any]) -> str:
    """Content-bound candidate identity. Independent of AI-1 style labels."""
    return _short(normalize_candidate(raw), "cand-")


def display_id(raw: Mapping[str, Any]) -> str:
    return str(raw.get("displayId") or raw.get("roundId") or raw.get("id") or "")


def bind_candidate(raw: Mapping[str, Any]) -> dict:
    """Return a candidate record with content id and preserved display id."""
    bound_id = candidate_id(raw)
    payload = normalize_candidate(raw)
    missing = raw.get("missingMaterial") if isinstance(raw.get("missingMaterial"), Mapping) else {}
    return {
        "candidateId": bound_id,
        "displayId": display_id(raw),
        "snapshotId": payload["snapshotId"],
        "auditProfileId": payload["auditProfileId"],
        "checkpointId": payload["checkpointId"],
        "assertion": payload["assertion"],
        "evidence": payload["evidence"],
        "clauseRefs": payload["clauseRefs"],
        "consideredExceptions": payload["consideredExceptions"],
        "versions": payload["versions"],
        "missingMaterial": _clean(missing) if missing else {},
        "proposedOutcome": raw.get("proposedOutcome") or raw.get("claim") or "",
    }


def same_issue(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Two candidates are the same issue only when their content ids match.

    Overlapping wording is not enough and must not auto-merge them.
    """
    return candidate_id(left) == candidate_id(right)


def parse_approval(value: Any) -> dict:
    """Accept only an approval bound to a content candidateId.

    A bare 'AI-1' string, or a record that only has a round/display id, is
    rejected. Server-side code must not trust a client-supplied sequence
    number as an approval key.
    """
    if isinstance(value, str):
        token = value.strip()
        if token.startswith("cand-") and len(token) >= 10:
            return {
                "candidateId": token,
                "snapshotId": "",
                "auditProfileId": "",
            }
        raise UnboundApprovalError(
            "approval %r is not bound to a candidateId; round labels such as AI-1 cannot be reused"
            % (value,)
        )
    if not isinstance(value, Mapping):
        raise UnboundApprovalError("approval must be a mapping bound to a candidateId")
    bound = str(value.get("candidateId") or value.get("candidate_id") or "").strip()
    if not bound:
        raise UnboundApprovalError(
            "approval is missing candidateId; display/round id %r is not a valid binding"
            % (value.get("id") or value.get("displayId") or value.get("roundId") or "",)
        )
    return {
        "candidateId": bound,
        "snapshotId": str(value.get("snapshotId") or value.get("snapshot_id") or ""),
        "auditProfileId": str(value.get("auditProfileId") or value.get("audit_profile_id") or ""),
    }


def approval_matches(approval: Any, candidate: Mapping[str, Any]) -> bool:
    """True only when the approval is content-bound to this candidate."""
    try:
        parsed = parse_approval(approval)
    except UnboundApprovalError:
        return False
    bound = bind_candidate(candidate)
    if parsed["candidateId"] != bound["candidateId"]:
        return False
    if parsed["snapshotId"] and parsed["snapshotId"] != bound["snapshotId"]:
        return False
    if parsed["auditProfileId"] and parsed["auditProfileId"] != bound["auditProfileId"]:
        return False
    return True


def apply_approval(approval: Any, candidate: Mapping[str, Any]) -> dict:
    """Return the bound candidate if the approval matches, otherwise raise."""
    parsed = parse_approval(approval)
    bound = bind_candidate(candidate)
    if parsed["candidateId"] != bound["candidateId"]:
        raise ApprovalTransferError(
            "approval for %s does not apply to candidate %s (display id %s)"
            % (parsed["candidateId"], bound["candidateId"], bound["displayId"] or "none")
        )
    if parsed["snapshotId"] and parsed["snapshotId"] != bound["snapshotId"]:
        raise ApprovalTransferError(
            "approval snapshot %s does not match candidate snapshot %s"
            % (parsed["snapshotId"], bound["snapshotId"])
        )
    if parsed["auditProfileId"] and parsed["auditProfileId"] != bound["auditProfileId"]:
        raise ApprovalTransferError(
            "approval profile %s does not match candidate profile %s"
            % (parsed["auditProfileId"], bound["auditProfileId"])
        )
    return bound


def promote_approved(candidates: Sequence[Mapping[str, Any]], approvals: Sequence[Any]) -> list:
    """Promote only candidates whose content-bound approval matches.

    This is the T15 contract the live report_api._promote_candidates path
    calls when deciding whether an approval may promote a candidate.
    """
    parsed = []
    for item in approvals or []:
        try:
            parsed.append(parse_approval(item))
        except UnboundApprovalError:
            # Unbound labels are ignored, never used as a transfer key.
            continue
    promoted = []
    seen = set()
    for candidate in candidates or []:
        bound = bind_candidate(candidate)
        if bound["candidateId"] in seen:
            continue
        if any(approval_matches(item, bound) for item in parsed):
            promoted.append(bound)
            seen.add(bound["candidateId"])
    return promoted


class WriterResult:
    """Outcome of a writer attempt against a frozen decision."""

    def __init__(
        self,
        *,
        rejected: bool,
        accepted: bool = False,
        reason: str = "",
        dispute_id: Optional[str] = None,
        decision_version: str = "",
        previous_decision_version: str = "",
        conclusion_unchanged: bool = True,
        status: str = "",
    ):
        self.rejected = rejected
        self.accepted = accepted
        self.reason = reason
        self.dispute_id = dispute_id
        self.decision_version = decision_version
        self.previous_decision_version = previous_decision_version
        self.conclusion_unchanged = conclusion_unchanged
        self.status = status or ("rejected" if rejected else ("accepted" if accepted else "recorded"))

    def as_dict(self) -> dict:
        return {
            "rejected": self.rejected,
            "accepted": self.accepted,
            "reason": self.reason,
            "disputeId": self.dispute_id,
            "decisionVersion": self.decision_version,
            "previousDecisionVersion": self.previous_decision_version,
            "conclusionUnchanged": self.conclusion_unchanged,
            "status": self.status,
        }


class DecisionSet:
    """Unified receiver for rule results, AI candidates, and human rulings."""

    def __init__(
        self,
        snapshot_id: str = "",
        audit_profile_id: str = "",
        rules_version: str = "",
        facts_digest: str = "",
        knowledge_version: str = "",
        registered_exceptions: Optional[Iterable[Any]] = None,
    ):
        self.snapshotId = str(snapshot_id or "")
        self.auditProfileId = str(audit_profile_id or "")
        self.decisions: list[dict] = []
        self.disputes: list[dict] = []
        self.permittedEdits: list[dict] = []
        self.pendingMaterials: list[dict] = []
        self._rules_version = str(rules_version or "")
        self._knowledge_version = str(knowledge_version or "")
        self._facts_digest = str(facts_digest or "")
        self._registered_exceptions = [_clean_exception(item) for item in (registered_exceptions or [])]
        self._candidates: dict[str, dict] = {}
        self._rejected_revisions: list[dict] = []
        self._frozen = False

    # -- identity ----------------------------------------------------------

    @property
    def decisionVersion(self) -> str:
        return self._compute_version()

    def _compute_version(self) -> str:
        payload = {
            "auditProfileId": self.auditProfileId,
            "decisions": [
                {
                    "candidateId": row.get("candidateId") or "",
                    "conclusion": row.get("conclusion") or "",
                    "decisionId": row.get("decisionId") or "",
                    "itemId": row.get("itemId") or "",
                    "rulings": [
                        {k: r.get(k) for k in ("candidateId", "conclusion", "reason", "operator")}
                        for r in (row.get("rulings") or [])
                    ],
                    "status": row.get("status") or "",
                }
                for row in sorted(self.decisions, key=lambda item: str(item.get("decisionId") or item.get("itemId") or ""))
            ],
            "disputes": [
                {
                    "decisionId": row.get("decisionId") or "",
                    "disputeId": row.get("disputeId") or "",
                    "exceptionRefs": row.get("exceptionRefs") or [],
                    "kind": row.get("kind") or "",
                    "reason": row.get("reason") or "",
                    "source": row.get("source") or "",
                    "status": row.get("status") or "",
                }
                for row in sorted(self.disputes, key=lambda item: str(item.get("disputeId") or ""))
            ],
            "factsDigest": self._facts_digest,
            "knowledgeVersion": self._knowledge_version,
            "pendingMaterials": [
                {k: row.get(k) for k in ("decisionId", "what", "fromWhom", "affects")}
                for row in sorted(self.pendingMaterials, key=lambda item: str(item.get("decisionId") or item.get("what") or ""))
            ],
            "rulesVersion": self._rules_version,
            "snapshotId": self.snapshotId,
        }
        return _short(payload, "dv-")

    def facts_digest(self) -> str:
        return self._facts_digest

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "decisionVersion": self.decisionVersion,
            "snapshotId": self.snapshotId,
            "auditProfileId": self.auditProfileId,
            "decisions": deepcopy(self.decisions),
            "disputes": deepcopy(self.disputes),
            "permittedEdits": deepcopy(self.permittedEdits),
            "pendingMaterials": deepcopy(self.pendingMaterials),
            "rulesVersion": self._rules_version,
            "knowledgeVersion": self._knowledge_version,
            "factsDigest": self._facts_digest,
        }

    # -- candidates --------------------------------------------------------

    def add_candidate(self, raw: Mapping[str, Any]) -> dict:
        """Register an unconfirmed candidate. Does not change decisionVersion."""
        payload = dict(raw)
        if not payload.get("snapshotId"):
            payload["snapshotId"] = self.snapshotId
        if not payload.get("auditProfileId"):
            payload["auditProfileId"] = self.auditProfileId
        if self._knowledge_version and not (payload.get("knowledgeVersion") or (payload.get("versions") or {}).get("knowledgeVersion") if isinstance(payload.get("versions"), Mapping) else None):
            payload["knowledgeVersion"] = self._knowledge_version
        if self._rules_version and not (payload.get("rulesVersion") or (payload.get("versions") or {}).get("rulesVersion") if isinstance(payload.get("versions"), Mapping) else None):
            payload["rulesVersion"] = self._rules_version
        bound = bind_candidate(payload)
        self._candidates[bound["candidateId"]] = bound
        return deepcopy(bound)

    def get_candidate(self, candidate: Union[str, Mapping[str, Any]]) -> dict:
        if isinstance(candidate, Mapping):
            bound_id = candidate.get("candidateId") or candidate_id(candidate)
        else:
            bound_id = str(candidate)
        row = self._candidates.get(bound_id)
        if row is None:
            raise UnknownDecisionError("unknown candidate %s" % bound_id)
        return deepcopy(row)

    # -- human rulings -----------------------------------------------------

    def record_ruling(
        self,
        candidate: Union[str, Mapping[str, Any]],
        ruling: Mapping[str, Any],
        *,
        expected_decision_revision: Optional[str] = None,
        operator: str = "",
    ) -> dict:
        """Record a confirmation bound to one candidate. Facts are not mutated."""
        if not isinstance(ruling, Mapping):
            raise RulingValidationError("ruling must be a mapping bound to a candidate, not free text")
        self._reject_fact_patch(ruling)
        if expected_decision_revision and expected_decision_revision != self.decisionVersion:
            raise StaleRevisionError(
                "expectedDecisionRevision %s does not match current %s"
                % (expected_decision_revision, self.decisionVersion)
            )

        if isinstance(candidate, Mapping):
            bound = self.add_candidate(candidate)
        else:
            bound = self.get_candidate(str(candidate))

        cited = str(ruling.get("candidateId") or ruling.get("candidate_id") or "").strip()
        if cited and cited != bound["candidateId"]:
            raise RulingValidationError(
                "ruling cites candidate %s but is being recorded on %s" % (cited, bound["candidateId"])
            )
        display = str(ruling.get("id") or ruling.get("displayId") or ruling.get("roundId") or "")
        if display and not cited and not str(ruling.get("candidateId") or ""):
            # A ruling that only names AI-1 is not a valid confirmation.
            if not display.startswith("cand-"):
                raise UnboundApprovalError(
                    "human confirmation must cite candidateId, not round label %r" % (display,)
                )

        snapshot_pin = str(ruling.get("snapshotId") or ruling.get("snapshot_id") or "")
        if snapshot_pin and snapshot_pin != bound["snapshotId"]:
            raise RulingValidationError(
                "ruling snapshot %s does not match candidate snapshot %s"
                % (snapshot_pin, bound["snapshotId"])
            )
        profile_pin = str(ruling.get("auditProfileId") or ruling.get("audit_profile_id") or "")
        if profile_pin and profile_pin != bound["auditProfileId"]:
            raise RulingValidationError(
                "ruling profile %s does not match candidate profile %s"
                % (profile_pin, bound["auditProfileId"])
            )
        self._check_version_pins(ruling, bound)

        conclusion = str(
            ruling.get("conclusion")
            or ruling.get("outcome")
            or bound.get("proposedOutcome")
            or ""
        ).strip()
        if not conclusion:
            raise RulingValidationError("ruling must include a conclusion for the cited candidate")

        record = {
            "candidateId": bound["candidateId"],
            "conclusion": conclusion,
            "operator": str(ruling.get("operator") or operator or ""),
            "reason": str(ruling.get("reason") or ""),
            "snapshotId": bound["snapshotId"],
            "auditProfileId": bound["auditProfileId"],
            "clauseVersions": _clause_version_pins(bound),
            "evidenceVersions": _evidence_version_pins(bound),
        }

        item_id = str(ruling.get("itemId") or bound.get("checkpointId") or bound["candidateId"])
        decision = self._find(item_id) or self._find(bound["candidateId"])
        if decision is None:
            decision = self._new_decision(
                item_id=item_id,
                conclusion=conclusion,
                candidate_id=bound["candidateId"],
                checkpoint_id=bound.get("checkpointId") or "",
            )
            self.decisions.append(decision)
        else:
            # A new ruling may confirm or change the slot only as a recorded
            # ruling; it does not silently patch facts.
            decision["conclusion"] = conclusion
            decision["candidateId"] = bound["candidateId"]
            decision["status"] = "confirmed"

        if not any(
            existing.get("candidateId") == record["candidateId"]
            and existing.get("conclusion") == record["conclusion"]
            and existing.get("operator") == record["operator"]
            and existing.get("reason") == record["reason"]
            for existing in decision["rulings"]
        ):
            decision["rulings"].append(record)

        decision["status"] = "confirmed"
        self._sync_materials_and_edits(decision, bound if bound.get("missingMaterial") else None)
        return deepcopy(record)

    def confirm(self, candidate: Union[str, Mapping[str, Any]], conclusion: str, **pins: Any) -> dict:
        ruling = {"conclusion": conclusion, "candidateId": ""}
        ruling.update(pins)
        if isinstance(candidate, Mapping):
            ruling["candidateId"] = candidate_id({
                **dict(candidate),
                "snapshotId": candidate.get("snapshotId") or self.snapshotId,
                "auditProfileId": candidate.get("auditProfileId") or self.auditProfileId,
            })
        else:
            ruling["candidateId"] = str(candidate)
        return self.record_ruling(candidate, ruling)

    def _check_version_pins(self, ruling: Mapping[str, Any], bound: Mapping[str, Any]) -> None:
        clause_pins = ruling.get("clauseVersions") or ruling.get("clause_versions")
        if clause_pins:
            expected = {row["clauseId"]: row.get("textHash") or row.get("version") or "" for row in bound["clauseRefs"]}
            for pin in _as_list(clause_pins):
                if isinstance(pin, Mapping):
                    clause_id = str(pin.get("clauseId") or pin.get("clause") or "")
                    pin_hash = str(pin.get("textHash") or pin.get("version") or "")
                    if clause_id in expected and pin_hash and pin_hash != expected[clause_id]:
                        raise RulingValidationError(
                            "ruling clause version for %s does not match the candidate" % clause_id
                        )
                elif str(pin) and str(pin) not in expected.values() and str(pin) not in expected:
                    raise RulingValidationError("ruling clause version pin %s does not match the candidate" % pin)
        evidence_pins = ruling.get("evidenceVersions") or ruling.get("evidence_versions")
        if evidence_pins:
            expected = {row["evidenceId"]: row.get("version") or "" for row in bound["evidence"]}
            for pin in _as_list(evidence_pins):
                if isinstance(pin, Mapping):
                    evidence_id = str(pin.get("evidenceId") or pin.get("id") or "")
                    pin_hash = str(pin.get("version") or pin.get("textHash") or "")
                    if evidence_id in expected and pin_hash and pin_hash != expected[evidence_id]:
                        raise RulingValidationError(
                            "ruling evidence version for %s does not match the candidate" % evidence_id
                        )

    @staticmethod
    def _reject_fact_patch(ruling: Mapping[str, Any]) -> None:
        present = _FACT_PATCH_KEYS.intersection(ruling.keys())
        if present:
            raise RulingValidationError(
                "human confirmation cannot mutate facts; refused keys: %s" % (", ".join(sorted(present)),)
            )

    # -- lock / add (T17 hook compatibility) -------------------------------

    def lock(self, item_id: str, conclusion: Optional[str] = None, **kwargs: Any) -> dict:
        """Freeze a conclusion for an item or candidate.

        Compatible with the P0 T17 hook: lock("item-1", conclusion="须整改").
        """
        if conclusion is None:
            conclusion = kwargs.get("conclusion")
        if not conclusion:
            raise DecisionError("lock() requires a conclusion")
        candidate_ref = kwargs.get("candidate") or kwargs.get("candidate_id") or kwargs.get("candidateId")
        bound = None
        if isinstance(candidate_ref, Mapping):
            bound = self.add_candidate(candidate_ref)
            candidate_ref = bound["candidateId"]
        existing = self._find(item_id) or (self._find(candidate_ref) if candidate_ref else None)
        if existing:
            if existing.get("conclusion") == conclusion:
                return deepcopy(existing)
            raise RulingValidationError(
                "locked decision %s already has conclusion %s; file a dispute instead of overwriting"
                % (existing.get("decisionId"), existing.get("conclusion"))
            )
        decision = self._new_decision(
            item_id=str(item_id),
            conclusion=str(conclusion),
            candidate_id=str(candidate_ref or ""),
            checkpoint_id=str(kwargs.get("checkpointId") or kwargs.get("checkpoint_id") or ""),
        )
        if kwargs.get("operator") or kwargs.get("reason"):
            decision["rulings"].append({
                "candidateId": decision["candidateId"],
                "conclusion": decision["conclusion"],
                "operator": str(kwargs.get("operator") or ""),
                "reason": str(kwargs.get("reason") or ""),
                "snapshotId": self.snapshotId,
                "auditProfileId": self.auditProfileId,
            })
        self.decisions.append(decision)
        self._sync_materials_and_edits(decision, bound)
        return deepcopy(decision)

    def add(self, item: Mapping[str, Any]) -> dict:
        """Add or lock a decision from a mapping. Compatible with the P0 T17 hook."""
        if not isinstance(item, Mapping):
            raise DecisionError("add() expects a decision mapping")
        item_id = str(item.get("id") or item.get("itemId") or item.get("decisionId") or "")
        if not item_id:
            raise DecisionError("add() requires an id")
        return self.lock(
            item_id,
            conclusion=item.get("conclusion") or item.get("outcome"),
            candidate=item.get("candidate"),
            candidate_id=item.get("candidateId") or item.get("candidate_id"),
            operator=item.get("operator"),
            reason=item.get("reason"),
            checkpointId=item.get("checkpointId"),
        )

    def freeze(self) -> "DecisionSet":
        """Mark the set frozen for writer consumption. Rulings already recorded stay."""
        self._frozen = True
        return self

    # -- writer path (T17) -------------------------------------------------

    def reject_writer_revision(
        self,
        item_id: str,
        text: str = "",
        new_evidence: bool = False,
        **kwargs: Any,
    ) -> bool:
        """P0 T17 hook: return True when an overwrite without new evidence is refused."""
        result = self.apply_writer(
            item_id,
            text=text,
            new_evidence=new_evidence,
            proposed_conclusion=kwargs.get("proposed_conclusion") or kwargs.get("conclusion"),
            exception_refs=kwargs.get("exception_refs") or kwargs.get("exceptionRefs"),
        )
        return bool(result.rejected)

    def apply_writer(
        self,
        item_id: str,
        text: str = "",
        new_evidence: bool = False,
        proposed_conclusion: Optional[str] = None,
        exception_refs: Optional[Iterable[Any]] = None,
        **kwargs: Any,
    ) -> WriterResult:
        """Apply a writer draft to a frozen decision.

        - Changing the conclusion with no new evidence is rejected.
        - Even when new evidence is offered, the conclusion is not silently
          overwritten; that path must come back as a dispute.
        - A registered exception becomes a dispute and a new decisionVersion.
        """
        decision = self._find(item_id)
        if decision is None:
            raise UnknownDecisionError("no frozen decision for %s" % item_id)
        previous = self.decisionVersion
        current = decision.get("conclusion") or ""
        proposed = proposed_conclusion or infer_conclusion_from_text(text) or kwargs.get("conclusion")
        refs = [_clean_exception(item) for item in _as_list(exception_refs)]

        if refs:
            unknown = [ref for ref in refs if not self._is_registered_exception(ref)]
            if unknown:
                result = WriterResult(
                    rejected=True,
                    accepted=False,
                    reason="unregistered_exception",
                    decision_version=previous,
                    previous_decision_version=previous,
                    conclusion_unchanged=True,
                )
                self._remember_rejection(decision, text, result)
                return result
            dispute = self._add_dispute(
                decision,
                kind="writer_exception",
                source="writer",
                reason=str(kwargs.get("reason") or text or "writer cited a registered exception"),
                exception_refs=refs,
            )
            decision["status"] = "disputed"
            return WriterResult(
                rejected=False,
                accepted=False,
                reason="exception_dispute",
                dispute_id=dispute["disputeId"],
                decision_version=self.decisionVersion,
                previous_decision_version=previous,
                conclusion_unchanged=decision.get("conclusion") == current,
                status="disputed",
            )

        if proposed and proposed != current:
            # New evidence still cannot silently overwrite; it must come
            # back through a dispute / ruling. Without new evidence the
            # attempt is simply rejected.
            if not new_evidence:
                result = WriterResult(
                    rejected=True,
                    accepted=False,
                    reason="overwrite_without_new_evidence",
                    decision_version=previous,
                    previous_decision_version=previous,
                    conclusion_unchanged=True,
                )
                self._remember_rejection(decision, text, result)
                return result
            dispute = self._add_dispute(
                decision,
                kind="writer_new_evidence",
                source="writer",
                reason=str(kwargs.get("reason") or text or "writer offered new evidence"),
                exception_refs=[],
            )
            decision["status"] = "disputed"
            return WriterResult(
                rejected=False,
                accepted=False,
                reason="new_evidence_dispute",
                dispute_id=dispute["disputeId"],
                decision_version=self.decisionVersion,
                previous_decision_version=previous,
                conclusion_unchanged=True,
                status="disputed",
            )

        return WriterResult(
            rejected=False,
            accepted=False,
            reason="no_conclusion_change",
            decision_version=previous,
            previous_decision_version=previous,
            conclusion_unchanged=True,
        )

    def _remember_rejection(self, decision: Mapping[str, Any], text: str, result: WriterResult) -> None:
        self._rejected_revisions.append({
            "decisionId": decision.get("decisionId"),
            "itemId": decision.get("itemId"),
            "attemptedText": text,
            "reason": result.reason,
            "decisionVersion": result.decision_version,
        })

    def rejected_revisions(self) -> list:
        return deepcopy(self._rejected_revisions)

    # -- disputes / materials ----------------------------------------------

    def _add_dispute(
        self,
        decision: Mapping[str, Any],
        *,
        kind: str,
        source: str,
        reason: str,
        exception_refs: Sequence[Mapping[str, Any]],
    ) -> dict:
        payload = {
            "kind": kind,
            "source": source,
            "reason": reason,
            "decisionId": decision.get("decisionId") or "",
            "itemId": decision.get("itemId") or "",
            "candidateId": decision.get("candidateId") or "",
            "exceptionRefs": list(exception_refs),
            "priorConclusion": decision.get("conclusion") or "",
        }
        dispute = {
            **payload,
            "disputeId": _short(payload, "dsp-"),
            "status": "open",
        }
        # Idempotent: the same writer exception against the same decision
        # does not accumulate duplicate disputes.
        for existing in self.disputes:
            if existing.get("disputeId") == dispute["disputeId"]:
                return deepcopy(existing)
        self.disputes.append(dispute)
        return deepcopy(dispute)

    def _is_registered_exception(self, ref: Mapping[str, Any]) -> bool:
        if not self._registered_exceptions:
            return False
        for item in self._registered_exceptions:
            if _exceptions_match(item, ref):
                return True
        return False

    def _sync_materials_and_edits(self, decision: dict, candidate: Optional[Mapping[str, Any]]) -> None:
        missing = (candidate or {}).get("missingMaterial") if candidate else None
        if missing and (missing.get("what") or missing.get("fromWhom") or missing.get("affects")):
            material = {
                "decisionId": decision["decisionId"],
                "what": str(missing.get("what") or ""),
                "fromWhom": str(missing.get("fromWhom") or ""),
                "affects": str(missing.get("affects") or ""),
            }
            if not any(
                row.get("decisionId") == material["decisionId"] and row.get("what") == material["what"]
                for row in self.pendingMaterials
            ):
                self.pendingMaterials.append(material)
        if decision.get("conclusion") == FAIL_CONCLUSION:
            edit = {
                "decisionId": decision["decisionId"],
                "actionId": _short({"decisionId": decision["decisionId"], "action": "remediate"}, "act-"),
                "decisionVersion": self.decisionVersion,
                "kind": "remediate",
                "allowed": True,
            }
            if not any(row.get("decisionId") == edit["decisionId"] and row.get("kind") == "remediate" for row in self.permittedEdits):
                self.permittedEdits.append(edit)

    def _new_decision(
        self,
        *,
        item_id: str,
        conclusion: str,
        candidate_id: str = "",
        checkpoint_id: str = "",
    ) -> dict:
        decision_id = _short(
            {
                "auditProfileId": self.auditProfileId,
                "factsDigest": self._facts_digest,
                "itemId": item_id,
                "rulesVersion": self._rules_version,
                "snapshotId": self.snapshotId,
            },
            "dec-",
        )
        return {
            "decisionId": decision_id,
            "itemId": item_id,
            "candidateId": candidate_id,
            "checkpointId": checkpoint_id,
            "conclusion": conclusion,
            "status": "confirmed",
            "rulings": [],
            "snapshotId": self.snapshotId,
            "auditProfileId": self.auditProfileId,
        }

    def _find(self, item_id: str) -> Optional[dict]:
        token = str(item_id or "")
        if not token:
            return None
        for row in self.decisions:
            if token in {
                str(row.get("itemId") or ""),
                str(row.get("decisionId") or ""),
                str(row.get("candidateId") or ""),
                str(row.get("checkpointId") or ""),
            }:
                return row
        return None


def _clean_exception(item: Any) -> dict:
    if isinstance(item, Mapping):
        return {
            "exceptionId": str(item.get("exceptionId") or item.get("id") or ""),
            "clauseId": str(item.get("clauseId") or item.get("clause") or ""),
            "document": str(item.get("document") or item.get("documentId") or ""),
            "textHash": str(item.get("textHash") or item.get("digest") or ""),
            "summary": str(item.get("summary") or ""),
        }
    return {
        "exceptionId": str(item),
        "clauseId": "",
        "document": "",
        "textHash": "",
        "summary": "",
    }


def _exceptions_match(registered: Mapping[str, Any], offered: Mapping[str, Any]) -> bool:
    if registered.get("exceptionId") and registered.get("exceptionId") == offered.get("exceptionId"):
        if registered.get("textHash") and offered.get("textHash") and registered["textHash"] != offered["textHash"]:
            return False
        return True
    if (
        registered.get("clauseId")
        and registered.get("clauseId") == offered.get("clauseId")
        and (not registered.get("document") or registered.get("document") == offered.get("document"))
    ):
        if registered.get("textHash") and offered.get("textHash") and registered["textHash"] != offered["textHash"]:
            return False
        return True
    return False


def _clause_version_pins(bound: Mapping[str, Any]) -> list:
    return [
        {"clauseId": row.get("clauseId"), "textHash": row.get("textHash"), "version": row.get("version")}
        for row in bound.get("clauseRefs") or []
    ]


def _evidence_version_pins(bound: Mapping[str, Any]) -> list:
    return [
        {"evidenceId": row.get("evidenceId"), "version": row.get("version")}
        for row in bound.get("evidence") or []
    ]



def conclusion_for_item(item: Mapping[str, Any]) -> Optional[str]:
    """Map a report/audit item to a frozen conclusion.

    Disabled, uncovered, blocked, failed, or silent checkpoints are not
    treated as passes. Only clearly actionable or pending outcomes are locked.
    """
    if not isinstance(item, Mapping):
        return None
    explicit = str(item.get("conclusion") or "").strip()
    if explicit in {FAIL_CONCLUSION, PASS_CONCLUSION, PENDING_CONCLUSION, "证据不足", "不适用"}:
        return explicit
    outcome = str(item.get("outcome") or item.get("group") or "").strip()
    severity = str(item.get("severity") or "")
    if outcome == "issue" or severity in {"critical", "redline", "draft-hold"}:
        return FAIL_CONCLUSION
    if outcome == "internal" or item.get("group") == "internal":
        return FAIL_CONCLUSION
    if outcome in {"review", "gap"}:
        return PENDING_CONCLUSION
    return None


def seed_from_report(decisions: "DecisionSet", report: Optional[Mapping[str, Any]]) -> "DecisionSet":
    """Lock already-final audit items into the set. Does not invent passes."""
    if not isinstance(report, Mapping):
        return decisions
    try:
        from audit_retrieval import (
            is_formal_finding_item, sanitize_review_row, unsound_fail_confirmation,
        )
    except Exception:
        is_formal_finding_item = lambda _row: str(_row.get("id") or _row.get("itemId") or "") in {
            "I01", "I02", "I03", "I04"}
        sanitize_review_row = lambda row, bundle_id=None: dict(row)
        unsound_fail_confirmation = lambda *_a, **_k: False
    for item in list(report.get("items") or []) + list(report.get("lockedJudgments") or []):
        if not isinstance(item, Mapping):
            continue
        original = dict(item)
        item_id = str(item.get("id") or item.get("itemId") or item.get("decisionId") or "")
        if not is_formal_finding_item(original):
            item = sanitize_review_row(original, original.get("bundleId"))
        conclusion = conclusion_for_item(item) or conclusion_for_item(original)
        if (conclusion == FAIL_CONCLUSION and not is_formal_finding_item(original)
                and unsound_fail_confirmation(item, original=original)):
            continue
        if not item_id or not conclusion:
            continue
        if decisions._find(item_id):
            continue
        candidate = item.get("candidate") or item
        kwargs = {}
        if isinstance(candidate, Mapping) and (candidate.get("claim") or candidate.get("assertion") or candidate.get("candidateId")):
            kwargs["candidate"] = candidate
        if item.get("candidateId"):
            kwargs["candidateId"] = item.get("candidateId")
        if item.get("checkpointId"):
            kwargs["checkpointId"] = item.get("checkpointId")
        decisions.lock(item_id, conclusion=conclusion, **kwargs)
    return decisions


def approval_tries_fact_patch(approval: Any) -> bool:
    return isinstance(approval, Mapping) and bool(_FACT_PATCH_KEYS.intersection(approval.keys()))


def enrich_candidate(
    raw: Mapping[str, Any],
    *,
    snapshot_id: str = "",
    audit_profile_id: str = "",
    knowledge_version: str = "",
    rules_version: str = "",
    evidence_version: str = "",
) -> dict:
    """Fill missing version pins without inventing assertion or evidence content.

    Empty pins are left empty so a unit-test candidate without a snapshot
    keeps the same candidateId as its raw form.
    """
    row = dict(raw)
    if snapshot_id and not row.get("snapshotId") and not row.get("snapshot_id"):
        row["snapshotId"] = snapshot_id
    if audit_profile_id and not row.get("auditProfileId") and not row.get("audit_profile_id"):
        row["auditProfileId"] = audit_profile_id
    versions = dict(row.get("versions") or {}) if isinstance(row.get("versions"), Mapping) else {}
    if knowledge_version and not (row.get("knowledgeVersion") or versions.get("knowledgeVersion")):
        row["knowledgeVersion"] = knowledge_version
    if rules_version and not (row.get("rulesVersion") or versions.get("rulesVersion")):
        row["rulesVersion"] = rules_version
    if evidence_version and not (row.get("evidenceVersion") or versions.get("evidenceVersion")):
        row["evidenceVersion"] = evidence_version
    return row


def gate_writer_revisions(
    decisions: "DecisionSet",
    remediations: Optional[Mapping[str, Any]],
) -> tuple[dict, list]:
    """Apply writer drafts to frozen decisions.

    Rejected overwrites and exception/new-evidence disputes are removed from
    the accepted map. A dispute is recorded on the DecisionSet; the conclusion
    is not overwritten. Items with no frozen decision are left unchanged.
    """
    accepted: dict = {}
    rejected: list = []
    for item_id, entry in (remediations or {}).items():
        text = entry.get("text") if isinstance(entry, Mapping) else str(entry or "")
        refs = None
        new_evidence = False
        if isinstance(entry, Mapping):
            refs = entry.get("exceptionRefs") or entry.get("exception_refs")
            new_evidence = bool(entry.get("newEvidence") or entry.get("new_evidence"))
        try:
            result = decisions.apply_writer(
                str(item_id),
                text=text,
                new_evidence=new_evidence,
                exception_refs=refs,
            )
        except UnknownDecisionError:
            accepted[item_id] = entry
            continue
        if result.rejected or result.status == "disputed":
            rejected.append({
                "itemId": item_id,
                "reason": result.reason,
                "disputeId": result.dispute_id,
                "decisionVersion": result.decision_version,
                "status": result.status,
                "conclusionUnchanged": result.conclusion_unchanged,
            })
            continue
        accepted[item_id] = entry
    return accepted, rejected


# -- minimal task hook (not a P5 task database) ----------------------------

_RUNTIME_STORE: dict[str, "DecisionSet"] = {}
_STORE_DIR = Path(__file__).resolve().parent / ".runtime" / "decisions"
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def reset_store() -> None:
    """Drop the in-process map. Tests use this; files are left in place."""
    _RUNTIME_STORE.clear()


def _safe_task_id(task_id: str) -> str:
    token = str(task_id or "").strip()
    if not _TASK_ID_RE.match(token):
        raise DecisionError("invalid task id for DecisionSet persistence")
    return token


def _store_path(task_id: str) -> Path:
    return _STORE_DIR / ("%s.json" % _safe_task_id(task_id))


def DecisionSet_to_persist(self) -> dict:
    payload = self.to_dict()
    payload["candidates"] = [deepcopy(row) for row in self._candidates.values()]
    payload["registeredExceptions"] = deepcopy(self._registered_exceptions)
    payload["rejectedRevisions"] = self.rejected_revisions()
    payload["frozen"] = self._frozen
    return payload


def from_persist(data: Mapping[str, Any], *, registered_exceptions: Optional[Iterable[Any]] = None) -> "DecisionSet":
    """Restore a DecisionSet from to_persist()/to_dict() output."""
    if not isinstance(data, Mapping):
        raise DecisionError("from_persist expects a mapping")
    exceptions = registered_exceptions
    if exceptions is None:
        exceptions = data.get("registeredExceptions") or []
    ds = DecisionSet(
        snapshot_id=str(data.get("snapshotId") or ""),
        audit_profile_id=str(data.get("auditProfileId") or ""),
        rules_version=str(data.get("rulesVersion") or ""),
        facts_digest=str(data.get("factsDigest") or ""),
        knowledge_version=str(data.get("knowledgeVersion") or ""),
        registered_exceptions=exceptions,
    )
    ds.decisions = deepcopy(list(data.get("decisions") or []))
    ds.disputes = deepcopy(list(data.get("disputes") or []))
    ds.permittedEdits = deepcopy(list(data.get("permittedEdits") or []))
    ds.pendingMaterials = deepcopy(list(data.get("pendingMaterials") or []))
    for cand in data.get("candidates") or []:
        if not isinstance(cand, Mapping):
            continue
        bound_id = str(cand.get("candidateId") or candidate_id(cand))
        ds._candidates[bound_id] = deepcopy(dict(cand))
        if not ds._candidates[bound_id].get("candidateId"):
            ds._candidates[bound_id]["candidateId"] = bound_id
    ds._rejected_revisions = deepcopy(list(data.get("rejectedRevisions") or []))
    ds._frozen = bool(data.get("frozen"))
    expected = str(data.get("decisionVersion") or "")
    if expected and expected != ds.decisionVersion:
        raise DecisionError(
            "persisted decisionVersion %s does not match recomputed %s"
            % (expected, ds.decisionVersion)
        )
    return ds


def get_or_create_for_task(
    task_id: str,
    *,
    snapshot_id: str = "",
    audit_profile_id: str = "",
    rules_version: str = "",
    facts_digest: str = "",
    knowledge_version: str = "",
    registered_exceptions: Optional[Iterable[Any]] = None,
    persist: bool = True,
) -> "DecisionSet":
    """Return the in-process DecisionSet for a task, loading a file hook if needed.

    This is only a same-process / best-effort file hook. It is not crash-safe
    same-taskId resume, and it is not the P5 task database.
    """
    token = str(task_id or "").strip()
    if token:
        existing = _RUNTIME_STORE.get(token)
        if existing is not None:
            return existing
        if persist:
            loaded = _load_for_task(token)
            if loaded is not None:
                _RUNTIME_STORE[token] = loaded
                return loaded
    ds = DecisionSet(
        snapshot_id=snapshot_id,
        audit_profile_id=audit_profile_id,
        rules_version=rules_version,
        facts_digest=facts_digest,
        knowledge_version=knowledge_version,
        registered_exceptions=registered_exceptions,
    )
    if token:
        _RUNTIME_STORE[token] = ds
    return ds


def persist_for_task(task_id: str, decisions: "DecisionSet") -> Optional[Path]:
    """Remember the set in process and write a JSON snapshot. Best-effort only."""
    token = str(task_id or "").strip()
    if not token:
        return None
    _RUNTIME_STORE[token] = decisions
    try:
        path = _store_path(token)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = decisions.to_persist()
        payload["taskId"] = token
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
        return path
    except DecisionError:
        raise
    except OSError:
        return None


def _load_for_task(task_id: str) -> Optional["DecisionSet"]:
    try:
        path = _store_path(task_id)
    except DecisionError:
        return None
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return from_persist(data)


# Bind to_persist on the class without re-indenting the class body.
DecisionSet.to_persist = DecisionSet_to_persist
DecisionSet.from_persist = staticmethod(from_persist)


__all__ = [
    "SCHEMA",
    "ApprovalTransferError",
    "DecisionError",
    "DecisionSet",
    "RulingValidationError",
    "StaleRevisionError",
    "UnboundApprovalError",
    "UnknownDecisionError",
    "WriterOverwriteError",
    "WriterResult",
    "apply_approval",
    "approval_matches",
    "bind_candidate",
    "candidate_id",
    "display_id",
    "enrich_candidate",
    "from_persist",
    "gate_writer_revisions",
    "get_or_create_for_task",
    "infer_conclusion_from_text",
    "normalize_candidate",
    "parse_approval",
    "persist_for_task",
    "promote_approved",
    "reset_store",
    "same_issue",
    "seed_from_report",
]
