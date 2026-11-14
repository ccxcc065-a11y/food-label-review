"""Local OCR backend when Apple Vision / Swift is unavailable.

This module keeps the same stdin/stdout contract as ocr.swift:

- stdin: JPEG or PNG bytes
- stdout: {"width", "height", "blocks": [{"text", "confidence", "box": [x, y, w, h]}]}
  or {"error": "..."} with exit code 2

Boxes are normalized to 0–1 with the origin at the top-left, matching the
Swift backend after it converts Vision's bottom-left coordinates.

The engine is RapidOCR (PP-OCRv6 via ONNX Runtime), not Apple Vision.
Accuracy can differ, especially on dense nutrition tables. Cloud vision
review, when configured, still goes to the existing DeepSeek path.
"""

from __future__ import annotations

import io
import json
import sys

MAX_EDGE = 12_000
MAX_PIXELS = 60_000_000
ENGINE_LABEL = "本机 RapidOCR（PP-OCRv6）"

_ENGINE = None


def get_engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


def _load_image(data):
    from PIL import Image, ImageOps

    if data.startswith(b"\xff\xd8\xff"):
        pass
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        pass
    else:
        raise ValueError("无法读取图片，请上传有效的 JPEG 或 PNG。")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise ValueError("无法读取图片，请上传有效的 JPEG 或 PNG。") from exc
    image = ImageOps.exif_transpose(image) or image
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("无法读取图片，请上传有效的 JPEG 或 PNG。")
    if width > MAX_EDGE or height > MAX_EDGE or width * height > MAX_PIXELS:
        raise ValueError("图片像素过大，请缩小到 6000 万像素以内后重试。")
    if image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    return image, width, height


def _box(points, width, height):
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    x0 = min(1.0, max(0.0, min(xs) / width))
    y0 = min(1.0, max(0.0, min(ys) / height))
    x1 = min(1.0, max(x0, max(xs) / width))
    y1 = min(1.0, max(y0, max(ys) / height))
    return [x0, y0, x1 - x0, y1 - y0]


def recognize_bytes(data):
    image, width, height = _load_image(data)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    result = get_engine()(buffer.getvalue())
    boxes = result.boxes if result.boxes is not None else []
    texts = result.txts or ()
    scores = result.scores or ()
    blocks = []
    for points, text, score in zip(boxes, texts, scores):
        if not text:
            continue
        blocks.append({
            "text": str(text),
            "confidence": float(score) if score is not None else 0.0,
            "box": _box(points, width, height),
        })
    return {"width": int(width), "height": int(height), "blocks": blocks}


def _fail(message):
    sys.stdout.buffer.write(json.dumps({"error": message}, ensure_ascii=False).encode())
    raise SystemExit(2)


def main():
    data = sys.stdin.buffer.read()
    try:
        result = recognize_bytes(data)
    except ValueError as exc:
        _fail(str(exc))
    except Exception:
        _fail("系统文字识别未能完成，请检查图片后重试。")
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode())


if __name__ == "__main__":
    main()
