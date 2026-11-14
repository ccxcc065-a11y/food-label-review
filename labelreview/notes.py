"""08 审核说明的检查点库。

每个检查点一个 detector，命中才输出：没有证据的检查点不写，也不为了凑字数硬塞。
文案与条款放在 knowledge/review_notes.json，代码只负责判定、取证据块和按 order 排序。

分区（order 决定输出顺序）：
  10 依据声明   20～22 事实核对   30～38 排除论证与提示   40 覆盖声明   50～51 边界与待核实

detector 返回 (variant, values, refs)：variant 用于同一检查点的不同事实分支（如名称提及的
「归类名／已定量／需定量」），为空串时用模板的单一 text。
"""

import json
import labelreview.evidence_checklist as evidence_checklist
import re
import statistics
from decimal import Decimal
from pathlib import Path

from labelreview.extract import MASS, NUTRIENT_PATTERN, UNITS


ROOT = Path(__file__).resolve().parent.parent / "knowledge"
DEFINITIONS = ROOT / "review_notes.json"

PLACE = re.compile(r"([\u4e00-\u9fff]{1,3}(?:州|市|县|镇|乡|区))")
QUANTITY = re.compile(r"([\u4e00-\u9fff]{2,8})\s*[（(]\s*(?:添加量|含量)?\s*[≥≤]?\s*(\d+(?:\.\d+)?)\s*%")
QUANTITY_TAIL = re.compile(r"\s*[（(]\s*((?:添加量|含量)?\s*[≥≤]?\s*\d+(?:\.\d+)?\s*%?)\s*[）)]")
PACK_SPLIT = re.compile(r"(?:^|[。；;\n])\s*([\u4e00-\u9fff]{1,10}包[A-Z]?)\s*:")
GROUPED_ADDITIVE = re.compile(r"食品添加剂\s*[（(]")
TRADEMARK = re.compile(r"[®™]|(?<![A-Za-z])TM(?![A-Za-z])")
LATIN = re.compile(r"[A-Za-z]{2,}")
TIME_CLAIM = re.compile(r"\d+(?:\s*[-~—–至]\s*\d+)?\s*(?:分钟|秒钟|秒|小时|天)")
DATE_CONTEXT = re.compile(r"保质期|生产日期|到期|日期|贮存|保存期限|见包装")
ABSOLUTE_SKIP = re.compile(
    r"第[一二三四五六七八九十]步|第[一二三四五六七八九十]次|第[一二三四五六七八九十]天|"
    r"第[一二三四五六七八九十]章|第[一二三四五六七八九十]节|第[一二三四五六七八九十]条|"
    r"第[一二三四五六七八九十]种|第[一二三四五六七八九十]项|第[一二三四五六七八九十]部分|"
    r"最后|最近|最终|最初|最新|最大|最小|最高|最低|最多|最少"
)
STOP_LATIN = {
    "MM", "CM", "KG", "MG", "ML", "G", "L", "KJ", "KCAL", "NRV", "SC", "GB", "SB", "TM",
    "PT", "QQ", "ISO", "QS", "ID",
}
ADDITIVE_NAMES = (
    "D-异抗坏血酸钠", "乳酸链球菌素", "山梨酸钾", "苯甲酸钠", "三氯蔗糖", "胭脂红", "冰醋酸",
    "焦糖色", "柠檬酸", "柠檬酸钠", "黄原胶", "卡拉胶", "谷氨酸钠", "阿斯巴甜", "甜菊糖苷",
)
CATEGORY_NAMES = ("植物油", "食用植物油", "淀粉", "食用淀粉", "糖", "食糖", "食用香精", "香精")


def proof_text(report, result):
    """A6：08 声明从实际记录生成；verifiedFields 为空时不出现「已人工校对」。

    注意模板已带前缀「文字与数值」，这里只写谓语部分，且不输出句号，
    由模板统一接到「；标示位置与字符高度…」之前。
    """
    context = report.context if isinstance(getattr(report, "context", None), dict) else {}
    verified = [row for row in context.get("verifiedFields") or [] if isinstance(row, dict) and row.get("field")]
    stale = [row for row in context.get("staleVerifications") or [] if isinstance(row, dict)]
    raw_reread = (result or {}).get("autoReread")
    if isinstance(raw_reread, dict):
        reread = [str(row.get("name")) for row in (result or {}).get("nutritionRows") or []
                  if isinstance(row, dict) and row.get("reread") and row.get("name")]
    else:
        reread = [str(name) for name in raw_reread or [] if name]
    parts = ["经自动识别"]
    if reread:
        parts.append("、".join(reread) + "经局部放大重读确认")
    if verified:
        fields = "、".join(f"{row.get('field')}（{row.get('value')}）" if row.get("value") else str(row.get("field"))
                           for row in verified)
        parts.append("人工已确认字段：" + fields)
        parts.append("其余仍需核对")
        if stale:
            parts.append("另有 " + str(len(stale)) + " 项原确认因字段或证据变化已失效")
        return "；".join(parts)
    parts.append("尚无人工核验字段记录")
    parts.append("仍需核对")
    if stale:
        parts.append("另有 " + str(len(stale)) + " 项原确认因字段或证据变化已失效")
    return parts[0] + "；" + "，".join(parts[1:])


def string(value):
    return value.strip() if isinstance(value, str) else ""


def definitions():
    try:
        loaded = json.loads(DEFINITIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    notes = loaded.get("notes") if isinstance(loaded.get("notes"), dict) else {}
    vocabulary = loaded.get("vocabulary") if isinstance(loaded.get("vocabulary"), dict) else {}
    return notes, vocabulary


def _refs(report, *keys, nutrients=()):
    return report.evidence(keys=keys, nutrients=nutrients)


def _object_refs(report):
    """被审核对象的证据块：名称、配料表、净含量、标准代号。"""
    refs = _refs(report, "name", "ingredients", "net", "standard")
    if refs:
        return refs
    return [block.id for block in report.blocks[:3]]


def _strip_groups(value):
    previous = None
    text = string(value)
    while previous != text:
        previous = text
        text = re.sub(r"[（(][^（）()]*[）)]", "", text)
    return text


def _packs(ingredients):
    parts = PACK_SPLIT.split(string(ingredients))
    return [(parts[index], parts[index + 1]) for index in range(1, len(parts) - 1, 2)]


def _mass(match):
    """把 MASS 命中项换算成 (数值, 维度)：克系归到 g，体积系归到 mL。"""
    families = {"kg": ("g", 1000), "g": ("g", 1), "mg": ("g", Decimal("0.001")),
                "L": ("mL", 1000), "mL": ("mL", 1)}
    dimension, multiplier = families[UNITS[match.group(2).lower()]]
    return Decimal(match.group(1)) * multiplier, dimension


def _pending(result):
    items = [item for item in result.get("items", []) if isinstance(item, dict)]

    def standard(item):
        return item.get("track") == "standard2025"

    pending = [item for item in items
               if not standard(item) and item.get("group") not in ("issue", "pass", "internal")]
    pending += [item for item in items
                if standard(item) and item.get("outcome") not in ("issue", "pass")]
    anchor = next((item for item in pending if item.get("id") == "date-anchor"), None)
    if anchor:
        refs = set(anchor.get("refs") or [])
        shadow = next((item for item in pending if item.get("id") in ("F04", "G02")
                       and refs & set(item.get("refs") or [])), None)
        if shadow:
            pending.remove(anchor)
    return pending


def _label_facts(result):
    facts = result.get("labelFacts") if isinstance(result, dict) else None
    return facts if isinstance(facts, dict) else {}


# ---------------------------------------------------------------- 10 / 50 / 51

def basis_declaration(report, draft, result):
    refs = _object_refs(report)
    if not refs:
        return None
    transition = ""
    if result.get("versionTransition"):
        transition = "；GB 7718-2025 与 GB 28050-2025 自 2027-03-16 起实施，本次按新版口径核对"
    values = {
        "object": "刀版图（印刷版面）" if report.artwork else "标签图片",
        "product": report.field("name") or "本产品",
        "scope": string(result.get("scopeLabel")) or "适用范围待确认",
        "standards": string(result.get("versionLabel")) or "本次启用的指定标准",
        "transition": transition,
        "proof": proof_text(report, result),
    }
    return "", values, refs


def judgement_authority(report, draft, result):
    refs = _object_refs(report)
    return ("", {}, refs) if refs else None


def pending_summary(report, draft, result):
    pending = _pending(result)
    if not pending:
        return None
    materials = []
    for item in pending:
        analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        for value in analysis.get("missingEvidence") or []:
            value = string(value)
            if value and value not in materials:
                materials.append(value)
    fallback = ["完整配方与各料包投料记录", "供应商规格与检测报告", "原稿矢量文件或清晰照片"]
    materials = [*materials, *[item for item in fallback if item not in materials]][:6]
    refs = _refs(report, "name") or _object_refs(report)
    if not refs:
        return None
    return "", {"count": len(pending), "materials": "、".join(materials)}, refs


# ---------------------------------------------------------------- 20 / 21 / 22

def origin_fact(report, draft, result):
    name = report.field("name")
    if not name:
        return None
    vocabulary = definitions()[1]
    provinces = vocabulary.get("provinces") or []
    place = ""
    found = PLACE.search(name)
    if found:
        place = found.group(1)
    else:
        place = next((province for province in provinces if province in name), "")
    if not place:
        return None
    address = report.field("address")
    facts = _label_facts(result)
    origin = string(facts.get("origin"))
    origins = [item for item in (facts.get("packOrigins") or [])
               if isinstance(item, dict) and item.get("pack")]
    pack_line = ""
    if origins:
        pack_line = "，分包产地为" + "、".join(f"「{item['pack']}：{item.get('value')}」" for item in origins)
    place_province = next((province for province in provinces if province in address), "") if address else ""
    if (place in address) or (origin and place == origin):
        relation = f"名称中的地名与生产者地址一致（{address or origin}）。"
    elif place_province:
        relation = f"名称中的地名与生产者地址所在地区不同（生产者地址在{place_province}）。"
    else:
        relation = "名称中的地名未出现在生产者地址中。"
    refs = _refs(report, "name", "producer", "address")
    if not refs:
        return None
    values = {
        "name": name,
        "place": place,
        "origin_line": f"「产地：{origin}」" if origin else "生产者地址",
        "address": address or "未读到",
        "pack_line": pack_line,
        "relation": relation,
    }
    return "", values, refs


def job_ticket_mass(report, draft, result):
    facts = _label_facts(result)
    ticket = string(facts.get("jobTicketMass"))
    net = report.field("net")
    if not ticket or not net:
        return None
    ticket_match, net_match = MASS.fullmatch(ticket), MASS.fullmatch(net)
    if not ticket_match or not net_match:
        return None
    if _mass(ticket_match) == _mass(net_match):
        return None
    compact = re.sub(r"\s+", "", ticket)
    refs = [block.id for block in report.blocks if compact in re.sub(r"\s+", "", block.text)]
    refs = [*refs, *_refs(report, "net")]
    if not refs:
        return None
    return "", {"ticket": ticket, "net": net}, list(dict.fromkeys(refs))


def pack_quantity(report, draft, result):
    packs = report.field("packs")
    ingredients = report.field("ingredients")
    if not packs or not ingredients:
        return None
    pairs = _packs(ingredients)
    if len(pairs) < 2:
        return None
    labels = [name.strip() for name in re.findall(r"([\u4e00-\u9fff]{1,9}包)", packs)]
    quantified, others = [], []
    for name, contents in pairs:
        found = QUANTITY.findall(contents)
        if found:
            parts = "、".join(f"{word} 添加量≥{amount}%" for word, amount in found[:2])
            quantified.append(f"{name}（{parts}）")
            continue
        base = re.sub(r"[A-Z]$", "", name)
        if base in labels:
            others.append(name)
    if not quantified:
        return None
    refs = [*_refs(report, "packs", "ingredients")]
    if not refs:
        return None
    values = {
        "count": len(labels) or len(pairs),
        "quantified": "、".join(quantified),
        "rest": len(others),
        "others": "、".join(others) or "无",
    }
    return "", values, refs


# ---------------------------------------------------------------- 30 ~ 32

def _helpers():
    from labelreview.audit import ingredient_words, name_ingredient_hits
    return ingredient_words, name_ingredient_hits


def _ingredient_quantity(ingredients, word):
    for match in re.finditer(re.escape(word), ingredients):
        found = QUANTITY_TAIL.match(ingredients[match.end():match.end() + 24])
        if found:
            return re.sub(r"\s+", "", found.group(1))
    return ""


def _standalone(report, word):
    pattern = re.compile(rf"(?:配料表?)?\s*[:：]?\s*{re.escape(word)}")
    return any(pattern.fullmatch(block.text.strip()) for block in report.blocks)


def name_mention(report, draft, result):
    name = report.field("name")
    ingredients = report.field("ingredients")
    if not name or not ingredients:
        return None
    words_of, hits_of = _helpers()
    hits = hits_of(name, words_of(ingredients))
    refs = _refs(report, "name", "ingredients")
    if not hits or not refs:
        return None
    facts = _label_facts(result)
    category = string(facts.get("type")) + string(facts.get("flavor"))
    pending_ids = {item.get("id") for item in _pending(result) if isinstance(item, dict)}
    for fragment, ingredient in hits:
        if _standalone(report, ingredient):
            continue
        quantity = _ingredient_quantity(ingredients, ingredient)
        if quantity:
            return "quantified", {"word": fragment, "ingredient": ingredient,
                                  "quantity": quantity}, refs
        if fragment and fragment in category:
            return "category", {"word": fragment, "ingredient": ingredient,
                                "type": string(facts.get("type")) or category}, refs
        pending_line = "该事项已在第 06 节列为待核实。" if "C01" in pending_ids else ""
        return "needs", {"word": fragment, "ingredient": ingredient,
                         "pending_line": pending_line}, refs
    return None


def additive_forms(report, draft, result):
    ingredients = report.field("ingredients")
    if not ingredients or not GROUPED_ADDITIVE.search(ingredients):
        return None
    grouped, direct = [], []
    for name, contents in _packs(ingredients):
        if GROUPED_ADDITIVE.search(contents):
            grouped.append(name)
            continue
        outside = _strip_groups(contents)
        if any(item in outside for item in ADDITIVE_NAMES):
            direct.append(name)
    refs = _refs(report, "ingredients")
    if not grouped or not direct or not refs:
        return None
    values = {
        "grouped": "、".join(grouped[:3]),
        "direct": "、".join(dict.fromkeys(direct))[:40],
    }
    return "", values, refs


def category_name(report, draft, result):
    ingredients = report.field("ingredients")
    if not ingredients:
        return None
    found = []
    for segment in re.split(r"[、,;；。\n]+", _strip_groups(ingredients)):
        plain = re.split(r"[:：]", segment)[-1].strip()
        if plain in CATEGORY_NAMES and plain not in found:
            found.append(plain)
    refs = _refs(report, "ingredients")
    if not found or not refs:
        return None
    return "", {"names": "、".join(found)}, refs


# ---------------------------------------------------------------- 33 ~ 38

def trademark_symbol(report, draft, result):
    blocks = [block for block in report.blocks if TRADEMARK.search(block.text)]
    if not blocks:
        return None
    block = blocks[0]
    found = TRADEMARK.search(block.text)
    symbol = "®" if found.group() == "®" else ("™" if found.group() == "™" else "TM")
    mark = re.sub(r"[®™]", "", block.text).replace("TM", "").strip(" ：:、,")
    mark = re.sub(r"\s+", " ", mark)[:12] or "该标识"
    hint = ""
    if symbol == "TM":
        hint = "另：TM 并非注册标记，标注 TM 只表示作商标使用，不表示已获准注册。"
    return "", {"mark": mark, "symbol": symbol, "tm_hint": hint}, [block.id for block in blocks[:3]]


def pinyin(report, draft, result):
    """拼音/外文：排除标准代号、许可证号、单位、注册编号等结构性字母串。"""
    tokens, refs, texts = [], [], []
    for block in report.blocks:
        text = block.text
        hits = []
        for match in LATIN.finditer(text):
            token = match.group()
            if token.upper() in STOP_LATIN:
                continue
            if re.search(r"\d", token):
                continue
            before = text[max(0, match.start() - 3):match.start()]
            after = text[match.end():match.end() + 1]
            if re.search(r"\d", before + after) or before.endswith(("Q/", "T/")):
                continue
            if before.endswith(("GB", "SB", "Q/")):
                continue
            if not (token.islower() or token.isupper()):
                continue
            if token not in tokens:
                tokens.append(token)
            hits.append(token)
        if hits:
            refs.append(block.id)
            snippet = re.sub(r"\s+", " ", text).strip().strip("'\"“”「」 ")[:24]
            if snippet not in texts:
                texts.append(snippet)
    if not tokens or not refs:
        return None
    sample = "、".join(f"「{text}」" for text in texts[:2]) if texts else "、".join(tokens[:4])
    imported = report.context.get("scope") == "imported"
    values = {
        "text": sample,
        "imported_clause": "；8.1.2 要求可见的外文、繁体字内容与规范汉字有对应关系" if imported else "",
        "pinyin_clause": "3.6、8.1.2" if imported else "3.6",
    }
    return "", values, refs


def time_claim(report, draft, result):
    for block in report.blocks:
        if DATE_CONTEXT.search(block.text):
            continue
        found = TIME_CLAIM.search(block.text)
        if not found:
            continue
        claim = re.sub(r"\s+", "", found.group())
        where = ("食用方法" if re.search(r"煮|泡|蒸|炒|微波|加热|解冻|冲调", block.text)
                 else "原料或工艺说明")
        return "", {"claim": claim, "where": where}, [block.id]
    return None


def sensory_words(report, draft, result):
    words = definitions()[1].get("sensory") or []
    hits, refs = [], []
    for block in report.blocks:
        for word in words:
            if word in block.text and word not in hits:
                hits.append(word)
                if block.id not in refs:
                    refs.append(block.id)
    if not hits or not refs:
        return None
    return "", {"words": "、".join(hits[:6])}, refs


def absolute_words(report, draft, result):
    words = definitions()[1].get("absolute") or []
    hits, refs = [], []
    for block in report.blocks:
        text = block.text
        for word in words:
            for match in re.finditer(re.escape(word), text):
                around = text[max(0, match.start() - 2):match.end() + 3]
                if ABSOLUTE_SKIP.search(around):
                    continue
                if word not in hits:
                    hits.append(word)
                if block.id not in refs:
                    refs.append(block.id)
                break
    if not hits or not refs:
        return None
    return "", {"words": "、".join(hits[:4])}, refs


def font_size(report, draft, result):
    groups = {"配料表": [], "营养成分表": []}
    for page in report.pages:
        for block in page:
            mark = report.typography_of(block)
            if not isinstance(mark, dict):
                continue
            height = mark.get("height") or 0
            if not isinstance(height, (int, float)) or height <= 0:
                continue
            if NUTRIENT_PATTERN.match(block.text):
                groups["营养成分表"].append(float(height))
            elif "配料" in block.text or block.text.count("、") >= 2:
                groups["配料表"].append(float(height))
    if not groups["配料表"] or not groups["营养成分表"]:
        return None
    first = statistics.median(groups["配料表"])
    second = statistics.median(groups["营养成分表"])
    large, small = max(first, second), min(first, second)
    ratio = large / small if small else 0
    if ratio <= 1.15:
        return None
    bigger = "营养成分表" if second > first else "配料表"
    pairs = (f"配料表正文中位字高 {first:.0f}px，营养成分表中位字高 {second:.0f}px，"
             f"两者比值 {ratio:.2f}，{bigger}略大")
    refs = _refs(report, "ingredients", nutrients=("energy", "fat", "carb"))
    if not refs:
        return None
    return "", {"pairs": pairs}, refs


def allergen_bold(report, draft, result):
    """致敏物质：原稿以加粗方式提示时说明其符合附录 D.2.1，不另设提示语。"""
    items = [item for item in result.get("items", []) if isinstance(item, dict)]
    confirmed = [item for item in items
                 if item.get("allergenBold") is True and item.get("outcome") == "pass"]
    if not confirmed:
        return None
    refs = _refs(report, "ingredients")
    return ("", {}, refs) if refs else None


def coverage_gap(report, draft, result):
    scope = report.context.get("scope")
    if scope not in ("workshop", "imported"):
        return None
    if scope == "workshop":
        values = {"scope": string(result.get("scopeLabel")) or "小作坊食品",
                  "extra": "属地省级小作坊管理法规中的标示要求、登记证与销售区域限制",
                  "materials": "小作坊登记证、属地管理条例适用说明、完整标签原稿",
                  "clause": "《食品安全法》第三十六条；属地食品小作坊管理法规"}
    else:
        values = {"scope": string(result.get("scopeLabel")) or "进口食品",
                  "extra": "进口食品专项要求（在华注册编号、中外文对应关系、原产国与境内代理商信息）",
                  "materials": "境外生产企业注册编号、原标签与外文对应关系、进口商资质",
                  "clause": "《食品安全法》第九十七条；GB 7718-2025 第 8 章"}
    refs = _object_refs(report)
    if not refs:
        return None
    return "", values, refs


DETECTORS = (
    ("basis-declaration", basis_declaration),
    ("origin-fact", origin_fact),
    ("job-ticket-mass", job_ticket_mass),
    ("pack-quantity", pack_quantity),
    ("name-mention", name_mention),
    ("additive-forms", additive_forms),
    ("category-name", category_name),
    ("trademark-symbol", trademark_symbol),
    ("pinyin", pinyin),
    ("time-claim", time_claim),
    ("sensory-words", sensory_words),
    ("absolute-words", absolute_words),
    ("font-size", font_size),
    ("allergen-bold", allergen_bold),
    ("coverage-gap", coverage_gap),
    ("judgement-authority", judgement_authority),
    ("pending-summary", pending_summary),
)


def _plain_conclusion(conclusion, item):
    """把清单里的内部标识换成客户看得懂的描述。

    清单的 conclusion 形如 "N02-nrv 脂肪标示与 NRV% 不一致"，
    前半段是内部条目键，不应出现在客户报告里。
    """
    text = str(conclusion or "").strip()
    if not text:
        return str((item or {}).get("title") or "").strip()
    parts = text.split(" ", 1)
    if len(parts) == 2 and ("-" in parts[0] or parts[0].replace("_", "").isalnum()):
        return parts[1].strip()
    return text


def build(report, draft, result):
    """返回 [{"id","title","text","clause","refs"}]，只返回真正命中的。"""
    catalogue, _ = definitions()
    produced = []
    for key, detector in DETECTORS:
        entry = catalogue.get(key)
        if not isinstance(entry, dict):
            continue
        try:
            found = detector(report, draft, result)
        except Exception:  # 单个检查点出错不影响其它说明
            found = None
        if not found:
            continue
        variant, values, refs = found
        refs = [ref for ref in dict.fromkeys(refs or []) if isinstance(ref, str)]
        if not refs:
            continue
        templates = entry.get("templates") if isinstance(entry.get("templates"), dict) else {}
        template = string(templates.get(variant)) if variant else ""
        template = template or string(entry.get("template"))
        clause = string(entry.get("clause"))
        if not template or not clause:
            continue
        try:
            text = template.format(**values)
            clause = clause.format(**values)
        except (KeyError, IndexError, ValueError):
            continue
        produced.append({
            "id": key,
            "order": entry.get("order", 99),
            "title": string(entry.get("title")),
            "text": text,
            "clause": clause,
            "refs": refs,
        })
    # 每个带证据清单的结论在 08 展示：需要的证据、当前状态、是否可升级。
    # 面向客户：不出现内部条目 ID、不出现英文枚举；同一标题只出现一次。
    seen_titles = set()
    for item in report.items:
        check = item.get("evidenceChecklist") if isinstance(item, dict) else None
        if not isinstance(check, dict):
            continue
        title = _plain_conclusion(check.get("conclusion"), item)
        if title in seen_titles:
            continue
        seen_titles.add(title)
        verdict = ("全部必需项已核验，可升级为确定问题" if check.get("upgradeAllowed")
                   else "证据不足，保持待核实；满足几项不参与放行")
        produced.append({
            "id": "evidence:" + str(item.get("id")),
            "order": 90,
            "title": "证据清单｜" + title,
            "text": evidence_checklist.state_text(check) + "；" + verdict + "。",
            "clause": "各项的核验状态由服务端判定，客户版只显示中文结论。",
            "refs": [ref for ref in (item.get("refs") or [])[:3] if isinstance(ref, str)],
        })
    produced.sort(key=lambda note: (note.get("order", 99), note["id"]))
    return produced
