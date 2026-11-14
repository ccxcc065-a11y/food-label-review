"""08 节「看起来像问题、其实合规」的排除论证。

只输出判断条件能在现有证据上核验的项：命中条件不成立就不出现，不为了凑字数硬塞。
每条命中返回 {key, title, text, clause, refs}，由 audit() 放进 result["exclusionNotes"]，
报告 08 节在既有条目之后逐条渲染。文案与条款放在 knowledge/exclusions.json，代码只管判定。

五条判定：
  name-mention-no-emphasis  名称含配料词，且配料表未对该配料作定量标示
  job-ticket-not-label      制作表头（印前工单）与版面净含量不同
  additive-two-forms        食品添加剂（…）与直接列出的添加剂名并存
  category-name-allowed     配料表按归类名称标示植物油／淀粉／糖／食用香精
  no-quantity-claim         全文没有定量声称
"""

import json
import re
from pathlib import Path

from crosscheck import mass_of
from extract import MASS

ROOT = Path(__file__).resolve().with_name("knowledge")
DEFINITIONS = ROOT / "exclusions.json"

GROUPED_ADDITIVE = re.compile(r"食品添加剂\s*[（(]")
QUANTITY_TAIL = re.compile(r"\s*[（(]\s*((?:添加量|含量)?\s*[≥≤><]?\s*\d+(?:\.\d+)?\s*%?)\s*[）)]")
# 常见添加剂名：只在括号外出现时才算「直接列出」。
ADDITIVE_NAMES = (
    "D-异抗坏血酸钠", "乳酸链球菌素", "山梨酸钾", "苯甲酸钠", "三氯蔗糖", "胭脂红", "冰醋酸", "焦糖色",
    "柠檬酸", "柠檬酸钠", "黄原胶", "卡拉胶", "磷酸盐", "谷氨酸钠", "阿斯巴甜", "甜菊糖苷", "β-胡萝卜素",
)
# 附录 A.4 允许按归类方式标示的配料名。
CATEGORY_NAMES = ("植物油", "食用植物油", "淀粉", "食用淀粉", "糖", "食糖", "食用香精", "香精")
# 定量声称只认「对配料或成分给出数量」的表述：贮存条件的 ≤85% 湿度之类不算，
# 所以比较符只取正向强调（≥、超过、高达），并要求带百分号。
CLAIM = re.compile(
    r"(?<!净)含量|添加量|(?:≥|>|不低于|不少于|超过|高达)\s*\d+(?:\.\d+)?\s*%"
    r"|\d+(?:\.\d+)?\s*%\s*(?:以上|以下)"
)
# 营养成分表的单元格不是「声称」，扫全文时不参与判定。
NUTRITION_CELL = re.compile(
    r"^(?:项目|每100[克g毫升]|营养素参考值%?|NRV%?)$"
    r"|^\d+(?:\.\d+)?\s*(?:%|千焦|kJ|千卡|kcal|克|g|毫克|mg)?$"
    r"|^(?:能量|蛋白质|脂肪|饱和脂肪|饱和脂肪酸|碳水化合物|糖|钠)$"
)


def string(value):
    return value.strip() if isinstance(value, str) else ""


def _definitions():
    try:
        loaded = json.loads(DEFINITIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _helpers():
    """配料名解析复用 audit.py 的口径，运行期再导入，避免模块循环引用。"""
    from audit import ingredient_words, name_ingredient_hits
    return ingredient_words, name_ingredient_hits


def _strip_groups(value):
    """去掉所有括号内容，只留配料序列本身。"""
    previous = None
    text = value
    while previous != text:
        previous = text
        text = re.sub(r"[（(][^（）()]*[）)]", "", text)
    return text


def _segments(value):
    return [part.strip() for part in re.split(r"[、,;；。\n]+", value) if part.strip()]


def _ingredient_quantity(ingredients, word):
    """该配料在配料表里是否随附了添加量／含量。（如「小米（≥60%）」）"""
    for match in re.finditer(re.escape(word), ingredients):
        found = QUANTITY_TAIL.match(ingredients[match.end():][:24])
        if found:
            return re.sub(r"\s+", "", found.group(1))
    return ""


def _standalone(report, word):
    """该配料是否单独占了一行：单独成行本身是强调形式，那样就不能说「未强调」。"""
    standalone = re.compile(rf"(?:配料表?)?\s*[:：]?\s*{re.escape(word)}")
    return any(standalone.fullmatch(block.text.strip()) for block in report.blocks)


def _name_mention(report, _facts):
    name = report.field("name")
    ingredients = report.field("ingredients")
    if not name or not ingredients:
        return None
    words_of, hits_of = _helpers()
    hits = hits_of(name, words_of(ingredients))
    refs = report.evidence(keys=("name", "ingredients"))
    for fragment, word in hits:
        quantity = _ingredient_quantity(ingredients, word)
        if _standalone(report, word) and not quantity:
            continue
        template = "textWhenQuantityMarked" if quantity else "text"
        return template, {"word": fragment, "ingredient": word, "quantity": quantity}, refs
    return None


def _job_ticket(report, facts):
    ticket = string(facts.get("jobTicketMass"))
    net = report.field("net")
    ticket_match, net_match = MASS.fullmatch(ticket), MASS.fullmatch(net)
    if not ticket_match or not net_match:
        return None
    ticket_value, ticket_unit = mass_of(ticket_match)
    net_value, net_unit = mass_of(net_match)
    if (ticket_value, ticket_unit) == (net_value, net_unit):
        return None
    compact = re.sub(r"\s+", "", ticket)
    refs = [block.id for block in report.blocks if compact in re.sub(r"\s+", "", block.text)]
    return "text", {"ticket": ticket, "net": net}, [*refs, *report.evidence(keys=("net",))]


def _additive_forms(report, _facts):
    ingredients = report.field("ingredients")
    if not ingredients or not GROUPED_ADDITIVE.search(ingredients):
        return None
    outside = _strip_groups(ingredients)
    direct = [name for name in ADDITIVE_NAMES if name in outside]
    if not direct:
        return None
    ordered = sorted(direct, key=lambda name: outside.index(name))
    return "text", {"names": "、".join(ordered[:4])}, report.evidence(keys=("ingredients",))


def _category_names(report, _facts):
    ingredients = report.field("ingredients")
    if not ingredients:
        return None
    found = []
    for segment in _segments(_strip_groups(ingredients)):
        candidates = (segment, re.split(r"[:：]", segment)[-1])
        for candidate in candidates:
            if candidate in CATEGORY_NAMES and candidate not in found:
                found.append(candidate)
    if not found:
        return None
    return "text", {"names": "、".join(found)}, report.evidence(keys=("ingredients",))


def _no_quantity_claim(report, _facts):
    for block in report.blocks:
        text = block.text.strip()
        if NUTRITION_CELL.fullmatch(text):
            continue
        if CLAIM.search(text):
            return None
    return "text", {}, report.evidence(keys=("name", "ingredients"))


DETECTORS = (
    ("name-mention-no-emphasis", _name_mention),
    ("job-ticket-not-label", _job_ticket),
    ("additive-two-forms", _additive_forms),
    ("category-name-allowed", _category_names),
    ("no-quantity-claim", _no_quantity_claim),
)


def notes(report, result):
    """返回 [{"key","title","text","clause","refs"}]，只返回真正命中的。"""
    facts = result.get("labelFacts") if isinstance(result, dict) else None
    facts = facts if isinstance(facts, dict) else {}
    definitions = _definitions()
    collected = []
    for key, detector in DETECTORS:
        definition = definitions.get(key)
        if not isinstance(definition, dict):
            continue
        hit = detector(report, facts)
        if not hit:
            continue
        template_key, values, refs = hit
        template = string(definition.get(template_key)) or string(definition.get("text"))
        if not template:
            continue
        try:
            text = template.format(**values)
        except (KeyError, IndexError, ValueError):
            continue
        collected.append({
            "key": key,
            "title": string(definition.get("title")),
            "text": text,
            "clause": string(definition.get("clause")),
            "refs": list(dict.fromkeys(ref for ref in refs if isinstance(ref, str))),
        })
    return collected
