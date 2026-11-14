import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass


FIELD_KEYS = (
    "name", "ingredients", "net", "packs", "producer", "address", "contact",
    "license", "standard", "production", "shelf", "expiry", "storage", "basis",
)
NUTRITION_KEYS = ("energy", "protein", "fat", "saturated", "carb", "sugar", "sodium")

FIELD_LABELS = (
    ("name", r"(?:产品名称|食品名称|应品音称|品名|名称)"),
    ("ingredients", r"(?:配料表|配料)"),
    ("net", r"净含量(?:及规格)?"),
    ("producer", r"(?:生产商\s*[/、]\s*委托商|生产企业名称|生产企业|生产厂家|生产公司|生产商|制造商|受委托方|委托方|委托商|代理商|进口商|经销商)"),
    ("address", r"(?:生产地址|企业地址|厂址|地址)"),
    ("contact", r"(?:全国服务热线|消费者服务热线|服务热线|联系电话|联系方式|电话)"),
    ("license", r"(?:食品生产许可证编号|食品生产许可证号|食品生产许可证|生产许可证编号|生产许可证号|SC编号|食品小作坊登记证编号|在华注册编号)"),
    ("standard", r"(?:产品执行标准(?:代号|编号|号)?|产品标准(?:代号|编号|号)?|执行标准(?:代号|编号|号)?|标准代号)"),
    ("production", r"(?:生产日期\s*[/、及]\s*保质期(?:到期日|至)|生产日期|制造日期|制作日期)"),
    ("expiry", r"(?:保质期到期日|保质期至|到期日期|到期日|有效期至|食用期限)"),
    ("shelf", r"(?:保质期|保存期限)"),
    ("storage", r"(?:(?:贮存|储存|储藏|贮藏|存储|保存)(?:条件|方法|方式))"),
)
OTHER_LABELS = (
    r"致敏原(?:信息|提示)?|过敏原(?:信息|提示)?|产品(?:类别|类型)|产地|原产(?:国|地)(?:国)?|"
    r"食用方法|使用方法|用途|温馨提示|条形码|营养成分表"
)
FIELD_PATTERN = re.compile(
    r"(?P<mark>[【\[])?(?P<label>" + "|".join(
        f"(?P<{key}>{pattern})" for key, pattern in FIELD_LABELS
    ) + r")\s*(?(mark)[】\]]\s*[:;]?|[:;])\s*"
)
END_PATTERN = re.compile(r"(?:[【\[])?(?:" + OTHER_LABELS + r")\s*[】\]:;]|^营养成分表")
NET_LABEL = re.compile(r"(?:[【\[]\s*)?净含量(?:及规格)?\s*(?:[】\]:;]\s*)?\(?\s*")
NUM = r"\d+(?:\.\d+)?"
MASS = re.compile(r"(?<![\d.])(" + NUM + r")\s*(千克|毫克|毫升|克|升|kg|mg|ml|g|L)(?:(?![a-z])|(?=x\s*\d))", re.I)
MULTIPLIER = re.compile(r"(?:[×x*]|乘(?:以)?)\s*(?=\d)", re.I)
LEADING_COUNT = re.compile(r"\d+\s*(?:[×x*]|乘(?:以)?)\s*", re.I)
NUTRIENT_UNIT = r"千焦(?:耳)?|千\s*\(\s*k\.?j\s*\)|k\.?j|千卡|kcal|毫克|mg|克|g"
NUTRIENT_AMOUNT = re.compile(r"(?<![\d.])(" + NUM + r")\s*(" + NUTRIENT_UNIT + r")(?![a-z])", re.I)
PERCENT = re.compile(r"(?<!\d)(" + NUM + r")\s*%")
NUTRIENT_PATTERN = re.compile(
    r"^\s*[-—–一－·]?\s*(?:" + "|".join(
        f"(?P<{key}>{value})" for key, value in (
            ("energy", "能量"), ("protein", "蛋白质"), ("saturated", "饱和脂肪(?:酸)?"),
            ("fat", "脂肪"), ("carb", "碳水化合物"), ("sugar", "糖"), ("sodium", "钠"),
        )
    ) + r")(?=$|[\s\d:;(/])"
)
BASIS = re.compile(
    r"每\s*(?:100\s*(?:毫升|mL|克|g)|份(?:\s*" + NUM + r"\s*(?:千克|毫克|毫升|克|升|kg|mg|ml|g|L))?|包装)",
    re.I,
)
PLATE_ANCHORS = (
    "成品尺寸", "印刷方式", "光点尺寸", "客户签字处", "色序", "凹印制版",
)
UNITS = {
    "千焦": "kJ", "千焦耳": "kJ", "kj": "kJ", "千卡": "kcal", "kcal": "kcal",
    "克": "g", "g": "g", "千克": "kg", "kg": "kg",
    "毫克": "mg", "mg": "mg", "毫升": "mL", "ml": "mL", "升": "L", "l": "L",
}


@dataclass
class Block:
    id: str
    text: str
    box: tuple
    confidence: float = 0

    @property
    def x(self):
        return self.box[0]

    @property
    def y(self):
        return self.box[1]

    @property
    def right(self):
        return self.x + self.box[2]

    @property
    def height(self):
        return self.box[3]

    @property
    def middle(self):
        return self.y + self.height / 2


def normalized(text):
    return unicodedata.normalize("NFKC", text).strip()


def read_pages(payload):
    pages = payload.get("pages", [])
    if not isinstance(pages, list) or len(pages) > 24:
        raise ValueError("请提交至多 24 页图片的识别结果。")
    result = []
    ids = set()
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("blocks"), list):
            raise ValueError("图片的识别区块格式无效。")
        if len(page["blocks"]) > 4000:
            raise ValueError("单页识别文字过多，请裁出标签区域后重试。")
        blocks = []
        for block in page["blocks"]:
            if not isinstance(block, dict):
                raise ValueError("识别区块格式无效。")
            identifier = block.get("id")
            content = block.get("text")
            box = block.get("box")
            if (not isinstance(identifier, str) or not identifier or identifier in ids
                or not isinstance(content, str)
                or not isinstance(box, list) or len(box) != 4
                or not all(type(number) in (int, float) and math.isfinite(number) and 0 <= number <= 1 for number in box)
                # 识别框可能因为字号或排版溢出图像边缘一点点（例如页脚紧贴下沿），
                # 只要起点仍在图内、整体溢出不超过 2% 就接受，超出才判为坐标无效。
                or box[0] + box[2] > 1.02 or box[1] + box[3] > 1.02):
                raise ValueError("识别区块的编号、文字或坐标无效。")
            ids.add(identifier)
            if content.strip():
                confidence = block.get("confidence", 0)
                blocks.append(Block(identifier, normalized(content), tuple(box),
                                    confidence if type(confidence) in (float, int) and 0 <= confidence <= 1 else 0))
        result.append(blocks)
    return result


def refs(blocks):
    return list(dict.fromkeys(block.id for block in blocks))


def empty_field():
    return {"value": "", "refs": []}


def empty_nutrient():
    return {"value": "", "unit": "", "nrv": "", "refs": []}


def looks_like_plate(block):
    return block.y < .17 and any(anchor in block.text for anchor in PLATE_ANCHORS)


def label_blocks(blocks):
    # A printer's job ticket often includes dimensions, a design date and a previous product name.
    if sum(looks_like_plate(block) for block in blocks) >= 2:
        job_bottom = max(block.y + block.height for block in blocks if looks_like_plate(block))
        return [block for block in blocks if block.y >= max(.15, job_bottom)]
    return blocks


def nearby_lines(anchor, blocks, inline_continuations=False):
    prior = anchor
    for block in sorted(blocks, key=lambda entry: (entry.middle, entry.x)):
        if block.id == anchor.id or block.middle < anchor.middle - anchor.height * .45:
            continue
        same_line = abs(block.middle - prior.middle) <= max(prior.height, block.height) * .55
        if same_line:
            if block.x < prior.right - .018 or block.x - prior.right > .04:
                continue
            if not inline_continuations and prior.id != anchor.id:
                continue
        elif abs(block.x - anchor.x) > .035:
            continue
        if block.y - prior.y - prior.height > max(.013, min(.065, prior.height * 1.6)):
            break
        yield block
        prior = block


def ingredient_lines(anchor, blocks):
    prior = anchor
    for block in sorted(blocks, key=lambda item: (item.middle, item.x)):
        delta = block.middle - prior.middle
        if delta <= max(.007, min(block.height, prior.height) * .25):
            continue
        if not anchor.x - .09 <= block.x <= min(anchor.x + .08, anchor.right):
            continue
        if delta > max(.023, min(.064, prior.height * 2)):
            break
        if FIELD_PATTERN.match(block.text) or END_PATTERN.search(block.text) or prior.text.endswith(("。", ";")):
            break
        if block.x < anchor.x - .043:
            break
        yield block
        prior = block


def field_views(blocks):
    """Join only an unambiguous, horizontally split field heading."""
    merged, consumed = {}, set()
    for left in blocks:
        if not re.fullmatch(r"[\u4e00-\u9fff]{1,3}", left.text):
            continue
        choices = []
        for right in blocks:
            if not 0 <= right.x - left.right <= .041 or right.id in merged:
                continue
            if abs(left.middle - right.middle) > max(left.height, right.height) * .52:
                continue
            joined = left.text + right.text
            match = FIELD_PATTERN.match(joined)
            if match and match.end() > len(left.text) and not FIELD_PATTERN.match(right.text):
                choices.append((right, joined))
        if len(choices) == 1:
            right, joined = choices[0]
            merged[right.id] = (Block(right.id, joined, right.box, min(left.confidence, right.confidence)), [left, right])
            consumed.add(left.id)
    return [merged.get(block.id, (block, [block])) for block in blocks if block.id not in consumed]


def field_candidates(blocks):
    views = field_views(blocks)
    originals = {block.id: source for block, source in views}
    lines = [block for block, _ in views]
    for block, source in views:
        # Text printed on inner-pack panels often embeds several fields in a single long block.
        # Match the first field only so it cannot override the overall label's fields.
        matches = list(FIELD_PATTERN.finditer(block.text))
        for index, match in enumerate(matches):
            key = next(name for name, _ in FIELD_LABELS if match.group(name))
            if match.start() and key not in ("net", "production", "shelf", "expiry", "contact"):
                continue
            if match.start() and re.match(r"^(?:米线包生产商|被委托商|被委托方)", block.text):
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(block.text)
            value = block.text[match.end():end].strip()
            if key == "ingredients":
                separator = END_PATTERN.search(value)
                if separator:
                    value = value[:separator.start()].strip()
            elif key == "contact":
                contact = re.match(r"\+?\d[\d() +\-/]{6,35}", value)
                value = contact.group().strip() if contact else value
            evidence = list(source)
            if key in {"ingredients", "address", "contact"} or not value:
                continuation = ingredient_lines(block, lines) if key == "ingredients" and value else nearby_lines(block, lines)
                for next_block in continuation:
                    if FIELD_PATTERN.search(next_block.text) or END_PATTERN.search(next_block.text):
                        break
                    if key == "contact" and not re.fullmatch(r"[\d() +\-]{7,25}", next_block.text):
                        break
                    if key == "address" and next_block.x > block.x + .035:
                        continue
                    value += (" / " if key == "contact" and value else "") + next_block.text
                    evidence.extend(originals[next_block.id])
                    if key not in {"ingredients", "address"}:
                        break
            if value:
                yield key, value, evidence
                if key == "production" and re.search(r"[/、及]", match.group("production")):
                    yield "expiry", value, evidence


def net_candidates(blocks):
    for block in blocks:
        label = NET_LABEL.search(block.text)
        if not label:
            continue
        value = block.text[label.end():]
        evidence = [block]
        if not (MASS.match(value) or LEADING_COUNT.match(value)):
            nearest = next(nearby_lines(block, blocks), None)
            if not nearest or not (MASS.match(nearest.text) or LEADING_COUNT.match(nearest.text)):
                continue
            evidence.append(nearest)
            value = nearest.text
        prefix = LEADING_COUNT.match(value)
        if prefix and MASS.match(value[prefix.end():]):
            yield "net", value, evidence
            continue
        mass = MASS.match(value)
        if not mass:
            continue
        remainder = value[mass.end():]
        if MULTIPLIER.match(remainder.lstrip()):
            yield "net", value, evidence
            continue
        yield "net", mass.group(), evidence
        pack_evidence = list(evidence)
        start = evidence[-1]
        for nearby in nearby_lines(start, blocks, inline_continuations=True):
            if ")" in remainder:
                break
            if FIELD_PATTERN.search(nearby.text) or END_PATTERN.search(nearby.text):
                break
            if not re.search(r"包|" + MASS.pattern, nearby.text, re.I):
                break
            remainder += nearby.text
            pack_evidence.append(nearby)
        if "包" in remainder and (len(list(MASS.finditer(remainder))) >= 2 or MULTIPLIER.search(remainder)):
            yield "packs", remainder.strip("() 、"), pack_evidence


def choose_field(candidates):
    distinct = {}
    for value, blocks in candidates:
        comparable = re.sub(r"\s+", "", value)
        if comparable not in distinct:
            distinct[comparable] = (value, [])
        distinct[comparable][1].extend(blocks)
    if len(distinct) == 1:
        value, blocks = next(iter(distinct.values()))
        return {"value": value, "refs": refs(blocks)}
    return {"value": "", "refs": refs(block for _, blocks in distinct.values() for block in blocks)}


def choose_packs(candidates):
    # The same list can be printed on two panels with different line breaks or ordering.
    distinct = {}
    for value, blocks in candidates:
        amounts = sorted((match.group(1), UNITS[match.group(2).lower()]) for match in MASS.finditer(value))
        key = tuple(amounts)
        if key not in distinct:
            distinct[key] = (value, [])
        distinct[key][1].extend(blocks)
    largest = max(distinct, key=len, default=())
    if largest and all(not (Counter(key) - Counter(largest)) for key in distinct):
        value = distinct[largest][0]
        return {"value": value, "refs": refs(block for _, blocks in distinct.values() for block in blocks)}
    return choose_field(distinct.values())


def basis_name(value):
    """归一计量基准。必须先识别「每份」，避免「每份 30 克」被误写成每 100 克。"""
    text = value or ""
    flat = re.sub(r"\s+", "", text)
    if re.search(r"每份", flat, re.I):
        serving = re.match(r"每份(" + NUM + r")(千克|毫克|毫升|克|升|kg|mg|ml|g|L)$", flat, re.I)
        if serving:
            unit = UNITS[re.sub(r"[.\s]", "", serving.group(2)).lower()]
            labels = {"g": "克", "mg": "毫克", "kg": "千克", "mL": "毫升", "L": "升"}
            return f"每份{serving.group(1)}{labels.get(unit, unit)}"
        return "每份"
    if "每包装" in flat:
        return "每包装"
    if re.search(r"毫升|ml", text, re.I):
        return "每100毫升"
    if re.search(r"克|g", text, re.I):
        return "每100克"
    return flat


def is_serving_basis(value):
    return "每份" in re.sub(r"\s+", "", str(value or ""))


def is_valid_basis(value):
    name = re.sub(r"\s+", "", str(value or ""))
    return name in {"每100克", "每100毫升"} or name.startswith("每份")


def nutrient_unit(token):
    if "千" in token and "(" in token:
        return "kJ"
    return UNITS[re.sub(r"[.\s]", "", token).lower()]


# 独立单位或纯数字单元格：清晰原图常见的行列拆分结果。
BARE_NUMBER = re.compile(r"(?<![\d.])(" + NUM + r")(?![\d.%])")


def same_table_row(label, block):
    """同一物理行、位于项目名称右侧的单元格，不要求名称和数值写在同一个识别块里。"""
    if block.id == label.id:
        return False
    return (block.x >= label.right - .013
            and block.x > label.x + .009
            and abs(block.middle - label.middle) <= max(.007, max(block.height, label.height) * .55))


def in_table_region(block, box):
    if box is None:
        return True
    left, top, right, bottom = box
    return block.right >= left and block.x <= right and (block.y + block.height) >= top and block.y <= bottom


def _table_labels(blocks, headers):
    """标题下方连续的项目名称行。不用标题左边界限制整张表。"""
    labels = [block for block in blocks if NUTRIENT_PATTERN.match(block.text)]
    if not headers:
        return labels
    top = min(header.y for header in headers)
    labels = [block for block in labels if block.y >= top - .01]
    labels.sort(key=lambda block: (block.y, block.x))
    if not labels:
        return []
    kept = [labels[0]]
    for block in labels[1:]:
        prior = kept[-1]
        gap = block.y - (prior.y + prior.height)
        if gap > max(.08, prior.height * 2.5):
            break
        kept.append(block)
    return kept


def table_bounds(blocks):
    """用标题、列表头和已定位的行列单元格求表格区域，而不是把标题左右边界当成整表边界。"""
    headers = [block for block in blocks if "营养成分表" in block.text]
    if not headers:
        return headers, None
    anchors = list(headers)
    for block in blocks:
        text = block.text.strip()
        if any(block.id == header.id for header in headers):
            continue
        near_title = any(block.y >= header.y - .04 and block.y <= header.y + .22 for header in headers)
        if not near_title:
            continue
        if (text == "项目" or BASIS.search(block.text)
                or re.search(r"营养素参考值|NRV\s*%?", text, re.I)):
            anchors.append(block)
    labels = _table_labels(blocks, headers)
    anchors.extend(labels)
    for label in labels:
        anchors.extend(block for block in blocks if same_table_row(label, block))
    left = min(block.x for block in anchors)
    top = min(block.y for block in anchors)
    right = max(block.right for block in anchors)
    bottom = max(block.y + block.height for block in anchors)
    return headers, (left - .04, top - .04, right + .04, bottom + .04)


def _spans_overlap(left, right):
    return left[0] < right[1] and right[0] < left[1]


def _cell_tokens(block, text):
    """从单个单元格里取出已拼好的含量、百分数、纯数字和独立单位。"""
    tokens, claimed = [], []
    for match in NUTRIENT_AMOUNT.finditer(text):
        tokens.append({"kind": "amount", "value": match.group(1), "unit": nutrient_unit(match.group(2)),
                       "block": block, "span": (match.start(), match.end())})
        claimed.append((match.start(), match.end()))
    for match in PERCENT.finditer(text):
        span = (match.start(), match.end())
        if any(_spans_overlap(span, item) for item in claimed):
            continue
        tokens.append({"kind": "percent", "value": match.group(1), "block": block, "span": span})
        claimed.append(span)
    for match in BARE_NUMBER.finditer(text):
        span = (match.start(), match.end())
        if any(_spans_overlap(span, item) for item in claimed):
            continue
        tokens.append({"kind": "number", "value": match.group(1), "block": block, "span": span})
        claimed.append(span)
    for match in re.finditer("(" + NUTRIENT_UNIT + ")", text, re.I):
        span = (match.start(), match.end())
        if any(_spans_overlap(span, item) for item in claimed):
            continue
        tokens.append({"kind": "unit", "unit": nutrient_unit(match.group(1)), "block": block, "span": span})
        claimed.append(span)
    return tokens


def _reconstruct_split_amounts(tokens):
    """把同一行里拆开的数字和单位配对。存在多种合理配对时返回 ambiguous，不取最近值。"""
    pairs = []
    ambiguous = False
    used_numbers = set()
    for index, token in enumerate(tokens):
        if token["kind"] != "unit":
            continue
        previous = None
        for candidate in reversed(tokens[:index]):
            if candidate["kind"] in {"number", "unit", "amount", "percent"}:
                previous = candidate
                break
        if previous is None or previous["kind"] != "number":
            continue
        extras = []
        for candidate in reversed(tokens[:index]):
            if candidate is previous:
                continue
            if candidate["kind"] in {"unit", "amount"}:
                break
            if candidate["kind"] == "number":
                extras.append(candidate)
        if extras:
            # 「660」「8」「千焦」这类行：两个数字都能对上同一个单位，不得悄悄取最近的。
            ambiguous = True
            continue
        used_numbers.add(id(previous))
        pairs.append({
            "kind": "amount",
            "value": previous["value"],
            "unit": token["unit"],
            "block": previous["block"],
            "unit_block": token["block"],
            "span": previous["span"],
        })
    unused_numbers = [token for token in tokens if token["kind"] == "number" and id(token) not in used_numbers]
    # 右侧多余数字多半是缺百分号的 NRV，不把它当成第二个含量；未配对的单位才构成歧义。
    unused_units = []
    for token in tokens:
        if token["kind"] != "unit":
            continue
        if any(pair.get("unit_block") is token["block"] and pair["unit"] == token["unit"] for pair in pairs):
            continue
        unused_units.append(token)
    if unused_units and unused_numbers:
        ambiguous = True
    if not pairs and tokens and any(token["kind"] == "number" for token in tokens) and any(
            token["kind"] == "unit" for token in tokens):
        ambiguous = True
    return pairs, ambiguous


def _row_tokens(label, match, candidates):
    pieces = [(label, label.text[match.end():])] + [(block, block.text) for block in candidates]
    tokens = []
    for block, part in pieces:
        tokens.extend(_cell_tokens(block, part))
    tokens.sort(key=lambda token: (token["block"].x, token["span"][0]))
    return tokens


def table_rows(blocks):
    headers, region = table_bounds(blocks)
    labels = _table_labels(blocks, headers) if headers else [
        block for block in blocks if NUTRIENT_PATTERN.match(block.text)
    ]
    bases = [(basis_name(match.group()), block) for block in blocks for match in BASIS.finditer(block.text)]
    rows = []
    for label in labels:
        match = NUTRIENT_PATTERN.match(label.text)
        if not match:
            continue
        if region is not None and not in_table_region(label, region):
            continue
        # 同一物理行的各列，不论 Vision 是否把名称、数值、单位、NRV 拆成多个块。
        candidates = [block for block in blocks if same_table_row(label, block)]
        candidates.sort(key=lambda block: block.x)
        tokens = _row_tokens(label, match, candidates)
        joined = [token for token in tokens if token["kind"] == "amount"]
        split, split_ambiguous = _reconstruct_split_amounts(tokens)
        # 已写在同一块里的「660 千焦」优先；拆开的单元格只在没有整块含量时补上。
        amounts = joined if joined else split
        if joined and split:
            extras = [item for item in split if not any(
                item["value"] == ready["value"] and item["unit"] == ready["unit"] for ready in joined
            )]
            amounts = joined + extras
        percentages = [token for token in tokens if token["kind"] == "percent"]
        involved = [label] + candidates
        if not amounts:
            if split_ambiguous:
                rows.append((match.lastgroup, None, refs(involved)))
            continue
        if split_ambiguous or len(amounts) > 1 or len(percentages) > 1:
            rows.append((match.lastgroup, None, refs(involved + [token["block"] for token in amounts])))
            continue
        amount = amounts[0]
        percent = percentages[0] if percentages else None
        evidence = [label, amount["block"]]
        if amount.get("unit_block"):
            evidence.append(amount["unit_block"])
        if percent:
            evidence.append(percent["block"])
        rows.append((match.lastgroup, (amount["value"], amount["unit"],
                     percent["value"] if percent else ""), refs(evidence)))
    if rows and region is not None:
        bases = [(value, block) for value, block in bases if in_table_region(block, region)]
    elif rows and headers:
        bases = [(value, block) for value, block in bases if any(
            block.y >= header.y - .025 and block.y <= header.y + .22
            and in_table_region(block, region) for header in headers
        )]
    elif not rows and not headers:
        bases = []
    return rows, bases


def extract_blocks(pages):
    fields = {key: empty_field() for key in FIELD_KEYS}
    nutrition = {key: empty_nutrient() for key in NUTRITION_KEYS}
    candidates = {key: [] for key in FIELD_KEYS}
    table = {key: [] for key in NUTRITION_KEYS}
    basis_occurrences = []
    table_signatures = []
    scope_flags = set()
    for blocks in pages:
        for key, value, evidence in field_candidates(blocks):
            if key != "net":
                candidates[key].append((value, evidence))
        for key, value, evidence in net_candidates(blocks):
            candidates[key].append((value, evidence))
        rows, bases = table_rows(blocks)
        if rows:
            table_signatures.append({(key, value) for key, value, _ in rows})
        basis_occurrences.extend((value, [block]) for value, block in bases)
        for key, data, evidence in rows:
            table[key].append((data if bases else None, evidence))
        for block in blocks:
            if re.search(r"食品小作坊(?:登记|备案|许可)|小作坊登记证", block.text):
                scope_flags.add("workshop")
            if re.search(r"原产国(?:\(地区\))?\s*[:;]|在华注册编号", block.text):
                scope_flags.add("imported")
            if re.search(r"(?:普通|一般)预包装食品", block.text):
                scope_flags.add("regular")
            if re.search(r"(?:食品类别|产品类型|产品类别)\s*[:;】]\s*(?:特殊医学用途配方食品|婴幼儿配方食品|保健食品|食品添加剂)", block.text):
                scope_flags.add("other")
    candidates["basis"] = basis_occurrences
    for key, values in candidates.items():
        fields[key] = choose_packs(values) if key == "packs" else choose_field(values)
    if not fields["name"]["value"] and not fields["name"]["refs"]:
        for blocks in pages:
            for block in blocks:
                marker = NET_LABEL.search(block.text)
                if marker and marker.start() > 3:
                    fields["name"] = {"value": block.text[:marker.start()].strip(), "refs": [block.id]}
                    break
            if fields["name"]["value"]:
                break
    if fields["basis"]["value"]:
        for key, values in table.items():
            if not values:
                continue
            evidence = list(dict.fromkeys(ref for _, row_refs in values for ref in row_refs))
            choices = {value for value, _ in values}
            if len(choices) == 1 and None not in choices:
                value, unit, nrv = next(iter(choices))
                nutrition[key] = {"value": value, "unit": unit, "nrv": nrv, "refs": evidence}
            else:
                nutrition[key]["refs"] = evidence
    else:
        for key, values in table.items():
            nutrition[key]["refs"] = list(dict.fromkeys(ref for _, row_refs in values for ref in row_refs))
    # A close-up may corroborate a complete table. Unrelated partial tables cannot supply each other's cells.
    largest = max(table_signatures, key=len, default=set())
    conflicting = any(not signature <= largest for signature in table_signatures)
    conflicting |= any(len({value for value, _ in values if value is not None}) > 1 for values in table.values())
    conflicting |= len({value for value, _ in basis_occurrences}) > 1
    draft = {"fields": fields, "nutrition": nutrition, "nutritionConflict": conflicting,
            "scopeHint": next(iter(scope_flags)) if len(scope_flags) == 1 else "unknown"}
    # 延迟导入，避免与 fact_qa 互相引用。
    from fact_qa import attach_nutrition_qa
    attach_nutrition_qa(draft, pages)
    return draft


def extract(payload):
    # Keep the same entry point for HTTP, the sample generator and local callers.
    from evidence import inspect_pages
    pages, _, attribution, anchors = inspect_pages(payload)
    if attribution["status"] == "confirmed":
        draft = extract_blocks(pages)
        from evidence import DATE, DATE_HINT
        for role in ("production", "expiry"):
            field = draft["fields"][role]
            targets = [item for anchor in anchors for item in anchor["targets"] if item["role"] == role]
            if (targets and len({item["value"] for item in targets}) == 1 and
                    (not field["value"] or (DATE_HINT.search(field["value"]) and not re.search(DATE, field["value"])))):
                draft["fields"][role] = {"value": targets[0]["value"],
                                         "refs": list(dict.fromkeys(item["ref"] for item in targets))}
    else:
        draft = extract_blocks([])
        # pages 已按重复图去重，逐页草稿必须用归属结果里的页编号配对，避免错位。
        draft["pageDrafts"] = [
            {"id": image_id, **extract_blocks([blocks])}
            for image_id, blocks in zip(attribution["pages"], pages)
        ]
    draft["attribution"] = attribution
    return draft
