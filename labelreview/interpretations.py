"""核定解释库：客户版关键句的唯一来源。

为什么需要这一层：
    标准原文只写"是什么"，不写"因此在报告里怎么讲"。此前报告的条款解释是手写散文，
    没有与原文逐字绑定，出现过错把 4.2.2 的字高比较方向写反的事故。

本模块的规则：
    1. **不改动标准原文**。原文另存于 knowledge/index.json，这里只引用并登记定位。
    2. 每条解释必须绑定：标准号、版本、条款号、原文（逐字）、来源位置。
    3. 必须完整保留适用条件与全部要求，不得只抄其中一句，也不得省略例外。
    4. 客户版关键句由 `clause_statement()` 生成，模型不得改写、交换主语或改变否定关系。

条目状态：
    verified   —— 已对照原文逐条核定，可用于客户版
    pending    —— 待核定，只能在工作台显示，不得进入客户报告
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _source_index():
    """取条款原文：走 knowledge.clause()，与报告引用的取法完全一致（含跨页清洗）。

    只读，不改动知识库文件。
    """
    import labelreview.knowledge as knowledge
    found = {}
    for key, entry in INTERPRETATIONS.items():
        record = knowledge.clause(entry["documentId"], entry["clause"])
        if record:
            found[entry["clause"]] = record["text"]
    return found


# 条款 → 核定解释。每条都必须能追溯到原文。
INTERPRETATIONS = {
    "gb7718-2025:4.2.2": {
        "documentId": "gb7718-2025",
        "edition": "2025",
        "clause": "4.2.2",
        # 适用条件：不是所有名称都管，只在含易误解词时适用。
        "applicability": (
            "在属性名称的同一展示版面同时标示了「新创名称」「奇特名称」「音译名称」"
            "「地区俚语名称」或「商标名称」中的一种或多种，**且上述名称中含有易使消费者误解、"
            "混淆食品属性的文字或词语**"
        ),
        "notApplicable": (
            "名称中不含易使消费者误解、混淆食品属性的文字或词语时，本条不适用；"
            "此时版面上的字高差异不受本条约束"
        ),
        # 四项要求，缺一不可；此前报告只讲字高且方向写反。
        "requirements": [
            {"aspect": "位置", "text": "在属性名称同一展示版面的临近位置标示"},
            {"aspect": "字高", "text": "使用不大于属性名称的字高（即被标示名称不得比属性名称更大）"},
            {"aspect": "字体", "text": "与属性名称相同的字体"},
            {"aspect": "颜色", "text": "与属性名称相同的颜色"},
        ],
        "directionNote": (
            "比较方向：被标示的那个名称的字高 ≤ 属性名称的字高。"
            "不是「属性名称不得大于该名称」，两者方向相反。"
        ),
        "sourceLocation": "knowledge/index.json 条款 4.2.2 条目",
        "status": "verified",
        "verifiedAt": "2026-11-10",
        "verifiedBy": "对照 GB 7718-2025 原文逐字核对",
    },
}


def entries():
    return dict(INTERPRETATIONS)


def get(reference):
    """reference 形如 "gb7718-2025:4.2.2"，或 {"documentId","edition","clause"}。"""
    if isinstance(reference, str):
        key = reference
    else:
        key = "%s:%s" % (reference.get("documentId"), reference.get("clause"))
        edition = str(reference.get("edition") or "")
        if edition and not key.endswith(":" + edition):
            key = "%s:%s" % (reference.get("documentId"), reference.get("clause"))
    return INTERPRETATIONS.get(key)


def source_text(reference):
    """取标准原文（不许改写）。找不到返回空串，调用方须视为未核定。"""
    entry = get(reference)
    if not entry:
        return ""
    index = _source_index()
    return index.get(entry["clause"], "")


def clause_statement(reference, condition_met, observed=""):
    """生成客户版关键句。

    condition_met：本条适用条件是否成立（由调用方依事实判断）。
    observed：本次实测到的情况，例如字高比值。

    条件不成立时，语句必须写成"本条不适用"，不能改写成本条的要求。
    """
    entry = get(reference)
    if not entry:
        return ""
    if entry.get("status") != "verified":
        return ""
    parts = []
    if condition_met:
        parts.append("适用条件成立：" + entry["applicability"].replace("**", "") + "。")
        for row in entry["requirements"]:
            parts.append("%s：%s。" % (row["aspect"], row["text"]))
        parts.append(entry["directionNote"])
    else:
        parts.append("本条不适用：" + entry["notApplicable"].replace("**", "") + "。")
        parts.append("适用条件为：" + entry["applicability"].replace("**", "") + "。")
    if observed:
        parts.insert(0, observed)
    return "".join(parts)


def check():
    """自检：每条解释必须能取到原文，且 status 合法。"""
    problems, index = [], _source_index()
    for key, entry in INTERPRETATIONS.items():
        if entry.get("status") not in ("verified", "pending"):
            problems.append("%s 的 status 非法" % key)
        text = index.get(entry["clause"], "")
        if not text:
            problems.append("%s 取不到原文，无法核对" % key)
        if entry.get("status") == "verified" and not text:
            problems.append("%s 标为 verified 但无原文核对依据" % key)
        if not entry.get("applicability"):
            problems.append("%s 缺适用条件" % key)
        if key.startswith("gb7718-2025:4.2.2") and len(entry.get("requirements") or []) != 4:
            problems.append("%s 要求项不足四项（位置/字高/字体/颜色）" % key)
    return problems


if __name__ == "__main__":
    issues = check()
    print("核定解释 %d 条" % len(INTERPRETATIONS))
    for key, entry in INTERPRETATIONS.items():
        text = source_text(key)
        print("  %-24s status=%s 原文 %d 字符" % (key, entry["status"], len(text)))
    if issues:
        print("\n自检问题：")
        for row in issues:
            print("  -", row)
    else:
        print("\n自检通过")
