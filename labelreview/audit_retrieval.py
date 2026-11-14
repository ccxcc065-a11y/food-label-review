"""Program prep, published-library retrieval, and checklist coverage for the audit agent.

The first (audit) agent reads only the published bundle pinned on the task. It
does not edit the knowledge base. Coverage is recorded for every checklist
item; the agent not raising an issue is never treated as a pass.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

import labelreview.clause_index as clause_index
import labelreview.knowledge as knowledge
import labelreview.clause_attribution as clause_attribution
import labelreview.registry as registry_mod
CHECKED = "已查"
UNCHECKED = "未查"
APPLICABLE = "适用"
NOT_APPLICABLE = "不适用"
INSUFFICIENT = "证据不足"
UNCOVERED = "未检查／未覆盖"

COVERAGE_STATES = {CHECKED, UNCHECKED, UNCOVERED}
APPLICABILITY_STATES = {APPLICABLE, NOT_APPLICABLE, INSUFFICIENT, ""}
RECORD_STATUSES = {CHECKED, UNCHECKED, APPLICABLE, NOT_APPLICABLE, INSUFFICIENT, UNCOVERED}

# R14 (GB 7718-2025 4.14) 2026-11-14 起由 audit.py:warning_statements 实现：
# 命中 knowledge/warnings.json 登记的依据时给出「读到／读不到／转人工」，其余仍按未覆盖处理。
# 本集合只保留「仍然完全没有实现的规则」，目前为空。
# 注意：UNIMPLEMENTED_LIBRARY_CLAUSES 保留，4.14 不能当作某条确定缺陷的直接法律依据，
# 固体饮料一项的实际依据是市场监管总局 2021 年第 46 号公告第三条。
UNIMPLEMENTED_RULE_IDS = set()
UNIMPLEMENTED_LIBRARY_CLAUSES = {"4.14"}

# Generic words that should not drive whole-library keyword search.
_STOP = {
    "食品", "标签", "标示", "应当", "不得", "以及", "或者", "产品", "预包装", "根据",
    "按照", "要求", "规定", "进行", "可以", "如果", "其他", "有关", "相关", "内容",
    "信息", "使用", "一般", "项目", "可见", "文字", "原图", "审核", "检查", "是否",
    "完整", "情况", "本次", "需要", "已经", "没有", "不是", "只是", "还有", "这个",
    "一个", "对于", "因为", "所以", "然后", "并且", "同时", "必须", "应在", "包括",
}

# Product-fact cues → library search queries (clause number, keyword, or topic).
TOPIC_CUES = (
    (("致敏", "过敏原", "过敏", "花生", "乳类", "麸质", "大豆", "坚果", "芝麻", "小麦",
      "蛋类", "甲壳", "鱼类"), ("致敏物质", "4.12")),
    (("营养成分表", "营养素参考值", "NRV", "nrv"), ("营养成分", "NRV%", "A.3.2")),
    (("辐照", "电离辐射"), ("辐照食品", "4.15")),
    (("转基因",), ("转基因",)),
    (("冷冻干燥", "复水"), ("冷冻干燥", "复水")),
    (("质量等级", "品质等级"), ("质量等级", "4.11")),
    (("净含量",), ("净含量", "4.5")),
    (("保质期", "生产日期", "年月日"), ("日期标示", "4.7")),
    (("贮存", "储藏"), ("贮存条件",)),
    (("食品名称", "属性名称"), ("食品名称", "4.2")),
    (("配料表", "配料"), ("配料表", "4.3")),
)

_TOKEN = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9\-]{1,}|\d+(?:\.\d+)+")
_PASS_LIKE = re.compile(r"可以通过|符合要求|无问题|无需整改|已放行|可以上市|无风险")
_NEED_ACTION = {"须整改", "待核实", "证据不足", "不适用"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _tokens(text):
    parts = _TOKEN.findall(str(text or ""))
    return [part for part in parts if part not in _STOP and part.lower() not in _STOP]


def _unique(items):
    seen, out = set(), []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def evidence_version_id(pages=None, fact_qa=None, corrections=None, verified_fields=None):
    """Stable id for the frozen image / fact / correction snapshot."""
    images = []
    for page in pages or []:
        blocks = [{"id": block.get("id"), "text": block.get("text"),
                   "box": block.get("box")} for block in page.get("blocks") or []]
        images.append({"id": page.get("id"), "name": page.get("name"),
                       "digest": _digest(blocks), "blockCount": len(blocks)})
    payload = {
        "images": images,
        "factQa": fact_qa or {},
        "corrections": corrections or [],
        "verifiedFields": verified_fields or [],
    }
    return "ev-" + _digest(payload)[:16]


def evidence_catalog(pages):
    """Every OCR block, full text. Not limited to refs already on a rule item."""
    catalog = []
    for page in pages or []:
        image_id = page.get("id")
        for block in page.get("blocks") or []:
            if not isinstance(block, dict) or not block.get("id"):
                continue
            catalog.append({
                "id": str(block.get("id")),
                "pageId": image_id,
                "imageId": image_id,
                "text": str(block.get("text") or ""),
                "box": block.get("box"),
            })
    return catalog


def product_situation(report=None, payload=None, context=None, draft=None):
    """Compact product facts used to drive retrieval and evidence selection."""
    report = report if isinstance(report, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    context = context if isinstance(context, dict) else (payload.get("context") or {})
    draft = draft if isinstance(draft, dict) else (payload.get("draft") or {})
    fields = []
    raw_fields = report.get("fields") or draft.get("fields") or {}
    if isinstance(raw_fields, dict):
        for key, row in raw_fields.items():
            if isinstance(row, dict):
                fields.append({"key": key, "label": row.get("label") or key,
                               "value": row.get("value")})
            else:
                fields.append({"key": key, "value": row})
    elif isinstance(raw_fields, list):
        fields = [{k: row.get(k) for k in ("key", "label", "value", "status") if k in row}
                  for row in raw_fields if isinstance(row, dict)]
    fact_qa = report.get("factQa") if isinstance(report.get("factQa"), dict) else draft.get("factQa")
    texts = []
    for row in fields:
        texts.append(str(row.get("label") or ""))
        texts.append(str(row.get("value") or ""))
    if isinstance(fact_qa, dict):
        table = fact_qa.get("nutritionTable") or {}
        for reading in table.get("ocrReadings") or []:
            if isinstance(reading, dict):
                texts.append(str(reading.get("name") or reading.get("text") or ""))
                texts.append(str(reading.get("value") or ""))
    blob = " ".join(texts)
    return {
        "scope": context.get("scope") or report.get("scopeLabel") or "",
        "sampleKind": context.get("sampleKind") or "",
        "nutritionRequired": context.get("nutritionRequired") or "",
        "fields": fields,
        "factQa": fact_qa,
        "keywords": _unique(_tokens(blob)),
        "text": blob,
    }


def _block_score(block, terms):
    text = str(block.get("text") or "")
    if not terms:
        return 0
    score = 0
    for term in terms:
        if term and term in text:
            score += 2 if len(term) >= 2 else 1
    return score


def select_evidence(item, catalog, product=None, fact_qa=None, corrections=None, limit=40):
    """Pick relevant original-image / fact-QA evidence for one checklist item.

    Relevance is based on the item and product facts, not on the first five
    refs already attached to a local rule. Matching blocks are returned in
    full (no 300-character cut).
    """
    product = product or {}
    terms = _unique(_tokens(item.get("title") or "")
                    + list(item.get("keywords") or [])
                    + list(product.get("keywords") or [])[:12])
    ranked = []
    for block in catalog or []:
        score = _block_score(block, terms)
        # Always keep blocks the item already pointed at, but do not *only* keep those.
        if block.get("id") in set(item.get("seedRefs") or []):
            score = max(score, 1)
        if score:
            ranked.append((score, block))
    ranked.sort(key=lambda row: (-row[0], row[1].get("id") or ""))
    chosen = [deepcopy(block) for _, block in ranked[:limit]]
    if not chosen and catalog:
        # No keyword overlap: still show a full-text window from the snapshot,
        # rather than hiding every block the local rules did not already attach.
        chosen = [deepcopy(block) for block in list(catalog)[:min(limit, 20)]]
    # Nutrition / fact QA travels with nutrition-related items or when the table is in question.
    title = str(item.get("title") or "") + " " + " ".join(item.get("keywords") or [])
    qa = fact_qa if isinstance(fact_qa, dict) else (product.get("factQa") if isinstance(product.get("factQa"), dict) else {})
    attach_qa = False
    if qa:
        if any(token in title for token in ("营养", "NRV", "能量", "脂肪", "钠", "糖", "蛋白质")):
            attach_qa = True
        if item.get("source") in {"rule:N01", "rule:N02", "rule:N03", "rule:F01", "rule:F02"}:
            attach_qa = True
    related_qa = qa.get("nutritionTable") if attach_qa else None
    related_corrections = []
    for row in corrections or []:
        if not isinstance(row, dict):
            continue
        field = str(row.get("field") or row.get("key") or "")
        if field and (field in title or field in " ".join(terms)):
            related_corrections.append(row)
        elif attach_qa and row.get("field") in {
                "energy", "protein", "fat", "saturated", "carb", "sugar", "sodium", "basis"}:
            related_corrections.append(row)
    return {
        "blocks": chosen,
        "factQa": related_qa,
        "corrections": related_corrections,
    }


def _rules_bundle(bundle_id=None):
    try:
        return registry_mod.default_registry().rules_bundle(bundle_id) or {}
    except Exception:
        return {}


def cited_clause_keys(bundle_id=None):
    """(documentId, clause) pairs already cited by published rules — the old input window."""
    rules = _rules_bundle(bundle_id)
    citations = rules.get("citations") or {}
    keys = set()
    for edition, rows in citations.items():
        if not isinstance(rows, dict):
            continue
        for record in rows.values():
            if not isinstance(record, dict):
                continue
            document = record.get("document") or record.get("documentId")
            for number in knowledge.clause_tokens(record.get("clause")):
                if document and number:
                    keys.add((str(document), str(number)))
            for related in record.get("related") or []:
                if not isinstance(related, dict):
                    continue
                rel_doc = related.get("document") or related.get("documentId") or document
                for number in knowledge.clause_tokens(related.get("clause")):
                    if rel_doc and number:
                        keys.add((str(rel_doc), str(number)))
    return keys


def pin_task_versions(context=None, payload=None, report=None):
    context = dict(context or {})
    report = report if isinstance(report, dict) else {}
    existing = context.get("versions") if isinstance(context.get("versions"), dict) else {}
    if not existing.get("bundleId") and isinstance(report.get("versions"), dict):
        existing = report.get("versions") or existing
        if existing:
            context = dict(context, versions=existing)
    try:
        return registry_mod.pin_versions(context, payload)
    except Exception:
        return dict(existing or {})


def _published_clause(document_id, number, bundle_id=None):
    """Read a clause from the published index (descendants and corrections applied)."""
    if not document_id or not number:
        return None
    if not clause_attribution.clause_belongs_to_instrument(document_id, number):
        return None
    record = knowledge.lookup_clause(number, document=document_id, bundle_id=bundle_id)
    if isinstance(record, list):
        record = next((row for row in record if row and row.get("text")), None)
    if not record or record.get("conflict") and record.get("text") is None:
        return record
    if not clause_attribution.clause_belongs_to_instrument(document_id, number, record):
        return None
    if not record.get("documentId"):
        record = dict(record, documentId=document_id)
    return record


def _validated_clause_refs(raw_refs, bundle_id=None):
    """Resolve agent refs, refuse wrong-instrument hits, and drop 4.14 stand-ins."""
    refined = clause_attribution.refine_clause_refs(
        raw_refs, lookup=_published_clause, bundle_id=bundle_id)
    return refined


def _hit_to_ref(hit, default_document=None):
    document = hit.get("documentId") or default_document
    number = hit.get("clause") or hit.get("number")
    if not document or not number:
        return None
    return document, number, hit


def retrieve_for_item(item, product=None, bundle_id=None, limit=12):
    """Search the published library for clauses, definitions, exceptions and annexes.

    Does not restrict the search to clauses already cited by local rules.
    """
    product = product or {}
    queries = list(item.get("queries") or [])
    for number in knowledge.clause_tokens(item.get("clause") or "") + list(item.get("clauseNumbers") or []):
        queries.append({"mode": "exact", "query": number, "document": item.get("documentId")})
    title = str(item.get("title") or "").strip()
    if title:
        queries.append({"mode": "topic", "query": title})
        queries.append({"mode": "keyword", "query": " ".join(_tokens(title)[:4])})
    for cue, searches in TOPIC_CUES:
        blob = " ".join([title, " ".join(item.get("keywords") or []), product.get("text") or ""])
        if any(token in blob for token in cue):
            for search in searches:
                mode = "exact" if re.match(r"^[A-Za-z]?\d+(?:\.\d+)*$", search) else "keyword"
                queries.append({"mode": mode, "query": search})
    for keyword in list(item.get("keywords") or [])[:6]:
        if keyword and keyword not in _STOP:
            queries.append({"mode": "keyword", "query": keyword})
    # Product tokens are used to find uncited library bases, but only on
    # retrieval-driven items — not sprayed onto every published rule.
    if item.get("source") in {"library-retrieval", "library-coverage"}:
        for keyword in list(product.get("keywords") or [])[:8]:
            if keyword and keyword not in _STOP:
                queries.append({"mode": "keyword", "query": keyword})

    seen, results, trail = set(), [], []
    for spec in queries:
        if not isinstance(spec, dict):
            continue
        query = str(spec.get("query") or "").strip()
        mode = spec.get("mode") or "keyword"
        if not query:
            continue
        document = spec.get("document") or item.get("documentId")
        trail.append({"mode": mode, "query": query, "document": document})
        try:
            if mode == "exact" and document:
                found = [_published_clause(document, query, bundle_id)]
            elif mode == "exact":
                found = knowledge.search_clauses(query, mode="exact", bundle_id=bundle_id, limit=8)
            else:
                found = knowledge.search_clauses(query, mode=mode, document=document, bundle_id=bundle_id, limit=8)
        except Exception:
            found = []
        for hit in found or []:
            if not hit:
                continue
            parsed = _hit_to_ref(hit, document)
            if not parsed:
                continue
            doc_id, number, raw = parsed
            key = (doc_id, number)
            record = raw if raw.get("text") and raw.get("clause") else _published_clause(doc_id, number, bundle_id)
            if not record or (record.get("conflict") and record.get("text") is None):
                if record and record.get("conflict"):
                    if key not in seen:
                        seen.add(key)
                        results.append(dict(record, retrievalMode=mode, retrievalQuery=query))
                continue
            if key in seen:
                continue
            seen.add(key)
            entry = {
                "documentId": record.get("documentId") or doc_id,
                "edition": str(record.get("documentVersion") or "2025"),
                "clause": record.get("clause") or number,
                "title": record.get("title"),
                "text": record.get("text"),
                "textHash": record.get("textHash"),
                "kind": record.get("kind"),
                "page": record.get("page"),
                "location": record.get("location"),
                "children": record.get("children") or [],
                "related": record.get("related") or [],
                "relatedSlices": record.get("relatedSlices") or [],
                "conflict": record.get("conflict"),
                "bundleId": record.get("bundleId") or bundle_id,
                "retrievalMode": mode,
                "retrievalQuery": query,
            }
            results.append(entry)
            # Expand definitions, exceptions, annexes / related slices from the published index.
            if mode != "related":
                try:
                    related_hits = knowledge.search_clauses(number, mode="related", document=doc_id,
                                                           bundle_id=bundle_id, limit=8)
                except Exception:
                    related_hits = []
                for rel in related_hits or []:
                    rel_doc = rel.get("documentId") or doc_id
                    rel_num = rel.get("clause")
                    rel_key = (rel_doc, rel_num)
                    if not rel_num or rel_key in seen:
                        continue
                    rel_record = _published_clause(rel_doc, rel_num, bundle_id) or rel
                    if not rel_record or not rel_record.get("text"):
                        continue
                    seen.add(rel_key)
                    results.append({
                        "documentId": rel_record.get("documentId") or rel_doc,
                        "edition": str(rel_record.get("documentVersion") or "2025"),
                        "clause": rel_record.get("clause") or rel_num,
                        "title": rel_record.get("title"),
                        "text": rel_record.get("text"),
                        "textHash": rel_record.get("textHash"),
                        "kind": rel_record.get("kind") or "related",
                        "relatedTo": number,
                        "retrievalMode": "related",
                        "retrievalQuery": number,
                        "bundleId": bundle_id,
                    })
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break
    return results[:limit], trail


def _library_coverage_rows():
    path = knowledge.ROOT / "knowledge" / "coverage.json" if hasattr(knowledge, "ROOT") else None
    try:
        from pathlib import Path
        data = json.loads((Path(__file__).resolve().parent.parent / "knowledge" / "coverage.json").read_text())
        return data.get("clauses") or {}
    except (OSError, ValueError):
        return {}


def is_unimplemented_item(item, record=None):
    """True for R14 / library 4.14 / any published unimplemented rule.

    These must stay 未检查／未覆盖 and must never be shown as 已查 / 通过 / 已勾选.
    F02 is a different rule and is not treated as a stand-in for R14.
    """
    record = record if isinstance(record, dict) else {}
    item = item if isinstance(item, dict) else {}
    rule_id = str(item.get("ruleId") or record.get("id") or "")
    if rule_id in UNIMPLEMENTED_RULE_IDS:
        return True
    kind = record.get("implementationKind") or item.get("implementationKind")
    if kind == "unimplemented":
        return True
    cid = str(item.get("id") or item.get("checklistId") or "")
    if cid in {"rule:R14", "library:4.14"} or cid.endswith(":4.14"):
        return True
    if cid.startswith("library:") and cid.split(":", 1)[-1] in UNIMPLEMENTED_LIBRARY_CLAUSES:
        return True
    if item.get("source") == "library-coverage":
        numbers = [str(n) for n in (item.get("clauseNumbers") or [])]
        if any(n in UNIMPLEMENTED_LIBRARY_CLAUSES for n in numbers):
            return True
    return False


FORMAL_FINDING_IDS = {"I01", "I02", "I03", "I04"}


def is_formal_finding_item(item):
    """Program findings I01–I04 on the formal report, not AI checklist rows."""
    item = item if isinstance(item, dict) else {}
    item_id = str(item.get("id") or item.get("itemId") or "")
    cid = str(item.get("checklistId") or "")
    return item_id in FORMAL_FINDING_IDS and not cid.startswith(
        ("rule:", "library:", "item:", "retrieved:"))


def sanitize_review_row(row, bundle_id=None):
    """Copy a candidate / locked judgment and clean citation prose and refs."""
    row = dict(row or {})
    for key in ("claim", "fact", "reason", "title", "detail"):
        if row.get(key):
            row[key] = clause_attribution.sanitize_citation_prose(row[key])
    refs = row.get("clauseRefs") or row.get("clause_refs")
    if refs:
        refined = _validated_clause_refs(refs, bundle_id or row.get("bundleId"))
        row["clauseRefs"] = refined["clauseRefs"]
        row["droppedClauseRefs"] = list(refined.get("dropped") or [])
        labels = dict(row.get("labels") or {})
        labels["citation"] = refined["label"]
        row["labels"] = labels
        row["citationLabel"] = refined["label"]
    return row


def _clause_ref_rows(*groups):
    rows = []
    for group in groups:
        for ref in group or []:
            if isinstance(ref, dict):
                rows.append(ref)
    return rows


def _warning_basis_document_ids(refresh=False):
    """依据表登记的实际依据文档 id（固体饮料公告、三新食品表）。

    R14 是转致条款：4.14 本身不能当缺陷依据，但公告第三条可以。
    这里直接读 knowledge/warnings.json，避免反向依赖 audit.py。
    """
    global _WARNING_BASIS_CACHE
    path = Path(__file__).resolve().parent.parent / "knowledge" / "warnings.json"
    try:
        stat = path.stat()
    except OSError:
        return set()
    stamp = (stat.st_mtime_ns, stat.st_size)
    if not refresh and _WARNING_BASIS_CACHE.get("stamp") == stamp:
        return _WARNING_BASIS_CACHE["ids"]
    ids = set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    for key in ("solidDrink", "threeNew"):
        basis = ((data.get(key) or {}).get("basis") or {})
        if isinstance(basis, dict) and basis.get("documentId"):
            ids.add(str(basis["documentId"]))
    _WARNING_BASIS_CACHE.update({"stamp": stamp, "ids": ids})
    return ids


_WARNING_BASIS_CACHE = {}


def _r14_basis_bound(item, src):
    """R14 条目是否绑到了实际依据文档（而不是 4.14 转致条款）。"""
    basis_ids = _warning_basis_document_ids()
    if not basis_ids:
        return False
    for row in (item.get("source"), src.get("source")):
        if isinstance(row, dict) and str(row.get("documentId") or "") in basis_ids:
            return True
    return False


def _r14_evidence_sufficient(item, src):
    """R14 确认须整改，必须有本项专属证据门槛，不能只靠全局布尔。"""
    for holder in (item, src):
        gate = holder.get("evidenceGate") if isinstance(holder, dict) else None
        if isinstance(gate, dict) and gate.get("sufficient") is True:
            return True
    return False


def unsound_fail_confirmation(item, original=None):
    """True: this row must not be locked as confirmed 须整改.

    Formal I01–I04 program findings are never unsound under this rule.
    Error classes: 4.14 used as a legal basis (including the GB 7718 Word-file
    id); R14 items that are not bound to the registered 公告 第三条 or lack the
    per-item evidence gate; job-ticket 315 g treated as pack net content
    vs 328 g; reason text that still pins article 125 on 办法; 「归属可对上」
    on a wrong instrument or wrong clause.
    """
    item = item if isinstance(item, dict) else {}
    src = original if isinstance(original, dict) else item
    if is_formal_finding_item(item) or is_formal_finding_item(src):
        return False
    cid = str(item.get("checklistId") or src.get("checklistId") or "")
    # 4.14 是转致条款，永远不能当作某条缺陷的直接依据。
    if cid == "library:4.14" or cid.endswith(":4.14"):
        return True
    if cid == "rule:R14" or str(item.get("ruleId") or src.get("ruleId") or "") == "R14":
        # 唯一放行条件：绑到了公告第三条，且本项证据门槛已满足。
        if not _r14_basis_bound(item, src):
            return True
        if not _r14_evidence_sufficient(item, src):
            return True
    probe = {"id": cid, "ruleId": item.get("ruleId") or src.get("ruleId"),
             "implementationKind": item.get("implementationKind") or src.get("implementationKind"),
             "source": item.get("source") or src.get("source"),
             "clauseNumbers": item.get("clauseNumbers") or src.get("clauseNumbers")}
    if is_unimplemented_item(item) or is_unimplemented_item(probe):
        return True
    refs = _clause_ref_rows(item.get("clauseRefs"), item.get("droppedClauseRefs"),
                            src.get("clauseRefs"), src.get("droppedClauseRefs"))
    for ref in refs:
        clause = str(ref.get("clause") or ref.get("clauseId") or "").strip()
        document = str(ref.get("documentId") or ref.get("document") or "")
        if clause == "4.14" or ref.get("reason") == "4.14-standin":
            return True
        if ref.get("reason") == "wrong-instrument":
            return True
        if clause_attribution.is_unimplemented_standin(document, clause):
            return True
    if clause_attribution.is_job_ticket_net_content_conflict(src) or (
            clause_attribution.is_job_ticket_net_content_conflict(item)):
        return True
    prose = " ".join(str(item.get(key) or "") for key in ("claim", "fact", "reason", "title", "detail"))
    if clause_attribution.attributes_article_125_to_measures(prose):
        return True
    if clause_attribution.uses_414_as_basis(prose):
        return True
    label = str((item.get("labels") or {}).get("citation") or item.get("citationLabel")
                or (src.get("labels") or {}).get("citation") or src.get("citationLabel") or "")
    if "归属可对上" in label:
        if any(str(ref.get("clause") or "").strip() == "4.14" or ref.get("reason") in {
                "4.14-standin", "wrong-instrument"} for ref in refs):
            return True
        # Defense: a 可对上 label on a 4.14 citation that slipped through refine.
        if any(str(ref.get("clause") or "").strip() == "4.14" for ref in refs):
            return True
    return False


def replay_review_decision_set(payload_or_bundle):
    """Rebuild an in-memory DecisionSet from a saved 03:51-style task bundle.

    Replays items + locked judgments through sanitize + the unsound-fail filter.
    Does not write disk, does not call the model, does not mutate archives.
    """
    import labelreview.decision_set as decision_set_mod
    if hasattr(payload_or_bundle, "report"):
        report = payload_or_bundle.report or {}
        review = getattr(payload_or_bundle, "review", None) or {}
        plan = getattr(payload_or_bundle, "plan", None) or report.get("reportPlan") or {}
    else:
        raw = payload_or_bundle if isinstance(payload_or_bundle, dict) else {}
        report = raw.get("report") or {}
        review = raw.get("review") or {}
        plan = raw.get("plan") or report.get("reportPlan") or {}
    snapshot = str(plan.get("snapshotId") or report.get("snapshotId") or "")
    versions = report.get("versions") or review.get("versions") or {}
    decisions = decision_set_mod.DecisionSet(
        snapshot_id=snapshot,
        knowledge_version=str(versions.get("knowledgeVersion") or ""),
        rules_version=str(versions.get("rulesVersion") or ""),
    )
    locked = list(review.get("lockedJudgments") or report.get("lockedJudgments") or [])
    decision_set_mod.seed_from_report(decisions, {
        "items": list(report.get("items") or []),
        "lockedJudgments": locked,
    })
    return decisions


def build_checklist(report=None, product=None, bundle_id=None, catalog=None,
                    fact_qa=None, corrections=None):
    """Inspection checklist: published rules, library coverage, and uncited hits."""
    report = report if isinstance(report, dict) else {}
    product = product or product_situation(report)
    rules = _rules_bundle(bundle_id)
    registered = (rules.get("rules") or {}) if isinstance(rules, dict) else {}
    cited = cited_clause_keys(bundle_id)
    report_items = {str(row.get("id")): row for row in report.get("items") or [] if isinstance(row, dict)}
    items = []

    for rule_id, record in registered.items():
        if not isinstance(record, dict):
            continue
        enabled = bool(record.get("enabled", True))
        row = report_items.get(str(rule_id), {})
        item = {
            "id": "rule:%s" % rule_id,
            "ruleId": str(rule_id),
            "title": record.get("title") or row.get("title") or str(rule_id),
            "source": "rule",
            "checks": record.get("checks") or "",
            "appliesWhen": record.get("appliesWhen") or "",
            "requiredEvidence": record.get("requiredEvidence") or "",
            "seedRefs": list(row.get("refs") or []),
            "keywords": _tokens((record.get("title") or "") + " " + (record.get("checks") or "")),
            "clauseNumbers": knowledge.clause_tokens(
                (record.get("judgment") or "") + " " + str((rules.get("citations") or {}).get("2025", {}).get(rule_id, {}).get("clause") or "")
            ),
            "documentId": ((rules.get("citations") or {}).get("2025") or {}).get(rule_id, {}).get("document"),
        }
        if not enabled or row.get("outcome") == "uncovered" or row.get("status") == UNCOVERED:
            item["coverage"] = UNCOVERED
            item["applicability"] = ""
            item["status"] = UNCOVERED
            item["finding"] = ""
            item["passed"] = False
            item["reason"] = "rule-disabled"
        elif is_unimplemented_item(item, record):
            item["coverage"] = UNCOVERED
            item["applicability"] = ""
            item["status"] = UNCOVERED
            item["finding"] = ""
            item["passed"] = False
            item["reason"] = "rule-unimplemented"
            item["implementationKind"] = "unimplemented"
        else:
            item["coverage"] = UNCHECKED
            item["applicability"] = ""
            item["status"] = UNCHECKED
            item["finding"] = ""
            item["passed"] = False
        items.append(item)

    seen_rules = {str(item.get("ruleId")) for item in items}
    for rule_id, row in report_items.items():
        if rule_id in seen_rules:
            continue
        if row.get("outcome") == "uncovered" or row.get("status") == UNCOVERED:
            items.append({
                "id": "rule:%s" % rule_id, "ruleId": rule_id, "title": row.get("title") or rule_id,
                "source": "rule", "seedRefs": list(row.get("refs") or []),
                "coverage": UNCOVERED, "applicability": "", "status": UNCOVERED,
                "finding": "", "passed": False, "reason": "rule-disabled",
            })
        else:
            items.append({
                "id": "item:%s" % rule_id, "ruleId": rule_id, "title": row.get("title") or rule_id,
                "source": "report-item", "seedRefs": list(row.get("refs") or []),
                "keywords": _tokens(row.get("title") or ""),
                "coverage": UNCHECKED, "applicability": "", "status": UNCHECKED,
                "finding": "", "passed": False,
            })

    for number, row in _library_coverage_rows().items():
        if not isinstance(row, dict):
            continue
        # Keep library rows that rules only partially cover, or do not cover, so the
        # agent can still retrieve from the published index.
        if row.get("status") not in {"missing", "partial", "unimplemented"}:
            continue
        library_item = {
            "id": "library:%s" % number,
            "title": row.get("title") or number,
            "source": "library-coverage",
            "clauseNumbers": knowledge.clause_tokens(number) or [number],
            "keywords": _tokens(row.get("title") or number),
            "coverage": UNCHECKED, "applicability": "", "status": UNCHECKED,
            "finding": "", "passed": False,
            "note": row.get("note"),
        }
        if is_unimplemented_item(library_item) or row.get("status") == "unimplemented":
            library_item["coverage"] = UNCOVERED
            library_item["status"] = UNCOVERED
            library_item["passed"] = False
            library_item["reason"] = "rule-unimplemented"
        items.append(library_item)

    # Product-driven extras: library hits that published rules did not cite.
    extra_queries = []
    blob = product.get("text") or ""
    for cue, searches in TOPIC_CUES:
        if any(token in blob for token in cue):
            extra_queries.extend(searches)
    extra_queries.extend(list(product.get("keywords") or [])[:10])
    seen_extra = {(item.get("documentId"), num) for item in items for num in item.get("clauseNumbers") or []}
    for query in _unique(extra_queries):
        mode = "exact" if re.match(r"^[A-Za-z]?\d+(?:\.\d+)*$", str(query)) else "keyword"
        try:
            hits = knowledge.search_clauses(str(query), mode=mode, bundle_id=bundle_id, limit=6)
        except Exception:
            hits = []
        for hit in hits or []:
            doc_id = hit.get("documentId")
            number = hit.get("clause")
            if not doc_id or not number or (doc_id, number) in seen_extra:
                continue
            # Skip if any published rule already cites this exact pair (or a parent).
            if (doc_id, number) in cited:
                continue
            parent = number.rsplit(".", 1)[0] if "." in number else None
            if parent and (doc_id, parent) in cited:
                continue
            seen_extra.add((doc_id, number))
            items.append({
                "id": "retrieved:%s:%s" % (doc_id, number),
                "title": hit.get("title") or ("%s %s" % (doc_id, number)),
                "source": "library-retrieval",
                "documentId": doc_id,
                "clauseNumbers": [number],
                "keywords": _tokens((hit.get("title") or "") + " " + str(query)),
                "coverage": UNCHECKED, "applicability": "", "status": UNCHECKED,
                "finding": "", "passed": False,
                "seedQuery": query,
            })
            if sum(1 for item in items if item.get("source") == "library-retrieval") >= 12:
                break
        if sum(1 for item in items if item.get("source") == "library-retrieval") >= 12:
            break

    # Attach retrieval and evidence to each item (disabled rules stay uncovered, no retrieval needed).
    for item in items:
        if item.get("status") == UNCOVERED:
            item["retrievedClauses"] = []
            item["queries"] = []
            item["evidence"] = {"blocks": [], "factQa": None, "corrections": []}
            continue
        clauses, trail = retrieve_for_item(item, product=product, bundle_id=bundle_id)
        item["retrievedClauses"] = clauses
        item["queries"] = trail
        item["evidence"] = select_evidence(item, catalog or [], product=product,
                                           fact_qa=fact_qa, corrections=corrections)
    return items


def prepare_audit_task(report=None, payload=None, plan=None, context=None, pages=None, draft=None):
    """Freeze this task's images, facts, corrections and published versions; build the checklist."""
    payload = payload if isinstance(payload, dict) else {}
    report = report if isinstance(report, dict) else {}
    context = context if isinstance(context, dict) else (payload.get("context") or {})
    pages = pages if pages is not None else (payload.get("pages") or [])
    draft = draft if isinstance(draft, dict) else (payload.get("draft") or {})
    plan = plan if isinstance(plan, dict) else (report.get("reportPlan") or {})
    versions = pin_task_versions(context, payload, report)
    bundle_id = versions.get("bundleId")
    token = None
    active = None
    try:
        active = registry_mod.active_bundle()
    except Exception:
        active = None
    if bundle_id and not (isinstance(active, dict) and active.get("id") == bundle_id):
        try:
            token = registry_mod.use_bundle(bundle_id)
        except Exception:
            token = None
    try:
        fact_qa = report.get("factQa") if isinstance(report.get("factQa"), dict) else draft.get("factQa")
        corrections = list(context.get("verifiedFields") or []) + list(draft.get("manualCorrections") or [])
        if isinstance(fact_qa, dict) and fact_qa.get("staleVerifications"):
            corrections = corrections + list(fact_qa.get("staleVerifications") or [])
        ev_id = evidence_version_id(pages, fact_qa, corrections, context.get("verifiedFields"))
        catalog = evidence_catalog(pages)
        product = product_situation(report, payload, context, draft)
        checklist = build_checklist(report, product=product, bundle_id=bundle_id,
                                    catalog=catalog, fact_qa=fact_qa, corrections=corrections)
        frozen = {
            "images": [{"id": page.get("id"), "name": page.get("name"),
                        "blockCount": len(page.get("blocks") or [])} for page in pages or []],
            "facts": [{"key": row.get("key"), "value": row.get("value")} for row in product.get("fields") or []],
            "corrections": corrections,
            "factQaDigest": _digest(fact_qa or {}),
        }
        return {
            "versions": versions,
            "bundleId": bundle_id,
            "evidenceVersionId": ev_id,
            "snapshotId": plan.get("snapshotId"),
            "frozen": frozen,
            "product": {k: product[k] for k in ("scope", "sampleKind", "nutritionRequired", "keywords") if k in product},
            "factQa": fact_qa,
            "evidenceCatalog": catalog,
            "checklist": checklist,
        }
    finally:
        if token is not None:
            try:
                registry_mod.reset_bundle(token)
            except Exception:
                pass


def _block_ref(block):
    """Point at a catalog block without copying its text into every checklist row."""
    return {"id": block.get("id"), "imageId": block.get("imageId") or block.get("pageId")}


def model_payload(prep):
    """Input package for the audit agent: checklist, retrieved clauses, full evidence once.

    Block text is sent only in evidenceIndex. Checklist rows and items carry evidence
    ids (and imageId) so a long block is not repeated on every item. This is not a
    return to the old 5-block / 300-character window: the index keeps every block
    in full, and items still list every relevant id the retriever attached.
    """
    checklist = []
    retrieved = {}
    for item in prep.get("checklist") or []:
        evidence = item.get("evidence") or {}
        blocks = [_block_ref(block) for block in evidence.get("blocks") or []]
        clause_refs = []
        for clause in item.get("retrievedClauses") or []:
            key = "%s@%s %s" % (clause.get("documentId"), clause.get("edition") or "2025", clause.get("clause"))
            retrieved[key] = {
                "documentId": clause.get("documentId"),
                "edition": clause.get("edition") or "2025",
                "clause": clause.get("clause"),
                "title": clause.get("title"),
                "text": clause.get("text"),
                "textHash": clause.get("textHash"),
                "kind": clause.get("kind"),
                "relatedSlices": clause.get("relatedSlices") or [],
            }
            clause_refs.append(key)
        checklist.append({
            "id": item.get("id"),
            "title": item.get("title"),
            "source": item.get("source"),
            "ruleId": item.get("ruleId"),
            "status": item.get("status"),
            "coverage": item.get("coverage"),
            "checks": item.get("checks") or "",
            "requiredEvidence": item.get("requiredEvidence") or "",
            "retrievedClauseIds": clause_refs,
            "evidence": blocks,
            "factQa": evidence.get("factQa"),
            "corrections": evidence.get("corrections") or [],
        })
    # Full-text catalog, once: facts the rules did not attach remain citable.
    catalog = [{"id": block.get("id"), "imageId": block.get("imageId") or block.get("pageId"),
                "text": block.get("text")} for block in prep.get("evidenceCatalog") or []]
    items = []
    for row in checklist:
        items.append({
            "id": row["id"],
            "title": row.get("title"),
            "outcome": None,
            "citedClauses": [{"documentId": retrieved[key]["documentId"],
                              "edition": retrieved[key]["edition"],
                              "clause": retrieved[key]["clause"]}
                             for key in row.get("retrievedClauseIds") or [] if key in retrieved],
            "evidenceIds": [block.get("id") for block in row.get("evidence") or []],
        })
    return {
        "snapshotId": prep.get("snapshotId"),
        "evidenceVersionId": prep.get("evidenceVersionId"),
        "versions": prep.get("versions") or {},
        "scope": (prep.get("product") or {}).get("scope") or "",
        "checklist": checklist,
        "items": items,
        "clauses": retrieved,
        "factQa": prep.get("factQa"),
        "evidenceIndex": catalog,
        "decisions": [],
        "instructions": {
            "coverageValues": [CHECKED, UNCHECKED, APPLICABLE, NOT_APPLICABLE, INSUFFICIENT, UNCOVERED],
            "noIssueIsNotPass": True,
            "disabledStaysUncovered": True,
            "readOnlyPublishedLibrary": True,
        },
    }


def _normalize_agent_payload(value):
    if isinstance(value, dict):
        return value
    text = re.sub(r"^```(?:json)?|```$", "", str(value or "").strip(), flags=re.M)
    return json.loads(text)


def _conclusion_of(applicability, finding, claim):
    if applicability == NOT_APPLICABLE:
        return "不适用"
    if applicability == INSUFFICIENT:
        return "证据不足"
    if finding == "issue":
        if claim and "待核实" in claim:
            return "待核实"
        return "须整改"
    if finding == "no-issue":
        return "未发现问题"
    return ""


def apply_coverage(prep, agent_payload=None, rejected_reason=None):
    """Merge agent coverage into the checklist. Omissions stay 未查, never pass.

    Disabled rules remain 未检查／未覆盖 even if the model tries to mark them passed.
    Citations must resolve to real published clause texts.
    """
    prep = deepcopy(prep)
    checklist = {item["id"]: item for item in prep.get("checklist") or []}
    versions = prep.get("versions") or {}
    ev_id = prep.get("evidenceVersionId")
    catalog_ids = {block.get("id") for block in prep.get("evidenceCatalog") or []}
    parsed = {}
    parse_error = rejected_reason
    if agent_payload is not None and not parse_error:
        try:
            parsed = _normalize_agent_payload(agent_payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
            parse_error = "候选输出格式无效"

    coverage_rows = parsed.get("coverage") if isinstance(parsed.get("coverage"), list) else []
    candidates = parsed.get("candidates") if isinstance(parsed.get("candidates"), list) else []
    by_id = {}
    for row in coverage_rows:
        if isinstance(row, dict) and row.get("checklistId"):
            by_id[str(row.get("checklistId"))] = row

    # Legacy candidates (claim/fact/reason) still count as a finding for that item,
    # but items they do not mention stay 未查 — not pass.
    unbound_candidates = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        target_ids = [str(x) for x in (candidate.get("checklistIds") or candidate.get("itemIds") or [])
                      if x]
        if not target_ids:
            evidence_ids = [ref for ref in (candidate.get("evidenceIds") or []) if isinstance(ref, str)]
            match = None
            for item in checklist.values():
                ev = {block.get("id") for block in ((item.get("evidence") or {}).get("blocks") or [])}
                if evidence_ids and ev.intersection(evidence_ids) and item.get("status") != UNCOVERED:
                    match = item
                    break
            if match:
                target_ids = [match["id"]]
            else:
                unbound_candidates.append(candidate)
        for cid in target_ids:
            if cid in checklist and cid not in by_id:
                by_id[cid] = {
                    "checklistId": cid,
                    "coverage": CHECKED,
                    "applicability": APPLICABLE,
                    "finding": "issue",
                    "claim": candidate.get("claim"),
                    "fact": candidate.get("fact"),
                    "reason": candidate.get("reason"),
                    "evidenceIds": candidate.get("evidenceIds") or [],
                    "clauseRefs": candidate.get("clauseRefs") or [],
                    "missingMaterial": candidate.get("missingMaterial"),
                }

    records, locked, proposed, rejected = [], [], [], []
    for item in prep.get("checklist") or []:
        cid = item["id"]
        base = {
            "checklistId": cid,
            "title": item.get("title"),
            "source": item.get("source"),
            "ruleId": item.get("ruleId"),
            "evidenceVersionId": ev_id,
            "bundleId": versions.get("bundleId"),
            "knowledgeVersion": versions.get("knowledgeVersion"),
            "rulesVersion": versions.get("rulesVersion"),
            "passed": False,
            "retrievedClauseIds": [
                "%s@%s %s" % (c.get("documentId"), c.get("edition") or "2025", c.get("clause"))
                for c in item.get("retrievedClauses") or []
            ],
        }
        if (item.get("status") == UNCOVERED or item.get("coverage") == UNCOVERED
                or is_unimplemented_item(item)):
            record = dict(base, coverage=UNCOVERED, applicability="", status=UNCOVERED,
                          finding="", passed=False,
                          reason=item.get("reason") or "rule-unimplemented")
            records.append(record)
            continue
        row = by_id.get(cid)
        if not row:
            records.append(dict(base, coverage=UNCHECKED, applicability="", status=UNCHECKED,
                                finding="", reason=parse_error or "agent-omitted"))
            continue
        coverage = str(row.get("coverage") or CHECKED)
        if coverage not in COVERAGE_STATES:
            coverage = CHECKED if coverage in {APPLICABLE, NOT_APPLICABLE, INSUFFICIENT} else UNCHECKED
        if coverage == UNCOVERED:
            coverage = CHECKED  # only a disabled rule may carry this status
        applicability = str(row.get("applicability") or "")
        if applicability not in APPLICABILITY_STATES:
            applicability = ""
        finding = str(row.get("finding") or "")
        if finding not in {"issue", "no-issue", ""}:
            finding = ""
        if coverage == CHECKED and not applicability:
            if finding == "issue":
                applicability = APPLICABLE
            elif row.get("missingMaterial") and any(
                    str((row.get("missingMaterial") or {}).get(k) or "").strip()
                    for k in ("what", "fromWhom", "affects")):
                applicability = INSUFFICIENT
        status = coverage
        if coverage == CHECKED and applicability in {APPLICABLE, NOT_APPLICABLE, INSUFFICIENT}:
            status = applicability
        evidence_ids = [ref for ref in (row.get("evidenceIds") or []) if isinstance(ref, str)]
        bad_evidence = [ref for ref in evidence_ids if ref not in catalog_ids]
        refined = _validated_clause_refs(row.get("clauseRefs") or [],
                                         versions.get("bundleId") or prep.get("bundleId"))
        clause_refs, clause_reason = refined["clauseRefs"], refined["clauseReason"]
        citation_note = refined["label"]
        reason_extra = ""
        if bad_evidence:
            reason_extra = "evidenceIds 不属于当前快照"
            evidence_ids = [ref for ref in evidence_ids if ref in catalog_ids]
        if clause_reason:
            reason_extra = (reason_extra + "；" if reason_extra else "") + clause_reason
        claim_text = clause_attribution.sanitize_citation_prose(str(row.get("claim") or ""))[:200]
        fact_text = clause_attribution.sanitize_citation_prose(str(row.get("fact") or ""))[:500]
        reason_text = clause_attribution.sanitize_citation_prose(str(row.get("reason") or ""))[:800]
        conclusion = _conclusion_of(applicability, finding, claim_text)
        decision = {
            "decisionId": "DEC-" + _digest({
                "checklistId": cid, "evidenceVersionId": ev_id,
                "conclusion": conclusion, "clauseRefs": clause_refs,
                "claim": claim_text, "finding": finding,
            })[:12],
            "checklistId": cid,
            "conclusion": conclusion or (CHECKED if coverage == CHECKED else UNCHECKED),
            "locked": True,
            "passAllowed": False,
            "finding": finding,
            "clauseRefs": clause_refs,
            "droppedClauseRefs": list(refined.get("dropped") or []),
            "evidenceIds": evidence_ids,
            "evidenceVersionId": ev_id,
            "knowledgeVersion": versions.get("knowledgeVersion"),
            "bundleId": versions.get("bundleId"),
            "claim": claim_text,
            "fact": fact_text,
            "reason": reason_text,
            "labels": {"citation": citation_note},
            "missingMaterial": row.get("missingMaterial") if isinstance(row.get("missingMaterial"), dict) else {},
        }
        unsound = unsound_fail_confirmation(decision, original=row)
        if unsound and conclusion == "须整改":
            conclusion = ""
            decision["conclusion"] = "待人工复核"
            decision["locked"] = False
            decision["unsoundFailConfirmation"] = True
        record = dict(base, coverage=coverage, applicability=applicability, status=status,
                      finding=finding, conclusion=conclusion, passed=False,
                      evidenceIds=evidence_ids, clauseRefs=clause_refs,
                      claim=decision["claim"], fact=decision["fact"], reason=decision["reason"],
                      missingMaterial=decision["missingMaterial"],
                      decisionId=decision["decisionId"],
                      validationNote=reason_extra)
        records.append(record)
        if coverage == CHECKED and not decision.get("unsoundFailConfirmation"):
            locked.append(decision)
        if finding == "issue" and not clause_reason and not decision.get("unsoundFailConfirmation"):
            proposed.append({
                "id": "AI-%d" % (len(proposed) + 1),
                "checklistId": cid,
                "decisionId": decision["decisionId"],
                "claim": decision["claim"],
                "fact": decision["fact"],
                "reason": decision["reason"],
                "evidenceIds": evidence_ids[:12],
                "clauseRefs": clause_refs,
                "missingMaterial": {key: str((decision["missingMaterial"] or {}).get(key) or "")[:200]
                                    for key in ("what", "fromWhom", "affects")},
                "evidenceVersionId": ev_id,
                "labels": {"citation": citation_note},
                "locked": True,
            })
        elif clause_reason:
            rejected.append({"claim": decision["claim"][:120], "reason": clause_reason,
                             "checklistId": cid})
        elif decision.get("unsoundFailConfirmation"):
            rejected.append({"claim": decision["claim"][:120],
                             "reason": "候选不得确认为须整改（R14/4.14 依据、办法第125条误挂、或制作表头315克被当成净含量冲突）",
                             "checklistId": cid})

    # Candidates that pointed at unknown ids are rejected, not silently dropped.
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        ids = [str(x) for x in (candidate.get("checklistIds") or candidate.get("itemIds") or []) if x]
        if ids and not any(i in checklist for i in ids):
            rejected.append({"claim": str(candidate.get("claim") or "")[:120],
                             "reason": "checklistId/itemIds 不在本次检查清单中"})

    for candidate in unbound_candidates:
        claim = str(candidate.get("claim") or "").strip()
        fact = str(candidate.get("fact") or "").strip()
        reason_text = str(candidate.get("reason") or "").strip()
        evidence_ids = [ref for ref in (candidate.get("evidenceIds") or []) if isinstance(ref, str)]
        refined = _validated_clause_refs(candidate.get("clauseRefs") or [],
                                         versions.get("bundleId") or prep.get("bundleId"))
        clause_refs, clause_reason = refined["clauseRefs"], refined["clauseReason"]
        citation_note = refined["label"]
        if not claim or not fact or not reason_text:
            rejected.append({"claim": claim[:120], "reason": "claim/fact/reason 缺失"})
            continue
        if not evidence_ids:
            rejected.append({"claim": claim[:120], "reason": "没有 evidenceIds"})
            continue
        if any(ref not in catalog_ids for ref in evidence_ids):
            rejected.append({"claim": claim[:120], "reason": "evidenceIds 不属于当前快照"})
            continue
        if clause_reason:
            rejected.append({"claim": claim[:120], "reason": clause_reason})
            continue
        proposed.append({
            "id": "AI-%d" % (len(proposed) + 1),
            "checklistId": None,
            "decisionId": "DEC-" + _digest({"unbound": claim, "evidenceVersionId": ev_id})[:12],
            "claim": claim[:200], "fact": fact[:500], "reason": reason_text[:800],
            "evidenceIds": evidence_ids[:12], "clauseRefs": clause_refs,
            "missingMaterial": {key: str(((candidate.get("missingMaterial") or {}) if isinstance(candidate.get("missingMaterial"), dict) else {}).get(key) or "")[:200]
                                for key in ("what", "fromWhom", "affects")},
            "evidenceVersionId": ev_id,
            "labels": {"citation": citation_note},
            "locked": True,
        })

    summary = {
        CHECKED: sum(1 for row in records if row.get("coverage") == CHECKED),
        UNCHECKED: sum(1 for row in records if row.get("coverage") == UNCHECKED),
        UNCOVERED: sum(1 for row in records if row.get("coverage") == UNCOVERED),
        APPLICABLE: sum(1 for row in records if row.get("applicability") == APPLICABLE),
        NOT_APPLICABLE: sum(1 for row in records if row.get("applicability") == NOT_APPLICABLE),
        INSUFFICIENT: sum(1 for row in records if row.get("applicability") == INSUFFICIENT),
        "passed": sum(1 for row in records if row.get("passed")),
    }
    return {
        "coverage": records,
        "lockedJudgments": locked,
        "candidates": proposed,
        "rejected": rejected,
        "evidenceVersionId": ev_id,
        "versions": versions,
        "snapshotId": prep.get("snapshotId"),
        "summary": summary,
        "parseError": parse_error,
    }


def writer_revision_rejected(judgment, writer_text, new_evidence=False, decisions=None):
    """Hook for Step 4: the writing agent must not silently overwrite a locked conclusion.

    撰稿把「须整改」写成「可以通过」且没有新证据 → 拒绝该段文案。
    When a DecisionSet is supplied (or attached on the judgment), the live
    apply_writer / reject_writer_revision path is used first.
    """
    if decisions is None and isinstance(judgment, dict):
        decisions = judgment.get("_decisionSet") or judgment.get("decisionSet")
    if decisions is not None and hasattr(decisions, "reject_writer_revision"):
        item_id = ""
        if isinstance(judgment, dict):
            item_id = str(judgment.get("itemId") or judgment.get("decisionId") or judgment.get("id") or "")
        if item_id:
            try:
                if decisions.reject_writer_revision(item_id, text=writer_text, new_evidence=new_evidence):
                    return "撰稿不得把已锁定的审核结论改成通过"
                return None
            except Exception:
                pass
    if not isinstance(judgment, dict) or not judgment.get("locked"):
        return None
    conclusion = str(judgment.get("conclusion") or "")
    text = str(writer_text or "")
    if conclusion in _NEED_ACTION and _PASS_LIKE.search(text) and not new_evidence:
        return "撰稿不得把已锁定的审核结论改成通过"
    if judgment.get("passAllowed") is False and re.search(r"通过|符合要求", text) and conclusion == "未发现问题":
        return "未发现问题不能写成确定通过"
    return None
