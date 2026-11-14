"""阶段 2：正式客户报告生成前的全文一致性检查（确定性，不调用 AI）。

`run(report, plan, html_parts)` 返回 {"ok": bool, "errors": [{"code","message","itemIds","detail"}]}。
错误码固定：count-mismatch / dangling-reference / edit-not-applied / state-conflict /
content-lost / snapshot-drift / unverified-claim / wrong-target。

数据来源：`report`（audit.Report）、`plan`（report_plan.build 的结果）；
`html_parts` 可选，供渲染层把统计卡数字、指引、正文传进来做交叉核对：
    {"counts": [红线, 规范性, 新规必改, 待核实],
     "references": [{"text": "…见第 06 节", "targetItemIds": ["GB7718AL"], "section": "06"}],
     "07": "渲染后的第 07 节纯文本", "report": "完整报告 HTML/纯文本"}
未提供时按计划与 revision 结构推导。
"""
import re
import unicodedata

CODES = ("count-mismatch", "dangling-reference", "edit-not-applied", "state-conflict",
         "content-lost", "snapshot-drift", "unverified-claim", "wrong-target", "truncated-name")
REFERENCE = re.compile(r"见第\s*(0?[1-9])\s*节")
CLAIM = "已人工校对"


def _flat(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")))


def _items(report, plan):
    """报告条目：优先 audit 结果的 items（dict 或对象），没有时退回计划条目。"""
    if isinstance(report, dict):
        rows = report.get("items")
    else:
        rows = getattr(report, "items", None)
    if isinstance(rows, list) and rows:
        return [row for row in rows if isinstance(row, dict)]
    return [dict(row, id=row.get("id") or row.get("itemId"))
            for row in plan.get("items") or [] if isinstance(row, dict)]


def _context(report):
    raw = report.get("context") if isinstance(report, dict) else getattr(report, "context", None)
    return raw if isinstance(raw, dict) else {}


def _revision(report, plan):
    """audit() 把 revision 放在 reportPlan 的同级（result["revision"]），不在 plan 内部。"""
    revision = plan.get("revision")
    if isinstance(revision, dict) and revision:
        return revision
    if isinstance(report, dict):
        revision = report.get("revision")
    else:
        revision = getattr(report, "revision", None)
    return revision if isinstance(revision, dict) else {}


def _section_text(section):
    return _flat(section.get("text") or "")


def _counts(report, plan):
    """与 report.js 的四档分组保持一致：红线 / 规范性（含标签内对照）/ 新规必改 / 待核实。

    When a DecisionSet projection is present, those unique-decision counts win
    so the checker, cards, and section 07 share one source.
    """
    if isinstance(report, dict):
        view = report.get("reportView") if isinstance(report.get("reportView"), dict) else {}
        if view.get("source") == "decisionSet" and isinstance(view.get("counts"), list) and len(view["counts"]) == 4:
            return [int(value) for value in view["counts"]]
        classification = report.get("classification") if isinstance(report.get("classification"), dict) else None
        if view.get("source") == "decisionSet" and classification:
            return [int(classification.get(key) or 0)
                    for key in ("recheckRequired", "normative", "mustChange", "pending")]
    return _group_counts(_items(report, plan))


def _classification(report, plan):
    try:
        import report_plan
        return report_plan.classify(_items(report, plan))
    except Exception:
        return None


def _group_counts(items):
    standard = [item for item in items if item.get("track") == "standard2025"]
    actual = [item for item in items if item.get("track") != "standard2025"]
    critical = [item for item in actual if item.get("group") == "issue"
                and item.get("severity") in ("critical", "redline", "draft-hold")]
    issues = [item for item in actual if item.get("group") == "issue" and item not in critical]
    internal = [item for item in actual if item.get("group") == "internal"]
    normative = issues + internal
    must = [item for item in standard if item.get("outcome") == "issue"]
    pending = [item for item in actual if item.get("group") not in ("issue", "pass", "internal")]
    pending += [item for item in standard if item.get("outcome") not in ("issue", "pass")]
    # report.js 会把「日期锚点」并入同一证据上的 F04/G02 行并去掉锚点，这里必须用同一口径。
    anchor = next((item for item in pending if item.get("id") == "date-anchor"), None)
    if anchor:
        anchor_refs = set(anchor.get("refs") or [])
        shadow = next((item for item in pending if item.get("id") in ("F04", "G02")
                       and anchor_refs & set(item.get("refs") or [])), None)
        if shadow:
            pending.remove(anchor)
    return [len(critical), len(normative), len(must), len(pending)]


def run(report, plan, html_parts=None):
    errors = []
    parts = html_parts if isinstance(html_parts, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    items = [item for item in plan.get("items") or [] if isinstance(item, dict)]
    report_items = _items(report, plan)
    document = {node["id"]: node for node in plan.get("document") or [] if isinstance(node, dict)}
    revision = _revision(report, plan)
    sections = [section for section in revision.get("sections") or [] if isinstance(section, dict)]
    by_section = {section.get("id"): section for section in sections}
    applied = {entry.get("actionId"): entry for entry in revision.get("applied") or [] if isinstance(entry, dict)}
    copy_text = parts.get("07") or "\n".join(
        "%s：%s" % (section.get("heading") or "", section.get("text") or "") for section in sections)
    report_text = parts.get("report") or copy_text
    counts = _counts(report, plan)
    classification = _classification(report, plan)

    def fail(code, message, item_ids=(), detail=""):
        errors.append({"code": code, "message": message, "itemIds": list(item_ids), "detail": detail})

    # 1 统计与章节：渲染层给的数字必须等于按最终分组算出的数字
    given = parts.get("counts")
    if isinstance(given, list) and [int(value) for value in given] != counts:
        fail("count-mismatch", "统计卡数字与章节条目数不一致", detail=f"期望 {counts}，实际 {given}")
    given_classification = parts.get("classification")
    if isinstance(given_classification, dict) and classification is not None and \
            {key: int(given_classification.get(key, -1)) for key in classification} != classification:
        fail("count-mismatch", "卡片/正文/章节的分类数据不一致", detail=f"期望 {classification}，实际 {given_classification}")

    # 2 跨章节指引：引用必须带具体事项，目标为空时不得输出
    known = {item.get("itemId") for item in items}
    for reference in parts.get("references") or []:
        targets = reference.get("targetItemIds") or []
        if not targets or any(target not in known for target in targets):
            fail("dangling-reference", "跨章节指引没有关联到真实事项",
                 targets, str(reference.get("text") or ""))
    if counts[3] == 0 and "见第 06 节" in copy_text:
        fail("dangling-reference", "第 06 节没有待核实事项，却仍写「见第 06 节」")

    # 3 整改落实：已确认动作必须落到 07，且原目标位置不再保留错误原文
    for item in items:
        edit = item.get("edit")
        if not edit or item.get("decisionState") != "confirmed" or edit.get("operation") == "suggest_reorder":
            continue
        action_id = item.get("actionId")
        section = by_section.get(edit.get("targetNodeId"))
        if action_id not in applied or section is None:
            fail("edit-not-applied", "已确认的整改动作没有落实到改版稿", [item.get("itemId")],
                 f"actionId={action_id} target={edit.get('targetNodeId')}")
            continue
        text = _section_text(section)
        if edit.get("operation") == "reorder_nodes":
            order = [node_id for node_id in edit.get("order") or []]
            rendered = [entry.get("id") for entry in sections
                        if str(entry.get("id") or "").startswith("ingredients/")]
            if rendered and rendered != order:
                fail("edit-not-applied", "料包换序没有按计划落到改版稿", [item.get("itemId")])
            continue
        after = edit.get("after") or ""
        # 料包节点在 07 里以「包名：正文」渲染，计划里的 after 可能带包名前缀，比较前去掉。
        if str(section.get("id") or "").startswith("ingredients/") and section.get("heading"):
            after = re.sub(r"^" + re.escape(str(section["heading"])) + r"\s*[:：]\s*", "", after)
        after = _flat(after)
        before = _flat(edit.get("before"))
        if after and after not in text:
            fail("edit-not-applied", "改版稿里找不到整改后的文本", [item.get("itemId")],
                 f"应为：{edit.get('after')}")
        elif before and before not in after and before in text:
            fail("edit-not-applied", "原目标位置仍保留整改前的错误文本", [item.get("itemId")],
                 f"残留：{edit.get('before')}")

    # 3c 条目与计划必须唯一对应：报告里重复出现的条目无法唯一落到一个整改动作
    planned = {}
    for item in items:
        planned[item.get("itemId")] = planned.get(item.get("itemId"), 0) + 1
    seen_rows = {}
    for row in report_items:
        item_id = row.get("id")
        seen_rows[item_id] = seen_rows.get(item_id, 0) + 1
        if seen_rows[item_id] > 1 and seen_rows[item_id] > planned.get(item_id, 0):
            fail("edit-not-applied", "同一条目重复出现，整改动作无法唯一对应", [item_id],
                 f"报告条目出现 {seen_rows[item_id]} 次，计划中 {planned.get(item_id, 0)} 次")

    # 3d 公司名截断：生产商名称缺企业后缀且未标〔名称待确认〕
    TRUNCATED = re.compile(r"(?:生产商|被委托商\s*[A-Z]?|委托商)\s*[:：]\s*([^\s，,。；;:：]{2,30}?)(?=\s|地址|产地|$)")
    SUFFIX = re.compile(r"(?:有限公司|有限责任公司|股份有限公司|公司|集团|厂|商行|合作社|门市部|店)$")
    for node in document.values():
        if node.get("kind") != "supplier":
            continue
        for match in TRUNCATED.finditer(str(node.get("value") or "")):
            name = match.group(1)
            if name and not SUFFIX.search(name) and "〔名称待确认〕" not in str(node.get("value")):
                fail("truncated-name", "被委托商名称疑似截断，未标〔名称待确认〕", [node.get("id")], name)
    for section in sections:
        text = str(section.get("text") or "")
        if "〔名称待确认〕" in text:
            continue
        for match in TRUNCATED.finditer(text):
            name = match.group(1)
            if name and not SUFFIX.search(name):
                fail("truncated-name", "07 出现疑似截断的主体名称", [], name)

    # 4 条件一致：同一条目不得同时出现两种 decisionState；未确认的条件不得带可执行动作
    states = {}
    for item in items:
        item_id = item.get("itemId")
        state = item.get("decisionState")
        if item_id in states and states[item_id] != state:
            fail("state-conflict", "同一条目出现两种整改状态", [item_id], f"{states[item_id]} 与 {state}")
        states.setdefault(item_id, state)
        if item.get("edit") and state != "confirmed" and \
                (item.get("edit") or {}).get("operation") != "suggest_reorder":
            fail("state-conflict", "条件尚未确认的条目却带可执行动作", [item_id], f"decisionState={state}")

    # 5 内容保留：未被动作修改的节点必须原样出现在 07；加粗格式与儿童提示不得丢
    edited = {item["edit"].get("targetNodeId") for item in items
              if item.get("edit") and item.get("decisionState") == "confirmed"}
    for node_id, node in document.items():
        value = _flat(node.get("value"))
        if not value or node_id in edited or node.get("kind") == "pack":
            continue
        if node.get("kind") in ("child", "allergen", "warning"):
            section = by_section.get(node_id)
            if section is None or value not in _section_text(section):
                fail("content-lost", "已确认的提示语在改版稿中丢失或改错", [], f"{node.get('heading')}：{node.get('value')}")
        elif node.get("kind") in ("license", "net", "standard", "production", "shelf", "storage"):
            if value not in _flat(copy_text):
                fail("content-lost", "未编辑的标示内容在改版稿中丢失", [], f"{node.get('heading')}：{node.get('value')}")
    for node in document.values():
        if node.get("kind") != "pack" or not node.get("bold"):
            continue
        section = by_section.get(node["id"])
        if section is None:
            continue
        lost = [word for word in node["bold"] if word not in (section.get("bold") or [])]
        if lost:
            fail("content-lost", "原稿加粗（致敏强调）在改版稿中丢失", [], f"{node.get('heading')}：{'、'.join(lost)}")

    # 6 数值与来源：同一快照的读数、编号不得漂移
    for node_id, node in document.items():
        if node.get("kind") not in ("license", "net", "pack", "nutrition"):
            continue
        if node_id in edited:
            continue
        value = _flat(node.get("value"))
        numbers = re.findall(r"\d+(?:[./-]\d+)*", value)
        missing = [number for number in numbers if number not in _flat(copy_text)]
        if missing:
            fail("snapshot-drift", "改版稿中的编号或读数与审核快照不一致", [], f"{node.get('heading')}：{'、'.join(missing)}")
    if parts.get("snapshot") and parts["snapshot"] != plan.get("snapshotId"):
        fail("snapshot-drift", "检查用的快照与整改计划不一致", [], f"{parts['snapshot']} != {plan.get('snapshotId')}")

    # 7 核验声明：没有人工核验记录时不得写「已人工校对」
    verified_fields = [row for row in (_context(report).get("verifiedFields") or [])
                       if isinstance(row, dict) and row.get("field") and str(row.get("value") or "")]
    if not verified_fields and (CLAIM in report_text or CLAIM in copy_text):
        fail("unverified-claim", "没有人工核验记录却写了「已人工校对」")

    # 8 改写身份：修改目标必须是标签文档节点，不能拿对比证据块当目标
    for item in items:
        edit = item.get("edit")
        if not edit:
            continue
        target = edit.get("targetNodeId")
        refs = set(item.get("evidenceRefs") or [])
        if target not in document:
            fail("wrong-target", "整改目标不是标签文档节点", [item.get("itemId")], str(target))
        elif target in refs and document[target].get("kind") == "pack":
            fail("wrong-target", "把证据块当成了修改目标", [item.get("itemId")], str(target))

    return {"ok": not errors, "errors": errors, "counts": counts}
