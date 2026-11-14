"""营养数值合理性守卫：在任何计算之前拦截不可能的读数。

只对「每 100 克」「每 100 毫升」这类可确定的基准启用绝对上限；
「每份」的份量未知，全部跳过，交给其他检查处理。
"""

import re
from decimal import Decimal


# 读数里可能出现千分位逗号，先去掉再取数字。
THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
NUMBER = re.compile(r"\d+(?:\.\d+)?")

# 常见单位写法与「每 100 克（毫升）」下的物理上限。
GRAMS = {"g": "g", "克": "g"}
MILLIGRAMS = {"mg": "mg", "毫克": "mg"}
KILOGRAMS = {"kg": "kg", "千克": "kg"}
KILOJOULES = {"kj": "kJ", "千焦": "kJ", "千焦耳": "kJ"}
KILOCALORIES = {"kcal": "kcal", "千卡": "kcal", "大卡": "kcal"}
MILLILITRES = {"ml": "mL", "毫升": "mL"}
LITRES = {"l": "L", "升": "L"}
UNITS = {**GRAMS, **MILLIGRAMS, **KILOGRAMS, **KILOJOULES, **KILOCALORIES, **MILLILITRES, **LITRES}

# 单项营养素不可能超过食品本身：每 100 克最多 100 克。
GRAM_LIMITS = {
    "protein": "蛋白质",
    "fat": "脂肪",
    "carb": "碳水化合物",
    "sugar": "糖",
    "saturated": "饱和脂肪",
}
ENERGY_LIMITS = {"kJ": (Decimal("3800"), "纯脂肪 100 g 约 3700 kJ"), "kcal": (Decimal("900"), "约合 3800 kJ")}
# 食用盐本身就是产品，钠约 39000 mg/100 g；上限取 40000 mg 才不会把纯盐判成错读。
SODIUM_LIMIT = Decimal("40000")

ORDER = ("energy", "protein", "fat", "saturated", "carb", "sugar", "sodium")


def scope_name(basis):
    """把计量基准归一成「每 100 克」「每 100 毫升」；份量未知时返回空串。"""
    flat = re.sub(r"\s+", "", str(basis or "")).lower()
    # 「每份 100 克」的份量由包装决定，不能套用每 100 克的绝对上限。
    if "每份" in flat:
        return ""
    if re.search(r"(?:每)?100\s*(?:毫升|ml)", flat):
        return "每 100 毫升"
    if re.search(r"(?:每)?100\s*(?:克|g)", flat):
        return "每 100 克"
    return ""


def unit_name(value):
    """把常见单位写法归一；数值本身不参与判定。"""
    clean = re.sub(r"[.\s]", "", str(value or "")).lower()
    return UNITS.get(clean, "")


def amount(value):
    """从 "125"、"125.0"、"125克" 之类的读数中取数字；取不到返回 None。"""
    if type(value) not in (str, int, float):
        return None
    found = NUMBER.search(THOUSANDS.sub("", str(value)))
    if not found:
        return None
    try:
        return Decimal(found.group())
    except ArithmeticError:
        return None


def display(value):
    """把 Decimal 写成标签习惯的读数，去掉多余的零。"""
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def reading(key, label, value, raw_unit, reason):
    return {"key": key, "label": label, "value": display(value), "unit": raw_unit, "reason": reason}


def anomalies(nutrition, basis):
    """
    nutrition 形如 {"energy": {"value": "1228", "unit": "kJ", ...},
                    "fat": {"value": "125", "unit": "g", ...}, ...}
    basis 是计量基准字符串，如 "每100克"、"每100毫升"、"每份"
    返回异常列表，每项至少含 {"key","label","value","unit","reason"}
    空列表 = 未发现异常
    """
    scope = scope_name(basis)
    # 「每份」或无法确定基准时不做绝对上限判断。
    if not scope or not isinstance(nutrition, dict):
        return []
    results = []
    for key in ORDER:
        item = nutrition.get(key)
        if not isinstance(item, dict):
            continue
        value = amount(item.get("value"))
        # 取不到数字时不判异常，交给其他检查。
        if value is None:
            continue
        raw_unit = str(item.get("unit") or "").strip()
        unit = unit_name(raw_unit)
        if not unit:
            # 单位留空或无法识别时，找不到「与项目不符」的确定证据。
            continue
        if key == "energy":
            if unit not in ENERGY_LIMITS:
                results.append(reading(key, "能量", value, raw_unit,
                                       f"能量的标示单位是「{raw_unit}」，应为「kJ」或「kcal」，疑似口径或单位读错。"))
                continue
            limit, basis_note = ENERGY_LIMITS[unit]
            if value > limit:
                results.append(reading(key, "能量", value, raw_unit,
                                       f"{scope}食品的能量不可能超过 {display(limit)} {unit}（{basis_note}），"
                                       "疑似小数点或单位读错。"))
        elif key == "sodium":
            if unit != "mg":
                results.append(reading(key, "钠", value, raw_unit,
                                       f"钠的标示单位是「{raw_unit}」，应为「mg」，疑似口径或单位读错。"))
            elif value > SODIUM_LIMIT:
                results.append(reading(key, "钠", value, raw_unit,
                                       f"{scope}食品的钠不可能超过 {display(SODIUM_LIMIT)} mg"
                                       "（纯食盐约 39000 mg/100 g），疑似小数点或单位读错。"))
        else:
            label = GRAM_LIMITS[key]
            if unit != "g":
                results.append(reading(key, label, value, raw_unit,
                                       f"{label}的标示单位是「{raw_unit}」，应为「g」，疑似口径或单位读错。"))
            elif value > 100:
                results.append(reading(key, label, value, raw_unit,
                                       f"{scope}食品中{label}不可能超过 100 g（单项营养素不会超过食品本身），"
                                       "疑似小数点或单位读错。"))
    return results
