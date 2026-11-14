"""可疑读数放大重读：用物理上限在两次读取之间做仲裁。

Apple Vision 在小字号里会丢小数点（原图 12.5 被读成 125）。单次读取不可靠，
但整图读和放大裁切读的错点并不相同：把两次读数放在同一套「物理上不可能」
上限下比对，取物理上成立的那个，就能把丢点的小数修回来。

这里不针对任何产品写特判：上限全部来自 nutrition_guard 的既有规则，
重读只在整图读数已经违反上限时发生，合理读数不会付出任何额外 OCR 成本。

对外接口（营养数值）：
    table_box(blocks, margin=0.012)      定位营养成分表，返回裁切用归一化 box
    crops(image_bytes, box, scales=(2,4)) 裁切并放大，返回 [(scale, png_bytes), ...]
    parse_nutrients(blocks)              {key: (value, unit)}，key 与 NUTRIENTS 一致
    reconcile(current, candidates, basis) 用上限仲裁，返回修正表
    improve(image_bytes, blocks, read)   整条流程，返回响应字段或 None

对外接口（通用可疑文字）：
    suspicious_blocks(blocks, threshold=0.6)  低置信度或含可疑标点的 block
    reread_block(image_bytes, block, scales=(2,4,6))  单块多尺度重读的候选文本
    settle(candidates)                   多数投票 + 最高置信度，判不了返回 None
    reread_blocks(image_bytes, blocks, read, scales)  多块拼一张图批量重读（性能用）
"""

import datetime
import io
import json
import re
import subprocess
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PIL import Image

from labelreview.extract import BASIS, NUTRIENT_AMOUNT, NUTRIENT_PATTERN, basis_name, nutrient_unit
from labelreview.nutrition_guard import ORDER, anomalies, display


# NRV% 列的读数通常单独成块，形如 "21%"；表头「营养素参考值%」不参与定位。
PERCENT_BLOCK = re.compile(r"^\s*\d+(?:\.\d+)?\s*[%％]\s*$")
# 同一行的判定容差，与 extract.table_rows 的取数口径保持一致。
LINE_SLACK = 0.013
# 单个裁切放大后的像素上限，超过就跳过该尺度，避免一次申请过多内存。
MAX_PIXELS = 40_000_000


def _text(block):
    value = block.get("text") if isinstance(block, dict) else getattr(block, "text", "")
    return value if isinstance(value, str) else ""


def _box(block):
    value = block.get("box") if isinstance(block, dict) else getattr(block, "box", None)
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        numbers = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    return tuple(numbers)


def _middle(box):
    return box[1] + box[3] / 2


def _decimal(text):
    """把读数写成 Decimal；带千分位逗号时先去掉，与 nutrition_guard.amount 的口径一致。"""
    try:
        return Decimal(str(text).replace(",", "").replace("，", ""))
    except (ArithmeticError, ValueError, InvalidOperation):
        return None


def _line(blocks, label, match):
    """产出 (block, offset)：标签块自身（数值可紧跟名称之后）与同一行右侧的相邻块。"""
    yield label, match.end()
    label_box = _box(label)
    if label_box is None:
        return
    neighbours = []
    for block in blocks:
        if block is label:
            continue
        box = _box(block)
        if box is None:
            continue
        if box[0] < label_box[0] + label_box[2] - LINE_SLACK or box[0] <= label_box[0] + .009:
            continue
        if abs(_middle(box) - _middle(label_box)) > max(.007, max(box[3], label_box[3]) * .55):
            continue
        neighbours.append(block)
    neighbours.sort(key=lambda block: _box(block)[0])
    for block in neighbours:
        yield block, 0


def _rows(blocks):
    """按阅读顺序产出 (key, block, start, end, value, unit)，同一项目只取最靠前的一处。"""
    labels = []
    for block in blocks:
        match = NUTRIENT_PATTERN.match(_text(block))
        if match and _box(block) is not None:
            labels.append((block, match))
    labels.sort(key=lambda item: (round(_middle(_box(item[0])), 3), _box(item[0])[0]))
    seen = set()
    for label, match in labels:
        key = match.lastgroup
        if key in seen:
            continue
        for block, offset in _line(blocks, label, match):
            text = _text(block)
            amount = NUTRIENT_AMOUNT.search(text, offset)
            if not amount:
                continue
            seen.add(key)
            yield key, block, amount.start(1), amount.end(1), amount.group(1), nutrient_unit(amount.group(2))
            break


def table_box(blocks, margin=0.012):
    """若检测到营养成分表，返回裁切用的归一化 box (x, y, w, h)；否则返回 None。

    判定：同时出现 ≥3 个营养素名（能量/蛋白质/脂肪/碳水化合物/钠/饱和脂肪/糖）
    以及至少 2 个百分比读数（NRV% 列）。返回这些块的并集，四周各留 margin。
    """
    names, percents = set(), []
    label_blocks, percent_blocks = [], []
    for block in blocks:
        box = _box(block)
        if box is None:
            continue
        text = _text(block)
        match = NUTRIENT_PATTERN.match(text)
        if match:
            names.add(match.lastgroup)
            label_blocks.append(box)
        elif PERCENT_BLOCK.match(text):
            percent_blocks.append(box)
    if len(names) < 3 or len(percent_blocks) < 2:
        return None
    boxes = label_blocks + percent_blocks
    left = max(0.0, min(box[0] for box in boxes) - margin)
    top = max(0.0, min(box[1] for box in boxes) - margin)
    right = min(1.0, max(box[0] + box[2] for box in boxes) + margin)
    bottom = min(1.0, max(box[1] + box[3] for box in boxes) + margin)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None
    return [left, top, width, height]


def crops(image_bytes, box, scales=(2, 4)):
    """按归一化 box 裁切原图并分别放大，返回 [(scale, png_bytes), ...]。"""
    result = []
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
        width, height = image.size
        left = max(0, min(width - 1, int(round(box[0] * width))))
        top = max(0, min(height - 1, int(round(box[1] * height))))
        right = max(left + 1, min(width, int(round((box[0] + box[2]) * width))))
        bottom = max(top + 1, min(height, int(round((box[1] + box[3]) * height))))
        region = image.crop((left, top, right, bottom))
        for scale in scales:
            if type(scale) is not int or scale < 2:
                continue
            if region.width * region.height * scale * scale > MAX_PIXELS:
                continue
            enlarged = region.resize((region.width * scale, region.height * scale), Image.LANCZOS)
            buffer = io.BytesIO()
            enlarged.save(buffer, format="PNG")
            result.append((scale, buffer.getvalue()))
    return result


def parse_nutrients(blocks):
    """从 OCR blocks 解析 {key: (value, unit)}，key 用 energy/protein/fat/saturated/carb/sugar/sodium。"""
    values = {}
    for key, _block, _start, _end, value, unit in _rows(blocks):
        values.setdefault(key, (value, unit))
    return values


def basis_of(blocks):
    """从 blocks 里推出计量基准；同时出现每 100 克与每 100 毫升时视为不可判，返回空串。"""
    kinds = {basis_name(match.group()) for block in blocks for match in BASIS.finditer(_text(block))}
    hundreds = {"每100克", "每100毫升"} & kinds
    if len(hundreds) == 1:
        return next(iter(hundreds))
    return ""


def violations(values, basis=None):
    """复用 nutrition_guard 的上限规则，返回 {key: 异常项}；基准不可判时返回空表。"""
    nutrition = {}
    for key, pair in (values or {}).items():
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        nutrition[key] = {"value": pair[0], "unit": pair[1]}
    return {item["key"]: item for item in anomalies(nutrition, basis or "")}


def reconcile(current, candidates, basis=None):
    """按上限仲裁：current 违反上限的项，取各尺度里第一个不违反上限的值。

    current 是整图读数 {key: (value, unit)}，candidates 是 [{"scale": 2, "values": {...}}, ...]。
    返回 {"key": {"from": ..., "to": ..., "scale": ...}}；无修正返回 {}。
    """
    broken = violations(current, basis)
    if not broken:
        return {}
    ordered = sorted(candidates or [], key=lambda item: item.get("scale", 0))
    corrections = {}
    for key in ORDER:
        if key not in broken:
            continue
        original = current.get(key)
        if not original:
            continue
        for candidate in ordered:
            values = candidate.get("values") or {}
            if key not in values:
                continue
            value, unit = values[key]
            number = _decimal(value)
            if number is None or number == _decimal(original[0]):
                continue
            if key in violations({key: (value, unit)}, basis):
                continue
            corrections[key] = {"from": str(original[0]), "to": display(number), "scale": candidate["scale"]}
            break
    return corrections


def apply_corrections(blocks, corrections):
    """把修正值写回对应的 block：替换数值、保留原单位写法，并加上 reread 标记。"""
    applied = []
    if not corrections:
        return applied
    rows = {row[0]: row for row in _rows(blocks)}
    for key, change in corrections.items():
        row = rows.get(key)
        if not row:
            continue
        _, block, start, end, original, _unit = row
        if not isinstance(block, dict):
            continue
        if _decimal(original) != _decimal(change["from"]):
            continue
        # 改写前先留底：原始识别结果必须保留，否则「重读确认」会退化成
        # 拿改后的值和改后的值比较，自己证明自己没错。
        block.setdefault("originalText", block["text"])
        block["text"] = block["text"][:start] + change["to"] + block["text"][end:]
        # to 一并记下：报告只在当前值仍等于重读结果时才声称「已重读确认」。
        block["reread"] = {"from": original, "to": change["to"], "scale": change["scale"],
                           "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
        applied.append({"key": key, "from": original, "to": change["to"], "scale": change["scale"]})
    return applied


def improve(image_bytes, blocks, read, scales=(2, 4), margin=0.012):
    """整条重读流程；读数本来就合理、没有营养成分表、或两次读都不可信时返回 None。

    read(png_bytes) 由调用方提供，返回同一格式的 OCR blocks；单个尺度识别失败时
    只保留已成功的尺度，不抛出异常，避免重读把整图识别拖垮。
    """
    box = table_box(blocks, margin)
    if box is None:
        return None
    basis = basis_of(blocks)
    current = parse_nutrients(blocks)
    broken = violations(current, basis)
    if not current or not broken:
        return None
    candidates, used = [], []
    for scale, png in crops(image_bytes, box, scales):
        try:
            again = read(png)
        except Exception:  # 重读是增强步骤：失败时退回整图读数，由 nutrition_guard 拦截
            break
        used.append(scale)
        candidates.append({"scale": scale, "values": parse_nutrients(again)})
        # 所有可疑项目都已在小尺度拿到物理上成立的值时，不再多花一次识别。
        if set(reconcile(current, candidates, basis)) == set(broken):
            break
    applied = apply_corrections(blocks, reconcile(current, candidates, basis))
    if not applied:
        return None
    return {"applied": applied, "scales": used, "box": [round(value, 4) for value in box]}


# ---------------------------------------------------------------------------
# 通用可疑重读：不只营养表，任何「读数可疑」的文字都可以放大重读。
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
OCR_BINARY = ROOT / ".runtime" / "ocr"
# 右括号后直接接汉字（疑似漏分隔符）、重复分隔符、替换字符、未闭合引号。
SUSPICIOUS_PUNCT = re.compile(
    r"[)）]\s*[\u4e00-\u9fff]"
    r"|[、,，]{2,}"
    r"|[\ufffd]"
    r"|[「“][^」”]{0,3}$"
)
NUTRIENT_HINTS = ("能量", "蛋白质", "脂肪", "碳水化合物", "钠", "糖")
MAX_REREAD_BLOCKS = 8
MAX_CANVAS_PIXELS = 40_000_000


def suspicious_blocks(blocks, threshold=0.6):
    """置信度低于阈值、或含可疑标点的 block（按阅读顺序）。"""
    picked = []
    for block in blocks or []:
        text = _text(block).strip()
        if len(text) < 2:
            continue
        try:
            confidence = float(block.get("confidence", 1) if isinstance(block, dict) else 1)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < threshold or SUSPICIOUS_PUNCT.search(text):
            picked.append(block)
    return picked


def priority(block):
    """重读优先级：影响规则判定的块先读，避免 8 块上限把关键行挤掉。"""
    text = _text(block)
    score = 0.0
    if SUSPICIOUS_PUNCT.search(text):
        score += 5
    if text.count("、") >= 2 and len(text) >= 8:
        score += 4
    if len(text) <= 8 and any(word in text for word in NUTRIENT_HINTS):
        score += 3
    if len(text) >= 12:
        score += 1
    return score


def choose_blocks(blocks, threshold=0.6, limit=MAX_REREAD_BLOCKS):
    """挑出最需要重读的 block：先按优先级，再按置信度低者优先，最后按阅读顺序。"""
    ranked = []
    for index, block in enumerate(suspicious_blocks(blocks, threshold)):
        box = _box(block)
        if box is None:
            continue
        ranked.append((index, block))
    ranked.sort(key=lambda item: (-priority(item[1]), _confidence(item[1]), item[0]))
    return [block for _, block in ranked[:limit]]


def _confidence(block):
    try:
        return float(block.get("confidence", 1) if isinstance(block, dict) else 1)
    except (TypeError, ValueError):
        return 0.0


def flat(text):
    return re.sub(r"\s+", "", text or "")


def _crop_block(image, block, pad_x=0.002, pad_y=0.0005):
    width, height = image.size
    x, y, w, h = _box(block)
    left = max(0, int(round((x - pad_x) * width)))
    top = max(0, int(round((y - pad_y) * height)))
    right = min(width, int(round((x + w + pad_x) * width)))
    bottom = min(height, int(round((y + h + pad_y) * height)))
    return image.crop((left, top, max(left + 2, right), max(top + 2, bottom)))


def _default_read(png_bytes):
    result = subprocess.run([str(OCR_BINARY)], input=png_bytes, capture_output=True, timeout=90)
    return json.loads(result.stdout)


def _compose(image, blocks, scale):
    """把多个 block 裁切放大后竖向拼成一张图，返回 (PNG 字节, 各块的像素纵向范围, 画布高度)。"""
    crops = [_crop_block(image, block) for block in blocks]
    scaled = [crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS) for crop in crops]
    gap = max(4, int(max(crop.height for crop in scaled) * 0.6))
    width = max(crop.width for crop in scaled) + 40
    height = sum(crop.height for crop in scaled) + gap * (len(scaled) + 1)
    if width * height > MAX_CANVAS_PIXELS:
        return None, [], 0
    canvas = Image.new("RGB", (width, height), "white")
    offsets, top = [], gap
    for crop in scaled:
        canvas.paste(crop, (20, top))
        offsets.append((top, top + crop.height))
        top += crop.height + gap
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue(), offsets, height


def _candidate(ocr, top, bottom, scale):
    pixel_height = ocr.get("height") or 0
    inside = []
    for block in ocr.get("blocks", []):
        box = _box(block)
        if box is None or not pixel_height:
            continue
        center = (box[1] + box[3] / 2) * pixel_height
        if top <= center <= bottom:
            inside.append(block)
    inside.sort(key=lambda block: (round(_box(block)[1], 3), _box(block)[0]))
    confidence = min((_confidence(block) for block in inside), default=0.0)
    return {"scale": scale, "text": "".join(_text(block) for block in inside), "confidence": confidence}


def reread_blocks(image_bytes, blocks, read=None, scales=(2, 4, 6)):
    """批量重读：每个尺度把所有 block 拼成一张图，只调用一次识别。

    返回与 blocks 等长的列表，每项是该 block 在各尺度下的候选 [{"scale","text","confidence"}]。
    单个尺度失败（画布过大或识别异常）时跳过该尺度，不影响其它尺度。
    """
    read = read or _default_read
    result = [[] for _ in blocks]
    if not blocks:
        return result
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
        for scale in scales:
            if type(scale) is not int or scale < 2:
                continue
            try:
                png, offsets, _height = _compose(image, blocks, scale)
            except (OSError, ValueError):
                continue
            if not png:
                continue
            try:
                ocr = read(png)
            except Exception:  # 重读是增强步骤：失败就少一个尺度，交给 settle 判断
                continue
            for index, (top, bottom) in enumerate(offsets):
                result[index].append(_candidate(ocr, top, bottom, scale))
    return result


def reread_block(image_bytes, block, scales=(2, 4, 6), read=None):
    """单块多尺度重读，返回候选 [{"scale", "text", "confidence"}]。"""
    read = read or _default_read
    result = []
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
        region = _crop_block(image, block)
        for scale in scales:
            if type(scale) is not int or scale < 2:
                continue
            enlarged = region.resize((region.width * scale, region.height * scale), Image.LANCZOS)
            buffer = io.BytesIO()
            enlarged.save(buffer, format="PNG")
            try:
                ocr = read(buffer.getvalue())
            except Exception:
                continue
            result.append(_candidate(ocr, 0, ocr.get("height") or enlarged.height, scale))
    return result


def settle(candidates):
    """多数投票 + 取最高置信度：返回 (text, confidence, method)；判不了返回 None。"""
    votes = Counter(flat(item.get("text")) for item in candidates or [] if flat(item.get("text")))
    if not votes:
        return None
    winner, count = votes.most_common(1)[0]
    runner_up = max((value for key, value in votes.items() if key != winner), default=0)
    # 必须严格多数且至少两票，否则宁可判不了。
    if count < 2 or count <= runner_up:
        return None
    best = max((item for item in candidates if flat(item.get("text")) == winner),
               key=lambda item: item.get("confidence", 0.0))
    return best["text"].strip(), float(best.get("confidence", 0.0)), "vote"
