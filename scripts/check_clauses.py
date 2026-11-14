#!/usr/bin/env python3
"""条款库自检：knowledge/ 里记录的条款号与标题，必须与标准 PDF 原文一致。

存在的意义：报告内部的任何一致性检查，都查不出「库里的条款标题写错了」。
V2.6 之前的真实事故是 coverage.json 把 GB 7718-2025 4.1 写成
「一般要求（真实准确、不得虚假、不得误导）」——4.1 原文其实是「应标示的项目范围」，
「真实准确」在 3.4。这个错误直接导致 AI 把「报告引用了 4.1 讲真实性」当成事实，
产生三条误报。

用法：
    cd work && python3 scripts/check_clauses.py            # 全部检查
    python3 scripts/check_clauses.py --json                # 机器可读
    python3 scripts/check_clauses.py --strict              # 有差异即退出码 1

退出码：0 全部一致；1 存在差异（--strict 时）。
"""
import argparse
import json
import re
import sys
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = {
    "gb7718-2025": ROOT / "knowledge" / "documents" / "GB 7718-2025.pdf",
    "gb28050-2025": ROOT / "knowledge" / "documents" / "GB 28050-2025.pdf",
}
# 条款号 → 允许出现的标题关键词（任一命中即视为一致）
# 标题写法的差异（如「产品质量等级」vs「产品质量（品质）等级」）通过关键词容忍。
KEYWORDS = {
    "3.2": ("清晰", "醒目", "辨认"),
    "3.4": ("真实", "准确"),
    "4.1": ("一般要求",),
    "4.2": ("食品名称",),
    "4.3": ("配料表",),
    "4.4": ("配料强调", "定量"),
    "4.5": ("净含量", "规格"),
    "4.6": ("生产者", "经营者"),
    "4.7": ("日期",),
    "4.8": ("贮存条件",),
    "4.9": ("许可证",),
    "4.10": ("标准代号",),
    "4.11": ("质量", "等级"),
    "4.12": ("致敏",),
    "4.13": ("营养标签",),
    "4.15": ("辐照",),
    "6.2": ("醒目",),
}


def flatten(document):
    """把 PDF 全部页面拼成一段，去掉汉字之间的多余空格。"""
    reader = PdfReader(document)
    text = "".join((page.extract_text() or "") for page in reader.pages)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"\s+", " ", text)


def clause_window(text, number, size=90):
    """取条款号后面的一段原文，用于人工核对。"""
    for pattern in (f" {number} ", f" {number}"):
        index = text.find(pattern)
        while index > 0:
            following = text[index + len(pattern):index + len(pattern) + size].strip()
            if following:
                return following
            index = text.find(pattern, index + 1)
    return ""


def collect():
    """收集知识库里所有「条款号 + 标题」条目。"""
    entries = []
    coverage = ROOT / "knowledge" / "coverage.json"
    if coverage.is_file():
        data = json.loads(coverage.read_text(encoding="utf-8"))
        for number, row in (data.get("clauses") or {}).items():
            if isinstance(row, dict):
                entries.append({"source": "coverage.json", "clause": number,
                                "title": row.get("title") or "", "document": "gb7718-2025"})
    notes = ROOT / "knowledge" / "review_notes.json"
    if notes.is_file():
        data = json.loads(notes.read_text(encoding="utf-8"))
        for key, row in (data.get("notes") or {}).items():
            if not isinstance(row, dict):
                continue
            for number in re.findall(r"(\d+\.\d+(?:\.\d+)?)", str(row.get("clause") or "")):
                entries.append({"source": "review_notes.json", "id": key, "clause": number,
                                "title": row.get("title") or "",
                                "document": "gb28050-2025" if number.startswith("6.") else "gb7718-2025"})
    return entries


def check(entries):
    cache, results = {}, []
    for entry in entries:
        document = entry["document"]
        if document not in cache:
            cache[document] = flatten(DOCUMENTS[document]) if DOCUMENTS[document].is_file() else ""
        text = cache[document]
        if not text:
            results.append(dict(entry, status="missing-document", actual=""))
            continue
        actual = clause_window(text, entry["clause"])
        if not actual:
            results.append(dict(entry, status="clause-not-found", actual=""))
            continue
        words = KEYWORDS.get(entry["clause"][:4]) or KEYWORDS.get(entry["clause"], ())
        hit = any(word in actual[:60] for word in words) if words else None
        status = "ok" if hit else ("unverified" if hit is None else "mismatch")
        results.append(dict(entry, status=status, actual=actual[:70]))
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    results = check(collect())
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        bad = [row for row in results if row["status"] in ("mismatch", "clause-not-found", "missing-document")]
        print(f"条款条目 {len(results)} 条 · 一致 {sum(1 for r in results if r['status'] == 'ok')} · "
              f"存疑 {len(bad)} · 无关键词规则 {sum(1 for r in results if r['status'] == 'unverified')}\n")
        for row in results:
            mark = {"ok": "✓", "mismatch": "✗", "clause-not-found": "?", "missing-document": "?", "unverified": "·"}[row["status"]]
            label = row.get("id") or row.get("source", "")
            print(f"  {mark} {row['clause']:7s} {row['title'][:26]:28s} 原文：{row['actual'][:46]}")
            if row["status"] == "mismatch":
                print(f"      ↑ 「{label}」记录的标题与原文对不上")
    return 1 if (args.strict and any(r["status"] in ("mismatch", "clause-not-found") for r in results)) else 0


if __name__ == "__main__":
    sys.exit(main())
