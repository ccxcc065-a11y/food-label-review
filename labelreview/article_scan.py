"""Offline scanner for 第N条 article slices in a clause pack (blocker 1).

Independent of the slicer's heading logic: it recomposes each document from
the authorized source index and checks every article slice against genuine
article headings. Three defect classes:

  (a) out-of-range: the slice's article number exceeds the document's real
      maximum. The real maximum is the largest N such that genuine headings
      第一条 … 第N条 all exist, where a genuine heading sits at a line start
      and is not followed by a citation continuation (第M款、、、规定、的…).
  (b) cross-reference fragment: an article slice whose text is an in-text
      citation (「第N条第M款规定的…」「…第N条的规定…」) rather than the real
      article, i.e. its start is not a genuine heading or its text opens with a
      citation continuation.
  (c) duplicate article ids within one document.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict

import labelreview.clause_index as clause_index
CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
ARTICLE_ID = re.compile(r"^第([一二三四五六七八九十百零两]+)条$")
ARTICLE_TOKEN = re.compile(r"第([一二三四五六七八九十百零两]+)\s*条")
# Citation continuation right after 「第N条」.
CITATION_AFTER = re.compile(
    r"^[ \t\u3000\xa0]*(?:第[一二三四五六七八九十百零两]+[款项条]|[、，,至和及或]|或者|以及|规定|的规定|的|有关规定)"
)


def cn_to_int(text):
    text = str(text or "")
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total, section, number = 0, 0, 0
    for ch in text:
        if ch in CN_DIGITS:
            number = CN_DIGITS[ch]
        elif ch == "十":
            section += (number or 1) * 10
            number = 0
        elif ch == "百":
            section += (number or 1) * 100
            number = 0
        else:
            return None
    return total + section + number


def article_number(clause_id):
    match = ARTICLE_ID.match(str(clause_id or ""))
    return cn_to_int(match.group(1)) if match else None


def _line_start(whole, pos):
    head = whole[:pos].rstrip(" \t\u3000\xa0")
    return head == "" or head.endswith("\n")


def genuine_headings(whole):
    """[(pos, n)] for line-start, non-citation 第N条 tokens in the source."""
    found = []
    for match in ARTICLE_TOKEN.finditer(whole or ""):
        if not _line_start(whole, match.start()):
            continue
        if CITATION_AFTER.match(whole[match.end():match.end() + 16]):
            continue
        number = cn_to_int(match.group(1))
        if number:
            found.append((match.start(), number))
    return found


def real_max(whole):
    numbers = {n for _, n in genuine_headings(whole)}
    top = 0
    while top + 1 in numbers:
        top += 1
    return top


def text_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def scan_document(doc_id, document, passages):
    """Scan one pack document. Returns a dict with per-class findings."""
    whole, _ = clause_index.compose_document(passages or [])
    genuine = genuine_headings(whole)
    genuine_pos = {pos for pos, _ in genuine}
    top = real_max(whole)
    articles = [item for item in (document or {}).get("slices") or []
                if item.get("kind") == "article" and article_number(item.get("clause"))]
    counts = Counter(item["clause"] for item in articles)
    out_of_range, fragments, duplicates = [], [], []
    seen = set()
    for order, item in enumerate(articles):
        n = article_number(item["clause"])
        start = item.get("start")
        text = item.get("text") or ""
        row = {
            "id": item.get("id"), "documentId": doc_id, "clause": item["clause"], "n": n,
            "order": order, "start": start, "location": item.get("location"),
            "page": item.get("page"), "textHash": item.get("textHash") or text_hash(text),
            "fullTextHash": item.get("fullTextHash"), "first60": text[:60],
        }
        reasons = []
        if start is not None and start not in genuine_pos:
            reasons.append("start-not-genuine-heading")
        token = ARTICLE_TOKEN.match(text)
        if token and CITATION_AFTER.match(text[token.end():token.end() + 16]):
            reasons.append("text-opens-with-citation")
        if top and n > top:
            out_of_range.append({**row, "realMax": top})
        if reasons:
            fragments.append({**row, "reasons": reasons})
        if counts[item["clause"]] > 1:
            duplicates.append({**row, "occurrence": "repeat" if item["clause"] in seen else "first",
                               "copies": counts[item["clause"]]})
        seen.add(item["clause"])
    return {
        "documentId": doc_id,
        "name": (document or {}).get("name"),
        "articleSlices": len(articles),
        "distinctArticles": len(counts),
        "realMax": top,
        "genuineHeadings": len(genuine),
        "outOfRange": out_of_range,
        "fragments": fragments,
        "duplicateIds": sorted(clause for clause, c in counts.items() if c > 1),
        "duplicateRows": duplicates,
    }


def scan_pack(pack_documents, source_index):
    """Scan every document of a pack. pack_documents: {docId: built}."""
    per_doc = {}
    for doc_id, document in sorted((pack_documents or {}).items()):
        result = scan_document(doc_id, document, (source_index or {}).get(doc_id) or [])
        if result["articleSlices"]:
            per_doc[doc_id] = result
    totals = {
        "a_outOfRange": sum(len(r["outOfRange"]) for r in per_doc.values()),
        "b_fragments": sum(len(r["fragments"]) for r in per_doc.values()),
        "c_duplicateIds": sum(len(r["duplicateIds"]) for r in per_doc.values()),
        "c_duplicateSurplusSlices": sum(sum(1 for x in r["duplicateRows"] if x["occurrence"] == "repeat")
                                        for r in per_doc.values()),
    }
    suspects = {}
    for r in per_doc.values():
        for cls, rows in (("a", r["outOfRange"]), ("b", r["fragments"]),
                          ("c", [x for x in r["duplicateRows"] if x["occurrence"] == "repeat"])):
            for x in rows:
                key = (x["documentId"], x["clause"], x["order"])
                entry = suspects.setdefault(key, {**{k: x[k] for k in ("id", "documentId", "clause", "n", "order", "start", "location", "page", "textHash", "fullTextHash", "first60")}, "classes": []})
                if cls not in entry["classes"]:
                    entry["classes"].append(cls)
    return {"docsWithArticles": len(per_doc), "totals": totals,
            "suspectSlices": len(suspects),
            "suspects": sorted(suspects.values(), key=lambda s: (s["documentId"], s["order"])),
            "perDoc": per_doc}


__all__ = ["cn_to_int", "article_number", "genuine_headings", "real_max",
           "scan_document", "scan_pack", "CITATION_AFTER", "ARTICLE_TOKEN"]
