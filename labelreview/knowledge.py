"""Read-only access to the approved local documents and verified rule citations.

Runtime reads only a published registry bundle. Clause text comes from the
hierarchical clause index, not from an unbounded span in index.json.
"""

from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import unicodedata


ROOT = Path(__file__).resolve().parent.parent / "knowledge"
DOCUMENTS = (ROOT / "documents").resolve()
EFFECTIVE_2025 = "2027-03-16"


def _load(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _documents():
    return {item["id"]: item for item in _load("manifest.json")["documents"]}


@lru_cache(maxsize=1)
def _rules():
    return _load("rules.json")


@lru_cache(maxsize=1)
def _index():
    return _load("index.json")


@lru_cache(maxsize=1)
def _grades():
    """标准代号 -> 食品名称：只管判定该标准是否规定质量等级。

    这份清单是可维护的数据表，不在代码里枚举标准号；文件缺失或格式无效时返回空表，
    审核只能得出「无法确认该标准是否分级」，不会替客户推定等级要求。
    """
    try:
        loaded = _load("grades.json")
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(key): str(value) for key, value in loaded.items() if str(key).strip()}


def grades():
    return dict(_grades())


def document_metadata(key):
    document = _documents().get(key) if isinstance(key, str) else None
    return dict(document) if document else None


def resolve_document(key):
    """Serve files by manifest ID, never by a user-provided filesystem path."""
    document = document_metadata(key)
    if not document:
        return None
    try:
        path = (ROOT / document["path"]).resolve(strict=True)
        if not path.is_relative_to(DOCUMENTS) or not path.is_file():
            return None
        if hashlib.sha256(path.read_bytes()).hexdigest() == document["sha256"]:
            return path
    except OSError:
        return None
    return None


def _citation(record):
    key = record["document"]
    document = document_metadata(key)
    if not document or resolve_document(key) is None:
        return None
    pages = record.get("pages", [])
    paragraphs = record.get("paragraphs", [])
    path = f"/knowledge/{key}"
    location = (
        "PDF 第 " + "、".join(map(str, pages)) + " 页"
        if pages else "正文 XML 第 " + "、".join(map(str, paragraphs)) + " 段"
    )
    return {
        "name": document["name"],
        "filename": document["filename"],
        "clause": record["clause"],
        "standardId": document["name"],
        "documentId": key,
        "url": f"{path}#page={pages[0]}" if pages else path,
        "localUrl": path,
        "page": pages[0] if pages else None,
        "pages": pages,
        "paragraphs": paragraphs,
        "location": location,
        "effectiveDate": document["effectiveDate"],
        "sha256": document["sha256"],
    }


def source(rule_id, version, bundle_id=None):
    """A rule gets a citation only when it is registered for its version.

    Citations come from the published registry, never from the root rules.json.
    """
    record = None
    try:
        from labelreview.registry import default_registry, active_bundle
        bundle_id = bundle_id or ((active_bundle() or {}).get("id"))
        record = default_registry().citation_record(rule_id, version, bundle_id)
    except Exception:
        record = None
    if not record:
        # Only used before a bundle exists (bootstrap). Still not root rules.json.
        rules = _rules()
        record = rules.get(str(version), {}).get(rule_id) or (rules.get("all") or {}).get(rule_id)
    if not record:
        return None
    citation = _citation(record)
    if not citation:
        return None
    related = [_citation(item) for item in record.get("related", [])]
    if not all(related):
        return None
    if related:
        citation["related"] = related
    citation["registryBundle"] = bundle_id
    return citation


CLAUSE_END = re.compile(r"\s\d{1,2}(?:\.\d+)+\.?\s|第[一二三四五六七八九十百零]+条")


def _tidy_quote(text):
    """整理原文语料的空白（PDF 抽取会在汉字之间插空格），不改动条款文字。"""
    text = unicodedata.normalize("NFKC", str(text))
    text = re.sub(r"(?<=[\u4e00-\u9fff、，。；：“”（）])\s+(?=[\u4e00-\u9fff、，。；：“”（）])", "", text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=64)
def resolve_name(label):
    """按「GB 28050-2025 4.1 …」这类引用行的标准名称前缀反查文档键；同名取最长匹配。"""
    text = str(label or "")
    best = None
    for key in _index():
        name = (document_metadata(key) or {}).get("name") or ""
        if not name or not text.startswith(name):
            continue
        # 同一标准可能同时有 docx 与 pdf 两份语料：优先用规则引用时使用的短键（gb7718-2025）。
        score = (len(name), 1 if not key.startswith("doc-") else 0)
        if best is None or score > best[0]:
            best = (score, key)
    return best[1] if best else None


# 页眉/页脚噪声：PDF 每页顶部的「GB 7718—2025」与页脚页码会被拼进条款句中，
# 例如「包装物、容器 1 GB 7718—2025\n分离」。
# 只认长破折号形式（标准引用一律用短横 GB 7718-2025），避免误删正文里的标准号。
# 连同前后空白与紧随的换行一起去掉，避免留下 "容器\n分离" 这种断行。
_PAGE_NOISE = re.compile(r"[ \t]*(?:\d{1,3}[ \t]*)?GB\s*(?:7718|28050)—2025[ \t]*\n?")


def _strip_page_noise(text):
    """去掉跨页拼接带进来的页眉与页码，不做任何其他删改。"""
    if not isinstance(text, str) or not text:
        return text
    cleaned = _PAGE_NOISE.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+\n", "\n", cleaned)
    return cleaned


_TIMING = {"clauseMs": 0.0, "calls": 0}


def reset_timing():
    _TIMING["clauseMs"] = 0.0
    _TIMING["calls"] = 0


def timing():
    return dict(_TIMING)


def clause(document, number):
    import time as _time
    _t0 = _time.perf_counter()
    try:
        return _clause(document, number)
    finally:
        _TIMING["clauseMs"] += (_time.perf_counter() - _t0) * 1000
        _TIMING["calls"] += 1


def _active_bundle_id(bundle_id=None):
    if bundle_id:
        return bundle_id
    try:
        from labelreview.registry import active_bundle, default_registry
        current = active_bundle()
        if current and current.get("id"):
            return current.get("id")
        return (default_registry().current() or {}).get("bundleId")
    except Exception:
        return None


def _apply_corrections(record, document, number, bundle_id):
    """Apply published parse typo corrections; never alter the original file."""
    if not record:
        return record
    try:
        from labelreview.registry import default_registry
        rows = default_registry().corrections(document, bundle_id)
    except Exception:
        rows = []
    applied = []
    text = record.get("text") or ""
    original = text
    for row in rows:
        if row.get("clause") not in {number, record.get("clause")} and row.get("sliceId") not in {record.get("id")}:
            continue
        if row.get("originalText") and row.get("correctedText") and row["originalText"] in text:
            text = text.replace(row["originalText"], row["correctedText"], 1)
            applied.append({
                "originalText": row.get("originalText"),
                "correctedText": row.get("correctedText"),
                "confirmedBy": row.get("confirmedBy"),
                "confirmedAt": row.get("confirmedAt"),
                "reason": row.get("reason"),
            })
    record = dict(record)
    record["originalText"] = original
    record["text"] = text
    record["textHash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    record["corrections"] = applied or record.get("corrections") or []
    return record


def _attach_conflict(record, document, number, bundle_id):
    if not record:
        return record
    try:
        from labelreview.registry import default_registry
        conflict = default_registry().conflict_for(document, bundle_id)
    except Exception:
        conflict = None
    if not conflict:
        return record
    clause_diff = next((row for row in (conflict.get("diffs") or []) if row.get("clause") == number), None)
    record = dict(record)
    record["conflict"] = {
        "standardPair": True,
        "agree": conflict.get("agree"),
        "diffCount": conflict.get("diffCount"),
        "clauseDiff": clause_diff,
        "left": conflict.get("left"),
        "right": conflict.get("right"),
        "message": "同一标准的 PDF 与 Word 内容存在差异，须先核对，不能随机取用其中一份。",
    } if (clause_diff or not conflict.get("agree")) else None
    if record["conflict"] and clause_diff:
        record["conflictReported"] = True
    return record


def _clause(document, number, bundle_id=None):
    """按条款号从已发布的层级条款索引取出原文。

    读取主条款时带回子条款、条件和例外；在条款边界结束，不把后文附录带入。
    返回 {text, location, page, paragraph, ...}；取不到时返回 None。
    只整理空白，不改写条款文字。模型解释不作为条款原文返回。
    """
    import labelreview.clause_index as clause_index
    if not isinstance(document, str) or not isinstance(number, str) or not number:
        return None
    number = clause_index.normalize_clause_number(number)
    bundle_id = _active_bundle_id(bundle_id)
    slices = None
    try:
        from labelreview.registry import default_registry, KnowledgePackMissing
    except Exception:
        default_registry = None
        KnowledgePackMissing = ()
    try:
        if default_registry:
            default_registry().ensure_bootstrap()
            slices = default_registry().clauses_for(document, bundle_id)
    except KnowledgePackMissing:
        # A bundle whose pinned pack is gone must fail loudly, not answer from another version.
        raise
    except Exception:
        slices = None
    if not slices:
        # Last resort: build from the authorized index (still not a random file pick).
        passages = _index().get(document)
        meta = document_metadata(document) or {"id": document}
        if not isinstance(passages, list):
            return None
        slices = clause_index.build_slices(document, meta, passages)
    record = clause_index.read_clause(slices, number, include_descendants=True)
    if not record:
        return None
    text = record.get("text") or ""
    if len(text) <= len(number):
        return None
    record = _apply_corrections(record, document, number, bundle_id)
    record = _attach_conflict(record, document, number, bundle_id)
    record["bundleId"] = bundle_id
    return record


def clause_for_standard(standard, number, bundle_id=None):
    """Read a clause by standard identity. If PDF and Word disagree, report the
    conflict and do not pick a file at random.
    """
    import labelreview.clause_index as clause_index
    number = clause_index.normalize_clause_number(number)
    bundle_id = _active_bundle_id(bundle_id)
    try:
        from labelreview.registry import default_registry
        links = default_registry().file_links(bundle_id)
    except Exception:
        links = []
    group = next((item for item in links if item.get("standardId") == standard
                  or standard in {item.get("standardId"), *(f.get("filename") for f in item.get("files") or [])}),
                 None)
    if not group:
        # Fall back to resolve_name.
        key = resolve_name(standard) or resolve_name(f"{standard} {number}")
        return _clause(key, number, bundle_id) if key else None
    files = group.get("files") or []
    readings = []
    for item in files:
        rec = _clause(item.get("documentId"), number, bundle_id)
        if rec:
            readings.append({**rec, "documentId": item.get("documentId"),
                             "filename": item.get("filename"), "format": item.get("format")})
    if not readings:
        return None
    if group.get("conflict"):
        diffs = ((group.get("pairDiff") or {}).get("diffs") or [])
        clause_diff = next((row for row in diffs if row.get("clause") == number), None)
        if clause_diff:
            return {
                "conflict": True,
                "message": "同一标准的 PDF 与 Word 在该条款上内容不一致，须先核对差异，不能随机取用其中一份。",
                "clause": number,
                "standardId": group.get("standardId"),
                "readings": readings,
                "diff": clause_diff,
                "text": None,
            }
    # Texts agree (or only one file has the clause): return a deterministic file,
    # preferring the short registered key (PDF) used by current rule citations.
    preferred = sorted(readings, key=lambda row: (0 if not str(row.get("documentId")).startswith("doc-") else 1,
                                                   str(row.get("documentId"))))[0]
    preferred = dict(preferred)
    preferred["conflict"] = False
    preferred["readings"] = readings
    return preferred


def library_documents(bundle_id=None):
    """Documents in the authorized / published library. No blanket 2011 exclusion."""
    import labelreview.clause_index as clause_index
    try:
        from labelreview.registry import default_registry
        docs = ((default_registry().load_bundle(bundle_id) or {}).get("knowledge") or {}).get("manifest", {}).get("documents")
    except Exception:
        docs = None
    docs = docs or list(_documents().values())
    return [item for item in docs if clause_index.keep_authorized_document(item)]


def clause_line(label, document, number):
    """把条款原文排成「标准 条款号　原文」一行；取不到原文时返回空串。"""
    record = clause(document, number)
    if not record:
        return ""
    body = re.sub(rf"^{re.escape(number)}[\s　]*", "", record["text"])
    return f"{label} {number}　{body}" if body else ""


def _fold(value):
    value = unicodedata.normalize("NFKC", value).lower()
    value = value.replace("—", "-").replace("–", "-").replace("－", "-")
    return re.sub(r"\s+", "", value)


def _article_number(match):
    number = int(match[1])
    if not 1 <= number <= 999:
        return match[0]
    digits = "零一二三四五六七八九"
    hundreds, rest = divmod(number, 100)
    tens, ones = divmod(rest, 10)
    chinese = digits[hundreds] + "百" if hundreds else ""
    if tens:
        chinese += (digits[tens] if hundreds or tens != 1 else "") + "十"
    elif hundreds and ones:
        chinese += "零"
    if ones:
        chinese += digits[ones]
    return "第" + chinese + "条"


def _query_terms(query):
    value = unicodedata.normalize("NFKC", query).lower().replace("—", "-").replace("–", "-")
    value = re.sub(r"(gb(?:/t)?)\s+(?=\d)", r"\1", value)
    value = re.sub(r"\s*-\s*(?=\d{4})", "-", value)
    value = re.sub(r"第\s*(\d{1,3})\s*条", _article_number, value)
    # Support a standard or title and a clause without requiring a space.
    value = re.sub(r"(gb(?:/t)?\d{3,6}(?:-\d{4})?)(?=(?:\d+\.|第|附录|[\u4e00-\u9fff]))", r"\1 ", value)
    value = re.sub(r"(?<=[\u4e00-\u9fff])(?=第[一二三四五六七八九十百\d]+条)", " ", value)
    return [_fold(term) for term in re.split(r"[\s,，、;；]+", value.strip()) if term]


@lru_cache(maxsize=1)
def _search_corpus():
    return {
        key: [(passage, _fold(passage["text"])) for passage in passages]
        for key, passages in _index().items()
    }


def _clause_heading(raw, term):
    if not re.fullmatch(r"(?:第[一二三四五六七八九十百零]+条|[a-z]?\d+(?:\.\d+)+)", term):
        return False
    return any(_fold(line).startswith(term) for line in raw.splitlines())


def _excerpt(raw, terms):
    compact = re.sub(r"\s+", " ", raw).strip()
    for term in terms:
        # Match inside original text so the excerpt stays a readable quotation.
        match = re.search(r"\s*".join(map(re.escape, term)), compact, re.IGNORECASE)
        if match:
            start = max(0, match.start() - 38)
            return ("…" if start else "") + compact[start:start + 200]
    return compact[:200]


def search(query):
    """Return up to 20 local keyword hits with an exact original locator."""
    if not isinstance(query, str):
        return []
    terms = _query_terms(query[:160])
    if not terms:
        return []
    hits = []
    corpus = _search_corpus()
    for key, document in _documents().items():
        title = _fold(document["filename"] + " " + document["name"])
        text_terms = [term for term in terms if term not in title]
        title_score = 12 * (len(terms) - len(text_terms))
        if not text_terms:
            first = corpus.get(key, [])[:1]
            hits.extend((title_score, key, passage) for passage, _ in first)
            continue
        matches = []
        for passage, folded in corpus.get(key, []):
            if all(term in folded for term in text_terms):
                count = sum(min(folded.count(term), 4) for term in text_terms)
                heading = 8 * sum(_clause_heading(passage["text"], term) for term in text_terms)
                pdf = 2 if passage.get("page") and title_score else 0
                matches.append((title_score + count + heading + pdf, key, passage))
        hits.extend(sorted(matches, key=lambda item: -item[0])[:4])
    result = []
    valid = {}
    for _, key, passage in sorted(hits, key=lambda item: -item[0]):
        if key not in valid:
            valid[key] = resolve_document(key) is not None
        if not valid[key]:
            continue
        metadata = _documents()[key]
        page = passage.get("page")
        path = f"/knowledge/{key}"
        result.append({
            "documentId": key,
            "name": metadata["name"],
            "filename": metadata["filename"],
            "location": passage["location"],
            "page": page,
            "paragraph": passage.get("paragraph"),
            "excerpt": _excerpt(passage["text"], terms),
            "url": f"{path}#page={page}" if page else path,
            "localUrl": path,
        })
        if len(result) == 20:
            break
    return result


CLAUSE_TOKEN = re.compile(r"(?:第[一二三四五六七八九十百零]+问|第[一二三四五六七八九十百零]+条|[A-Z]\.\d+(?:\.\d+)*|\d+(?:\.\d+)+)")


def clause_tokens(value):
    """从「4.4.1、4.4.1.2」「第 4.12.1 条」「附录 A.1、A.3」这类写法里取出单个条款号。"""
    text = re.sub(r"[（(][^（）()]*[）)]", "", str(value or ""))
    return list(dict.fromkeys(token for token in CLAUSE_TOKEN.findall(text)))


def lookup_clause(number, document=None, standard=None, bundle_id=None):
    """Exact clause-number lookup in the published index."""
    if standard and not document:
        return clause_for_standard(standard, number, bundle_id)
    if document:
        return _clause(document, number, bundle_id)
    # Search every published document for an exact number match.
    try:
        from labelreview.registry import default_registry
        docs = default_registry().all_clauses(bundle_id)
    except Exception:
        docs = {}
    hits = []
    for key in docs:
        record = _clause(key, number, bundle_id)
        if record:
            hits.append(record)
    return hits


def _clause_search_corpus(bundle_id=None):
    try:
        from labelreview.registry import default_registry
        return default_registry().all_clauses(bundle_id)
    except Exception:
        return {}


def search_clauses(query, mode="keyword", document=None, bundle_id=None, limit=20):
    """Exact clause-number, keyword, topic, or related-clause expansion. No vectors."""
    import labelreview.clause_index as clause_index
    query = str(query or "").strip()
    if not query:
        return []
    docs = _clause_search_corpus(bundle_id)
    if document:
        docs = {document: docs.get(document)} if document in docs else {}
    results = []
    if mode == "exact":
        number = clause_index.normalize_clause_number(query.split()[-1] if " " in query else query)
        standard = query.rsplit(" ", 1)[0] if " " in query else None
        found = lookup_clause(number, document=document, standard=standard if standard and document is None else None,
                              bundle_id=bundle_id)
        if isinstance(found, dict):
            found = [found]
        for item in found or []:
            if item.get("conflict") and item.get("text") is None:
                results.append(item)
            else:
                results.append(item)
    elif mode == "topic":
        for key, pack in docs.items():
            if not pack:
                continue
            for item in clause_index.topic_hits(pack, query)[:8]:
                results.append({**item, "documentId": key, "mode": "topic"})
    elif mode == "related":
        number = clause_index.normalize_clause_number(query.split()[-1] if query else "")
        for key, pack in docs.items():
            for item in clause_index.expand_related(pack, number):
                results.append({**item, "documentId": key, "mode": "related"})
    else:
        terms = re.split(r"[\s,，、;；]+", query)
        for key, pack in docs.items():
            if not pack:
                continue
            for item in clause_index.keyword_hits(pack, terms)[:8]:
                results.append({**item, "documentId": key, "mode": "keyword",
                                "excerpt": (item.get("title") or "") + " " + (item.get("text") or "")[:200]})
    return results[:limit]


def document_kept_despite_2011(document_id):
    """Helper for acceptance: GB 26687-2011 stays in the library."""
    import labelreview.clause_index as clause_index
    meta = document_metadata(document_id) or {}
    return clause_index.keep_authorized_document(meta) and document_id in _documents()
