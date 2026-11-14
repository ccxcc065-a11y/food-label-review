"""同一标签上多处读数的一致性交叉核对（X01 / X02）。

与既有规则的分工，避免同一件事被报两遍：
  * I02（consistency.license_wording）比的是许可证「引导词」的写法（食品许可证 vs 食品生产许可证），
    X01 比的是读数本身。
  * G03（audit.Report.net）比的是「净含量 vs 分包重量之和」在分包规格层面能不能核对，
    并在能复算时自己给出结论；X02 只在两处读数都明确可算、且数值确实不等时，把算式单独列出来。

比对口径：
  * 净含量、生产日期是整件销售单元的项目，全部展示面上出现的读数都要一致；
  * 食品生产许可证编号、产品标准代号按主体分组比对——被委托商 A/B/C…、各料包生产商
    本来就可以各有各的许可证和标准代号，跨主体比对会产生假问题，所以只在同一主体名下比。
  * 不比较正文里的其它数字（营养成分、保质期、电话等），也不替客户判断哪一处是对的。
"""

import re
from datetime import date
from decimal import Decimal

from labelreview.extract import MASS, MULTIPLIER, UNITS


NET_LABEL = re.compile(r"净含量(?:及规格)?\s*[:：]?\s*[（(【]?\s*")
PRODUCTION = re.compile(
    r"生产日期\s*[:：]?\s*"
    r"([0-9]{4}\s*[-/.年]\s*[0-9]{1,2}\s*[-/.月]\s*[0-9]{1,2}\s*日?)"
)
LICENSE = re.compile(
    r"(?:食品生产许可证编号|食品生产许可证号|食品许可证编号|生产许可证编号|"
    r"生产许可证号|许可证编号)?\s*[:：]?\s*(SC[0-9]{14})",
    re.I,
)
STANDARD = re.compile(
    r"(?:产品执行标准(?:代号|编号|号)?|产品标准(?:代号|编号|号)?|执行标准(?:代号|编号|号)?|标准代号)"
    r"\s*[:：]?\s*"
    r"([A-Za-z]{1,4}\s*/?\s*[A-Za-z]{0,6}\s*[0-9]{2,}(?:\.[0-9]+)?[A-Za-z]?(?:[-—–][0-9]{4})?)"
)
# 同一主体的判定：块首若是被委托商、料包生产商之类的抬头，就按它区分主体；
# 其余块（标签主体部分的净含量、许可证等）统一记为「标签主体」。
# 抬头没带字母时再拼上生产商名称作区分——同一格式的两家不同企业不能当成一个主体。
SUBJECT = re.compile(r"^(被委托商|受委托方|受托方|[\u4e00-\u9fff]{1,6}包生产商)\s*([A-Z])?")
COMPANY = re.compile(r"(?:生产商|制造商|生产企业|委托商)\s*[:：]\s*([^，,。;；:：]{2,30}?(?:有限公司|集团|厂|合作社))")
MAIN = "标签主体"
PROJECTS = (
    ("净含量", "net", False),
    ("生产日期", "production", False),
    ("食品生产许可证编号", "license", True),
    ("产品标准代号", "standard", True),
)
# 分包重量之和无法逐包求和时用的排除词：出现乘数或小计的写法交给 G03，不在 X02 里重算。
PACK_COMPLEX = re.compile(r"净含量|含量|合计|共计|总量|总重|约")
NUMBER = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)")


def mass_of(match):
    """把 MASS 的命中项换算成 (数值, 维度)，克系归到 g、体积系归到 mL。"""
    families = {"kg": ("g", 1000), "g": ("g", 1), "mg": ("g", Decimal(".001")),
                "L": ("mL", 1000), "mL": ("mL", 1)}
    dimension, multiplier = families[UNITS[match.group(2).lower()]]
    return Decimal(match.group(1)) * multiplier, dimension


def _display(value):
    return re.sub(r"\s+", "", value)


def _net_readings(text):
    for label in NET_LABEL.finditer(text):
        match = MASS.match(text, label.end())
        if not match:
            continue
        amount, dimension = mass_of(match)
        yield (dimension, amount), _display(match.group())


def _production_readings(text):
    for match in PRODUCTION.finditer(text):
        parts = NUMBER.findall(match.group(1))
        if len(parts) != 3:
            continue
        try:
            day = date(*(int(part) for part in parts))
        except ValueError:
            continue
        yield day.isoformat(), _display(match.group(1))


def _license_readings(text):
    for match in LICENSE.finditer(text):
        yield match.group(1).upper(), match.group(1).upper()


def _standard_readings(text):
    for match in STANDARD.finditer(text):
        code = re.sub(r"\s+", "", match.group(1)).upper()
        # 年份后缀（-2025 之类）是版本信息，不参与「同一个代号」的判定。
        yield re.sub(r"[-—–]\d{4}$", "", code), _display(match.group(1))


READERS = {"net": _net_readings, "production": _production_readings,
           "license": _license_readings, "standard": _standard_readings}


def _subject(block):
    found = SUBJECT.match(block.text)
    if not found:
        return MAIN
    label = f"{found.group(1)}{found.group(2) or ''}"
    company = COMPANY.search(block.text)
    return f"{label}｜{company.group(1)}" if company else label


def _collect(report, kind):
    reader = READERS[kind]
    collected = []
    for page in report.pages:
        for block in page:
            for key, shown in reader(block.text):
                collected.append({"key": key, "shown": shown, "block": block})
    return collected


def inconsistencies(report):
    """返回 [{项目, 主体, 读数}]；只包含同一项目出现 2 个以上读数的情形。"""
    rows = []
    for label, kind, per_subject in PROJECTS:
        collected = _collect(report, kind)
        if not collected:
            continue
        groups = {}
        for reading in collected:
            groups.setdefault(_subject(reading["block"]) if per_subject else MAIN, []).append(reading)
        for subject, readings in groups.items():
            if len({reading["key"] for reading in readings}) < 2:
                continue
            rows.append({"label": label, "subject": subject, "readings": readings})
    return rows


def _excerpt(block):
    text = re.sub(r"\s+", "", block.text)
    return text if len(text) <= 28 else text[:28] + "…"


def check_cross_source(report):
    """同一字段多处读数不一致 → 一条 X01 review 汇总。全部一致或只有一处时不产出条目。"""
    rows = inconsistencies(report)
    if not rows:
        return
    parts, refs = [], []
    for row in rows:
        seen = {}
        for reading in row["readings"]:
            seen.setdefault(reading["key"], []).append(reading)
            refs.append(reading["block"].id)
        listing = "、".join(
            f"{items[0]['shown']}（原文「{_excerpt(items[0]['block'])}」）"
            for items in seen.values()
        )
        scope = "" if row["subject"] == MAIN else f"，主体：{row['subject']}"
        parts.append(f"「{row['label']}」读到 {len(seen)} 个不同读数：{listing}{scope}")
    detail = ("同一标签对同一项目给出了不同读数：" + "；".join(parts) + "。"
              "两处说的是同一项目，可直接对照。请核对原稿，确认以哪一处为准，并把各处文字统一；"
              "本项只作对照提示，不据此认定违规。")
    item = report.add("X01", "review", "同一项目多处读数不一致",
                      "确认原稿以哪一处为准，统一各处的读数",
                      detail, refs)
    item["basis"] = "同一标签内部一致性核对（同一项目的多处读数比对）"
    item["category"] = "标签内对照"


def check_pack_sum(report):
    """净含量与标签所列分包重量之和都明确、且数值不等 → X02 review，detail 写出算式。"""
    net = report.field("net")
    packs = report.field("packs")
    if not net or not packs:
        return
    match = MASS.fullmatch(net)
    if not match:
        return
    if MULTIPLIER.search(packs) or PACK_COMPLEX.search(packs):
        return
    amounts = [mass_of(reading) for reading in MASS.finditer(packs)]
    total, dimension = mass_of(match)
    if len(amounts) < 2 or any(unit != dimension for _, unit in amounts):
        return
    subtotal = sum((amount for amount, _ in amounts), Decimal(0))
    if subtotal == total:
        return
    unit = "克" if dimension == "g" else "毫升"
    formula = "＋".join(format(amount, "f").rstrip("0").rstrip(".") if amount % 1 else str(int(amount))
                        for amount, _ in amounts)
    difference = abs(total - subtotal)

    def shown(value):
        return format(value, "f").rstrip("0").rstrip(".") if value % 1 else str(int(value))

    detail = (f"外包装标示净含量 {net}；标签所列 {len(amounts)} 个分包重量之和为 "
              f"{formula}＝{shown(subtotal)} {unit}，两者相差 {shown(difference)} {unit}。"
              "分包重量与净含量属于同一销售单元的换算关系，可直接相加对照。"
              "请核对分包重量或净含量标示，确认哪一处需要修正。")
    item = report.add("X02", "review", "净含量与分包重量之和不一致",
                      "核对分包重量与净含量标示，确认修正其中一处",
                      detail, report.evidence(keys=("net", "packs")))
    item["basis"] = "同一标签内部一致性核对（净含量与分包重量之和）"
    item["category"] = "净含量"


def run(report):
    check_cross_source(report)
    check_pack_sum(report)
