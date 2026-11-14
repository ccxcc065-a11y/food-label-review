"""Validate that a cited clause actually belongs to the named instrument.

The published clause index can mis-slice a citation inside another article
(for example 《食品标识监督管理办法》第三十九条 quoting 《食品安全法》第一百二十五条)
and then look up as if the quoted article were a 办法 article. This module
does not republish or mutate the knowledge pack; it only refuses a wrong
instrument at use time.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Optional

SAMR100_MAX_ARTICLE = 54
SAMR100_DOC = "samr100"
FOOD_SAFETY_LAW_DOC = "food-safety-law"
GB7718_DOCS = {"gb7718-2025", "gb7718", "doc-4b502442884e"}
STANDIN_CLAUSES = {"4.14"}  # referral / unimplemented; must not substitute for a tighter basis
# Word/PDF pair of GB 7718-2025 (the docx uses a hash id). Q&A is a different file.
_GB7718_NAME = "GB 7718-2025"

# Reason/claim prose that pins article 125 on 办法, or uses 4.14 as a legal basis.
_SAMR125_DIRECT = re.compile(
    r"(《食品标识监督管理办法》|本办法|办法)\s*(第\s*125\s*条|第一百二十五条)"
)
_ARTICLE_125 = re.compile(r"第\s*125\s*条|第一百二十五条")
_BASIS_414 = re.compile(
    r"(?:GB\s*7718(?:-2025)?\s*)?4\.14(?:要求[^，；。]{0,24})?(?:\s*(?:及|和|与|、))?"
)

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_ARTICLE_RE = re.compile(r"第([零〇一二两三四五六七八九十百千]+)条")

Lookup = Callable[[str, str, Optional[str]], Optional[Mapping[str, Any]]]


def chinese_int(text: str) -> Optional[int]:
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if any(ch not in _CN_DIGITS and ch not in {"十", "百", "千"} for ch in raw):
        return None
    total = 0
    current = 0
    for ch in raw:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch == "十":
            total += (current or 1) * 10
            current = 0
        elif ch == "百":
            total += (current or 1) * 100
            current = 0
        elif ch == "千":
            total += (current or 1) * 1000
            current = 0
        else:
            return None
    return total + current


def article_number(clause: str) -> Optional[int]:
    match = _ARTICLE_RE.search(str(clause or ""))
    if not match:
        return None
    return chinese_int(match.group(1))


def clause_belongs_to_instrument(document_id: str, number: str,
                                 record: Optional[Mapping[str, Any]] = None) -> bool:
    """False when the clause is not an article of the named instrument."""
    document_id = str(document_id or "")
    number = str(number or "").strip()
    if not document_id or not number:
        return False
    if document_id == SAMR100_DOC:
        n = article_number(number)
        if n is not None and n > SAMR100_MAX_ARTICLE:
            return False
        # Even if the index stored a mis-sliced "第一百二十五条" row under samr100.
        if "第一百二十五条" in number and "第三十九条" not in number:
            return False
    return True


def is_gb7718_document(document_id: str, record: Optional[Mapping[str, Any]] = None) -> bool:
    """True for the 2025 standard itself (PDF or Word), not for the Q&A file."""
    key = str(document_id or "")
    if key in GB7718_DOCS or key == "gb7718-2025":
        return True
    name = ""
    if isinstance(record, Mapping):
        name = str(record.get("name") or record.get("standardName") or "")
    if not name and key:
        try:
            import knowledge
            meta = knowledge.document_metadata(key) or {}
            name = str(meta.get("name") or "")
        except Exception:
            name = ""
    return name.strip() == _GB7718_NAME


def is_unimplemented_standin(document_id: str, number: str,
                             record: Optional[Mapping[str, Any]] = None) -> bool:
    return str(number or "").strip() in STANDIN_CLAUSES and is_gb7718_document(document_id, record)


def is_misattributed_samr125(document_id: str, number: str) -> bool:
    return str(document_id or "") == SAMR100_DOC and "第一百二十五条" in str(number or "")


def citation_label(valid_refs: list, dropped: list) -> str:
    if any(row.get("reason") == "wrong-instrument" for row in dropped):
        return "引用归属未对上，须人工复核。办法无第一百二十五条；漏字等瑕疵见办法第三十九条，认定依据为《食品安全法》第一百二十五条。"
    if any(row.get("reason") == "4.14-standin" for row in dropped):
        return "4.14 为转致性未实现条款，不得作为本项依据；其余引用待人工复核。"
    if any(str((row.get("clause") or "")).strip() in STANDIN_CLAUSES for row in valid_refs):
        return "4.14 为转致性未实现条款，不得作为本项依据；其余引用待人工复核。"
    if valid_refs and not dropped:
        return "引用与归属可对上，语义待人工复核"
    if not valid_refs:
        return "无有效条款引用，待人工复核"
    return "引用待人工复核"


def attributes_article_125_to_measures(text: str) -> bool:
    """True when free text still pins 《食品安全法》第 125 条 on the 办法."""
    raw = str(text or "")
    if not raw or not _ARTICLE_125.search(raw):
        return False
    if _SAMR125_DIRECT.search(raw):
        return True
    for match in _ARTICLE_125.finditer(raw):
        window = raw[max(0, match.start() - 48):match.start()]
        if "食品安全法" in window:
            continue
        if re.search(r"办法|食品标识监督管理", window):
            return True
    return False


def uses_414_as_basis(text: str) -> bool:
    """True when the prose still treats GB 7718 4.14 as a legal basis."""
    raw = str(text or "")
    if "4.14" not in raw:
        return False
    # A system label that *rejects* 4.14 is not a basis claim.
    if "不得作为本项依据" in raw and raw.count("4.14") == 1:
        return False
    return True


def sanitize_citation_prose(text: str) -> str:
    """Rewrite reason/claim/fact so 125 is not pinned on 办法 and 4.14 is not a basis.

    Does not invent new findings. Formal I02/I03/I04/I01 wording is left alone
    when it does not contain these error classes.
    """
    raw = str(text or "")
    if not raw:
        return raw
    cleaned = _BASIS_414.sub("", raw)
    cleaned = re.sub(r"依\s*《", "依《", cleaned)
    cleaned = re.sub(r"依\s+《", "依《", cleaned)
    cleaned = re.sub(r"(?:^|[；。])\s*[及和与、]\s*", lambda m: m.group(0)[0] if m.group(0)[0] in "；。" else "", cleaned)
    cleaned = re.sub(r"^[及和与、，]\s*", "", cleaned)
    cleaned = _SAMR125_DIRECT.sub("《食品安全法》第一百二十五条（漏字等瑕疵见办法第三十九条）", cleaned)

    def _replace_bare_125(match: re.Match) -> str:
        window = cleaned[max(0, match.start() - 48):match.start()]
        if "食品安全法" in window:
            return match.group(0)
        if re.search(r"办法|食品标识监督管理", window):
            return "《食品安全法》第一百二十五条（漏字等瑕疵见办法第三十九条）"
        return match.group(0)

    cleaned = _ARTICLE_125.sub(_replace_bare_125, cleaned)
    cleaned = re.sub(r"[ 	]{2,}", " ", cleaned)
    cleaned = re.sub(r"，{2,}", "，", cleaned)
    cleaned = re.sub(r"；{2,}", "；", cleaned)
    cleaned = re.sub(r"[，、]\s*([；。])", r"\1", cleaned)
    cleaned = re.sub(r"^[；，、]\s*", "", cleaned)
    return cleaned.strip()


def is_job_ticket_net_content_conflict(row: Mapping[str, Any] | str) -> bool:
    """True when 315 g (prepress job-ticket header) is treated as pack net content vs 328 g.

    Formal section 08 correctly says 315 g is the job-ticket header and 328 g is
    the pack net content. That correct framing is not flagged.
    """
    if isinstance(row, Mapping):
        blob = " ".join(str(row.get(key) or "") for key in
                        ("claim", "fact", "reason", "title", "detail", "conclusion"))
    else:
        blob = str(row or "")
    if "315" not in blob or "328" not in blob:
        return False
    if re.search(r"制作表头|印前|不是销售包装净含量|不是销售包装", blob):
        return False
    return bool(re.search(
        r"矛盾|冲突|不一致|不唯一|虚假|误导|两个净含量|前板",
        blob,
    ))


def refine_clause_refs(raw_refs: list, lookup: Lookup, bundle_id: Optional[str] = None) -> dict:
    """Filter and, when needed, correct agent clause refs.

    lookup(document_id, clause, bundle_id) should return a published record or None.
    """
    valid: list[dict] = []
    dropped: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(document: str, clause: str, record: Mapping[str, Any], edition: str = "") -> None:
        key = (document, clause)
        if key in seen:
            return
        seen.add(key)
        ed = edition or str(record.get("documentVersion") or record.get("edition") or "2025")
        valid.append({
            "documentId": record.get("documentId") or document,
            "edition": ed,
            "clause": record.get("clause") or clause,
            "textHash": record.get("textHash"),
        })

    raw_list = [ref for ref in (raw_refs or []) if isinstance(ref, dict)]
    need_samr_correction = False
    for ref in raw_list:
        document = str(ref.get("documentId") or "")
        clause = str(ref.get("clause") or "").strip()
        if not document or not clause:
            dropped.append({"documentId": document, "clause": clause, "reason": "incomplete"})
            continue
        if is_unimplemented_standin(document, clause):
            dropped.append({"documentId": document, "clause": clause, "reason": "4.14-standin"})
            continue
        if is_misattributed_samr125(document, clause) or not clause_belongs_to_instrument(document, clause):
            dropped.append({"documentId": document, "clause": clause, "reason": "wrong-instrument"})
            if is_misattributed_samr125(document, clause):
                need_samr_correction = True
            continue
        record = lookup(document, clause, bundle_id)
        if not record or not record.get("text") or (record.get("conflict") and record.get("text") is None):
            dropped.append({"documentId": document, "clause": clause, "reason": "unresolved"})
            continue
        if is_unimplemented_standin(document, clause, record):
            dropped.append({"documentId": document, "clause": clause, "reason": "4.14-standin"})
            continue
        if not clause_belongs_to_instrument(document, clause, record):
            dropped.append({"documentId": document, "clause": clause, "reason": "wrong-instrument"})
            continue
        add(document, clause, record, str(ref.get("edition") or ""))

    if need_samr_correction:
        for document, clause in ((SAMR100_DOC, "第三十九条"), (FOOD_SAFETY_LAW_DOC, "第一百二十五条")):
            record = lookup(document, clause, bundle_id)
            if record and record.get("text") and clause_belongs_to_instrument(document, clause, record):
                add(document, clause, record)

    unresolved = [row for row in dropped if row.get("reason") == "unresolved"]
    # A wrong-instrument or 4.14 drop is not a hard resolution failure.
    clause_reason = ""
    if unresolved and not valid:
        clause_reason = "clauseRefs 三元组不存在或正文取不到"
    return {
        "clauseRefs": valid,
        "dropped": dropped,
        "clauseReason": clause_reason,
        "label": citation_label(valid, dropped),
        "corrected": need_samr_correction and any(
            ref.get("documentId") == SAMR100_DOC and ref.get("clause") == "第三十九条" for ref in valid
        ),
    }
