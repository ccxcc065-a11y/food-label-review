"""Hierarchical, bounded clause index built from the local document corpus.

The older reader stopped at the first dotted number, so a parent such as
GB 7718-2025 4.12 returned only the title, and an appendix clause such as
GB 28050-2025 A.3.2 ran on into later appendices. This module builds an
explicit heading tree and slices text at sibling / next-section boundaries.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from pathlib import Path


PARSE_VERSION = "clause-index-4"

# Label-general rules stay on the authorized 2025 editions. Other 2011
# standards in the authorized manifest (notably GB 26687-2011) are kept.
LABEL_GENERAL_STANDARDS = ("GB 7718", "GB 28050")
LABEL_GENERAL_EDITION = "2025"

_NUM_SPACES = re.compile(r"(?<=[0-9A-Za-z])\s*[.．]\s*(?=[0-9])")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_CJK_SPACE = re.compile(r"(?<=[\u4e00-\u9fff、，。；：“”（）])\s+(?=[\u4e00-\u9fff、，。；：“”（）])")

# High-confidence heading tokens, applied after number-space normalization.
_HEADING = re.compile(
    r"(?:"
    r"(附录\s*[A-Z])"
    r"|(?<![A-Za-z0-9.])([A-Z]\.\d+(?:\.\d+)*)(?![\d.])"
    r"|(?<![\d.A-Za-z])(\d+\.\d+(?:\.\d+)*)(?![\d.])"
    r"|(?<![\d.A-Za-z])(\d{1,2})(?=\s+[\u4e00-\u9fff])"
    r"|(第[一二三四五六七八九十百零]+\s*条)"
    r"|(第[一二三四五六七八九十百零]+\s*问)"
    r"|(（[一二三四五六七八九十]+）\s*[^。\n]{0,40}？)"
    r")"
)

_REF_BEFORE = re.compile(
    r"(?:见(?:式|附录)?|按(?:照)?|根据|依据|符合|执行|选用|参照|遵照|采用|适用|对应|"
    r"第[0-9A-Z.]+章(?:$|（|\()|式\s*\(|表)$"
)
# '式(' is a formula lead-in (式(A.1)); do not treat every word ending in 式
# (方式, 形式) as a reference, or 6.1 / A.1 after a short title are dropped.
_UNITS_AFTER = re.compile(
    r"^\s*(?:[%％]|月龄|微克|毫克|克|千克|千焦|毫升|升|质量|体积|厘米|毫米|"
    r"kJ|kcal|mg|μg|µg|mL|ml|cm|mm|g)(?![A-Za-z])"
)
_APPENDIX_REF_AFTER = re.compile(r"^\s*(?:中|的|见|及|和|或|、|，|,)")
# Mid-sentence clause citations: 「当标示4.1以外」「见表 A.1 中」「第5章」.
_CLAUSE_REF_AFTER = re.compile(r"^\s*(?:以外|除外|中|的|见|及|和|或|、|，|,|规定|执行|要求)")
_TABLE_BEFORE = re.compile(r"表$")
_PAREN_BEFORE = re.compile(r"[（(、，,]$")
_FALSE_CHAPTER_TITLE = re.compile(r"[章条节的是和或及]")

# 「第N条」 followed by a citation continuation is an in-text reference
# (「第十五条规定的…」「第七条第一款…」「第八条、第九条」「第七十三条的规定」),
# never the heading of an article of the current document.
_ARTICLE_CITATION_AFTER = re.compile(
    r"^[ \t\u3000\xa0]*(?:第[一二三四五六七八九十百零两]+[款项条]|[、，,至和及或]|或者|以及|规定|的规定|的|有关规定)"
)
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}

_EXPLICIT_REF = re.compile(
    r"(?:见|按|根据|依据|按照)?\s*(?:附录\s*)?([A-Z]\.\d+(?:\.\d+)*|\d+\.\d+(?:\.\d+)*)"
)
_TABLE_TITLE = re.compile(r"(表\s*[A-Z]?(?:\.\d+)?(?:\s*[^\n。]{0,40})?)")
_QA_SECTION = re.compile(r"(?:^|\n)\s*([一二三四五六七八九十百]+、[^\n]{1,80})")
_LONG_CLAUSE = 2500
_SPAN_TARGET = 1200


def normalize_clause_number(number):
    """Collapse '4. 12' / fullwidth dots into a canonical clause id."""
    text = unicodedata.normalize("NFKC", str(number or "")).strip()
    text = text.replace("．", ".").replace("—", "-").replace("–", "-")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"附录", "附录", text)
    return text


def normalize_numbers_in_text(text):
    text = unicodedata.normalize("NFKC", str(text or ""))
    # '4. 12. 1' -> '4.12.1'
    while True:
        updated = _NUM_SPACES.sub(".", text)
        if updated == text:
            break
        text = updated
    return text


# Page footers are often "5\nGB 28050—2025" or "5 GB 28050—2025".
# Only the long dash form is removed, so body citations with a short dash stay.
_PAGE_NOISE = re.compile(
    r"(?:[ \t]*\n)?[ \t]*(?:(?:\d{1,3}|[IVX]+)[ \t]*(?:\n[ \t]*)?)?"
    r"GB\s*(?:7718|28050)—2025[ \t]*\n?"
)


def _tidy_quote(text):
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"(?<=[\u4e00-\u9fff、，。；：“”（）])\s+(?=[\u4e00-\u9fff、，。；：“”（）])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_page_noise(text):
    if not isinstance(text, str) or not text:
        return text
    cleaned = _PAGE_NOISE.sub("\n", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def tidy_text(text, preserve_newlines=False):
    """Align with knowledge.py whitespace cleanup, plus clause-number spacing.

    preserve_newlines keeps line breaks so mid-page PDF headings (for example
    GB 28050-2025 chapter 7) still look like line-start headings. Stored
    clause text continues to go through _tidy_quote and is collapsed.
    """
    if not preserve_newlines:
        return normalize_numbers_in_text(_strip_page_noise(_tidy_quote(text)))
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = _strip_page_noise(text)
    text = re.sub(
        r"(?<=[\u4e00-\u9fff、，。；：“”（）])[ \t]+(?=[\u4e00-\u9fff、，。；：“”（）])",
        "",
        text,
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n[ \t]*", "\n", text)
    return normalize_numbers_in_text(text).strip()


def compose_document(passages):
    """Join passages into one string and remember original locations."""
    spans, parts, offset = [], [], 0
    for passage in passages or []:
        if not isinstance(passage, dict):
            continue
        text = tidy_text(passage.get("text"), preserve_newlines=True)
        if not text:
            continue
        spans.append({
            "start": offset,
            "end": offset + len(text),
            "page": passage.get("page"),
            "paragraph": passage.get("paragraph"),
            "location": passage.get("location"),
        })
        parts.append(text)
        offset += len(text) + 1
    return "\n".join(parts), spans


def location_at(spans, index):
    for span in spans:
        if span["start"] <= index < span["end"]:
            return {
                "page": span.get("page"),
                "paragraph": span.get("paragraph"),
                "location": span.get("location"),
            }
    return {"page": None, "paragraph": None, "location": None}


def _group(match):
    return next((group for group in match.groups() if group), "")


def _heading_kind(number):
    if number.startswith("附录"):
        return "appendix-title"
    if re.fullmatch(r"[A-Z]\.\d+(?:\.\d+)*", number):
        return "appendix-clause"
    if re.fullmatch(r"第[一二三四五六七八九十百零]+条", number):
        return "article"
    if re.fullmatch(r"第[一二三四五六七八九十百零]+问", number):
        return "qa"
    if number.startswith("（") and number.endswith("？"):
        return "qa"
    if re.fullmatch(r"\d{1,2}", number):
        return "chapter"
    if re.fullmatch(r"\d+\.\d+(?:\.\d+)*", number):
        return "clause"
    return "other"


def _is_descendant(parent, child):
    if not parent or not child or parent == child:
        return parent == child
    if parent.startswith("附录"):
        letter = parent.replace("附录", "").strip()
        return child == parent or child.startswith(letter + ".")
    return child.startswith(parent + ".")


def _active_appendix(headings, position):
    current = None
    for item in headings:
        if item["start"] >= position:
            break
        if item["kind"] == "appendix-title":
            current = item["number"].replace("附录", "").strip()
    return current


def _looks_like_short_title(text):
    body = re.sub(r"\s+", "", text or "")
    if not body or len(body) > 40:
        return False
    if re.search(r"[。；]", body):
        return False
    return True


def article_number_value(number):
    """第一百二十五条 -> 125; None when not an article id."""
    match = re.fullmatch(r"第([一二三四五六七八九十百零两]+)条", str(number or ""))
    if not match:
        return None
    section, digit = 0, 0
    for ch in match.group(1):
        if ch in _CN_DIGITS:
            digit = _CN_DIGITS[ch]
        elif ch == "十":
            section += (digit or 1) * 10
            digit = 0
        elif ch == "百":
            section += (digit or 1) * 100
            digit = 0
    return section + digit or None


def _enforce_article_sequence(accepted):
    """Article headings of one document form one ascending sequence.

    A 第N条 that repeats or goes back to an earlier number is a citation that
    happened to land at a line start (PDF line wrap), so it is dropped and its
    text stays in the article it belongs to. A forward jump is kept only when
    the following article heading continues from it (a genuine gap in the
    source) and the skipped number does not turn up later; otherwise it is a
    citation as well. The first article heading is kept unless 第一条 appears
    after it (excerpts may start mid-document). This is a general rule for every document; no
    article id or document id is special-cased.
    """
    articles = [index for index, item in enumerate(accepted) if item["kind"] == "article"]
    if not articles:
        return accepted
    values = {index: article_number_value(accepted[index]["number"]) for index in articles}
    drop = set()
    last = None
    for position, index in enumerate(articles):
        value = values[index]
        if value is None:
            drop.add(index)
            continue
        following = [values[i] for i in articles[position + 1:] if values[i] is not None]
        nxt = following[0] if following else None
        if last is None:
            # A first heading other than 第一条 is a citation when 第一条
            # itself turns up later.
            if value != 1 and 1 in following:
                drop.add(index)
                continue
        elif value <= last:
            drop.add(index)
            continue
        elif value != last + 1:
            # Jump: accept only if the sequence continues from here, and the
            # skipped number does not appear later (a later last+1 means this
            # one is a forward citation).
            if (nxt is None or nxt != value + 1) or (last + 1) in following:
                drop.add(index)
                continue
        last = value
    return [item for index, item in enumerate(accepted) if index not in drop]


def find_headings(whole):
    """Return accepted headings in document order."""
    accepted = []
    for match in _HEADING.finditer(whole or ""):
        raw = _group(match)
        number = normalize_clause_number(raw)
        if not number:
            continue
        kind = _heading_kind(number)
        start = match.start()
        before_raw = whole[max(0, start - 12):start]
        before = re.sub(r"\s+", "", before_raw)
        after = whole[match.end():match.end() + 16]

        if _REF_BEFORE.search(before) or _TABLE_BEFORE.search(before) or _PAREN_BEFORE.search(before):
            continue
        if after.startswith(")") or after.startswith("）"):
            continue
        if kind == "appendix-title" and _APPENDIX_REF_AFTER.match(whole[match.end():match.end() + 8]):
            continue
        # Table cells and measurements: 「维生素 B1 1.4 mg」, 「7.5% NRV」.
        if _UNITS_AFTER.match(after):
            continue
        if kind in {"clause", "appendix-clause"} and _CLAUSE_REF_AFTER.match(after):
            continue
        # 「第 5 章」and 「≤40cm 2 的食品」are not chapter headings.
        if kind == "chapter" and before.endswith("第"):
            continue

        # Appendix clauses (A.3.2) are headings only inside that appendix.
        if kind == "appendix-clause":
            letter = number.split(".", 1)[0]
            active = _active_appendix(accepted, start)
            if active != letter:
                continue

        # Citations of another instrument, e.g. 《食品安全法》第一百二十五条
        # inside 办法 第三十九条 / 第四十一条. The closing book-title mark is
        # not CJK, so the mid-sentence CJK-glue check would not drop these.
        # They are not headings of the current document.
        if kind == "article" and re.search(r"》\s*$", before_raw):
            continue

        # New paragraph / line-start headings are accepted even if the
        # previous line ended with CJK (section titles such as 第三节…).
        line_start = start == 0 or bool(re.search(r"\n\s*$", before_raw))

        # Articles (第N条) are headings only at a line start and only when not
        # followed by a citation continuation. Mid-line 「依照本法第N条…」
        # 「违反本办法第五条、第六条规定」 are references and stay inside the
        # article that contains them (no short-title exception for articles).
        if kind == "article":
            if not line_start:
                continue
            if _ARTICLE_CITATION_AFTER.match(whole[match.end():match.end() + 16]):
                continue
        # Mid-sentence dotted numbers: reject when glued to preceding CJK
        # unless the previous accepted heading left only a short title.
        if before and re.search(r"[\u4e00-\u9fff]$", before) and not line_start:
            if not (accepted and _looks_like_short_title(whole[accepted[-1]["end"]:start])):
                continue

        # Bare chapter numbers must be followed by a title-like Chinese phrase.
        if kind == "chapter":
            title = re.match(r"\s*([\u4e00-\u9fff]{2,30})", after)
            if not title or _FALSE_CHAPTER_TITLE.match(title.group(1)):
                continue

        accepted.append({
            "number": number,
            "raw": raw,
            "kind": kind,
            "start": start,
            "end": match.end(),
        })
    return _enforce_article_sequence(accepted)


def _clause_stop(headings, index, whole_len):
    current = headings[index]
    for later in headings[index + 1:]:
        if _is_descendant(current["number"], later["number"]):
            continue
        # Q&A items under a section title are not descendants by number;
        # a later sibling heading of the same/higher kind still stops us.
        return later["start"]
    return whole_len


def _related_refs(text, self_number):
    found = []
    for match in _EXPLICIT_REF.finditer(text or ""):
        number = normalize_clause_number(match.group(1))
        if number and number != self_number and number not in found:
            found.append(number)
    if re.search(r"免于|除外|不适用|可以不|可不", text or ""):
        if "exception" not in found:
            found.append("exception")
    return found


def _split_overlong(text, owner, loc, document_id, version, source_hash):
    if len(text) <= _LONG_CLAUSE:
        return []
    pieces, buf = [], ""
    parts = re.split(r"(?<=[。；])", text)
    for part in parts:
        if buf and len(buf) + len(part) > _SPAN_TARGET:
            pieces.append(buf.strip())
            buf = part
        else:
            buf += part
    if buf.strip():
        pieces.append(buf.strip())
    if len(pieces) <= 1:
        return []
    spans = []
    for i, piece in enumerate(pieces, 1):
        span_id = f"{document_id}:{owner}:span-{i}"
        spans.append(_slice_record(
            slice_id=span_id,
            document_id=document_id,
            document_version=version,
            number=f"{owner}#{i}",
            kind="subspan",
            title="",
            text=piece,
            location=loc,
            parent=owner,
            children=[],
            related=[owner],
            extra={"ownerClause": owner, "fullTextRef": f"{document_id}:{owner}"},
        ))
    return spans


def _title_of(number, text):
    body = re.sub(rf"^{re.escape(number)}\s*", "", text or "").strip()
    # First phrase / up to first period, short.
    chunk = re.split(r"[。；\n]", body, maxsplit=1)[0].strip()
    return chunk[:40]


def _hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _slice_record(slice_id, document_id, document_version, number, kind, title, text,
                  location, parent, children, related, extra=None, verification="parsed"):
    record = {
        "id": slice_id,
        "documentId": document_id,
        "documentVersion": document_version,
        "clause": number,
        "kind": kind,
        "title": title,
        "text": text,
        "location": location.get("location") if isinstance(location, dict) else location,
        "page": (location or {}).get("page") if isinstance(location, dict) else None,
        "paragraph": (location or {}).get("paragraph") if isinstance(location, dict) else None,
        "originalHash": _hash(text),
        "textHash": _hash(text),
        "parseVersion": PARSE_VERSION,
        "verificationStatus": verification,
        "parent": parent,
        "children": list(children or []),
        "related": [item for item in (related or []) if item and item != number],
        "corrections": [],
    }
    if extra:
        record.update(extra)
    return record


def _detect_tables(whole, headings, spans, document_id, version):
    """Keep each table as one slice: name, nearby header/row text, footnotes."""
    records = []
    for match in _TABLE_TITLE.finditer(whole or ""):
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        start = match.start()
        # Skip mid-sentence "见表 A.1"
        before = re.sub(r"\s+", "", whole[max(0, start - 4):start])
        if before.endswith("见") or before.endswith("按"):
            continue
        stop = len(whole)
        for heading in headings:
            if heading["start"] > start + 2:
                stop = heading["start"]
                break
        # Also stop at the next table title.
        nxt = _TABLE_TITLE.search(whole, match.end())
        if nxt and nxt.start() < stop:
            stop = nxt.start()
        text = whole[start:stop].strip()
        if len(text) < 8:
            continue
        footnotes = []
        for note in re.finditer(r"注[:：]\s*([^\n]{2,400})", text):
            footnotes.append(note.group(1).strip())
        loc = location_at(spans, start)
        records.append(_slice_record(
            slice_id=f"{document_id}:table:{_hash(title)[:10]}",
            document_id=document_id,
            document_version=version,
            number=title.split()[0] if title else "表",
            kind="table",
            title=title,
            text=text,
            location=loc,
            parent=None,
            children=[],
            related=[],
            extra={"tableName": title, "footnotes": footnotes},
        ))
    return records


def _detect_qa(whole, spans, document_id, version, standard_name):
    """One question + its complete answer, linked to any cited clause."""
    records = []
    # Pattern 1: （一）……？ answer until next （二） or 二、
    question = re.compile(
        r"(?:^|\n)\s*((?:（[一二三四五六七八九十]+）|[一二三四五六七八九十]+、|[0-9]+[、.．]|第[一二三四五六七八九十百零]+问)[^\n]{0,80}？?)"
    )
    starts = [(m.start(1), m.group(1).strip()) for m in question.finditer(whole or "")]
    # Prefer items that look like questions (contain ？ or 问 or start with （)
    starts = [item for item in starts if ("？" in item[1] or "问" in item[1] or item[1].startswith("（"))]
    for i, (pos, heading) in enumerate(starts):
        stop = starts[i + 1][0] if i + 1 < len(starts) else len(whole)
        # Don't swallow a later major section '三、' if this item didn't use that form
        text = whole[pos:stop].strip()
        if len(text) < 8:
            continue
        cited = [n for n in _related_refs(text, heading) if n != "exception"]
        loc = location_at(spans, pos)
        number = heading.split("？")[0][:40]
        records.append(_slice_record(
            slice_id=f"{document_id}:qa:{_hash(heading)[:10]}",
            document_id=document_id,
            document_version=version,
            number=number,
            kind="qa",
            title=heading[:80],
            text=text,
            location=loc,
            parent=None,
            children=[],
            related=cited,
            extra={"question": heading, "citedClauses": cited, "standardName": standard_name},
        ))
    return records


def build_slices(document_id, metadata, passages):
    """Build clause / table / Q&A slices for one document."""
    metadata = metadata or {}
    version = metadata.get("effectiveDate") or metadata.get("edition") or ""
    whole, spans = compose_document(passages)
    headings = find_headings(whole)
    slices = []
    by_number = {}

    for index, heading in enumerate(headings):
        stop = _clause_stop(headings, index, len(whole))
        own_end = headings[index + 1]["start"] if index + 1 < len(headings) else stop
        # Own text is up to the next heading (child or sibling); full text
        # (used when reading the main clause) includes descendants and stops
        # at the next non-descendant.
        own_text = _tidy_quote(whole[heading["start"]:own_end])
        full_text = _tidy_quote(whole[heading["start"]:stop])
        if not full_text:
            continue
        children = [item["number"] for item in headings[index + 1:]
                    if _is_descendant(heading["number"], item["number"])
                    and item["number"] != heading["number"]]
        # Direct children only in the children field; deeper kept via related.
        direct = [child for child in children
                  if child.rsplit(".", 1)[0] == heading["number"]
                  or (heading["kind"] == "appendix-title"
                      and child.startswith(heading["number"].replace("附录", "").strip() + ".")
                      and child.count(".") == 1)]
        parent = None
        for prev in reversed(headings[:index]):
            if _is_descendant(prev["number"], heading["number"]):
                parent = prev["number"]
                break
        loc = location_at(spans, heading["start"])
        related = _related_refs(own_text, heading["number"])
        for child in direct:
            if child not in related:
                related.append(child)
        if parent:
            related.append(parent)
        record = _slice_record(
            slice_id=f"{document_id}:{heading['number']}",
            document_id=document_id,
            document_version=version,
            number=heading["number"],
            kind=heading["kind"],
            title=_title_of(heading["number"], own_text),
            text=own_text,
            location=loc,
            parent=parent,
            children=direct,
            related=related,
            extra={"fullText": full_text, "fullTextHash": _hash(full_text),
                   "start": heading["start"], "stop": stop, "ownEnd": own_end},
        )
        slices.append(record)
        by_number[heading["number"]] = record
        slices.extend(_split_overlong(own_text, heading["number"], loc, document_id, version,
                                      record["originalHash"]))

    slices.extend(_detect_tables(whole, headings, spans, document_id, version))
    name = (metadata.get("name") or "")
    if "问答" in name or "问" in (metadata.get("filename") or ""):
        slices.extend(_detect_qa(whole, spans, document_id, version, name))

    return {
        "documentId": document_id,
        "name": metadata.get("name"),
        "filename": metadata.get("filename"),
        "sha256": metadata.get("sha256"),
        "format": metadata.get("format"),
        "effectiveDate": metadata.get("effectiveDate"),
        "parseVersion": PARSE_VERSION,
        "passageCount": len(passages or []),
        "clauseCount": sum(1 for item in slices if item["kind"] in
                           {"clause", "chapter", "appendix-title", "appendix-clause", "article"}),
        "slices": slices,
    }


def read_clause(document_slices, number, include_descendants=True):
    """Read one clause. Parents include subclauses, conditions and exceptions."""
    if not document_slices or not number:
        return None
    number = normalize_clause_number(number)
    slices = document_slices.get("slices") if isinstance(document_slices, dict) else document_slices
    if not isinstance(slices, list):
        return None
    record = next((item for item in slices if item.get("clause") == number), None)
    if not record:
        return None
    text = record.get("fullText") if include_descendants and record.get("fullText") else record.get("text")
    related = [item for item in slices
               if item.get("clause") in set(record.get("children") or [])
               or item.get("clause") in set(record.get("related") or [])
               or item.get("parent") == number]
    return {
        "text": text,
        "ownText": record.get("text"),
        "location": record.get("location"),
        "page": record.get("page"),
        "paragraph": record.get("paragraph"),
        "clause": number,
        "documentId": record.get("documentId"),
        "documentVersion": record.get("documentVersion"),
        "kind": record.get("kind"),
        "title": record.get("title"),
        "originalHash": record.get("originalHash"),
        "textHash": _hash(text or ""),
        "parseVersion": record.get("parseVersion"),
        "verificationStatus": record.get("verificationStatus"),
        "related": record.get("related") or [],
        "children": record.get("children") or [],
        "parent": record.get("parent"),
        "relatedSlices": [{
            "clause": item.get("clause"),
            "kind": item.get("kind"),
            "title": item.get("title"),
            "text": item.get("text"),
            "location": item.get("location"),
        } for item in related if item.get("clause") != number],
        "corrections": record.get("corrections") or [],
        "conflict": None,
    }


def fold_for_compare(text):
    value = unicodedata.normalize("NFKC", text or "")
    value = normalize_numbers_in_text(value)
    value = value.replace("—", "-").replace("–", "-").replace("－", "-")
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[“”\"'‘’]", "", value)
    return value


def compare_file_pair(left, right):
    """Compare clause texts of the same standard in two files (typically PDF vs Word).

    Returns agreement plus a list of differing / missing clauses. Callers must
    not pick a side at random when disagreements exist.
    """
    def by_clause(doc):
        mapping = {}
        for item in (doc or {}).get("slices") or []:
            if item.get("kind") in {"subspan", "table"}:
                continue
            mapping[item["clause"]] = item
        return mapping

    a, b = by_clause(left), by_clause(right)
    numbers = sorted(set(a) | set(b), key=lambda value: (len(value), value))
    diffs = []
    for number in numbers:
        left_item, right_item = a.get(number), b.get(number)
        if left_item and not right_item:
            diffs.append({"clause": number, "kind": "missing-in-right",
                          "leftExcerpt": (left_item.get("ownText") or left_item.get("text") or "")[:200],
                          "rightExcerpt": ""})
            continue
        if right_item and not left_item:
            diffs.append({"clause": number, "kind": "missing-in-left",
                          "leftExcerpt": "",
                          "rightExcerpt": (right_item.get("ownText") or right_item.get("text") or "")[:200]})
            continue
        left_text = left_item.get("fullText") or left_item.get("text") or ""
        right_text = right_item.get("fullText") or right_item.get("text") or ""
        if fold_for_compare(left_text) != fold_for_compare(right_text):
            diffs.append({"clause": number, "kind": "text-mismatch",
                          "leftExcerpt": left_text[:200], "rightExcerpt": right_text[:200],
                          "leftHash": _hash(left_text), "rightHash": _hash(right_text)})
    return {
        "left": {"documentId": (left or {}).get("documentId"),
                 "filename": (left or {}).get("filename"),
                 "format": (left or {}).get("format")},
        "right": {"documentId": (right or {}).get("documentId"),
                  "filename": (right or {}).get("filename"),
                  "format": (right or {}).get("format")},
        "agree": not diffs,
        "diffCount": len(diffs),
        "diffs": diffs,
    }


def identify_standard(filename, text="", metadata=None):
    """Identify standard identity and edition from a file name / opening text."""
    metadata = metadata or {}
    blob = " ".join([
        str(filename or ""),
        str(metadata.get("name") or ""),
        (text or "")[:800],
    ])
    blob = unicodedata.normalize("NFKC", blob).replace("—", "-").replace("–", "-")
    match = re.search(r"(GB(?:/T)?)\s*([0-9]{3,6})\s*-\s*([0-9]{4})", blob, re.I)
    if match:
        code = f"{match.group(1).upper()} {match.group(2)}-{match.group(3)}"
        return {
            "standardId": code,
            "family": f"{match.group(1).upper()} {match.group(2)}",
            "edition": match.group(3),
            "name": metadata.get("name") or code,
            "isLabelGeneral": f"{match.group(1).upper()} {match.group(2)}" in {
                "GB 7718", "GB 28050",
            },
        }
    name = metadata.get("name") or Path(str(filename or "")).stem
    return {
        "standardId": name,
        "family": name,
        "edition": metadata.get("effectiveDate") or "",
        "name": name,
        "isLabelGeneral": False,
    }


def keep_authorized_document(metadata):
    """Authorized-manifest documents are kept. Do not drop a file just because
    the name contains '2011'. Label-general rules still use the 2025 editions.
    """
    if not metadata:
        return False
    identity = identify_standard(metadata.get("filename"), metadata=metadata)
    if identity["isLabelGeneral"] and identity["edition"] and identity["edition"] != LABEL_GENERAL_EDITION:
        # 7718/28050 2011 editions are not the live label-general basis, but a
        # file already in the authorized 35-document manifest is still retained
        # in the library (none of those 35 are 7718/28050 2011).
        return True
    return True


def is_blanket_2011_exclusion(metadata):
    """True only if a naive '2011' filename filter would drop this document."""
    text = " ".join([
        str((metadata or {}).get("filename") or ""),
        str((metadata or {}).get("name") or ""),
        str((metadata or {}).get("id") or ""),
    ])
    return "2011" in text


def group_standard_files(documents):
    """Group PDF and Word files of the same standard identity + edition."""
    groups = defaultdict(list)
    for item in documents or []:
        identity = identify_standard(item.get("filename"), metadata=item)
        key = identity["standardId"] or identity["name"]
        groups[key].append({**item, "identity": identity})
    packed = []
    for key, files in groups.items():
        formats = {str(item.get("format") or Path(str(item.get("filename") or "")).suffix.lower().lstrip("."))
                   for item in files}
        packed.append({
            "standardId": key,
            "files": files,
            "hasPdf": "pdf" in formats,
            "hasWord": "docx" in formats or "doc" in formats,
        })
    return packed


def ingest_qa(path, metadata, passages, sibling=None):
    """Quality checks for an imported authorized file. Upload stays 待审核."""
    issues = []
    path = Path(path) if path else None
    texts = [tidy_text(item.get("text")) for item in passages or [] if item.get("text")]
    joined = "\n".join(texts)
    identity = identify_standard(metadata.get("filename") if metadata else (path.name if path else ""),
                                 joined, metadata)
    # Duplicates are reported by the caller with the known hash set.
    if metadata and metadata.get("duplicateOf"):
        issues.append({"code": "duplicate", "level": "error",
                       "detail": f"与已入库文件 {metadata['duplicateOf']} 哈希相同。"})
    format_ = (metadata or {}).get("format") or (path.suffix.lower().lstrip(".") if path else "")
    if format_ == "pdf":
        pages = [item for item in passages or [] if item.get("page")]
        expected = max((item.get("page") or 0) for item in pages) if pages else 0
        found = {item.get("page") for item in pages}
        missing = [number for number in range(1, expected + 1) if number not in found]
        if missing:
            issues.append({"code": "missing-pages", "level": "warning",
                           "detail": "缺页或空白页：" + "、".join(map(str, missing[:20]))})
        if expected and len(pages) < max(1, expected // 3):
            issues.append({"code": "sparse-pages", "level": "warning",
                           "detail": f"PDF 共 {expected} 页，仅解析出 {len(pages)} 页文本。"})
    if not joined.strip():
        issues.append({"code": "empty-parse", "level": "error", "detail": "未能解析出任何正文。"})
    else:
        replacement = joined.count("\ufffd") + joined.count("□")
        cjk = len(re.findall(r"[\u4e00-\u9fff]", joined))
        singles = len(re.findall(r"(?m)^[A-Za-z0-9]$", joined))
        if replacement > 5 or (len(joined) > 80 and cjk / max(len(joined), 1) < 0.08 and "GB" in (metadata or {}).get("name", "GB")):
            issues.append({"code": "garbled-text", "level": "warning",
                           "detail": "解析文本可能乱码或几乎不含汉字。"})
        if singles > 80:
            issues.append({"code": "garbled-text", "level": "warning",
                           "detail": "存在大量单字符行，可能是逐字抽取。"})
    if re.search(r"表\s*[A-Z0-9.]", joined) and "\t" not in joined and not re.search(r"表[A-Z0-9.].{0,8}\n", joined):
        issues.append({"code": "table-parse", "level": "info",
                       "detail": "原文含表，已按整表切片；行列关系仍须对照原文复核。"})
    result = {
        "status": "待审核",
        "identity": identity,
        "issueCount": len(issues),
        "issues": issues,
        "passageCount": len(passages or []),
        "textLength": len(joined),
        "sha256": (metadata or {}).get("sha256") or (hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else ""),
    }
    if sibling:
        result["pairDiff"] = sibling
    return result


def expand_related(document_slices, number):
    record = read_clause(document_slices, number, include_descendants=True)
    if not record:
        return []
    return [record] + (record.get("relatedSlices") or [])


def keyword_hits(document_slices, terms):
    hits = []
    folded_terms = [fold_for_compare(term) for term in terms if term]
    if not folded_terms:
        return hits
    for item in (document_slices or {}).get("slices") or []:
        hay = fold_for_compare((item.get("fullText") or item.get("text") or "") + (item.get("title") or ""))
        if all(term in hay for term in folded_terms):
            hits.append(item)
    return hits


def topic_hits(document_slices, topic):
    """Light topic match: clause title / kind plus keyword overlap. No vectors."""
    topic = str(topic or "").strip()
    if not topic:
        return []
    terms = [part for part in re.split(r"[\s,，、]+", topic) if part]
    scored = []
    for item in (document_slices or {}).get("slices") or []:
        title = item.get("title") or ""
        blob = (title + " " + (item.get("text") or ""))[:800]
        score = 0
        for term in terms:
            if term and term in title:
                score += 4
            elif term and term in blob:
                score += 1
        if score:
            scored.append((score, item))
    scored.sort(key=lambda row: -row[0])
    return [item for _, item in scored]


__all__ = [
    "PARSE_VERSION", "LABEL_GENERAL_STANDARDS", "LABEL_GENERAL_EDITION",
    "normalize_clause_number", "normalize_numbers_in_text", "tidy_text",
    "compose_document", "find_headings", "build_slices", "read_clause",
    "compare_file_pair", "identify_standard", "keep_authorized_document",
    "is_blanket_2011_exclusion", "group_standard_files", "ingest_qa",
    "expand_related", "keyword_hits", "topic_hits", "fold_for_compare",
]
