"""Build a fictional label and matching, explicitly pre-recorded OCR observations.

No real product, company, standards text or model call is used. This is a
distribution fixture, not a benchmark or a food-compliance ground truth set.
"""
import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]


def build(font_path):
    width, height = 1200, 1640
    image = Image.new("RGB", (width, height), "#fafbf7")
    draw = ImageDraw.Draw(image)
    body = ImageFont.truetype(str(font_path), 31)
    title = ImageFont.truetype(str(font_path), 56)
    small = ImageFont.truetype(str(font_path), 25)
    blocks = []

    def text(value, x, y, font=body):
        draw.text((x, y), value, fill="#213c2f", font=font)
        box = draw.textbbox((x, y), value, font=font)
        blocks.append({"id": "demo-%02d" % (len(blocks) + 1), "text": value,
                       "confidence": 1, "box": [round(box[0] / width, 6), round(box[1] / height, 6),
                                                round((box[2] - box[0]) / width, 6),
                                                round((box[3] - box[1]) / height, 6)]})

    draw.rectangle((0, 0, width, 125), fill="#e1eadf")
    text("合成标签 · 仅供流程演示", 65, 37, small)
    text("虚构原味饼干", 65, 165, title)
    for y, value in [
        (275, "食品名称：虚构原味饼干"),
        (335, "配料表：小麦粉、白砂糖、植物油、食用盐"),
        (395, "净含量：100克"),
        (455, "生产商：演示食品有限公司"),
        (515, "地址：演示市示例路1号"),
        (575, "联系方式：000-000-0000"),
        (635, "生产日期：2026年05月01日"),
        (695, "保质期：6个月"),
        (755, "贮存条件：阴凉干燥处保存"),
        (840, "营养成分表"),
    ]:
        text(value, 65, y)
    text("项目", 65, 905)
    text("每100克", 555, 905)
    text("NRV%", 910, 905)
    for i, (name, amount, nrv) in enumerate([
        ("能量", "2000千焦", "24%"), ("蛋白质", "6.0克", "10%"),
        ("脂肪", "20.0克", "33%"), ("饱和脂肪", "5.0克", "25%"),
        ("碳水化合物", "65.0克", "22%"), ("糖", "15.0克", ""),
        ("钠", "400毫克", "20%"),
    ]):
        y = 970 + i * 65
        draw.line((65, y - 12, 1110, y - 12), fill="#c7d0c2", width=1)
        text(name, 65, y)
        text(amount, 555, y)
        if nrv:
            text(nrv, 910, y)
    draw.text((65, 1490), "虚构数值不代表配方、检测结果或法规结论。", font=small, fill="#617265")
    draw.text((65, 1540), "演示读取预置 OCR；上传本图可另行测试本机 OCR。", font=small, fill="#617265")
    folder = ROOT / "demos"
    folder.mkdir(exist_ok=True)
    image.save(folder / "synthetic-label.png")
    (folder / "synthetic-ocr.json").write_text(json.dumps({
        "schemaVersion": 1, "synthetic": True,
        "recognitionMode": "pre-recorded-not-live-ocr",
        "description": "Manually generated observations matching the fictional image; not an accuracy benchmark.",
        "blocks": blocks,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", required=True, type=Path, help="Path to a locally licensed CJK font")
    build(parser.parse_args().font)
