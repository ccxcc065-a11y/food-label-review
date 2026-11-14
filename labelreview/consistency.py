"""Compare statements printed on the same label without asserting a legal violation."""
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
import re
import statistics

from labelreview.evidence import dated_targets
from labelreview.extract import NUTRIENT_PATTERN
from labelreview.knowledge import clause_line


PART = re.compile(r"(?:^|[、,;(])\s*([\u4e00-\u9fff]{1,9}包)\s*(\d+(?:\.\d+)?)\s*(?:克|g)(?=[、,;).。]|$)", re.I)
ORDER = re.compile(r"^([\u4e00-\u9fff]{1,9}包)\s*[A-Z]?\s*:")
LICENSE_WORD = re.compile(r"食品(?:生产)?许可证编号\s*:\s*SC\d{14}", re.I)
SUBCONTRACT = re.compile(r"^(?:被委托商|被委托方|受委托方)\s*[A-Z]\s*:", re.I)
DURATION = re.compile(r"([1-9]\d{0,3})\s*(个月|月|年|天|日)")


def note(report, key, title, detail, action, refs, group="internal"):
    report.add(key, group, title, action, detail, refs)
    report.items[-1]["basis"] = "同一标签内部一致性核对"
    report.items[-1]["category"] = {"I01": "配料表", "I02": "生产者信息", "I03": "提示语", "I04": "配料表",
                                    "I06": "日期", "I07": "营养成分表"}[key]


def cite_clause(report, label, document, number):
    """给刚加入的标签内对照条目补一条条款原文；取不到原文时什么也不做。"""
    line = clause_line(label, document, number)
    if not line:
        return
    item = report.items[-1]
    analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
    existing = analysis.get("requirement")
    lines = existing if isinstance(existing, list) else ([existing] if existing else [])
    item["analysis"] = {**analysis, "requirement": [*lines, line]}


def pack_order(report):
    packs = report.field("packs")
    pack_refs = set(report.evidence(keys=("packs",)))
    if not pack_refs or re.search(r"[×*]", packs):
        return
    matches = PART.findall(packs)
    weights = {name: Decimal(amount) for name, amount in matches}
    if len(weights) != len(matches) or len(weights) < 2:
        return
    for page in report.pages:
        if not any(block.id in pack_refs for block in page):
            continue
        headings = [(found[1], block) for block in page if (found := ORDER.match(block.text))]
        names = [name for name, _ in headings]
        if len(names) < 2 or len(names) != len(set(names)) or not set(names) <= weights.keys():
            continue
        if max(block.x for _, block in headings) - min(block.x for _, block in headings) > .045:
            continue
        headings.sort(key=lambda row: row[1].y)
        for (a, left), (b, right) in zip(headings, headings[1:]):
            if weights[a] < weights[b]:
                note(report, "I01", f"料包顺序与净含量明细打架：{weights[a]:g}克 的排在 {weights[b]:g}克 前面",
                     f"配料表把 {a} 列在 {b} 前。净含量明细分别写 {weights[a]:g} 克和 {weights[b]:g} 克，较轻的料包排在较重的前面。两处是同一销售包装的标示。"
                     "按 GB 7718-2025 4.3.2 的递减顺序要求，需核对投料记录确认分包关系与列示口径。"
                     "另需说明：加入量不超过 2% 的配料可以不按递减顺序排列，但 2% 豁免只作用于包内配料，不能用来解释包与包之间的倒置。",
                     f"先核对投料记录确认分包关系与列示口径；确认后按记录调整版面顺序（本报告仅给出建议顺序，不直接换序）。",
                     [left.id, right.id, *[block.id for block in page if block.id in pack_refs]])
                cite_clause(report, "GB 7718-2025", "gb7718-2025", "4.3.2")
                return


def license_wording(report):
    for page in report.pages:
        labels = [(block, match.group()) for block in page if SUBCONTRACT.match(block.text)
                  and (match := LICENSE_WORD.search(block.text))]
        unusual = [block for block, text in labels if text.startswith("食品许可证")]
        normal = [block for block, text in labels if text.startswith("食品生产许可证")]
        if len(unusual) == 1 and len(normal) >= 2:
            observed = LICENSE_WORD.search(unusual[0].text).group()
            contractor = SUBCONTRACT.match(unusual[0].text).group().rstrip(":")
            note(report, "I02", f"{contractor} 漏「生产」二字",
                 f"{contractor}一处为「{observed}」，同组其余 {len(normal)} 处写「食品生产许可证编号」。",
                 f"若核对原稿确为漏字，统一写为「{observed.replace('食品许可证', '食品生产许可证').replace(':', '：')}」。",
                 [unusual[0].id, *[block.id for block in normal]])
            report.items[-1]["replacement"] = observed.replace("食品许可证", "食品生产许可证").replace(":", "：")
            # 许可证编号的标示要求见 4.9；同一项目在标签内多处出现时写法应一致（3.2 易于辨认）。
            cite_clause(report, "GB 7718-2025", "gb7718-2025", "4.9")
            cite_clause(report, "GB 7718-2025", "gb7718-2025", "3.2")
            return


def warning_wording(report):
    for page in report.pages:
        ordered = sorted(page, key=lambda block: (block.y, block.x))
        for block in ordered:
            if not re.search(r"不可避免|属正常现象|可正常食用", block.text):
                continue
            lines = [candidate for candidate in ordered
                     if abs(candidate.x - block.x) <= .023 and block.y <= candidate.y <= block.y + max(.024, block.height * 6)]
            text = "".join(candidate.text for candidate in lines).replace("涨袋", "胀袋")
            ends = re.search(r"请勿食用|禁止食用|不可食用", text)
            if not ends or re.search(r"轻微|少量|运输外包装", text[:ends.start()]):
                continue
            if re.search(r"(?:不可避免|属正常现象|可正常食用).{0,30}(?:胀袋|漏气).{0,80}(?:胀袋|漏气).{0,28}(?:请勿食用|禁止食用|不可食用)", text):
                wording = re.search(r"(?:不可避免|属正常现象|可正常食用).*?(?:请勿食用|禁止食用|不可食用)", text).group()
                note(report, "I03", "「涨袋不可避免」与后句「胀袋请勿食用」自相矛盾",
                     f"标签「温馨提示」相关文字读为「{wording}」。同段既将胀袋或漏气描述为可能出现的情况，又要求遇到这些情况勿食用，处置指令容易读混。"
                     "把胀袋描述成「不可避免」，等于替变质背书；后半句又要求「胀袋请勿食用」，消费者读到不知道该信哪一句。",
                     "将贮运说明与处置警示分开，建议警示写为「如遇包装破损、胀袋、漏气或产品变质，请勿食用」。",
                     [line.id for line in lines])
                report.items[-1]["replacement"] = "如遇包装破损、胀袋、漏气或产品变质，请勿食用。"
                # 同段并存两种相反读法：违反 3.2（清晰、醒目、持久，易于辨认识读）与
                # 3.4（真实、准确，不得使用使消费者误解的文字）。
                cite_clause(report, "GB 7718-2025", "gb7718-2025", "3.2")
                cite_clause(report, "GB 7718-2025", "gb7718-2025", "3.4")
                return


PAREN_TAIL = re.compile(r"[（(][^（）()]{1,60}[）)]\s*(.)")
SEPARATOR = set("、，,;；")
EXPLAIN_TAIL = re.compile(r"^(?:以|计|含量|添加量|为|时|及|或|等|的)")


def _paren_tails(block):
    """返回该块内每个右括号后的下一个字符，用于判断分隔符是否缺失。"""
    for match in PAREN_TAIL.finditer(block.text):
        following = match.group(1)
        if not following:
            continue
        tail = block.text[match.end():match.end() + 4]
        yield match, following, tail


def ingredient_punctuation(report):
    """复合配料括号后的分隔符核对。

    先看同一标签上其他复合配料括号后有没有分隔符，再看这一处：
    若别处都有、只有这一处没有，可判定为疑似漏打；若别处也没有，只作提示。
    """
    refs = set(report.evidence(keys=("ingredients",)))
    separated, missing, unclear = [], [], []
    for page in report.pages:
        for block in page:
            if not (block.id in refs or ORDER.match(block.text)):
                continue
            # V2.0：低置信度不再要求人工勾选 verifiedRefs；只有多次重读仍不一致时才转为「看不清」。
            mark = report.reread.get(block.id) if isinstance(report.reread, dict) else None
            if isinstance(mark, dict) and mark.get("unresolved"):
                unclear.append(block)
                continue
            for match, following, tail in _paren_tails(block):
                if following in SEPARATOR:
                    separated.append((block, match, following))
                elif "\u4e00" <= following <= "\u9fff" and not EXPLAIN_TAIL.match(tail):
                    missing.append((block, match, following))
                elif re.search(r"[、,]{2}", following):
                    missing.append((block, match, following))
    if not missing:
        if unclear:
            block = unclear[0]
            note(report, "I04", "配料表分隔符无法从图中确认",
                 f"配料表「{block.text}」这处多次放大重读仍不一致，图上分辨不出括号后是否漏了分隔符。",
                 report.capture_action("该段清晰照片或矢量稿/排版文件") + "，再核对括号后的分隔符。",
                 [block.id], group="review")
        return
    block, match, following = missing[0]
    head = match.group(0)[:-len(following)] if following else match.group(0)
    original = head + following
    replacement = head + "、" + following
    others = [item for item in separated if item[0].id != block.id]
    if len(missing) == 1 and len(others) >= 2:
        examples = "、".join(sorted({item[1].group(0)[:-1].rstrip() for item in others})[:3])
        pack = re.match(r"^([\u4e00-\u9fff]{1,9}包[A-Z]?)\s*:", block.text)
        pack_name = pack.group(1) if pack else ""
        paren_head = match.group(0)[:-len(following)] if following else match.group(0)
        paren = paren_head.strip().replace("(", "（").replace(")", "）")
        title = f"{pack_name}「{paren}」后缺顿号" if pack_name else f"「{paren}」后缺顿号"
        detail = (f"配料表读为「{block.text}」。括号闭合后直接接「{following}」，没有分隔符。"
                  f"同一标签另 {len(others)} 处复合配料括号后均有分隔符（如{examples}），"
                  f"说明不是排版限制，此处疑似漏打。")
        action = f"在括号后补顿号，改为「{head}、」。"
    else:
        title = f"配料边界处缺分隔符：{original}"
        detail = f"配料表读为「{block.text}」。「{original}」两处配料的边界需要核对。"
        action = f"若确为相邻独立配料，改为「{replacement}」。"
    # refs 只放被修改的那一块。报告渲染会把 refs 里的每个块都当成改写目标，
    # 混入对比证据会把别的配料行整段替换掉。对比块另存 contrastRefs，仅供追溯。
    note(report, "I04", title, detail, action, [block.id])
    cite_clause(report, "GB 7718-2025", "gb7718-2025", "4.3.3")
    report.items[-1]["contrastRefs"] = [item[0].id for item in others[:3]]
    report.items[-1]["replacement"] = (block.text[:match.start()] + replacement
                                       + block.text[match.end():])


MANDATORY_NUTRIENTS = ("energy", "protein", "fat", "saturated", "carb", "sugar", "sodium")
# GB 28050-2025 4.1 以外的常见成分。只有表内出现这些，6.2 的「更醒目」要求才成立。
OPTIONAL_NUTRIENT_WORDS = ("反式脂肪", "胆固醇", "膳食纤维", "维生素", "钙", "铁", "锌", "硒",
                           "钾", "镁", "叶酸", "烟酸", "生物素", "胆碱", "DHA", "EPA",
                           "多不饱和脂肪", "单不饱和脂肪")


def nutrition_prominence(report):
    """I07：GB 28050-2025 6.2——仅当标示了 4.1 以外的营养成分时，才要求 4.1 的 7 项强制内容更醒目。

    米线这类表内只有 7 项强制内容的标签，6.2 不适用，本条不产生任何结论。
    """
    optional_blocks = [block for page in report.pages for block in page
                       if any(word in block.text for word in OPTIONAL_NUTRIENT_WORDS)]
    if not optional_blocks:
        return
    labels, optional_labels = {}, {}
    for page in report.pages:
        for block in page:
            if any(word in block.text for word in OPTIONAL_NUTRIENT_WORDS):
                optional_labels[block.id] = block
                continue
            found = NUTRIENT_PATTERN.match(block.text)
            if found and found.lastgroup in MANDATORY_NUTRIENTS:
                labels.setdefault(found.lastgroup, block)
    def stroke_of(block):
        mark = report.typography_of(block) if block else None
        return mark.get("stroke") if isinstance(mark, dict) else None
    mandatory_values = [value for value in (stroke_of(labels.get(key)) for key in MANDATORY_NUTRIENTS)
                        if value]
    optional_values = [value for value in (stroke_of(block) for block in optional_labels.values()) if value]
    evidence = report.evidence(nutrients=MANDATORY_NUTRIENTS) + list(optional_labels)
    if len(mandatory_values) < 3 or not optional_values:
        note(report, "I07", "营养表字重无法从图中判断",
             "表内标示了 4.1 以外的营养成分，但送检图上没能可靠测出强制项与额外项的字重差异（字太小或区域不清）。",
             "提供矢量稿、排版文件或清晰打样件，核对 4.1 强制项是否比额外成分更醒目。", evidence)
        return
    mandatory_median = statistics.median(mandatory_values)
    optional_median = statistics.median(optional_values)
    ratio = mandatory_median / optional_median if optional_median else 0
    if ratio > 1.25:
        return
    detail = (f"表内标示了 4.1 以外的营养成分。按送检图实测笔画宽度：4.1 的 7 项强制内容中位数 "
              f"{mandatory_median:.1f} px，额外成分 {optional_median:.1f} px，比值 {ratio:.2f}，"
              "未检出强制项更醒目的处理。字号和字重以矢量稿核验。")
    note(report, "I07", "标示 4.1 以外成分时强制项未更醒目", detail,
         "按 GB 28050-2025 6.2，采用增大字号、改变字体或调整颜色等方式让 4.1 的 7 项强制内容更醒目，并在打样稿中复审。",
         evidence)
    cite_clause(report, "GB 28050-2025", "gb28050-2025", "6.2")


def shelf_arithmetic(report):
    shelf = DURATION.fullmatch(report.field("shelf"))
    if not shelf:
        return
    observed = {}
    for role in ("production", "expiry"):
        refs = report.evidence(keys=(role,))
        candidates = [(target["value"], ref) for page in report.pages for block in page if block.id in refs
                      for target in dated_targets("", block) if target["role"] == role
                      for ref in [block.id]]
        if len({value for value, _ in candidates}) != 1:
            return
        observed[role] = candidates[0]
    start, end = (date.fromisoformat(observed[key][0]) for key in ("production", "expiry"))
    amount, unit = int(shelf[1]), shelf[2]
    try:
        if unit in ("天", "日"):
            expected = start + timedelta(days=amount)
        else:
            total = start.year * 12 + start.month - 1 + amount * (12 if unit == "年" else 1)
            year, month = divmod(total, 12)
            expected = date(year, month + 1, min(start.day, monthrange(year, month + 1)[1]))
    except (ValueError, OverflowError):
        return
    if end == expected:
        return
    difference = abs((end - expected).days)
    detail = (f"按自然日历相加，{start} 加 {shelf.group()} 为 {expected}；标签印为 {end}。"
              + ("相差 1 天，可能来自是否计入生产当天的口径。" if difference == 1 else f"相差 {difference} 天，请核对计算口径或批次。"))
    note(report, "I06", "保质期与日期算术需核对", detail, "向生产方确认起算与到期口径",
         [observed["production"][1], observed["expiry"][1], *report.evidence(keys=("shelf",))], "review")


def check_consistency(report):
    for check in (ingredient_punctuation, license_wording, nutrition_prominence, warning_wording, pack_order, shelf_arithmetic):
        check(report)
