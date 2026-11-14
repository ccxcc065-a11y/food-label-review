"""用图像测量字重：OCR 给不了粗细，像素可以。

做法（零依赖：PIL + 标准库）：
1. 按 block 的归一化框裁切、灰度化，先自动定极性（白字粉底 / 深字浅底都能处理）；
2. 列投影切字，宽度明显大于字高或中位字宽的段判为「粘连」直接丢弃——
   粘连段的游程会横跨两个字，量出来是假加粗；
3. 每个字段用近似笔画宽度变换（每个墨迹像素取横/竖游程的较小值，再取中位数）
   估计笔画宽度，汉字横竖结构差异被中和，比单纯数水平游程稳；
4. 以该行所有干净字段的中位数为局部基准，按比例把目标词映射到字段上：
   连续 ≥2 个字段超过基准 1.35 倍才判加粗，单字偏宽不算；
5. 段数不足、全部粘连、墨迹太少或对比度太低时返回 None，绝不猜。
"""

import io
import re
import statistics

from PIL import Image


THRESHOLD = 1.35          # 判加粗的笔画比值
MIN_CONSECUTIVE = 2       # 连续多少个字段偏宽才算加粗
MIN_WIDTH, MIN_HEIGHT = 20, 10   # 小于这个像素尺寸不测
MIN_WORD_HEIGHT = 24      # 词级判定要求的最小字高：再小笔画宽度只剩 2~3 个整数档，会误判
PUNCTUATION = set("、，。；：！？（）【】《》,.;:!?()[]「」“”\"' \u00a0")
TOKEN = re.compile(r"[、，,。;；:：（）()【】\[\]《》\s]+")


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


def _crop(image, box, pad_x=0.0008, pad_y=0.0):
    width, height = image.size
    x, y, w, h = box
    left = max(0, int(round((x - pad_x) * width)))
    top = max(0, int(round((y - pad_y) * height)))
    right = min(width, int(round((x + w + pad_x) * width)))
    bottom = min(height, int(round((y + h + pad_y) * height)))
    return image.crop((left, top, max(left + 2, right), max(top + 2, bottom)))


def _binarize(region):
    """返回 (二值图, 中位数, 墨迹是否偏亮)。极性由「明显偏离中位数的像素哪侧更多」决定。"""
    gray = region.convert("L")
    histogram = gray.histogram()
    total = sum(histogram) or 1
    accumulated, median = 0, 128
    for value, count in enumerate(histogram):
        accumulated += count
        if accumulated * 2 >= total:
            median = value
            break
    upper = sum(histogram[median + 25:])
    lower = sum(histogram[:max(0, median - 25)])
    light_ink = upper > lower
    if light_ink:
        threshold = min(250, median + max(20, (255 - median) // 2))
        table = bytes(1 if value > threshold else 0 for value in range(256))
    else:
        threshold = max(5, median - max(20, median // 2))
        table = bytes(1 if value < threshold else 0 for value in range(256))
    return gray.point(table), median, light_ink


def _columns(binary):
    width, height = binary.size
    data = binary.transpose(Image.TRANSPOSE).tobytes()
    return [data[index * height:(index + 1) * height].count(1) for index in range(width)]


def _segments(profile):
    result, start = [], None
    for index, value in enumerate(profile):
        if value and start is None:
            start = index
        elif not value and start is not None:
            result.append((start, index))
            start = None
    if start is not None:
        result.append((start, len(profile)))
    return result


def _stroke(binary, left, right):
    """近似笔画宽度变换：墨迹像素取横/竖游程较小值，再取中位数。"""
    height = binary.size[1]
    patch = binary.crop((left, 0, right, height))
    width = patch.size[0]
    if not width or not height:
        return 0.0
    data = patch.tobytes()
    rows = [data[row * width:(row + 1) * width] for row in range(height)]
    horizontal = [[0] * width for _ in range(height)]
    for row_index, row in enumerate(rows):
        column = 0
        while column < width:
            if row[column]:
                end = column
                while end < width and row[end]:
                    end += 1
                length = end - column
                for position in range(column, end):
                    horizontal[row_index][position] = length
                column = end
            else:
                column += 1
    vertical = [[0] * width for _ in range(height)]
    for column in range(width):
        row_index = 0
        while row_index < height:
            if rows[row_index][column]:
                end = row_index
                while end < height and rows[end][column]:
                    end += 1
                length = end - row_index
                for position in range(row_index, end):
                    vertical[position][column] = length
                row_index = end
            else:
                row_index += 1
    values = []
    for row_index in range(height):
        row = rows[row_index]
        for column in range(width):
            if row[column]:
                values.append(min(horizontal[row_index][column], vertical[row_index][column]))
    return statistics.median(values) if values else 0.0


def _units(character):
    if character in PUNCTUATION:
        return 1.0
    return 0.55 if ord(character) < 0x2000 else 1.0


def _tokens(text):
    """把一行文字拆成候选词（配料名、营养素名），保持与原文一致。"""
    return [token for token in TOKEN.split(text or "") if token]


def _unavailable(reason):
    return {"bold": None, "stroke_ratio": None, "baseline_ratio": None,
            "stroke": 0.0, "height": 0.0, "baseline": 0.0, "words": {}, "reason": reason}


def measure_image(image, block):
    """在已解码的 PIL 图像上测量一个 block；批量测量时复用它避免反复解码。"""
    box = _box(block)
    text = _text(block)
    if box is None or not text.strip():
        return _unavailable("缺少可测量的文字或坐标")
    width, height = image.size
    right = min(width, int(round((box[0] + box[2]) * width)))
    bottom = min(height, int(round((box[1] + box[3]) * height)))
    if right <= 0 or bottom <= 0 or box[2] * width < MIN_WIDTH or box[3] * height < MIN_HEIGHT:
        return _unavailable("区域太小，无法测量字重")
    region = _crop(image, box)
    if region.width < MIN_WIDTH or region.height < MIN_HEIGHT:
        return _unavailable("区域太小，无法测量字重")
    binary, median, light_ink = _binarize(region)
    profile = _columns(binary)
    pieces = _segments(profile)
    if len(pieces) < 2:
        return _unavailable("没有切出足够的字，无法比较笔画")
    widths = [end - start for start, end in pieces]
    median_width = statistics.median(widths)
    line_height = region.size[1]
    kept = []
    for left, right_edge in pieces:
        patch = binary.crop((left, 0, right_edge, binary.size[1]))
        found = patch.getbbox()
        glyph_height = (found[3] - found[1]) if found else 0
        merged = (right_edge - left) > max(1.5 * max(glyph_height, 1), 1.35 * median_width)
        kept.append({"left": left, "right": right_edge, "height": glyph_height,
                     "merged": merged,
                     "stroke": 0.0 if merged else _stroke(binary, left, right_edge)})
    usable = [piece for piece in kept
              if not piece["merged"] and piece["stroke"] > 0 and piece["height"] >= line_height * 0.5]
    if len(usable) < 2:
        return _unavailable("字段粘连或墨迹不足，无法建立笔画基准")
    baseline = statistics.median(piece["stroke"] for piece in usable)
    glyph_height = statistics.median(piece["height"] for piece in usable) or line_height
    ink_left = min(piece["left"] for piece in kept)
    ink_right = max(piece["right"] for piece in kept)
    units = sum(_units(character) for character in text) or 1.0
    per_unit = (ink_right - ink_left) / units if units else 0.0
    words = {}
    if glyph_height >= MIN_WORD_HEIGHT:
        for token in _tokens(text):
            words[token] = _judge(text, token, usable, baseline, ink_left, per_unit,
                                  line_height, THRESHOLD)
    verdicts = [value for value in words.values() if value is not None]
    if not verdicts:
        bold = None
    elif len(verdicts) == len(words) and all(verdicts):
        bold = True
    elif all(value is False for value in verdicts) and len(verdicts) == len(words):
        bold = False
    else:
        bold = None
    return {"bold": bold,
            "stroke_ratio": round(baseline / glyph_height, 4) if glyph_height else None,
            "baseline_ratio": 1.0,
            "stroke": round(baseline, 2), "height": round(glyph_height, 1),
            "baseline": round(baseline, 2),
            "words": words, "reason": "", "light_ink": light_ink, "median": median}


def _judge(text, word, pieces, baseline, ink_left, per_unit, line_height, threshold):
    index = text.find(word)
    if index < 0 or not per_unit:
        return None
    before = sum(_units(character) for character in text[:index])
    after = sum(_units(character) for character in text[:index + len(word)])
    left = ink_left + before * per_unit
    right = ink_left + after * per_unit
    hits = [piece for piece in pieces
            if left - per_unit * 0.35 <= (piece["left"] + piece["right"]) / 2 <= right + per_unit * 0.35]
    if len(hits) < MIN_CONSECUTIVE:
        return None
    wide = [piece["stroke"] > baseline * threshold for piece in hits]
    if any(wide[index] and wide[index + 1] for index in range(len(wide) - 1)):
        return True
    return False


def measure(image_bytes, block):
    """裁切单个 block 测量字重；返回 bold / stroke_ratio / baseline_ratio / words。"""
    with Image.open(io.BytesIO(image_bytes)) as source:
        return measure_image(source.convert("RGB"), block)


def is_bold(image_bytes, block, page_blocks, threshold=1.25, regular_ceiling=1.10):
    """与该页正文的笔画宽度中位数比较，明显更宽判 True；判不了返回 None。"""
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
        target = measure_image(image, block)
        if not target.get("stroke_ratio"):
            return None
        references = []
        for other in page_blocks or []:
            if other is block or _text(other).strip() == _text(block).strip():
                continue
            if len(_text(other).strip()) < 4:
                continue
            measured = measure_image(image, other)
            if measured.get("stroke_ratio"):
                references.append(measured["stroke_ratio"])
            if len(references) >= 12:
                break
        if len(references) < 3:
            return None
        ratio = target["stroke_ratio"] / statistics.median(references)
        if ratio > threshold:
            return True
        if ratio < regular_ceiling:
            return False
        return None
