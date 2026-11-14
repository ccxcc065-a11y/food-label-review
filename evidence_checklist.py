"""N02 证据清单：三态由服务端计算，模型/客户端不得填写。

状态：missing / extracted / verified / conflict
upgradeAllowed = 所有必需项均为 verified。
"""
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation


def _decimal(value):
    try:
        return Decimal(str(value).replace(",", "").replace("，", "").strip())
    except (ArithmeticError, ValueError, InvalidOperation):
        return None


def normalize(value, unit=None):
    """把读数规范化为同一单位口径：12.5 g == 12500 mg。"""
    number = _decimal(value)
    if number is None:
        return None
    unit = str(unit or "").strip().lower()
    if unit in ("mg", "毫克"):
        return number / Decimal(1000)
    return number


def _block_get(block, key, default=None):
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def readings_for(key, label, value, unit, block, manual_rows, field_aliases, reread=None):
    """收集同一字段的读数（OCR、程序重读、人工核验），保留历史。"""
    rows = []
    box = list(_block_get(block, "box") or []) if block is not None else []
    # 原始识别值：即使被放大重读改写，也必须作为一次独立读数保留下来，
    # 否则「两次一致」就是拿改后的值和改后的值比较。
    original = _block_get(block, "originalText") if block is not None else None
    if not original and isinstance(reread, dict):
        # 重读标记里保存的 from 就是首次识别值（Block 对象本身不带原文）。
        original = reread.get("from")
    if isinstance(original, str) and original.strip() and original.strip() != str(value or "").strip():
        rows.append({"value": original.strip(), "unit": str(unit or ""), "source": "ocr-original",
                     "blockId": _block_get(block, "id"), "box": box, "at": None})
    if value not in (None, ""):
        rows.append({"value": str(value), "unit": str(unit or ""), "source": "ocr",
                     "blockId": _block_get(block, "id"), "box": box, "at": None})
    if reread is None:
        reread = _block_get(block, "reread") if block is not None else None
    if isinstance(reread, dict) and reread.get("to") not in (None, ""):
        rows.append({"value": str(reread.get("to")), "unit": str(unit or ""), "source": "reread",
                     "scale": reread.get("scale"), "blockId": _block_get(block, "id"), "box": box,
                     "at": reread.get("at")})
    for row in manual_rows or []:
        if not isinstance(row, dict):
            continue
        field = str(row.get("field") or "")
        wants_unit = any("单位" in str(alias) for alias in field_aliases)
        wants_nrv = any("NRV" in str(alias).upper() for alias in field_aliases)
        if wants_nrv and "NRV" not in field.upper():
            continue
        if wants_unit and "单位" not in field:
            continue
        if not wants_nrv and not wants_unit and ("单位" in field or "NRV" in field.upper()):
            continue
        # 别名里的「单位」「NRV」只是修饰词，不能单独作为匹配依据：
        # 否则「脂肪单位」会被「能量单位」当成自己的记录，凭空造出冲突。
        # 剥掉「单位」「NRV」这类修饰词后比较词干：
        # 「脂肪-NRV」只应匹配脂肪这一项，而不该被别的营养素抢走。
        stems = [re.sub(r"(单位|NRV|%|％|[\s\-—－])", "", str(alias), flags=re.IGNORECASE)
                 for alias in field_aliases if alias]
        stems = [stem for stem in stems if stem]
        if stems and not any(stem in field for stem in stems):
            continue
        if row.get("value") in (None, ""):
            continue
        rows.append({"value": str(row.get("value")), "unit": str(row.get("unit") or unit or ""),
                     "source": "manual", "by": row.get("by") or "人工",
                     "reason": row.get("reason") or row.get("note") or "人工核验",
                     "at": row.get("at")})
    return rows


def evidence_item(key, label, value, unit, block, manual_rows, aliases,
                  bindings, adopted=None, required=True, reread=None):
    """单个证据项：missing / extracted / verified / conflict。"""
    readings = readings_for(key, label, value, unit, block, manual_rows, aliases, reread=reread)
    numbers = [normalize(row["value"], row.get("unit")) for row in readings]
    numbers = [number for number in numbers if number is not None]
    agrees = len(set(numbers)) <= 1
    current = normalize(value, unit)
    adopted_value = normalize((adopted or {}).get("value"), (adopted or {}).get("unit") or unit) \
        if adopted else None
    if value in (None, ""):
        state = "missing"
    elif current is None:
        # 非数值证据（单位、计量基准）：必须有同值的重读/人工记录才算核验。
        matching = [row for row in readings if str(row.get("value")).strip() == str(value).strip()]
        state = "verified" if any(row["source"] in ("reread", "manual") for row in matching) else "extracted"
        if any(str(row.get("value")).strip() != str(value).strip() and row["source"] == "manual"
               for row in readings):
            state = "conflict"
    elif adopted_value is not None and adopted_value == current:
        state = "verified"
    elif not agrees:
        # 人工回看原图并记录了与当前值相同的读数，视为冲突已解除；
        # 这是唯一允许「改后值」被判为已核验的途径。
        human_ok = any(row["source"] == "manual" and
                       normalize(row["value"], row.get("unit")) == current for row in readings)
        # 程序重读与当前读数不一致才是读数冲突；人工记录与当前值不符按“原核验失效”处理。
        reread_conflict = any(row["source"] == "reread" and
                              normalize(row["value"], row.get("unit")) not in (None, current)
                              for row in readings)
        # 原始识别值与当前值不同同样属于未解决冲突：这种情况下「已核验」
        # 等于用改后的值证明改得对，必须有人工确认或采纳记录才能解除。
        original_conflict = any(row["source"] == "ocr-original" and
                                normalize(row["value"], row.get("unit")) not in (None, current)
                                for row in readings)
        if human_ok:
            state = "verified"
        else:
            state = "conflict" if (reread_conflict or original_conflict) else "extracted"
    else:
        verified = any(row["source"] in ("reread", "manual") and
                       normalize(row["value"], row.get("unit")) == current for row in readings)
        state = "verified" if verified else "extracted"
    binding_material = json.dumps({"key": key, "value": str(value), "unit": str(unit or ""),
                                   "blockId": _block_get(block, "id"),
                                   "box": list(_block_get(block, "box") or []),
                                   "bindings": bindings}, ensure_ascii=False, sort_keys=True)
    return {"key": key, "label": label, "state": state,
            "satisfied": state == "verified", "required": bool(required),
            "value": None if value is None else str(value), "unit": str(unit or ""),
            "readings": readings, "agree": agrees,
            "adopted": adopted, "binding": hashlib.sha256(binding_material.encode()).hexdigest()[:16]}


def checklist(conclusion, required, bindings, upgrade_note=""):
    """一个结论一份清单；satisfied 由服务端逐项算，百分比不参与放行。"""
    items = list(required)
    allowed = bool(items) and all(item["state"] == "verified" for item in items)
    satisfied = sum(1 for item in items if item["state"] == "verified")
    return {"conclusion": conclusion, "required": items,
            "satisfiedCount": satisfied, "requiredCount": len(items),
            "upgradeAllowed": allowed,
            "note": upgrade_note or ("全部必需项核验通过才可升级为确定问题" if not allowed else "")}


def state_text(checklist_value):
    labels = {"missing": "缺失", "extracted": "已读到未核验", "verified": "已核验", "conflict": "读数冲突"}
    parts = ["%s：%s" % (item["label"], labels.get(item["state"], item["state"]))
             for item in checklist_value.get("required") or []]
    return "；".join(parts)
