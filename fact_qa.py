"""标签事实质检：营养表检测、OCR 读数、配对可靠性和待核对分流。

把「检测到表格、读到数字、最终提取为空或全零」判定为结构提取失败，
不得悄悄变成一张空的营养表。
"""
import hashlib
import json
import re

from extract import (
    BASIS,
    NUTRIENT_AMOUNT,
    NUTRIENT_PATTERN,
    NUTRITION_KEYS,
    PERCENT,
    basis_name,
    is_valid_basis,
    nutrient_unit,
    table_bounds,
    table_rows,
)


NUTRIENT_LABELS = {
    "energy": "能量",
    "protein": "蛋白质",
    "fat": "脂肪",
    "saturated": "饱和脂肪",
    "carb": "碳水化合物",
    "sugar": "糖",
    "sodium": "钠",
}

# 结论对营养读数的依赖。一个字段有问题，只阻断依赖它的结论。
NUTRITION_CONCLUSION_IDS = (
    "N00", "N01", "N02", "N03", "F01", "N02H",
    "nutrition-conflict", "nutrition-extract-failure",
)
CONCLUSION_DEPENDENCIES = {
    "N00": set(NUTRITION_KEYS),
    "N01": set(NUTRITION_KEYS) | {"basis"},
    "N02": set(NUTRITION_KEYS) | {"basis"},
    "N03": {"energy", "protein", "fat", "carb", "basis"},
    "F01": {"saturated", "sugar"},
    "N02H": {"energy", "protein", "fat", "carb", "basis"},
    "nutrition-conflict": set(NUTRITION_KEYS) | {"basis"},
    "nutrition-extract-failure": set(NUTRITION_KEYS) | {"basis"},
}

STRUCTURAL_FAILURE = "structural-failure"
STRUCTURAL_FAILURE_TEXT = (
    "已检测到营养成分表并读到数字，但项目与数值未能可靠配对，不能作为空表或全零表通过。"
    "请核对原图行列对应后再审。"
)

# 糖没有标准 NRV，空白是正确标示，不得改写成 0，也不得因此进入待核对。
ALLOWED_BLANK_NRV = {"sugar"}


def _block_id(block):
    return getattr(block, "id", None) if not isinstance(block, dict) else block.get("id")


def _block_text(block):
    return getattr(block, "text", None) if not isinstance(block, dict) else block.get("text")


def _block_box(block):
    box = getattr(block, "box", None) if not isinstance(block, dict) else block.get("box")
    return list(box or [])


def pages_digest(pages):
    """与 Report.image_digest 同一口径：页内块的编号、文字和坐标。"""
    material = [[[_block_id(block), _block_text(block), _block_box(block)]
                 for block in (page or [])] for page in (pages or [])]
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
                          .encode()).hexdigest()[:16]


def _empty_or_zero(value):
    text = str(value or "").strip()
    if not text:
        return True
    try:
        return float(text.replace(",", "")) == 0.0
    except ValueError:
        return False


def _structured_empty(nutrition):
    if not isinstance(nutrition, dict):
        return True
    return all(_empty_or_zero((nutrition.get(key) or {}).get("value")) for key in NUTRITION_KEYS)


def _in_header_region(block, headers, region=None):
    if not headers:
        return True
    y = getattr(block, "y", None)
    x = getattr(block, "x", None)
    if y is None or x is None:
        if isinstance(block, dict) and block.get("box"):
            x, y = block["box"][0], block["box"][1]
        else:
            return True
    # 标题左右边界不能当成整张表的左右边界；垂直方向仍用较宽的表带，
    # 以便把没能配对上、但同页读到的含量数字记入「已读到数字」。
    left = min(header.x for header in headers)
    if region is not None:
        left = min(left, region[0])
    return any(y >= header.y - .02 and y <= header.y + .90 and x >= left - .30
               for header in headers)


def nrv_check_status(key, nrv):
    """缺 NRV 时比较结果为「无法核对」，不得把空值算成数值不一致。糖的空白是允许的。"""
    if str(nrv or "").strip():
        return "可核对"
    if key in ALLOWED_BLANK_NRV:
        return "空白允许"
    return "无法核对"


def _scan_page(blocks):
    headers, region = table_bounds(blocks)
    if not headers:
        headers = [block for block in blocks if "营养成分表" in (_block_text(block) or "")]
    labels = []
    for block in blocks:
        match = NUTRIENT_PATTERN.match(_block_text(block) or "")
        if match:
            labels.append((match.lastgroup, block))
    amounts = []
    for block in blocks:
        text = _block_text(block) or ""
        # 「每份 30 克」是计量基准，不应当成未配对的营养含量。
        if BASIS.search(text) and not NUTRIENT_PATTERN.match(text):
            continue
        found_amounts = list(NUTRIENT_AMOUNT.finditer(text))
        if not found_amounts:
            continue
        in_region = _in_header_region(block, headers, region) if headers else True
        has_nutrient_unit = any(nutrient_unit(found.group(2)) in {"kJ", "kcal", "mg"}
                                for found in found_amounts)
        is_label = any(_block_id(block) == _block_id(label) for _, label in labels)
        # 表外净含量等「克」读数不计入营养表数字。
        if headers and not in_region and not has_nutrient_unit and not is_label:
            continue
        for found in found_amounts:
            amounts.append({
                "value": found.group(1),
                "unit": nutrient_unit(found.group(2)),
                "rawUnit": found.group(2),
                "ref": _block_id(block),
                "box": _block_box(block),
            })
    percents = []
    for block in blocks:
        for found in PERCENT.finditer(_block_text(block) or ""):
            if headers and not _in_header_region(block, headers, region):
                continue
            percents.append({
                "value": found.group(1),
                "unit": "%",
                "ref": _block_id(block),
                "box": _block_box(block),
            })
    bases = [(basis_name(match.group()), block)
             for block in blocks for match in BASIS.finditer(_block_text(block) or "")]
    rows, row_bases = table_rows(blocks)
    return {
        "headers": headers,
        "labels": labels,
        "amounts": amounts,
        "percents": percents,
        "bases": row_bases or bases,
        "rows": rows,
    }


def _pairing_reason(data, amounts_count, percents_count):
    if amounts_count > 1:
        return "ambiguous-pairing", "同一行读到多个含量数值，无法唯一配对。"
    if percents_count > 1:
        return "ambiguous-pairing", "同一行读到多个百分数，无法唯一配对。"
    if data is None:
        return "unpaired", "已读到项目名称，但未能与含量可靠配对。"
    return "reliable", ""


def build_nutrition_qa(pages, draft):
    nutrition = draft.get("nutrition") if isinstance(draft, dict) else {}
    nutrition = nutrition if isinstance(nutrition, dict) else {}
    fields = (draft.get("fields") if isinstance(draft, dict) else {}) or {}
    conflict = bool(isinstance(draft, dict) and draft.get("nutritionConflict") is True)
    header_refs = []
    ocr_readings = []
    pairing_reliable = []
    pairing_ambiguous = []
    pending = []
    detected = False
    numbers_read = False
    basis_values = []
    basis_refs = []

    for blocks in pages or []:
        scan = _scan_page(blocks)
        if scan["headers"] or scan["rows"] or len(scan["labels"]) >= 2:
            detected = True
        header_refs.extend(ref for ref in (_block_id(block) for block in scan["headers"]) if ref)
        if scan["amounts"] or any(data is not None for _, data, _ in scan["rows"]):
            # 表格区域内读到带单位的数字，或已从分行单元格还原出含量，即视为「读到了营养表数字」。
            if scan["headers"] or scan["labels"] or scan["rows"]:
                numbers_read = True
        for value, block in scan["bases"]:
            basis_values.append(value)
            if _block_id(block):
                basis_refs.append(_block_id(block))
        seen_keys = set()
        for key, data, evidence in scan["rows"]:
            seen_keys.add(key)
            amount_count = 0 if data is not None else 2  # table_rows 在多值时记 data=None
            # 用已配对结果判定；可靠行的 data 为 (value, unit, nrv)
            if data is None:
                # 可能是多值，也可能是缺基准被置空前的歧义行。再看原始行无法区分时按歧义处理。
                code, reason = "ambiguous-pairing", "同一行读到的项目与数值无法唯一配对。"
                pairing = "ambiguous"
                value = unit = nrv = ""
                pairing_ambiguous.append({"key": key, "label": NUTRIENT_LABELS.get(key, key),
                                          "reason": reason, "code": code, "refs": list(evidence or [])})
                pending.append({"key": key, "label": NUTRIENT_LABELS.get(key, key),
                                "reason": reason, "code": code, "refs": list(evidence or [])})
            else:
                value, unit, nrv = data
                pairing, reason = "reliable", ""
                code = ""
                pairing_reliable.append(key)
            ocr_readings.append({
                "key": key,
                "label": NUTRIENT_LABELS.get(key, key),
                "value": value,
                "unit": unit,
                "nrv": nrv,
                "nrvCheck": nrv_check_status(key, nrv),
                "pairing": pairing,
                "reason": reason,
                "refs": list(evidence or []),
            })
        # 标签被找到但 table_rows 因同行没有含量而整行丢弃：仍要记 OCR 读到了项目。
        for key, block in scan["labels"]:
            if key in seen_keys:
                continue
            detected = True
            reason = "已读到项目名称，但同行没有可配对的含量数值。"
            ocr_readings.append({
                "key": key,
                "label": NUTRIENT_LABELS.get(key, key),
                "value": "",
                "unit": "",
                "nrv": "",
                "nrvCheck": nrv_check_status(key, ""),
                "pairing": "unpaired",
                "reason": reason,
                "refs": [_block_id(block)] if _block_id(block) else [],
            })
            pending.append({"key": key, "label": NUTRIENT_LABELS.get(key, key),
                            "reason": reason, "code": "unpaired", "refs": [_block_id(block)] if _block_id(block) else []})
        # 表区内未挂到任何项目行的含量数字，也记入 OCR 读数。
        paired_refs = {ref for row in ocr_readings for ref in row.get("refs") or []}
        for amount in scan["amounts"]:
            if amount["ref"] and amount["ref"] in paired_refs:
                continue
            if any(row.get("value") == amount["value"] and row.get("unit") == amount["unit"]
                   for row in ocr_readings):
                continue
            numbers_read = True
            ocr_readings.append({
                "key": None,
                "label": "",
                "value": amount["value"],
                "unit": amount["unit"],
                "nrv": "",
                "nrvCheck": "无法核对",
                "pairing": "unpaired",
                "reason": "读到含量数值，但未能与营养项目可靠配对。",
                "refs": [amount["ref"]] if amount["ref"] else [],
            })

    basis_field = ((fields.get("basis") or {}) if isinstance(fields, dict) else {}).get("value") or ""
    unique_bases = list(dict.fromkeys(basis_values or ([basis_field] if basis_field else [])))
    displayed = []
    for key in NUTRITION_KEYS:
        item = nutrition.get(key) or {}
        value = item.get("value") or ""
        if str(value).strip():
            displayed.append({
                "key": key,
                "label": NUTRIENT_LABELS[key],
                "value": str(value),
                "unit": str(item.get("unit") or ""),
                "nrv": str(item.get("nrv") or ""),
                "nrvCheck": nrv_check_status(key, item.get("nrv") or ""),
                "reason": "已可靠配对，写入营养表。",
                "refs": list(item.get("refs") or []),
            })
        elif key not in {row["key"] for row in pending} and detected:
            # 表已出现但该行没有进入展示：若 OCR 也没读到该行，记为未覆盖，而不是结构失败的唯一依据。
            if not any(row.get("key") == key for row in ocr_readings):
                pending.append({
                    "key": key,
                    "label": NUTRIENT_LABELS[key],
                    "reason": "营养成分表中未读到该项目。",
                    "code": "missing-row",
                    "refs": [],
                })

    # 糖的空白 NRV% 是正确标示，从待核对里去掉「缺 NRV」这类误伤。
    pending = [row for row in pending if not (
        row.get("key") == "sugar" and "nrv" in str(row.get("code") or "").lower()
    )]

    empty = _structured_empty(nutrition)
    if detected and numbers_read and empty:
        status = STRUCTURAL_FAILURE
        failure = STRUCTURAL_FAILURE_TEXT
        if not any(row.get("code") == STRUCTURAL_FAILURE for row in pending):
            pending.insert(0, {
                "key": "nutrition-table",
                "label": "营养成分表",
                "reason": failure,
                "code": STRUCTURAL_FAILURE,
                "refs": list(dict.fromkeys(header_refs)),
            })
        displayed = []
    elif conflict:
        status = "conflict"
        failure = "营养表在不同图片或不同列上的读数不一致，需先核对归属后再展示。"
    elif not detected:
        status = "absent"
        failure = ""
    elif empty:
        status = "empty"
        failure = "未形成可展示的营养表行，请核对应标项目和计量基准。"
    else:
        status = "ok"
        failure = ""

    # 方案字段名与现有字段并列保留，便于审核详情卡按任一名称读取。
    return {
        "detected": detected,
        "tableDetected": detected,
        "numbersRead": numbers_read,
        "headerRefs": list(dict.fromkeys(header_refs)),
        "basis": {
            "value": basis_field,
            "normalized": unique_bases[0] if len(unique_bases) == 1 else "",
            "candidates": unique_bases,
            "valid": is_valid_basis(basis_field),
            "refs": list(dict.fromkeys(basis_refs)),
        },
        "ocrReadings": ocr_readings,
        "candidateRows": ocr_readings,
        "pairing": {
            "reliable": list(dict.fromkeys(pairing_reliable)),
            "ambiguous": pairing_ambiguous,
        },
        "extractStatus": status,
        "extractionStatus": status,
        "failureReason": failure,
        "displayedRows": displayed,
        "parsedRows": displayed,
        "pendingReview": pending,
        "unresolvedRows": pending,
        "nutritionConflict": conflict,
    }


def attach_nutrition_qa(draft, pages):
    if not isinstance(draft, dict):
        return draft
    qa = {"nutritionTable": build_nutrition_qa(pages, draft)}
    draft["factQa"] = qa
    draft["nutritionExtractFailure"] = is_structural_failure(qa)
    return draft


def is_structural_failure(qa):
    table = _table(qa)
    return table.get("extractStatus") == STRUCTURAL_FAILURE


def _table(qa):
    if isinstance(qa, dict) and isinstance(qa.get("nutritionTable"), dict):
        return qa["nutritionTable"]
    if isinstance(qa, dict) and "extractStatus" in qa:
        return qa
    return {}


def disputed_keys(qa, stale_corrections=None):
    """当前有争议、不可靠或已失效确认的营养项。"""
    keys = set()
    table = _table(qa)
    for row in table.get("pairing", {}).get("ambiguous") or []:
        if row.get("key"):
            keys.add(row["key"])
    for row in table.get("pendingReview") or []:
        if row.get("key") in NUTRITION_KEYS:
            keys.add(row["key"])
    for row in stale_corrections or []:
        key = correction_nutrient_key(row)
        if key:
            keys.add(key)
    return keys


def correction_nutrient_key(row):
    if not isinstance(row, dict):
        return None
    field = str(row.get("field") or row.get("key") or "")
    if field in NUTRITION_KEYS:
        return field
    if field.startswith("营养表-"):
        parts = field.split("-")
        if len(parts) >= 2 and parts[1] in NUTRITION_KEYS:
            return parts[1]
    for key, label in NUTRIENT_LABELS.items():
        if label and label in field:
            return key
    if field in {"计量基准", "basis"}:
        return "basis"
    return None


def is_bound_correction(row):
    if not isinstance(row, dict):
        return False
    return all(row.get(name) not in (None, "") for name in (
        "field", "adoptedValue", "unit", "basis")) and (
        row.get("imageId") or row.get("imageDigest") or row.get("evidenceDigest")
    ) and bool(row.get("region"))


def evaluate_manual_corrections(rows, draft, pages, image_digest=None):
    """核验人工改值是否仍有效。换图或相关数据变化后，旧确认不得继续视为有效。"""
    valid, stale = [], []
    current_digest = image_digest or pages_digest(pages)
    fields = (draft or {}).get("fields") if isinstance(draft, dict) else {}
    nutrition = (draft or {}).get("nutrition") if isinstance(draft, dict) else {}
    current_basis = ((fields or {}).get("basis") or {}).get("value") or ""
    blocks = {}
    page_ids = {}
    for page in pages or []:
        page_id = None
        if isinstance(page, dict):
            page_id = page.get("id")
            iterable = page.get("blocks") or []
        else:
            iterable = page
        for block in iterable:
            identifier = _block_id(block)
            if identifier:
                blocks[identifier] = block
                if page_id:
                    page_ids[identifier] = page_id
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("field"):
            continue
        if not is_bound_correction(row):
            # 未绑定出处的旧记录仍按值比较，交给 audit 原有逻辑处理。
            valid.append(row)
            continue
        reasons = []
        evidence = row.get("evidenceDigest") or ""
        # 兼容只写入了 16 位页摘要的记录；64 位文件哈希不得与 OCR 页摘要直接比较。
        if not evidence and len(str(row.get("imageDigest") or "")) == 16:
            evidence = row["imageDigest"]
        if evidence and evidence != current_digest:
            reasons.append("原图或识别内容已变化")
        file_digest = str(row.get("imageDigest") or "")
        if len(file_digest) == 64:
            current_files = set()
            for page in pages or []:
                if isinstance(page, dict) and page.get("digest"):
                    current_files.add(page["digest"])
            if current_files and file_digest not in current_files:
                reasons.append("原图或识别内容已变化")
        region = row.get("region") if isinstance(row.get("region"), dict) else {}
        block_id = region.get("blockId")
        if block_id:
            block = blocks.get(block_id)
            if block is None:
                reasons.append("原区域已不存在")
            else:
                if region.get("box") and _block_box(block) != list(region.get("box") or []):
                    reasons.append("原区域坐标已变化")
                original = str(row.get("originalReading") or "").strip()
                text = str(_block_text(block) or "")
                if original and original not in text and original not in re.sub(r"\s+", "", text):
                    reasons.append("原区域读数已变化")
            if row.get("imageId") and page_ids.get(block_id) and row["imageId"] != page_ids[block_id]:
                reasons.append("绑定的原图已变化")
        elif row.get("imageId"):
            current_ids = set(page_ids.values())
            if current_ids and row["imageId"] not in current_ids:
                reasons.append("绑定的原图已变化")
        adopted = str(row.get("adoptedValue") or "")
        current_value, current_unit, current_basis_value = _current_field(row, fields, nutrition, current_basis)
        if adopted != str(current_value or ""):
            reasons.append("采用值与当前字段不一致")
        if str(row.get("unit") or "") != str(current_unit or ""):
            reasons.append("单位已变化")
        if str(row.get("basis") or "") != str(current_basis_value or ""):
            reasons.append("计量基准已变化")
        if reasons:
            stale.append(dict(row, staleReason="；".join(reasons)))
        else:
            valid.append(row)
    return valid, stale


def _current_field(row, fields, nutrition, current_basis):
    key = correction_nutrient_key(row)
    field = str(row.get("field") or "")
    if key == "basis" or field in {"计量基准", "basis"}:
        return current_basis, "", current_basis
    if key and key in NUTRITION_KEYS:
        item = (nutrition or {}).get(key) or {}
        if "NRV" in field.upper() or str(field).endswith("-nrv"):
            return item.get("nrv") or "", "%", current_basis
        if "单位" in field or str(field).endswith("-unit"):
            return item.get("unit") or "", item.get("unit") or "", current_basis
        return item.get("value") or "", item.get("unit") or "", current_basis
    plain = ((fields or {}).get(field) or {}) if field in (fields or {}) else {}
    if plain:
        return plain.get("value") or "", "", current_basis
    return row.get("adoptedValue"), row.get("unit") or "", current_basis


def bind_manual_correction(*, field, image_id, image_digest="", evidence_digest="", region=None,
                           original_reading="", adopted_value="", unit="", basis="",
                           by="人工", at=None):
    """生成一条带完整出处的人工改值记录。

    imageDigest 用于原图文件哈希；evidenceDigest 用于当前识别页摘要。
    两者不要混用，以免前端文件哈希被误当成 OCR 摘要而立即失效。
    """
    box = list((region or {}).get("box") or []) if isinstance(region, dict) else []
    block_id = (region or {}).get("blockId") if isinstance(region, dict) else None
    return {
        "field": field,
        "imageId": image_id or "",
        "imageDigest": image_digest or "",
        "evidenceDigest": evidence_digest or "",
        "region": {"blockId": block_id, "box": box},
        "originalReading": "" if original_reading is None else str(original_reading),
        "adoptedValue": "" if adopted_value is None else str(adopted_value),
        "value": "" if adopted_value is None else str(adopted_value),
        "unit": "" if unit is None else str(unit),
        "basis": "" if basis is None else str(basis),
        "by": by,
        "at": at,
    }


def blocking_state(qa, attribution=None, stale_corrections=None, disputed=None):
    """分层阻断：字段问题只卡住依赖结论；整单归属冲突才整案阻断。

    混图判断仍沿用 evidence.attribution_for 的既有结果，本步不改写混图算法。
    商标名与属性名、整包与料包重量、不同受托方 SC 号的区分，留待后续步骤处理。
    """
    attribution = attribution if isinstance(attribution, dict) else {}
    status = attribution.get("status")
    conflicts = attribution.get("conflicts") or []
    case_blocked = status == "conflict" or (status != "confirmed" and bool(conflicts))
    keys = set(disputed or ())
    keys |= disputed_keys(qa, stale_corrections)
    table = _table(qa)
    structural = is_structural_failure(qa) or table.get("extractStatus") == "conflict"
    held = []
    reasons = {}
    if case_blocked:
        return {
            "caseBlocked": True,
            "reason": "存在影响整份报告归属的混图冲突，整单暂停归并。",
            "disputedKeys": sorted(keys),
            "heldConclusions": list(NUTRITION_CONCLUSION_IDS),
            "heldReasons": {item: "整单归属尚未确认。" for item in NUTRITION_CONCLUSION_IDS},
            "continues": [],
        }
    if structural:
        held.extend(NUTRITION_CONCLUSION_IDS)
        text = table.get("failureReason") or STRUCTURAL_FAILURE_TEXT
        for item in NUTRITION_CONCLUSION_IDS:
            reasons[item] = text
    for item, depends in CONCLUSION_DEPENDENCIES.items():
        overlap = depends & keys
        if overlap and item not in held:
            held.append(item)
            names = "、".join(NUTRIENT_LABELS.get(key, key) for key in sorted(overlap))
            reasons[item] = f"{names}的读数待核对，依赖该项的营养计算暂缓。"
    continues = ["name", "production", "expiry", "shelf", "license", "ingredients"]
    return {
        "caseBlocked": False,
        "reason": "",
        "disputedKeys": sorted(keys),
        "heldConclusions": held,
        "heldReasons": reasons,
        "continues": continues,
    }


def failure_message(qa):
    table = _table(qa)
    return table.get("failureReason") or (STRUCTURAL_FAILURE_TEXT if is_structural_failure(qa) else "")
