#!/usr/bin/env python3
"""从授权知识库生成警示语依据表 knowledge/warnings.json。

只允许从 knowledge/manifest.json 已登记的授权文件里取内容：

  · 市场监管总局关于加强固体饮料质量安全监管的公告（2021 年第 46 号）
  · “三新食品”推荐食用量及适宜人群一览表

生成物是**数据**，不是条文正文的搬运：固体饮料只保留本项目用于判定「是否已标示」
的那一句警示信息与版式要求摘要；三新食品只保留原料名、推荐食用量、适宜人群三列。

用法：
    python3 scripts/build_warnings.py            # 生成并打印摘要
    python3 scripts/build_warnings.py --check    # 只校验，不写文件（供测试使用）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "knowledge" / "documents"
OUT = ROOT / "knowledge" / "warnings.json"

SOLID_DRINK_DOC = "市场监管总局关于加强固体饮料质量安全监管的公告.docx"
THREE_NEW_DOC = "“三新食品”推荐食用量及适宜人群一览表.docx"

BUILD_VERSION = "warnings-1"
NEWLINE = re.compile(r"\s+")


def _normalize(text):
    """去掉空白与标点，与 audit.warning_normalize 保持同一口径。"""
    return re.sub(r"[\s\u00a0\u3000。.,，、;；:：·（）()【】\[\]“”\"'‘’\-—－/]+", "", text or "")


def _document_xml(path):
    with zipfile.ZipFile(path) as archive:
        return archive.read("word/document.xml").decode("utf-8", "ignore")


def _cell_text(xml):
    text = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S))
    text = (text.replace("&amp;", "&").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'"))
    return NEWLINE.sub(" ", text.strip())


def paragraphs(path):
    xml = _document_xml(path)
    rows = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        text = _cell_text(para)
        if text:
            rows.append(text)
    return rows


def table_rows(path):
    xml = _document_xml(path)
    rows = []
    for tr in re.findall(r"<w:tr[ >].*?</w:tr>", xml, re.S):
        cells = [_cell_text(tc) for tc in re.findall(r"<w:tc[ >].*?</w:tc>", tr, re.S)]
        if any(cells):
            rows.append(cells)
    return rows


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


MANIFEST = ROOT / "knowledge" / "manifest.json"


def manifest_documents():
    """授权清单：文件名 → {id, sha256}。依据只允许来自这里登记的文件。"""
    try:
        raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SystemExit("读不到 knowledge/manifest.json，拒绝生成依据表。")
    rows = raw if isinstance(raw, list) else (raw.get("documents") or raw.get("files") or [])
    index = {}
    for row in rows:
        if isinstance(row, dict) and row.get("filename"):
            index[str(row["filename"])] = {"id": row.get("id") or row.get("documentId"),
                                           "sha256": row.get("sha256")}
    return index


def registered(path, manifest):
    """文件必须在授权清单里，且当前字节的 sha256 与清单一致。"""
    entry = manifest.get(path.name)
    if not entry or not entry.get("id"):
        raise SystemExit(f"{path.name} 不在授权清单里，拒绝把它当依据。")
    actual = _sha256(path)
    if entry.get("sha256") and entry["sha256"] != actual:
        raise SystemExit(f"{path.name} 的 sha256 与授权清单不一致，文件可能被替换，拒绝生成。")
    return {"documentId": entry["id"], "sourceSha256": actual}


def paragraph_index(rows, needle):
    """找到含 needle 的段落序号（1 起，与 knowledge._citation 的「正文 XML 第 N 段」一致）。"""
    for index, text in enumerate(rows, 1):
        if needle in text:
            return [index]
    return []


def solid_drink():
    path = DOCS / SOLID_DRINK_DOC
    if not path.is_file():
        raise SystemExit(f"缺少授权文件：{SOLID_DRINK_DOC}")
    manifest = manifest_documents()
    registry = registered(path, manifest)
    rows = paragraphs(path)
    text = "\n".join(rows)
    # 公告第三条给出的警示信息原文，必须逐字取自公告，不在这里手写。
    quote = re.search(r"“([^”]*不能代替特殊医学用途配方食品[^”]*)”", text)
    if not quote:
        raise SystemExit("公告里没有找到固体饮料警示信息原文，拒绝编造。")
    required = quote.group(1)
    # 「直接提供给消费者的蛋白固体饮料、植物固体饮料、特殊用途固体饮料、
    #   风味固体饮料，以及添加可食用菌种的固体饮料最小销售单元」
    kinds = re.search(r"直接提供给消费者的(.+?)最小销售单元", text, re.S)
    covered = []
    if kinds:
        for part in re.split(r"[、，,]|以及|和", kinds.group(1)):
            part = NEWLINE.sub("", part).strip()
            if part:
                covered.append(part)
    return {
        "attributeName": "固体饮料",
        "coveredKinds": covered,
        "requiredText": required,
        "requiredNormalized": _normalize(required),
        "layout": {
            "areaRatio": "所占面积不应小于其所在面的 20%",
            "font": "警示信息文字应使用黑体字印刷",
            "contrast": "与警示信息区域背景有明显色差",
            "verifiableFromPhoto": False,
            "note": "面积比例、字体、色差无法从普通照片判定，程序只校验文字是否可读，版式一律标未校验。",
        },
        "basis": {
            "document": "市场监管总局关于加强固体饮料质量安全监管的公告（2021 年第 46 号）",
            "documentFile": SOLID_DRINK_DOC,
            "documentId": registry["documentId"],
            "clause": "第三条",
            "paragraphs": paragraph_index(rows, "不能代替特殊医学用途配方食品"),
            "effective": "2022-06-01",
            "sourceSha256": registry["sourceSha256"],
        },
    }


def three_new():
    path = DOCS / THREE_NEW_DOC
    if not path.is_file():
        raise SystemExit(f"缺少授权文件：{THREE_NEW_DOC}")
    manifest = manifest_documents()
    registry = registered(path, manifest)
    rows = table_rows(path)
    header = rows[0] if rows else []
    if not header or "新食品原料" not in header[0]:
        raise SystemExit("三新食品表头与预期不一致，拒绝按猜测解析。")
    entries, seen = [], {}
    for row in rows[1:]:
        name = row[0].strip() if len(row) > 0 else ""
        intake = row[1].strip() if len(row) > 1 else ""
        suitable = row[2].strip() if len(row) > 2 else ""
        if not name:
            continue
        suitable = suitable.strip(".。、， ")
        key = (name, _normalize(intake), _normalize(suitable))
        if key in seen:
            continue
        seen[key] = True
        entries.append({
            "name": name,
            "nameNormalized": _normalize(name),
            "intake": intake,
            "suitable": suitable,
        })
    if not entries:
        raise SystemExit("三新食品没有解析出任何条目。")
    return {
        # 只声明「本表是线索检索来源」，不再断言整表都是推荐性资料。
        "nature": "index-only",
        "natureNote": (
            "该表提供原料名、推荐食用量、适宜人群三类信息，供检索线索使用。"
            "具体适用范围、使用条件、需标示内容及约束性质须回溯对应公告，当前未完成核定。"
            "程序命中后只转人工核对，不得自动判为「应标示未检出」。"
        ),
        "entries": entries,
        "basis": {
            "document": "“三新食品”推荐食用量及适宜人群一览表",
            "documentFile": THREE_NEW_DOC,
            "documentId": registry["documentId"],
            "clause": "全表",
            "paragraphs": paragraph_index(paragraphs(path), "推荐食用量") or [1],
            "sourceSha256": registry["sourceSha256"],
        },
    }


def build():
    return {
        "version": BUILD_VERSION,
        "generatedBy": "scripts/build_warnings.py",
        "scope": (
            "只用 knowledge/manifest.json 登记的授权文件。"
            "本表覆盖不到的法律法规、公告要求一律显示未覆盖，不得写成通过。"
        ),
        "states": {
            "located": "命中依据且标签上读到要求文字（版式未校验）",
            "missing": "命中依据但标签上读不到要求文字",
            "human": "命中依据，但依据只提供推荐性信息或适用性需人工判断",
            "uncovered": "没有命中任何已登记依据；本轮未覆盖，不等于合规",
        },
        "solidDrink": solid_drink(),
        "threeNew": three_new(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成警示语依据表")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    book = build()
    blob = json.dumps(book, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not OUT.is_file():
            print("warnings.json 不存在", file=sys.stderr)
            return 1
        current = json.loads(OUT.read_text(encoding="utf-8"))
        if json.dumps(current, ensure_ascii=False, sort_keys=True) != json.dumps(
                book, ensure_ascii=False, sort_keys=True):
            print("warnings.json 与授权文件不一致，需要重新生成", file=sys.stderr)
            return 1
        print("warnings.json 与授权文件一致")
        return 0

    OUT.write_text(blob, encoding="utf-8")
    print(f"写入 {OUT.relative_to(ROOT)}")
    print(f"  固体饮料警示信息：{book['solidDrink']['requiredText']}")
    print(f"  覆盖的固体饮料类别：{'、'.join(book['solidDrink']['coveredKinds']) or '（未解析到）'}")
    print(f"  三新食品条目：{len(book['threeNew']['entries'])} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
