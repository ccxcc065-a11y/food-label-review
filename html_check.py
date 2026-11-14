"""V2.8 ②：检查最终 HTML（解析客户版字节），不是检查内部数据。"""
import html as html_lib
import re

CODES = ("stats-mismatch", "title-mismatch", "conditional-wording", "truncated-name",
         "residual-punctuation", "manual-claim", "ai-display", "old-text", "option-missing")

STAT = re.compile(r'<div class="stat ([ragb])"><div class="n">(\d+)</div><div class="l">([^<]*)(?:<br><small>([^<]*)</small>)?</div></div>')
VERDICT = re.compile(r"共列示 .*?>(\d+) 项须整改后复审.*?确认红线 .*?>(\d+) 项.*?稿件停版 .*?>(\d+) 项")
TITLE02 = re.compile(r'02</span>须整改后复审 (\d+) 项（确认红线 (\d+) · 稿件停版 (\d+)）')
TITLE = re.compile(r'0([2-6])</span>([^<]*)')
ROW = re.compile(r"<tr>")


def _text(value):
    return html_lib.unescape(re.sub(r"<[^>]+>", "", value or ""))


def _strip(value):
    return re.sub(r"\s+", "", _text(value))


def section_table_counts(page):
    counts = {}
    for match in re.finditer(r'<h2><span class="num">(0[2-6])</span>([\s\S]*?)(?=<h2>|<div class="foot">)', page):
        number, body = match.group(1), match.group(2)
        rows = re.findall(r'<tbody>([\s\S]*?)</tbody>', body)
        count = 0
        for tbody in rows:
            for row in re.findall(r"<tr>([\s\S]*?)</tr>", tbody):
                if 'colspan=' in row:
                    continue
                count += 1
        counts[number] = count
    return counts


def run(html_text, report, plan, context, ai_stats=None):
    errors = []
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", html_text or ""))
    classification = report.get("classification") or {}
    ai_stats = ai_stats or {}
    manual = [row for row in (context or {}).get("verifiedFields") or []
              if isinstance(row, dict) and row.get("field") and str(row.get("value") or "")]

    def fail(code, message, detail=""):
        errors.append({"code": code, "message": message, "detail": detail})

    # 1 统计卡 / 正文 / 章节数字同源
    cards = {tone: (int(number), label, note or "") for tone, number, label, note in STAT.findall(html_text or "")}
    if len(cards) != 4:
        fail("stats-mismatch", "统计卡不是四张", str(len(cards)))
    else:
        expected = [classification.get("recheckRequired"), classification.get("normative"),
                    classification.get("mustChange"), classification.get("pending")]
        actual = [cards[tone][0] for tone in ("r", "a", "b", "g")]
        if actual != expected:
            fail("stats-mismatch", "统计卡数字与分类数据不一致", f"卡片 {actual} 期望 {expected}")
        if "确认红线 %s · 稿件停版 %s" % (classification.get("confirmedRedline"), classification.get("draftHold")) not in cards["r"][2]:
            fail("stats-mismatch", "卡片副行与分类数据不一致", cards["r"][2])
    verdict = VERDICT.search(html_text or "")
    if not verdict:
        fail("stats-mismatch", "正文缺少四档数字")
    else:
        got = tuple(int(value) for value in verdict.groups())
        want = (classification.get("recheckRequired"), classification.get("confirmedRedline"), classification.get("draftHold"))
        if got != want:
            fail("stats-mismatch", "正文数字与分类数据不一致", f"{got} != {want}")

    # 2 章节标题数字与实际条目
    counts = section_table_counts(html_text or "")
    title02 = TITLE02.search(html_text or "")
    if not title02:
        fail("title-mismatch", "02 标题缺少数量")
    else:
        got = tuple(int(value) for value in title02.groups())
        want = (classification.get("recheckRequired"), classification.get("confirmedRedline"), classification.get("draftHold"))
        if got != want:
            fail("title-mismatch", "02 标题数字与分类数据不一致", f"{got} != {want}")
        if counts.get("02") != got[0]:
            fail("title-mismatch", "02 标题数量与表格行数不一致", f"标题 {got[0]} / 行 {counts.get('02')}")
    titles = {("0" + number): body for number, body in TITLE.findall(html_text or "")}
    expected_counts = {"03": classification.get("normative"), "04": classification.get("mustChange"),
                       "06": classification.get("pending")}
    for number, want in expected_counts.items():
        body = titles.get(number, "")
        match = re.search(r"(\d+) 项", body)
        if not match:
            fail("title-mismatch", f"{number} 标题缺少数量", body)
            continue
        if int(match.group(1)) != (want or 0):
            fail("title-mismatch", f"{number} 标题数量与分类数据不一致", f"{match.group(1)} != {want}")
        if counts.get(number) != (want or 0):
            fail("title-mismatch", f"{number} 标题数量与表格行数不一致", f"{want} / {counts.get(number)}")

    # 3 条件性整改与 07 措辞
    suggestion = any((item.get("edit") or {}).get("operation") == "suggest_reorder"
                     for item in plan.get("items") or [])
    if suggestion and "建议顺序（待投料记录确认）" not in text:
        fail("conditional-wording", "未确认的料包顺序缺少「建议顺序（待投料记录确认）」")
    if suggestion and re.search(r"料包顺序已|已调整料包顺序|已换序", text):
        fail("conditional-wording", "未确认的料包顺序出现已完成语气")

    # 3b Q2：有分支动作的条目，07 必须出现每个 optionId 的完整正文（不能只看标题或关键词）
    pre_html = re.search(r"<pre>([\s\S]*?)</pre>", html_text or "")
    pre_flat = _strip(pre_html.group(1)) if pre_html else ""
    section07 = (html_text or "")
    if "07</span>" in section07:
        start = section07.find("07</span>")
        end = section07.find("08</span>")
        section07 = section07[start:end if end > start else len(section07)]
    section07_flat = _strip(section07)
    for item in plan.get("items") or []:
        for option in item.get("options") or []:
            option_id = str(option.get("optionId") or "")
            option_text = _strip(option.get("text"))
            if option_id and ('data-option-id="%s"' % option_id) not in section07:
                fail("option-missing", "07 缺少 optionId 标记", option_id)
            if option_text and option_text[:80] not in section07_flat:
                fail("option-missing", "07 缺少 optionId 对应的完整正文", option_id or option_text[:30])
    # 3c Q3：未确认条目不得出现已应用的改稿
    for item in plan.get("items") or []:
        edit = item.get("edit") or {}
        if item.get("decisionState") != "confirmed" and edit.get("operation") in ("replace_text", "reorder_nodes"):
            fail("conditional-wording", "未确认条目仍带可执行改稿", str(item.get("itemId")))

    # 4 公司名称完整性
    pre = re.search(r"<pre>([\s\S]*?)</pre>", html_text or "")
    pre_text = _text(pre.group(1)) if pre else ""
    for match in re.finditer(r"(?:生产商|被委托商\s*[A-Z]?|委托商)\s*[:：]\s*([^\s，,。；;:：]{2,30}?)(?=\s|地址|产地|$)", pre_text):
        name = match.group(1)
        if name and not re.search(r"(有限公司|有限责任公司|股份有限公司|公司|集团|厂|商行|合作社|门市部|店)$", name):
            fail("truncated-name", "07 出现疑似截断的主体名称", name)
    if "〔名称待确认〕" in text and "可直接交生产方" in text:
        fail("truncated-name", "存在待确认名称却标注「可直接交生产方」")

    # 5 重复拼接与残留占位
    for token in ("文字与数值文字与数值", "。；", "；；", "{{", "}}", "{name}", "undefined", "NaN", "nullnull"):
        if token in text:
            fail("residual-punctuation", f"报告含残留片段：{token}")
    if re.search(r"\{[a-zA-Z_]+\}", text):
        fail("residual-punctuation", "报告含未替换占位符")

    # 6 核验声明
    if "已人工校对" in text and not manual:
        fail("manual-claim", "没有人工核验字段记录却出现「已人工校对」")

    # 7 AI 展示：完成时写统计；失败/未参与时写明确状态，不能只写 completed
    if ai_stats.get("enabled"):
        status = str(ai_stats.get("status") or "")
        if status == "completed":
            need = ("AI 审核：检查", "提出", "重复", "新增", "待人工复核")
            missing = [token for token in need if token not in text]
            if missing:
                fail("ai-display", "AI 审核展示不完整", "缺：" + "、".join(missing))
            if re.search(r"AI 审核状态：\s*completed\s*$", text):
                fail("ai-display", "AI 展示不能只有 completed")
        else:
            if "AI 审核未完成" not in text and "本报告为规则版" not in text:
                fail("ai-display", "AI 未完成时缺少明确状态声明", status)

    # 8 旧错消除
    for item in plan.get("items") or []:
        edit = item.get("edit") or {}
        if item.get("decisionState") != "confirmed" or edit.get("operation") != "replace_text":
            continue
        after = _strip(edit.get("after"))
        before = _strip(edit.get("before"))
        heading = next((node.get("heading") for node in plan.get("document") or []
                        if node.get("id") == edit.get("targetNodeId")), "")
        if heading and after.startswith(heading):
            after = after[len(heading):].lstrip(":：")
        node = re.search(re.escape(_strip(heading)) + r"[：:]([^\n]*)", pre_text) if heading else None
        body = _strip(node.group(1)) if node else _strip(pre_text)
        if after and after not in body:
            fail("old-text", "07 缺少已确认整改后的文本", after[:60])
        elif before and before not in after and before in body:
            fail("old-text", "07 仍残留整改前旧文本", before[:60])

    return {"ok": not errors, "errors": errors, "counts": counts}
