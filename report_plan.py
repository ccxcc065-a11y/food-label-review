"""唯一整改计划（V2.7 阶段 1）。

规则发现 → `build()` 生成 reportPlan；渲染层只读计划。
07 节不再重新组装标签，而是在「标签文档副本」上应用已确认动作；定位失败逐条记录并阻止导出。

计划结构（schemaVersion 1）：

    {"schemaVersion": 1, "snapshotId": "...",
     "items": [{"itemId", "actionId", "outcome", "decisionState",
                "clauseRefs": [{"documentId","edition","clause","textHash"}],
                "edit": {...} | None, "evidenceRefs": [...], "options": [...], "basis", "title"}],
     "document": {"nodes": [...]},
     "revision": {"sections": [...], "applied": [...], "failures": [...], "canExport": bool}}

硬规则：
- `replace_text` 只在指定节点内匹配；同一原文在节点内必须唯一，否则记录失败。
- 全角/半角、空格、换行差异按字符级规范化匹配，但保留到原文的索引对应。
- 料包换序用 `reorder_nodes`，校验节点集合完整。
- 多动作修改同一节点先检查冲突，后者记录失败而不是覆盖。
- 应用是幂等的：同一计划重复应用不会二次插入顿号或提示语。
"""
import copy
import hashlib
import json
import re
import unicodedata

SCHEMA_VERSION = 1

PACK_HEAD = re.compile(r"(?:^|[。；;\n])\s*([\u4e00-\u9fff]{1,12}包[A-Z]?)\s*[:：]")
PACK_WEIGHT = re.compile(r"(?:^|[、,;(])\s*([\u4e00-\u9fff]{1,9}包)\s*(\d+(?:\.\d+)?)\s*(?:克|g)", re.I)
SUPPLIER = re.compile(r"^(?:被委托商|被委托方|受委托方)\s*([A-Z])\s*[:：]")
SUPPLIER_HEAD = re.compile(r"^(?:被委托商|被委托方|受委托方)\s*[A-Z]\s*[:：]")
LICENSE_LINE = re.compile(r"食品(?:生产)?许可证编号\s*[:：]\s*SC\d{14}")
CHILD_PROMPT = re.compile(r"儿童|青少年|孕妇|婴幼儿")
# 必填项缺失时沿用原稿占位写法，避免 07 出现「配料表：」后空白
PLACEHOLDER = {
    "ingredients": "〔按确认的完整配方填写，复合配料与各料包按适用口径列示〕",
    "net": "〔填写确认的净含量与规格〕",
    "production": "〔按适用规则填写批次日期或标出可定位的实际喷码部位〕",
    "storage": "〔填写生产方核定的贮存条件〕",
    "producer": "〔按生产或进口责任主体的有效资料填写〕",
    "address": "〔按对应责任主体的有效资料填写〕",
}
ALLERGEN_LINE = re.compile(r"致敏|过敏")
WARNING_HEAD = re.compile(r"^温馨提示\s*[：:]?\s*")
FIELDS = (
    ("type", "产品类型"), ("ingredients", "配料表"), ("net", "净含量"),
    ("standard", "产品执行标准"), ("license", "食品生产许可证编号"),
    ("production", "生产日期"), ("expiry", "保质期到期日"), ("shelf", "保质期"),
    ("storage", "贮存条件"), ("producer", "责任主体名称"), ("address", "地址"),
    ("origin", "产地"), ("country", "原产国（地区）"), ("contact", "联系方式"),
    ("usage", "食用方法"), ("warning", "温馨提示"), ("allergen", "致敏物质提示"),
)


# ---------------------------------------------------------------- 文本工具

def _flat(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")))


def _normalize_map(text):
    """把原文逐字符规范化，返回 (规范化文本, 原文索引表)。用于把匹配位置映射回原文。"""
    pieces, origin = [], []
    for index, char in enumerate(text):
        folded = unicodedata.normalize("NFKC", char)
        if char.isspace():
            continue
        pieces.append(folded)
        origin.extend([index] * len(folded))
    return "".join(pieces), origin


def _find_unique(text, needle):
    """在 text 中按规范化匹配 needle，返回 (start, end) 或 None；多处命中视为不可定位。"""
    haystack, origin = _normalize_map(text)
    target = _flat(needle)
    if not target or len(target) > len(haystack):
        return None
    first = haystack.find(target)
    if first < 0 or haystack.find(target, first + 1) >= 0:
        return None
    return origin[first], origin[first + len(target) - 1] + 1


def _split_packs(ingredients):
    text = str(ingredients or "")
    marks = list(PACK_HEAD.finditer(text))
    packs = []
    for index, mark in enumerate(marks):
        start = mark.start(1)
        end = marks[index + 1].start(1) if index + 1 < len(marks) else len(text)
        segment = text[start:end].strip().strip("。；;，, ")
        name = mark.group(1)
        body = re.sub(r"^" + re.escape(name) + r"\s*[:：]\s*", "", segment)
        if body:
            packs.append({"name": name, "text": body})
    return packs


def _text_blocks(report):
    blocks = []
    for page in report.pages:
        blocks.extend(page)
    return blocks


def _block_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _ocr_value(blocks):
    return "".join(_block_text(block.text) for block in blocks)


def _column_text(blocks):
    """把同一列、相邻行的 OCR 块拼回一段（用于被切碎的温馨提示）。"""
    if not blocks:
        return ""
    ordered = sorted(blocks, key=lambda block: (block.y, block.x))
    return "".join(_block_text(block.text) for block in ordered)


def _warning_blocks(report):
    """定位温馨提示正文块：标题块或「温馨提示：」段落，取同列其下方的连续行。"""
    blocks = _text_blocks(report)
    heading = next((block for block in blocks if WARNING_HEAD.match(_block_text(block.text))
                    and len(_block_text(block.text)) <= 80), None)
    if not heading:
        return [], ""
    if WARNING_HEAD.match(_block_text(heading.text)) and len(_block_text(heading.text)) > 5:
        return [heading], WARNING_HEAD.sub("", _block_text(heading.text))
    picked = [heading]
    for block in sorted((block for block in blocks if block.y > heading.y), key=lambda block: block.y):
        if abs(block.x - heading.x) > .03:
            continue
        if block.y - picked[-1].y > .02:
            break
        picked.append(block)
    return picked, _column_text(picked[1:])


def _bold_words(report, text):
    """节点内已确认加粗的配料词：保留原稿的致敏强调等信息。"""
    words = []
    for word in re.split(r"[、,，;；。:：()（）\s]+", str(text or "")):
        word = word.strip()
        if len(word) < 2 or word in words:
            continue
        try:
            if report.bold_word(word) is True:
                words.append(word)
        except Exception:
            continue
    return words


# ---------------------------------------------------------------- 标签文档

def build_document(report, draft):
    """从提取字段 + OCR 证据建立带稳定节点 ID 的标签文档；原值与校对值分开保存。"""
    fields = draft.get("fields") if isinstance(draft, dict) else {}
    fields = fields if isinstance(fields, dict) else {}
    value = lambda key: str((fields.get(key) or {}).get("value") or "").strip()
    refs = lambda key: list((fields.get(key) or {}).get("refs") or [])
    blocks = _text_blocks(report)
    by_id = {block.id: block for block in blocks}

    nodes, index = [], {}

    def add(node_id, kind, heading, text, node_refs=(), ocr="", bold=None):
        node = {"id": node_id, "kind": kind, "heading": heading, "value": str(text or ""),
                "ocrValue": str(ocr or ""), "refs": [ref for ref in node_refs if ref in by_id],
                "bold": list(bold or [])}
        nodes.append(node)
        index[node_id] = node
        return node

    add("name", "name", "", value("name"), refs("name"), _ocr_value([by_id[ref] for ref in refs("name") if ref in by_id]))
    add("type", "type", "产品类型", value("type"))
    ingredients = add("ingredients", "ingredients", "配料表",
                      value("ingredients") or PLACEHOLDER["ingredients"],
                      refs("ingredients"), _ocr_value([by_id[ref] for ref in refs("ingredients") if ref in by_id]))
    for pack in _split_packs(value("ingredients")):
        pack_refs = [block.id for block in blocks if pack["name"] in _block_text(block.text)]
        node = add("ingredients/" + pack["name"], "pack", pack["name"], pack["text"], pack_refs,
                   _ocr_value([by_id[ref] for ref in pack_refs]),
                   [word for word in _bold_words(report, pack["text"]) if word != pack["name"]])
        node["parent"] = "ingredients"
    net_value = value("net")
    if value("packs"):
        net_value = f"{net_value}（{value('packs')}）" if net_value else value("packs")
    add("net", "net", "净含量", net_value or PLACEHOLDER["net"], refs("net") + refs("packs"))
    add("standard", "standard", "产品执行标准", value("standard"), refs("standard"))
    add("license", "license", "食品生产许可证编号", value("license"), refs("license"))
    production, expiry = value("production"), value("expiry")
    if production and production == expiry:
        add("production", "production", "生产日期／保质期到期日",
            production or PLACEHOLDER["production"], refs("production"))
    else:
        add("production", "production", "生产日期", production or PLACEHOLDER["production"], refs("production"))
        add("expiry", "expiry", "保质期到期日", expiry, refs("expiry"))
    add("shelf", "shelf", "保质期", value("shelf"), refs("shelf"))
    add("storage", "storage", "贮存条件", value("storage") or PLACEHOLDER["storage"], refs("storage"))
    add("producer", "producer", "责任主体名称", value("producer") or PLACEHOLDER["producer"], refs("producer"))
    add("address", "address", "地址", value("address") or PLACEHOLDER["address"], refs("address"))
    add("origin", "origin", "产地", value("origin"), refs("origin"))
    add("country", "country", "原产国（地区）", value("country"), refs("country"))
    add("contact", "contact", "联系方式", value("contact"), refs("contact"))
    add("usage", "usage", "食用方法", value("usage"), refs("usage"))

    warning_blocks, warning_text = _warning_blocks(report)
    warning_value = value("warning") or warning_text
    add("warning", "warning", "温馨提示", warning_value,
        [block.id for block in warning_blocks], _ocr_value(warning_blocks))

    allergen = add("allergen", "allergen", "致敏物质提示", value("allergen"),
                   refs("allergen"), "", [])
    for block in blocks:
        text = _block_text(block.text)
        if ALLERGEN_LINE.search(text) and len(text) < 200 and text not in allergen["value"]:
            allergen["value"] = (allergen["value"] + "；" + text).strip("；") if allergen["value"] else text
            allergen["refs"].append(block.id)

    child_blocks = [block for block in blocks if CHILD_PROMPT.search(_block_text(block.text))
                    and len(_block_text(block.text)) < 120]
    if child_blocks:
        add("child", "child", "儿童提示语", _column_text(child_blocks), [block.id for block in child_blocks])

    lead = re.compile(r"^(?:被委托商|被委托方|受委托方|生产商|地址|产地|电话|产品执行标准|食品生产许可证|食品许可证|邮编)")
    used = set()
    for block in blocks:
        text = _block_text(block.text)
        if not SUPPLIER_HEAD.match(text) or block.id in used:
            continue
        # 同一主体的续行：同 x 区间（±2%）、y 紧邻、前段不以句读结尾、后段不是新引导词。
        parts = [text]
        used.add(block.id)
        cursor = block
        while True:
            following = sorted((candidate for candidate in blocks
                                if candidate.id not in used and abs(candidate.x - block.x) <= .02
                                and 0 < candidate.y - cursor.y <= .02),
                               key=lambda candidate: candidate.y)
            if not following:
                break
            nxt = following[0]
            nxt_text = _block_text(nxt.text)
            if not nxt_text or lead.match(nxt_text) or re.search(r"[。；;：:]$", parts[-1]):
                break
            parts.append(nxt_text)
            used.add(nxt.id)
            cursor = nxt
        merged = "".join(parts)
        letter = SUPPLIER.match(text).group(1)
        node = add("producer/contractor-" + letter, "supplier", "", merged, [block.id])
        if _name_unresolved(merged):
            node["value"] = merged + "〔名称待确认〕"
            node["pendingText"] = True

    return {"nodes": nodes, "index": index}


def _name_unresolved(text):
    """主体名称是否拼不完整：看「生产商：」后的名称有没有企业后缀。"""
    match = re.search(r"(?:生产商|委托商|被委托商\s*[A-Z]?)\s*[:：]\s*([^\s，,。；;:：]{2,40}?)(?=\s|地址|产地|$)", str(text or ""))
    if not match:
        return False
    name = match.group(1)
    return not re.search(r"(?:有限公司|有限责任公司|股份有限公司|公司|集团|厂|商行|合作社|门市部|店)$", name)


def _order_labels(report, document, order):
    """建议顺序的可读标签：包名（净含量明细里的重量）。"""
    weights = {}
    for name, amount in PACK_WEIGHT.findall(report.field("packs") or ""):
        weights[name] = amount
    labels = []
    for node_id in order:
        node = document["index"].get(node_id) if isinstance(document.get("index"), dict) else None
        name = (node or {}).get("heading") or node_id.split("/")[-1]
        base = re.sub(r"[A-Z]$", "", name)
        labels.append("%s（%s克）" % (name, weights[base]) if base in weights else name)
    return labels


def _pack_order(report, document):
    """按净含量明细的重量降序给料包排序；取不到重量时返回空。"""
    packs_text = report.field("packs") or ""
    weights = {}
    for name, amount in PACK_WEIGHT.findall(packs_text):
        weights[name] = float(amount)
    packs = [node for node in document["nodes"] if node["kind"] == "pack"]
    if len(weights) < 2 or not packs or any(re.sub(r"[A-Z]$", "", node["value"].split(":")[0] or "") == "" for node in packs):
        return []
    names = []
    for node in packs:
        head = node["heading"]
        base = re.sub(r"[A-Z]$", "", head)
        if base not in weights:
            return []
        names.append((node["id"], weights[base]))
    names.sort(key=lambda pair: -pair[1])
    return [node_id for node_id, _ in names]


# ---------------------------------------------------------------- 整改计划

def _clause_refs(report, item):
    """条款引用必须带文档与版本；不同标准的同号条款不能互相顶替。"""
    from knowledge import clause as clause_text, clause_tokens, resolve_name

    refs = []
    sources = []
    for key in ("source", "sources", "guidanceSources"):
        value = item.get(key)
        if isinstance(value, dict):
            sources.append(value)
        elif isinstance(value, list):
            sources.extend(entry for entry in value if isinstance(entry, dict))
    requirement = (item.get("analysis") or {}).get("requirement")
    for line in (requirement if isinstance(requirement, list) else [requirement] if requirement else []):
        sources.append({"clause": str(line)})
    document = None
    for source in sources:
        document = source.get("documentId") or document
        # 条款引用行（如「GB 28050-2025 4.1 …」）没有 documentId，按标准名称前缀反查文档。
        target = source.get("documentId") or resolve_name(source.get("clause")) or document
        for number in clause_tokens(source.get("clause")):
            record = clause_text(target, number) if target else None
            if not record:
                continue
            entry = {"documentId": target, "edition": str(report.context.get("version") or "2025"),
                     "clause": number, "textHash": hashlib.sha256(record["text"].encode()).hexdigest()[:16]}
            if entry not in refs:
                refs.append(entry)
    return refs


def _decision_state(item, edit):
    outcome = item.get("outcome") or item.get("group")
    # C02/I01/I04 都需要外部材料才能定案：
    # I04 该不该补分隔符，取决于相邻内容是否属于同一复合配料，图中判断不了。
    if item.get("severity") == "draft-hold" or item.get("id") in ("C02", "I01", "I04"):
        return "conditional"
    if outcome in ("review", "gap"):
        return "needs_material"
    if edit:
        return "confirmed"
    return "needs_material" if outcome in ("review", "gap") else "conditional"


def _pack_node_for(report, document, item):
    """按证据块找该条要改的料包节点：块名匹配节点名，块正文必须能在节点内唯一定位。"""
    by_id = {block.id: block for block in _text_blocks(report)}
    for ref in item.get("refs") or []:
        block = by_id.get(ref)
        if not block:
            continue
        text = _block_text(block.text)
        for node in document["nodes"]:
            if node["kind"] != "pack":
                continue
            name = node["heading"]
            if not text.startswith(name + ":") and not text.startswith(name + "："):
                continue
            body = re.sub(r"^" + re.escape(name) + r"\s*[:：]\s*", "", text)
            if _find_unique(node["value"], body):
                return node, body
    return None, ""


def _warning_after(value, replacement):
    """温馨提示：去掉「不可避免」的变质背书，保留处置与调换信息。"""
    text = str(value or "")
    sentences = re.split(r"(?<=[。！？])", text)
    kept = []
    for sentence in sentences:
        stripped = sentence.strip()
        if not stripped:
            continue
        if "不可避免" in stripped or "属正常现象" in stripped:
            kept.append("运输、储存过程中因受力或受温度影响，包装可能出现胀袋、漏气或产生水珠。")
            continue
        if "请勿食用" in stripped:
            # 处置句的后半段（联系经销商、退换）属于原稿内容，保留不重写。
            tail = stripped[stripped.index("请勿食用") + len("请勿食用"):].strip("，, ")
            kept.append(replacement + tail)
            continue
        kept.append(stripped)
    return "".join(kept) if kept else replacement


def build(report, draft, context=None):
    """把规则发现转成 reportPlan；document 与 revision 一并返回。"""
    document = build_document(report, draft)
    by_id = {block.id: block for block in _text_blocks(report)}
    items, edits = [], {}
    for position, item in enumerate(report.items):
        item_id = str(item.get("id") or "")
        edit = None
        if item_id == "I04" and item.get("replacement"):
            node, before = _pack_node_for(report, document, item)
            if node and before:
                edit = {"operation": "replace_text", "targetNodeId": node["id"],
                        "before": before, "after": item["replacement"]}
        elif item_id == "I02" and item.get("replacement"):
            node = next((node for node in document["nodes"] if node["kind"] == "supplier"
                         and "食品许可证编号" in node["value"]), None)
            if node:
                edit = {"operation": "replace_text", "targetNodeId": node["id"],
                        "before": "食品许可证编号", "after": "食品生产许可证编号"}
        elif item_id == "I03" and item.get("replacement"):
            node = document["index"].get("warning")
            if node and node["value"]:
                edit = {"operation": "replace_text", "targetNodeId": "warning",
                        "before": node["value"], "after": _warning_after(node["value"], item["replacement"])}
        elif item_id == "I01":
            order = _pack_order(report, document)
            if order:
                # A3：料包先后需凭投料记录确认，07 只给建议顺序、不直接换序。
                edit = {"operation": "suggest_reorder", "targetNodeId": "ingredients", "order": order,
                        "labels": _order_labels(report, document, order)}
        state = _decision_state(item, edit)
        action_id = f"{item_id}-{position + 1}"
        # 未确认的动作不进入 07：只登记为待定动作，不当作“定位失败”。
        pending_edit = None
        if edit and state != "confirmed":
            pending_edit, edit = edit, None
        if edit:
            edits[action_id] = edit
        items.append({
            "itemId": item_id, "actionId": action_id, "title": item.get("title"),
            "outcome": item.get("outcome") or item.get("group"), "decisionState": state,
            # 只有已确认的动作才「预期有 edit」；条件性条目本就不执行，
            # 不能因此被记成定位失败把整份稿判成不可交付。
            "editExpected": item_id in ("I01", "I02", "I03", "I04") and state == "confirmed",
            "clauseRefs": _clause_refs(report, item),
            "edit": edit, "pendingEdit": pending_edit, "evidenceRefs": list(item.get("refs") or []),
            "options": _normalize_options(item.get("revisionOptions")),
            "basis": item.get("basis") or "",
        })
    snapshot = hashlib.sha256(json.dumps(
        {"fields": draft.get("fields"), "items": [(row["itemId"], row["outcome"]) for row in items],
         "version": report.context.get("version")}, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]
    plan = {"schemaVersion": SCHEMA_VERSION, "snapshotId": snapshot,
            "items": items, "document": document["nodes"]}
    plan["planVersion"] = hashlib.sha256(json.dumps(
        [[item["itemId"], item["decisionState"], item.get("actionId"), item.get("edit"), item.get("options")]
         for item in items], ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]
    plan["revision"] = apply(plan, document)
    return plan


def _conflict(used, node_id):
    return node_id in used


def _apply_replace(nodes, edit, used):
    node = nodes.get(edit["targetNodeId"])
    if not node:
        return None, "找不到目标节点 " + str(edit["targetNodeId"])
    if _conflict(used, node["id"]):
        return None, "同一位置已有其他动作，先处理冲突"
    before = edit["before"]
    if node["kind"] == "pack" and node.get("heading"):
        before = re.sub(r"^" + re.escape(node["heading"]) + r"\s*[:：]\s*", "", before)
    span = _find_unique(node["value"], before)
    if not span:
        return None, "目标原文未在「%s」中唯一定位" % (node.get("heading") or node["id"])
    start, end = span
    after = edit["after"]
    if node["kind"] == "pack" and node.get("heading"):
        after = re.sub(r"^" + re.escape(node["heading"]) + r"\s*[:：]\s*", "", after)
    updated = node["value"][:start] + after + node["value"][end:]
    if updated == node["value"]:
        return None, "替换后与原值相同，疑似重复应用"
    node["value"] = updated
    node["changed"] = True
    used.add(node["id"])
    return {"action": "replace_text", "targetNodeId": node["id"], "before": edit["before"],
            "after": edit["after"]}, ""


def _apply_reorder(nodes, order, document, used):
    """换序只调节点顺序（rank），不切割字符串；节点集合必须完整。"""
    packs = [node for node in document["nodes"] if node["kind"] == "pack"]
    if not packs:
        return None, "没有可换序的料包节点"
    if {node["id"] for node in packs} != set(order):
        return None, "料包节点集合不完整，拒绝换序"
    if _conflict(used, "ingredients"):
        return None, "配料表已有其他顺序动作，先处理冲突"
    for rank, node_id in enumerate(order):
        if node_id in nodes:
            nodes[node_id]["rank"] = rank
    used.add("ingredients")
    return {"action": "reorder_nodes", "targetNodeId": "ingredients", "order": order}, ""


def apply(plan, document=None):
    """在文档副本上执行计划里已确认的动作；返回 sections/applied/failures。"""
    source = document or {"nodes": plan.get("document") or []}
    nodes = {node["id"]: copy.deepcopy(node) for node in source["nodes"]}
    order = [node["id"] for node in source["nodes"] if "parent" not in node]
    used, applied, failures, suggestions = set(), [], [], []
    for item in plan.get("items") or []:
        edit = item.get("edit")
        if not edit:
            if item.get("editExpected") and item.get("decisionState") != "needs_material":
                failures.append({"itemId": item.get("itemId"), "actionId": item.get("actionId"),
                                 "reason": "未能从规则结果构造可执行动作（原文缺失或证据不足）",
                                 "targetNodeId": "", "before": ""})
            continue
        if edit.get("operation") == "suggest_reorder":
            suggestions.append({"itemId": item.get("itemId"), "actionId": item.get("actionId"),
                                "order": list(edit.get("order") or []),
                                "labels": list(edit.get("labels") or [])})
            continue
        if item.get("decisionState") != "confirmed":
            failures.append({"itemId": item.get("itemId"), "actionId": item.get("actionId"),
                             "reason": "条件尚未确认，07 节不执行该动作",
                             "targetNodeId": edit.get("targetNodeId"), "before": edit.get("before", "")[:200]})
            continue
        if edit["operation"] == "reorder_nodes":
            result, reason = _apply_reorder(nodes, edit["order"], source, used)
        else:
            result, reason = _apply_replace(nodes, edit, used)
        if result:
            applied.append(dict(result, itemId=item["itemId"], actionId=item["actionId"]))
        else:
            failures.append({"itemId": item["itemId"], "actionId": item["actionId"],
                             "reason": reason, "targetNodeId": edit.get("targetNodeId"),
                             "before": edit.get("before", "")[:200]})
    sections = _sections(nodes, order, applied, failures)
    for suggestion in suggestions:
        labels = suggestion.get("labels")
        if not labels:
            labels = [nodes[node_id].get("heading") for node_id in suggestion["order"] if node_id in nodes]
        if labels:
            sections.append({"id": "suggestion:" + str(suggestion.get("itemId")),
                             "heading": "建议顺序（待投料记录确认）",
                             "text": " → ".join(labels), "changed": False, "bold": [], "indent": False,
                             "suggestion": True, "itemId": suggestion.get("itemId")})
    pending_text = any(node.get("pendingText") for node in nodes.values())
    return {"snapshotId": plan.get("snapshotId"), "sections": sections, "applied": applied,
            "suggestions": suggestions, "pendingText": pending_text,
            "document": [nodes[node_id] for node_id in order] + [node for node in nodes.values() if "parent" in node],
            "failures": failures, "canExport": not failures, "pendingText": pending_text,
            "options": [{"itemId": item["itemId"], "title": item.get("title"),
                         "planVersion": plan.get("planVersion"), "values": item["options"]}
                        for item in plan.get("items") or [] if item.get("options")],
            "changed": bool(applied)}


def _sections(nodes, order, applied, failures):
    """把应用后的节点排成 07 节的行；保留加粗（原稿致敏强调）与变更标记。"""
    changed = {entry["targetNodeId"] for entry in applied}
    ranking = {}
    for node_id in nodes:
        ranking[node_id] = order.index(node_id) if node_id in order else len(order)
    sections = []

    def push(node, heading=None, indent=False):
        if not node or not node.get("value"):
            return
        sections.append({"id": node["id"], "heading": heading if heading is not None else node.get("heading", ""),
                         "text": node["value"], "changed": bool(node.get("changed")) or node["id"] in changed,
                         "bold": list(node.get("bold") or []), "indent": indent})

    push(nodes.get("name"), "", False)
    push(nodes.get("type"))
    packs = sorted((node for node in nodes.values() if node["kind"] == "pack"),
                   key=lambda node: (node.get("rank", ranking.get(node["id"], 0)), ranking.get(node["id"], 0)))
    if nodes.get("ingredients"):
        if packs:
            sections.append({"id": "ingredients", "heading": "配料表", "text": "",
                             "changed": bool(nodes["ingredients"].get("changed")), "bold": [], "indent": False})
            for pack in packs:
                push(pack, pack.get("heading") or pack["id"], True)
        else:
            # 没有分包（或配料表未能拆分）时直接输出配料表整段/占位
            push(nodes["ingredients"], "配料表")
    for key in ("net", "standard", "license", "production", "expiry", "shelf", "storage",
                "producer", "address", "origin", "country", "contact", "usage", "warning",
                "allergen", "child"):
        push(nodes.get(key))
    for node in nodes.values():
        if node["kind"] == "supplier":
            push(node)
    return sections


def summary(plan):
    revision = plan.get("revision") or {}
    return {"snapshotId": plan.get("snapshotId"), "items": len(plan.get("items") or []),
            "confirmed": sum(1 for item in plan.get("items") or [] if item.get("decisionState") == "confirmed"),
            "applied": len(revision.get("applied") or []),
            "failures": len(revision.get("failures") or []),
            "canExport": revision.get("canExport", True)}


def _normalize_options(values):
    """Q2：选项统一为 [{optionId, condition, text}]；旧字符串输入按顺序补 id。"""
    options = []
    for index, value in enumerate(values or []):
        if isinstance(value, dict) and value.get("optionId") and value.get("text"):
            options.append({"optionId": str(value["optionId"]),
                            "condition": str(value.get("condition") or ""),
                            "text": str(value["text"])})
        elif isinstance(value, str) and value.strip():
            options.append({"optionId": "option-%d" % (index + 1), "condition": "", "text": value.strip()})
    return options


def classify(items):
    """唯一分类数据：卡片、正文、章节标题与 report_check 都读这一份。"""
    rows = [item for item in items or [] if isinstance(item, dict)]
    standard = [item for item in rows if item.get("track") == "standard2025"]
    actual = [item for item in rows if item.get("track") != "standard2025"]
    critical = [item for item in actual if item.get("group") == "issue"
                and item.get("severity") in ("critical", "redline", "draft-hold")]
    draft_hold = [item for item in critical if item.get("severity") == "draft-hold"]
    confirmed = [item for item in critical if item not in draft_hold]
    issues = [item for item in actual if item.get("group") == "issue" and item not in critical]
    internal = [item for item in actual if item.get("group") == "internal"]
    normative = issues + internal
    must_change = [item for item in standard if item.get("outcome") == "issue"]
    pending = [item for item in actual if item.get("group") not in ("issue", "pass", "internal")]
    pending += [item for item in standard if item.get("outcome") not in ("issue", "pass")]
    anchor = next((item for item in pending if item.get("id") == "date-anchor"), None)
    if anchor:
        refs = set(anchor.get("refs") or [])
        shadow = next((item for item in pending if item.get("id") in ("F04", "G02")
                       and refs & set(item.get("refs") or [])), None)
        if shadow:
            pending.remove(anchor)
    return {"recheckRequired": len(critical), "confirmedRedline": len(confirmed),
            "draftHold": len(draft_hold), "normative": len(normative),
            "mustChange": len(must_change), "pending": len(pending)}
