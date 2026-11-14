import hashlib
import json
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP
from pathlib import Path
import re

import crosscheck
import evidence_checklist as evidence_lib
import exclusions
import notes
from consistency import check_consistency
from evidence import LABELS, dated_targets, inspect_pages
from extract import BASIS, MASS, MULTIPLIER, NUTRIENT_AMOUNT, PLATE_ANCHORS, UNITS, basis_name, extract_blocks, is_serving_basis, is_valid_basis, label_blocks, normalized, read_pages
import fact_qa
from knowledge import (EFFECTIVE_2025, clause_line, document_metadata as knowledge_metadata,
                       grades as grade_standards, resolve_document as knowledge_resolve,
                       source as source_for)
import registry as registry_mod
from nutrition_guard import anomalies


VERSION_LABELS = {
    "2025": "GB 7718-2025、GB 28050-2025",
    None: "食品范围待核对",
}
SCOPE_LABELS = {
    "regular": "国内普通预包装食品", "imported": "进口食品 · 专项人工审核",
    "workshop": "小作坊食品 · 属地人工审核", "other": "其他类别 · 专项人工审核",
    "unknown": "适用范围待确认",
}
REQUIRED_FIELDS = {
    "name": "食品名称", "ingredients": "配料表", "producer": "生产者名称", "address": "地址",
    "contact": "联系方式", "license": "食品生产许可证编号", "standard": "产品标准代号",
    "storage": "贮存条件",
}
NUTRIENTS = {
    "energy": ("能量", "kJ", 8400),
    "protein": ("蛋白质", "g", 60),
    "fat": ("脂肪", "g", 60),
    "carb": ("碳水化合物", "g", 300),
    "sodium": ("钠", "mg", 2000),
}
# 放大重读确认的说明文字使用不带符号的正式项目名。
NUTRIENT_LABELS = {
    "energy": "能量", "protein": "蛋白质", "fat": "脂肪", "saturated": "饱和脂肪",
    "carb": "碳水化合物", "sugar": "糖", "sodium": "钠",
}
POSITION = re.compile(r"见.*(?:封口|封边|瓶盖|瓶身|瓶底|盒盖|盒底|袋口|袋底|罐底|盖底|顶部|底部|下方|上方|正面|背面|左侧|右侧|标签下沿)")
REAL_DATE = re.compile(r"(?<!\d)(\d{4})(?:年|[./\-])(\d{1,2})(?:月|[./\-])(\d{1,2})(?:日)?(?!\d)")
COMPACT_DATE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")
CLAIM = re.compile(r"(?:未添加|不添加|零添加|0\s*添加|无添加|未使用|不使用)(?:任何)?[^，,。；;!?！!]{0,16}|(?:无|不含)(?:任何)?(?:食品添加剂|防腐剂|香精|香料|色素|甜味剂)")
WARNING = "儿童青少年应避免过量摄入盐油糖"
ALLERGENS = {
    "gluten": ("含麸质的谷物", r"小麦粉|小麦|黑麦|燕麦|大麦|含麸质(?:的)?谷物"),
    "crustaceans": ("甲壳纲类动物", r"甲壳纲|龙虾|虾仁|虾粉|虾米|蟹肉|蟹粉"),
    "fish": ("鱼类", r"鱼类|鱼肉|鱼糜|鱼粉|鳕鱼|鲈鱼|鲤鱼"),
    "egg": ("蛋类", r"鸡蛋|鸭蛋|蛋黄|全蛋粉|蛋液|蛋类"),
    "peanut": ("花生", r"花生碎|花生酱|花生粉|花生"),
    "soy": ("大豆", r"黄豆酱|黄豆|大豆|豆豉|酿造酱油|郫县豆瓣"),
    "milk": ("乳及乳制品", r"牛乳|牛奶|羊奶|乳粉|奶粉|乳清|乳糖|乳制品"),
    "nuts": ("坚果", r"榛子|榛仁|核桃|腰果|杏仁|巴旦木|开心果|坚果"),
}
# 八类的正式名称：报告逐类列结论时使用，和 ALLERGENS 的 key 一一对应。
ALLERGEN_NAMES = {
    "gluten": "含麸质的谷物（小麦、黑麦、大麦、燕麦）",
    "crustaceans": "甲壳纲类动物及其制品",
    "fish": "鱼类及其制品",
    "egg": "蛋类及其制品",
    "peanut": "花生及其制品",
    "soy": "大豆及其制品",
    "milk": "乳及乳制品",
    "nuts": "坚果及其果仁类制品",
}
# 前端语境里类别的英文键可能写成单数，两种写法都接受。
ALLERGEN_ALIASES = {
    "gluten": "gluten", "crustacean": "crustaceans", "crustaceans": "crustaceans",
    "fish": "fish", "egg": "egg", "peanut": "peanut", "soy": "soy",
    "milk": "milk", "nut": "nuts", "nuts": "nuts",
}
# 客户已拍板的口径：配料表内复合配料名加粗即按附录 D.2.1 视为已提示。
ALLERGEN_BOLD_NAMES = ("郫县豆瓣", "豆瓣酱", "黄豆酱", "甜面酱", "酿造酱油", "酱油", "豆豉")
# 附录 D.4 明列的深加工配料可免于提示，用于把「命中但属例外」和「命中且需核实」区分开。
ALLERGEN_EXEMPT = {
    "soy": ("大豆磷脂", "磷脂", "精炼大豆油", "维生素E", "植物甾醇", "植物甾醇酯", "植物甾烷醇酯", "黄原胶"),
    "peanut": ("精炼花生油",),
    "milk": ("乳糖醇",),
    "gluten": ("淀粉", "糊精", "葡萄糖浆", "谷物来源的精炼植物油"),
}
ALLERGEN_ADVISORY = re.compile(r"致敏|过敏|(?:可能)?含有(?:微量)?(?:麸质|花生|大豆|小麦|乳|蛋|坚果|鱼|虾|蟹)")
ALLERGEN_PRECAUTION = re.compile(r"可能含有|可能含|微量|生产设备|生产线还")
IRRADIATION = re.compile(r"辐照|电离辐射|电离能量")
IRRADIATION_DENIAL = re.compile(r"(?:未|不|非|无)(?:经|经过|采用|使用)?(?:辐照|电离辐射|电离能量)")
IRRADIATION_WORDS = re.compile(r"辐照|电离辐射|电离能量|辐照加工|经辐照处理")
# R14（GB 7718-2025 4.14 警示语）是转致性条款：只有命中已登记依据时才有结论。
# 依据表由 scripts/build_warnings.py 从授权文件生成；读不到时整条按未检查／未覆盖处理。
WARNING_BOOK_PATH = Path(__file__).resolve().parent / "knowledge" / "warnings.json"
SOLID_DRINK_DENIAL = re.compile(r"(?:不含|未含|不添加|未添加|不添加任何|不使用)(?:任何)?固体饮料|非固体饮料|不是固体饮料")
SOLID_DRINK_SPECIES = re.compile(r"可食用菌种|食用菌种|菌种|菌株")
GRADE_MARKS = re.compile(r"质量等级|品质等级|等级|一级|二级|三级|特级|优级")
STORAGE_PLACEHOLDER = re.compile(r"见包装|见标签|另一面|详见|按标识|包装上|另见|标识上")
STORAGE_DETAIL = re.compile(r"温度|湿度|阴凉|干燥|冷藏|冷冻|避光|通风|常温|℃|°C|阳光直射|密封|凉处")
DATE_FORMAT = re.compile(r"(?<!\d)(\d{4})\s*([-/.年])\s*(\d{1,2})\s*([-/.月])\s*(\d{1,2})(?!\d)")
DATE_DAY_FIRST = re.compile(r"日\s*[-/.．]\s*月\s*[-/.．]\s*年")
DATE_DAY_LEADING = re.compile(r"(?<!\d)(\d{1,2})\s*[-/.．]\s*(\d{1,2})\s*[-/.．]\s*(\d{4})(?!\d)")
CREATIVE_NAME = re.compile(r"(?:创立|始创|始于|源自|成立于|诞生于)\s*(?:19|20)\d{2}|^(?:19|20)\d{2}$")
NEW_NAME_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
TYPE_FIELD = re.compile(r"产品类型|产品类别|食品类别")

_WARNING_BOOK_CACHE = {"stamp": None, "book": None}


def warning_normalize(text):
    """把一句话压成只保留文字的比对形式：去掉全部空白与标点。

    OCR 经常把同一句警示语断成两三个 block，标点还常被单独识别；
    因此比对时两侧都去掉标点，跨 block 拼接也能对上。
    """
    return re.sub(r"[\s\u00a0\u3000。.,，、;；:：·（）()【】\[\]“”\"'‘’\-—－/]+", "", str(text or ""))


def warning_book():
    """读取警示语依据表。读不到或格式不对时返回 None，检查如实按未覆盖处理。"""
    try:
        stat = WARNING_BOOK_PATH.stat()
    except OSError:
        return None
    stamp = (stat.st_mtime_ns, stat.st_size)
    if _WARNING_BOOK_CACHE["stamp"] == stamp:
        return _WARNING_BOOK_CACHE["book"]
    try:
        book = json.loads(WARNING_BOOK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        book = None
    if not isinstance(book, dict):
        book = None
    _WARNING_BOOK_CACHE.update({"stamp": stamp, "book": book})
    return book


def warning_book_digest():
    """依据表字节摘要。报告要能说清「这份结论用的是哪一版依据表」。"""
    try:
        return hashlib.sha256(WARNING_BOOK_PATH.read_bytes()).hexdigest()
    except OSError:
        return ""


def warning_basis_source(basis):
    """把依据表登记的依据做成交互可核验的结构化来源。

    与 knowledge._citation 同形：文档身份、条款、位置、原文摘要都取自授权库，
    并且要求文件仍能通过 manifest 的 sha256 校验，否则返回 None（宁可不出结论）。
    """
    if not isinstance(basis, dict):
        return None
    document_id = str(basis.get("documentId") or "").strip()
    if not document_id:
        return None
    try:
        document = knowledge_metadata(document_id)
        resolved = knowledge_resolve(document_id)
    except Exception:
        return None
    if not document or resolved is None:
        return None
    paragraphs = []
    for raw in basis.get("paragraphs") or []:
        try:
            paragraphs.append(int(raw))
        except (TypeError, ValueError):
            continue
    path = f"/knowledge/{document_id}"
    return {
        "name": document.get("name"),
        "filename": document.get("filename"),
        "clause": str(basis.get("clause") or ""),
        "standardId": document.get("name"),
        "documentId": document_id,
        "url": path,
        "localUrl": path,
        "page": None,
        "pages": [],
        "paragraphs": paragraphs,
        "location": ("正文 XML 第 " + "、".join(map(str, paragraphs)) + " 段") if paragraphs else "",
        "effectiveDate": document.get("effectiveDate"),
        "sha256": document.get("sha256"),
    }


def string(value):
    if type(value) not in (str, int, float):
        return ""
    return normalized(str(value))


def entry(mapping, key):
    value = mapping.get(key, {})
    return value if isinstance(value, dict) else {}


def number(value):
    clean = string(value)
    if re.fullmatch(r"\d{1,12}(?:\.\d{1,8})?", clean):
        return Decimal(clean)


def number_text(value):
    return format(value, "f").rstrip("0").rstrip(".") if value % 1 else str(int(value))


def nrv_options(value):
    return sorted({int(value.to_integral_value(rounding=method)) for method in (ROUND_HALF_UP, ROUND_HALF_EVEN)})


def calendar_date(value):
    try:
        return date.fromisoformat(string(value))
    except ValueError:
        return None


def is_date(value):
    found = REAL_DATE.search(value) or COMPACT_DATE.search(value)
    if found:
        try:
            date(*(int(part) for part in found.groups()))
        except ValueError:
            return False
        return True
    return False


def shelf_six_months(value):
    length = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(个月|月|年)\s*", value)
    if not length:
        return False
    multiplier = 12 if length.group(2) == "年" else 1
    return Decimal(length.group(1)) * multiplier >= 6


def standard_code(value):
    """标准代号归一：去掉空白和年份后缀，方便和知识库清单逐字比对。"""
    code = re.sub(r"\s+", "", string(value)).upper()
    return re.sub(r"[-—–]\d{4}$", "", code)


def ingredient_words(value):
    """把配料表拆成候选配料名。

    只保留 2～8 个汉字的纯中文片段，并去掉括号里的规格、添加量和百分比，
    这样「小麦粉（≥30%）」会归一成「小麦粉」，可以拿去和食品名称互相印证。
    """
    words = []
    for part in re.split(r"[、，,。;；:：\s]+", string(value)):
        part = re.sub(r"[（(][^（）()]*[）)]", "", part)
        part = re.sub(r"(?:添加量|含量|规格)?\s*[≥≤<>]?\s*\d+(?:\.\d+)?\s*%?", "", part).strip()
        if 2 <= len(part) <= 8 and re.fullmatch(r"[\u4e00-\u9fff]+", part):
            words.append(part)
    return list(dict.fromkeys(words))


def name_ingredient_hits(name, words):
    """名称里出现、且能在配料表找到对应配料的词。

    允许用配料名的前/后缀匹配，例如名称「米线」对应配料「米线包」、名称「草鱼」对应「鲜草鱼」；
    匹配长度至少 2 个汉字，不足 2 字的片段不算命中，避免单字巧合。
    返回 (名称里的写法, 配料表里的配料名)。
    """
    hits = []
    for word in words:
        if word in name:
            hits.append((word, word))
            continue
        for size in range(min(len(word) - 1, 4), 1, -1):
            if word[:size] in name:
                hits.append((word[:size], word))
                break
            if word[-size:] in name:
                hits.append((word[-size:], word))
                break
    return list(dict.fromkeys(hits))


def date_reading(value):
    """读出一个日期写法：返回原文及日历是否有效。

    GB 7718-2025 第 4.7.1 条只要求按年、月、日的顺序清晰标示，没有规定月、日必须补零，
    所以这里只判断「顺序正确、日历有效」，不再对位数另加要求。
    """
    found = DATE_FORMAT.search(value)
    if found:
        year, _, month, _, day = found.groups()
        try:
            date(int(year), int(month), int(day))
        except ValueError:
            return {"text": found.group(0), "valid": False}
        return {"text": found.group(0), "valid": True}
    compact = COMPACT_DATE.search(value)
    if compact:
        try:
            date(*(int(part) for part in compact.groups()))
        except ValueError:
            return {"text": compact.group(0), "valid": False}
        return {"text": compact.group(0), "valid": True}
    return None


def artwork_date(pages):
    for page in pages:
        if sum(any(anchor in block.text for block in page) for anchor in PLATE_ANCHORS) < 2:
            continue
        for block in page:
            if "制作日期" not in block.text:
                continue
            adjacent = [candidate for candidate in page if candidate.x >= block.x and
                        abs(block.y - candidate.y) < .012 and candidate.x - block.right < .08]
            for candidate in adjacent:
                found = REAL_DATE.search(candidate.text)
                if found and is_date(found.group()):
                    return {"value": date(*(int(part) for part in found.groups())).isoformat(),
                            "refs": list(dict.fromkeys([block.id, candidate.id]))}


def mass_value(match):
    unit = UNITS[match.group(2).lower()]
    families = {
        "kg": ("g", 1000), "g": ("g", 1), "mg": ("g", Decimal(".001")),
        "L": ("mL", 1000), "mL": ("mL", 1),
    }
    dimension, multiplier = families[unit]
    return Decimal(match.group(1)) * multiplier, dimension


# 汤底呈味原料：按常见标示名称排列，用于从配料表反推「骨汤」的实际来源。
FLAVOR_SOURCES = ("鸡肉粉", "鸡粉", "鸡精调味料", "食用香精", "酵母抽提物", "呈味核苷酸二钠")
PACK_SPLIT = re.compile(r"(?:^|[。；;\n])\s*([\u4e00-\u9fff]{1,10}包[A-Z]?)\s*:")
BONE_RAW = re.compile(r"骨\s*(?:汤|高汤)|猪骨|鸡骨|牛骨|羊骨")


def claim_term(text):
    """从含「骨汤」的原句里取出用于定性和整改的那个词。"""
    found = re.search(r"骨\s*(?:汤|高汤)", string(text))
    return re.sub(r"\s+", "", found.group()) if found else "骨汤"


def flavor_source(ingredients):
    """从配料表反推汤底的呈味来源：按料包切分，取含呈味原料最多的一包。

    返回 (料包名, [原料名…])；配料表没有分包或没有呈味原料时返回 None。
    """
    parts = PACK_SPLIT.split(string(ingredients))
    packs = [(parts[index], parts[index + 1]) for index in range(1, len(parts) - 1, 2)]
    best = None
    for name, contents in packs:
        found = [word for word in FLAVOR_SOURCES if word in contents]
        if found and (best is None or len(found) > len(best[1])):
            best = (name, found)
    if not best:
        return None
    ordered = sorted(set(best[1]), key=FLAVOR_SOURCES.index)[:2]
    return best[0], ordered


def standards_requirements():
    """02 节引用的条款原文；取不到原文的条款不会出现在列表里。"""
    return [line for line in (
        clause_line("GB 7718-2025", "gb7718-2025", "4.2.1"),
        clause_line("GB 7718-2025", "gb7718-2025", "3.4"),
        clause_line("GB 7718-2025", "gb7718-2025", "4.4.1"),
        clause_line("《食品安全法》", "food-safety-law", "第七十一条"),
    ) if line]


class Report:
    def __init__(self, context, fields, nutrients, pages, coverage, reread=None, typography=None):
        self.context = context
        self.fields = fields
        self.nutrients = nutrients
        self.pages = pages
        self.blocks = [block for page in pages for block in page]
        self.blocks_by_id = {block.id: block for block in self.blocks}
        # 每个块所属的页序号：判断「同一展示版面邻近位置」时不能跨页比较坐标。
        self.page_index = {block.id: index for index, page in enumerate(pages) for block in page}
        # 放大重读确认过的读数：block id -> {"from": 原读数, "scale": 放大倍数}，由 /api/ocr 写入。
        self.reread = reread or {}
        # 字重测量：block id -> {"bold", "stroke_ratio", "baseline_ratio", "words"}，由 /api/ocr 写入。
        self.typography = typography or {}
        self.items = []
        # 读数值超出可能范围的项目：只排除该项，不牵连其它项目的复算。
        self.abnormal = {}
        self.verified = context.get("verified") is True
        self.complete = all(area["status"] == "已覆盖" for area in coverage)
        self.confirmed = self.verified and bool(self.blocks)
        self.layout_basis = string(context.get("layoutReviewBasis"))[:1000] if context.get("layoutReviewed") is True else ""
        self.artwork = False
        self.track = "actual"
        # 证据绑定：图片摘要 + 区域 + 字段值 + 单位/基准；任一变化即失效。
        digest_material = json.dumps(
            [[[getattr(block, "id", None), getattr(block, "text", None),
               list(getattr(block, "box", []) or [])] for block in page] for page in pages],
            ensure_ascii=False, sort_keys=True, default=str)
        self.image_digest = hashlib.sha256(digest_material.encode()).hexdigest()[:16]
        adopted = context.get("adoptedEvidence")
        self.adopted = adopted if isinstance(adopted, dict) else {}

    def _reread_keys(self):
        """哪些营养项的读数经过放大重读确认。

        重读标记按 block id 存在 self.reread 里；这里按「营养项 → 引用块」反查，
        再同时登记字段键与中文名，避免「脂肪」对不上「fat」这种静默漏判。
        若 context 里另有 autoReread（外部注入），一并并入。
        """
        keys, block_ids = set(), set()
        for key in NUTRIENTS:
            entry_row = (self.nutrients or {}).get(key)
            if isinstance(entry_row, dict):
                block_ids.update(str(ref) for ref in (entry_row.get("refs") or []))
        for block_id in block_ids:
            if block_id in (self.reread or {}):
                mark = self.reread[block_id]
                if isinstance(mark, dict) and mark.get("unresolved") is True:
                    continue
                for key in NUTRIENTS:
                    entry_row = (self.nutrients or {}).get(key)
                    if isinstance(entry_row, dict) and block_id in [str(r) for r in (entry_row.get("refs") or [])]:
                        keys.add(key)
        raw = self.context.get("autoReread") or {}
        if isinstance(raw, dict):
            keys.update(str(k) for k in raw)
        elif isinstance(raw, (list, tuple, set)):
            keys.update(str(k) for k in raw)
        for key in list(keys):
            name = (NUTRIENTS.get(key) or ("",))[0]
            if name:
                keys.add(str(name))
        return keys

    def value_confirmed(self, keys):
        """这些营养项的读数是否有可核验的依据。

        依据只有两种来源：
          - 程序放大重读确认过（autoReread，例如脂肪 125→12.5）；
          - 人工核验记录（verifiedFields）覆盖了该字段。
        都拿不到时，基于这些读数得出的结论只能算「待核实」，
        不能凭一个笼统的 verified 布尔升级为确定问题。
        """
        reread = self._reread_keys()
        fields = {str(row.get("field")) for row in (self.context.get("verifiedFields") or [])
                  if isinstance(row, dict)}
        adopted = {str(key) for key in (self.context.get("adoptedReadings") or {})}
        for key in keys:
            name = (NUTRIENTS.get(key) or ("",))[0]
            if key in fields:
                continue
            # 放大重读把原始读数改写过（如 125 → 12.5）时，单凭重读不算核验：
            # 必须有采纳记录，否则等于用改后的值证明改得对。
            if (name and name in reread) or key in reread:
                if self.reread_changed_original(key) and key not in adopted and name not in adopted:
                    return False
                continue
            # 营养表字段在人工核验记录里写作「营养表-xxx」，一并认。
            if any(str(row.get("field", "")).startswith("营养表-") and key in str(row.get("field"))
                   for row in (self.context.get("verifiedFields") or []) if isinstance(row, dict)):
                continue
            return False
        return True

    def _evidence_block(self, **kwargs):
        refs = self.evidence(**kwargs)
        return self.blocks_by_id.get(refs[0]) if refs else None

    def _value_block(self, **kwargs):
        """挑出真正承载数值的那个证据块。

        一个营养项通常引用多个块（项目名、数值、NRV%）。只取第一个会把
        原始识别值（如「125克」被重读改成「12.5克」）排除在外，
        于是「两次一致」就成了拿改后的值和自己比。
        """
        wanted = str(kwargs.pop("value", "") or "").strip()
        refs = [str(ref) for ref in self.evidence(**kwargs)]
        blocks = [self.blocks_by_id.get(ref) for ref in refs]
        blocks = [block for block in blocks if block is not None]
        if not blocks:
            return None
        if wanted:
            for block in blocks:
                if wanted and wanted in str(getattr(block, "text", "")):
                    return block
        for block in blocks:
            if getattr(block, "originalText", None):
                return block
        return blocks[0]

    def _value_item(self, key, label, aliases, required=True):
        item = entry(self.nutrients, key) if isinstance(self.nutrients, dict) else {}
        block = self._value_block(nutrients=[key], value=item.get("value"))
        mark = self.reread.get(getattr(block, "id", None)) if block is not None else None
        return evidence_lib.evidence_item(
            key, label, item.get("value"), item.get("unit"), block,
            self.context.get("verifiedFields") or [], aliases,
            {"image": self.image_digest, "basis": self.basis()},
            adopted=self.adopted.get(key), required=required, reread=mark)

    def _unit_item(self, key, label, aliases):
        item = entry(self.nutrients, key) if isinstance(self.nutrients, dict) else {}
        block = self._evidence_block(nutrients=[key])
        mark = self.reread.get(getattr(block, "id", None)) if block is not None else None
        return evidence_lib.evidence_item(
            key + "-unit", label, item.get("unit"), "", block,
            self.context.get("verifiedFields") or [], aliases,
            {"image": self.image_digest, "basis": self.basis()},
            adopted=self.adopted.get(key + "-unit"), reread=mark)

    def _basis_item(self):
        block = self._evidence_block(keys=("basis",))
        return evidence_lib.evidence_item(
            "basis", "计量基准", self.basis(), "", block,
            self.context.get("verifiedFields") or [], ["基准", "计量", "每100"],
            {"image": self.image_digest})

    def _nrv_item(self, key, label):
        block = None
        return evidence_lib.evidence_item(
            key + "-nrv", label, self.nrv(key), "%", block,
            self.context.get("verifiedFields") or [], [label + "NRV", "NRV", "营养素参考值"],
            {"image": self.image_digest, "basis": self.basis()},
            adopted=self.adopted.get(key + "-nrv"))

    def fat_nrv_checklist(self):
        """N02-fat-nrv：只要脂肪值、单位、标示 NRV%、计量基准四项。"""
        required = [self._value_item("fat", "脂肪含量值", ["脂肪"]),
                    self._unit_item("fat", "脂肪单位", ["脂肪", "单位"]),
                    self._nrv_item("fat", "标示 NRV%"),
                    self._basis_item()]
        return evidence_lib.checklist("N02-fat-nrv 脂肪标示与 NRV% 不一致", required,
                                      {"image": self.image_digest, "basis": self.basis()})

    def reading_status_note(self, keys):
        """说明这句「一致」是按当前读数复算出来的，还是读数本身也核验过。"""
        keys = tuple(keys or ("energy", "protein", "fat", "carb"))
        confirmed = [key for key in keys if self.value_confirmed((key,))]
        reread = self._reread_keys()
        fields = {str(row.get("field")) for row in (self.context.get("verifiedFields") or [])
                  if isinstance(row, dict)}
        names = "、".join((NUTRIENTS.get(key) or (key,))[0] for key in keys)
        if len(confirmed) == len(keys):
            return f"上述 {names} 的读数均已核验，复算与标示一致。"
        detail = []
        for key in keys:
            name = (NUTRIENTS.get(key) or (key,))[0]
            if key in fields or name in reread or key in reread:
                detail.append(f"{name}已核验")
            else:
                detail.append(f"{name}按当前读数")
        return ("本项结论为「按当前读数复算一致」：" + "、".join(detail) +
                "。读数本身是否与原图一致，仍以放大重读或人工核验为准。")

    def energy_checklist(self):
        """N02-energy-recalc：能量、参与计算的各成分（含单位）、计量基准。"""
        required = []
        for key, label in (("energy", "标示能量"), ("protein", "蛋白质"),
                           ("fat", "脂肪"), ("carb", "碳水化合物")):
            value = self._value_item(key, label + "值", [label])
            unit = self._unit_item(key, label + "单位", [label, "单位"])
            value_item = dict(value)
            value_item["label"] = label + "（值+单位）"
            value_item["state"] = "verified" if value["state"] == "verified" and unit["state"] == "verified" \
                else ("conflict" if "conflict" in (value["state"], unit["state"]) else "extracted")
            value_item["satisfied"] = value_item["state"] == "verified"
            value_item["unitState"] = unit["state"]
            required.append(value_item)
        required.append(self._basis_item())
        # 供能口径本身也要核验：换算系数、修约规则、是否存在其他供能成分。
        required.append(self._supply_item())
        return evidence_lib.checklist("N02-energy-recalc 能量复算差异", required,
                                      {"image": self.image_digest, "basis": self.basis()})

    def _supply_item(self):
        """供能口径：换算系数（蛋白/碳水 17、脂肪 37）+ 修约规则 + 其他供能成分确认。

        系数与修约是标准给定的固定口径，依所引条款视为已核验；
        但「是否存在膳食纤维/糖醇/酒精等其他供能成分」必须由原稿确认，
        没确认时该项只能是 extracted，整个能量复算结论不可升级。
        """
        extras = [block.text for block in self.blocks
                  if re.search(r"^\s*(?:[-—一]\s*)?(?:膳食纤维|糖醇|有机酸|乙醇|酒精)", block.text)]
        if extras:
            state = "conflict"
            note = "原稿出现其他供能成分，须按其能量系数单列：" + "；".join(extras[:2])
        elif self.context.get("energyBasisReviewed") is True:
            state = "verified"
            note = "口径已核验：蛋白/碳水 17 kJ/g、脂肪 37 kJ/g，按问答第二十七问修约"
        else:
            state = "extracted"
            note = "换算系数与修约按标准口径执行；其他供能成分尚未经人工确认"
        return {"key": "energy.supply", "label": "供能口径与其他供能成分",
                "state": state, "satisfied": state == "verified", "note": note}

    def nrv_checklist(self, keys):
        """N02-nrv：每个不一致的营养素各自的值/单位/NRV%，再加计量基准。"""
        required = []
        for key in keys:
            label = (NUTRIENTS.get(key) or ("该营养素",))[0]
            required.append(self._value_item(key, label + "值", [label]))
            required.append(self._unit_item(key, label + "单位", [label, "单位"]))
            required.append(self._nrv_item(key, label + " NRV%"))
        required.append(self._basis_item())
        names = "、".join((NUTRIENTS.get(key) or ("该营养素",))[0] for key in keys)
        return evidence_lib.checklist("N02-nrv %s标示与 NRV%% 不一致" % names, required,
                                      {"image": self.image_digest, "basis": self.basis()})

    def reread_changed_original(self, key):
        """该项的原始识别值是否被放大重读改写过。"""
        entry_row = (self.nutrients or {}).get(key)
        if not isinstance(entry_row, dict):
            return False
        for block in self.blocks or []:
            if str(getattr(block, "id", "")) not in [str(r) for r in (entry_row.get("refs") or [])]:
                continue
            mark = (self.reread or {}).get(str(getattr(block, "id", "")))
            if not isinstance(mark, dict):
                continue
            before = mark.get("from")
            after = mark.get("to")
            if before not in (None, "") and after not in (None, "") and str(before) != str(after):
                return True
        return False

    def field(self, key):
        return string(entry(self.fields, key).get("value"))

    def nvalue(self, key, attribute="value"):
        value = string(entry(self.nutrients, key).get(attribute))
        return UNITS.get(value.lower(), value) if attribute == "unit" else value

    def basis(self):
        value = self.field("basis")
        found = BASIS.fullmatch(value)
        return basis_name(found.group()) if found else ""

    def evidence(self, keys=(), nutrients=()):
        sources = [entry(self.fields, key).get("refs", []) for key in keys]
        sources += [entry(self.nutrients, key).get("refs", []) for key in nutrients]
        return list(dict.fromkeys(
            ref for group in sources if isinstance(group, list)
            for ref in group if isinstance(ref, str) and ref in self.blocks_by_id
        ))

    def missing_group(self, keys=(), nutrients=()):
        conflict = any(self.evidence(keys=[key]) and not self.field(key) for key in keys)
        conflict |= any(self.evidence(nutrients=[key]) and not self.nvalue(key) for key in nutrients)
        return "issue" if self.confirmed and self.complete and not conflict else "review"

    def rereaded(self, key):
        """该营养素是否有程序放大重读确认过的读数；没有就返回 None。

        只有当前读数仍等于重读结果时才作数：读数若在重读之后又被改动，
        再声称「已重读确认」就是假的，宁可不说。
        """
        for ref in self.evidence(nutrients=(key,)):
            mark = self.reread.get(ref)
            if not mark or not mark.get("to"):
                continue
            if number(self.nvalue(key)) == number(mark["to"]):
                return mark
        return None

    def typography_of(self, block):
        """该 block 的字重测量结果；没有测量或测不出来时返回 None。"""
        return self.typography.get(block.id) if block is not None else None

    def bold_word(self, word):
        """配料表里的某个词是否加粗：True / False / None（未测量或测不出）。"""
        verdicts = []
        for block in self.blocks:
            if word not in block.text:
                continue
            mark = self.typography.get(block.id)
            if not isinstance(mark, dict):
                continue
            words = mark.get("words") or {}
            exact = [value for token, value in words.items() if token == word]
            if exact:
                verdicts.extend(exact)
                continue
            # 目标词可能是更长字段的一部分（例如「小麦粉」之于「小麦粉（≥30%）」）。
            near = [value for token, value in words.items()
                    if word in token and len(token) <= len(word) + 2]
            verdicts.extend(near)
        if True in verdicts:
            return True
        if verdicts and all(value is False for value in verdicts):
            return False
        return None

    def reread_sentence(self):
        """把本次重读确认写成一句报告文字；没有重读时返回空串。"""
        parts = []
        for key in NUTRIENT_LABELS:
            mark = self.rereaded(key)
            if not mark:
                continue
            value, unit = self.nvalue(key), self.nvalue(key, "unit")
            label = NUTRIENT_LABELS[key]
            parts.append(f"{label} {value} {unit}".strip()
                         + f"（原读数 {mark['from']}，放大 {mark['scale']} 倍重读确认）")
        if not parts:
            return ""
        return "以下读数由程序裁切放大后重读确认，不是人工改值：" + "；".join(parts) + "。"

    def add(self, identifier, group, title, action, detail, evidence=(), version=None, rule=None):
        citation_id = rule or identifier
        # Some historical calls pass a citation id as `rule` (e.g. N01 cites F01).
        # Enablement follows the registered check id, not the citation alias.
        enable_id = identifier if (registry_mod.rule(identifier) is not None) else citation_id
        if not registry_mod.is_enabled(enable_id):
            return self.add_uncovered(identifier, title, rule_id=enable_id)
        source = source_for(citation_id, version) if version else None
        if version and not source:
            group, action = "gap", "补充指定知识库中的审核依据"
            detail = "当前指定资料没有启用这项检查所需的条款。"
        item = {"id": identifier, "group": group,
                "outcome": group, "track": self.track, "title": title, "action": action,
                "detail": detail, "refs": list(dict.fromkeys(evidence)),
                "ruleId": enable_id, "citationId": citation_id}
        if source:
            item["source"] = source
        self.items.append(item)
        return item

    def add_uncovered(self, identifier, title=None, rule_id=None):
        """Disabled rules must show 未检查／未覆盖, never pass."""
        rule_id = rule_id or identifier
        existing = next((item for item in self.items
                         if item.get("id") == identifier and item.get("outcome") == "uncovered"), None)
        if existing:
            return existing
        info = registry_mod.rule(rule_id) or {}
        item = {"id": identifier, "group": "uncovered", "outcome": "uncovered",
                "status": "未检查／未覆盖", "track": self.track,
                "title": (title or info.get("title") or identifier) + "（未检查／未覆盖）",
                "action": "该检查已停用，重新启用并发布后再审",
                "detail": "该检查已在已发布规则中停用，不能因此视为通过。",
                "refs": [], "uncoveredReason": "rule-disabled", "ruleId": rule_id}
        self.items.append(item)
        return item

    def close_disabled_rules(self):
        for rule_id in registry_mod.default_registry().registered_rule_ids():
            if registry_mod.is_enabled(rule_id):
                continue
            if any(item.get("ruleId") == rule_id or item.get("id") == rule_id for item in self.items):
                continue
            self.add_uncovered(rule_id)

    def explain(self, item, facts, requirement, reasoning, exceptions, missing, remediation):
        if item["outcome"] not in {"gap", "uncovered"}:
            item["analysis"] = {"facts": facts, "requirement": requirement, "reasoning": reasoning,
                                "exceptions": exceptions, "missingEvidence": missing, "remediation": remediation}
            return True
        return False

    def general(self, version):
        absent = [key for key in REQUIRED_FIELDS if not self.field(key)]
        evidence = self.evidence(keys=REQUIRED_FIELDS)
        if not absent:
            self.add("G01", "pass", "一般标示项目已定位", "复核实物与厂家资料",
                     "已定位食品名称、配料、生产者、地址、联系方式、许可证编号、产品标准和贮存条件。此项仅核对文字是否可见。",
                     evidence, version, "LAW67" if version == "all" else "G01")
            return
        # The small-label exception still requires the food name and producer information.
        necessary = [key for key in absent if version == "2025" and key == "name"]
        group = self.missing_group(keys=necessary) if necessary else "review"
        def names(keys):
            return "、".join(REQUIRED_FIELDS[key] for key in keys)

        if group == "issue":
            detail = f"已复核的完整标示中缺少：{names(necessary)}。"
            optional = [key for key in absent if key not in necessary]
            if optional:
                detail += f"其余待核实：{names(optional)}。请核对小包装或委托生产等简化条件。"
        else:
            detail = f"当前资料未确认：{names(absent)}。请核对小包装或委托生产等简化条件。"
        self.add("G01", group, "一般标示项目需补充", "检查全部展示面及适用例外",
                 detail, evidence, version, "LAW67" if version == "all" else "G01")

    def dates(self):
        version = "2025"
        production = self.field("production")
        expiry = self.field("expiry")
        shelf = self.field("shelf")
        extended = shelf_six_months(shelf)
        identifier = "F04"
        keys = ["production", "expiry"]
        if extended and expiry:
            keys.remove("production")
        evidence = self.evidence(keys=("production", "shelf", "expiry"))
        missing = [key for key in keys if not self.field(key)]
        labels = {"production": "生产日期", "expiry": "保质期到期日", "shelf": "保质期"}
        if missing:
            note = "2025 版一般要求生产日期及到期日。保质期至少 6 个月或最大表面积不大于 20 cm² 时，可仅标保质期及到期日。"
            self.add(identifier, "review", "日期标示需补充", "核对实物喷码与日期标示",
                     "当前资料未确认" + "、".join(labels[key] for key in missing) + "。" + note,
                     evidence, version)
            return
        unresolved = [(key, self.field(key)) for key in keys if not is_date(self.field(key))]
        production_refs = set(self.evidence(keys=("production",)))
        if "production" in keys and any(
            block.id in production_refs and "制作日期" in block.text for block in self.blocks
        ):
            unresolved.append(("production", production))
        if unresolved:
            locations = "；".join(f"{labels[key]}为「{value}」" for key, value in dict(unresolved).items())
            directed = any(POSITION.search(value) for _, value in unresolved)
            detail = locations + ("。已识别喷码位置指引，请对照对应部位实际日期。" if directed else
                                  "。请核对是否为生产或到期日期，并注明喷码具体部位。")
            self.add(identifier, "review", "日期需对照喷码", self.capture_action("日期区域"), detail,
                     evidence, version)
            return
        detail = f"生产日期 {production}，到期日 {expiry}。"
        if extended and "production" not in keys:
            detail = f"保质期 {shelf}，到期日 {expiry}。满足 6 个月及以上日期标示例外的时间门槛。"
        self.add(identifier, "pass", "日期文字已定位", "核对日期与实际批次", detail, evidence, version)

    def net(self, version):
        net = self.field("net")
        packs = self.field("packs")
        evidence = self.evidence(keys=("net", "packs"))
        match = MASS.fullmatch(net)
        if not match:
            group = "review"
            detail = (f"标示「{net}」含乘数，请核对总净含量、每包量和数量。" if MULTIPLIER.search(net)
                      else "当前资料未确认可读的净含量数值及单位。")
            self.add("G03", group, "净含量需核对", "确认净含量和计量单位",
                     detail, evidence, version, "METRO" if version == "all" else "G03")
            return
        if not packs:
            ambiguous = bool(self.evidence(keys=("packs",)))
            self.add("G03", "review" if ambiguous else "pass",
                     "分包规格待核对" if ambiguous else "净含量文字已定位",
                     "对照销售包装与规格", f"净含量 {net}。分包合计仅在标签提供同一销售单元内完整的分包重量时复算。",
                     evidence, version, "METRO" if version == "all" else "G03")
            return
        amounts = [mass_value(part) for part in MASS.finditer(packs)]
        total, unit = mass_value(match)
        complex_specification = MULTIPLIER.search(packs) or re.search(r"净含量|合计|共计|总量|总重", packs)
        if complex_specification or len(amounts) < 2 or any(dimension != unit for _, dimension in amounts):
            detail = ("分包规格含乘数或小计，请核对单包量、数量和总量。" if complex_specification
                      else "分包规格不能直接统一换算。")
            self.add("G03", "review", "分包规格需核对", "确认分包计量范围和单位",
                     f"外包装标示 {net}。{detail}", evidence, version, "METRO" if version == "all" else "G03")
            return
        subtotal = sum(amount for amount, _ in amounts)
        mismatch = subtotal != total
        group = ("issue" if self.confirmed else "review") if mismatch else "pass"
        detail = f"{len(amounts)} 包合计 {number_text(subtotal)} {unit}，外包装净含量 {net}。"
        self.add("G03", group, "分包重量与净含量" + ("有差异" if mismatch else "一致"),
                 "核对分包和包装稿" if mismatch else "核对分包对应关系", detail, evidence, version,
                 "METRO" if version == "all" else "G03")

    def nutrition(self):
        version = "2025"
        core = tuple(NUTRIENTS)
        basis = self.basis()
        required = self.context.get("nutritionRequired", "unknown")
        available = any(self.nvalue(key) or self.evidence(nutrients=(key,)) for key in (*core, "saturated", "sugar"))
        should_have = required == "required" or available
        evidence = self.evidence(keys=("basis",), nutrients=core)
        missing = [key for key in core if number(self.nvalue(key)) is None or self.nvalue(key, "unit") != NUTRIENTS[key][1]]
        valid_basis = is_valid_basis(basis)
        if required == "exempt" and not available:
            self.add("N01", "pass", "营养标示按所选豁免处理", "保留豁免依据",
                     "使用了手动确认的营养标示豁免。请保存食品类别或包装面积等依据。", evidence,
                     version, "F01")
        elif missing or not valid_basis:
            group = self.missing_group(nutrients=missing) if should_have and missing and valid_basis else "review"
            names = [NUTRIENTS[key][0] for key in missing]
            if not valid_basis:
                names.insert(0, "单一、可对应的标示基准")
            self.add("N01", group, "营养表基准或核心项目待核对", "核对原表和适用的营养标示义务",
                     "当前资料未确认" + "、".join(names) + "。请核对单位和列对应关系，适用义务未知时先核实类别与包装条件。",
                     evidence, version, "F01")
        else:
            self.add("N01", "pass", "能量与核心四项已定位", "复核原图列和单位",
                     f"标示基准为{basis}，已定位能量、蛋白质、脂肪、碳水化合物和钠。",
                     evidence, version, "F01")
        if required == "exempt" and not available:
            return
        extra = ("saturated", "sugar")
        missing = [key for key in extra if number(self.nvalue(key)) is None or self.nvalue(key, "unit") != "g"]
        absent_saturated_nrv = "saturated" not in missing and self.nrv("saturated") is None
        if missing or absent_saturated_nrv:
            names = ["饱和脂肪或饱和脂肪酸" if key == "saturated" else "糖" for key in missing]
            if absent_saturated_nrv:
                names.append("饱和脂肪的 NRV%")
            group = self.missing_group(nutrients=missing) if should_have else "review"
            self.add("F01", group, "2025 版新增营养项目需核对", "核对适用义务与新版营养表",
                     "当前资料未确认" + "、".join(names) + "。糖应标含量，糖没有标准 NRV。",
                     self.evidence(nutrients=extra), version)
        else:
            self.add("F01", "pass", "2025 版新增营养项目已定位", "复核新版营养表",
                     "已定位饱和脂肪或饱和脂肪酸、糖的含量。糖没有标准 NRV。",
                     self.evidence(nutrients=extra), version)
        self.warning(should_have)

    def warning(self, should_have):
        exact = [block.id for block in self.blocks if WARNING in re.sub(r"\s+", "", block.text)]
        possible = [block.id for block in self.blocks if "避免过量" in block.text and ("少年" in block.text or "儿童" in block.text)]
        if exact:
            self.add("F02", "pass", "盐油糖提示语已定位", "核对提示语位置",
                     "文字已定位，请确认印在营养成分表下方。", exact, "2025")
            return
        group = "issue" if self.confirmed and self.complete and should_have and not possible else "review"
        self.add("F02", group, "盐油糖提示语需核对", "核对原图文字和位置",
                 f"当前资料未确认完整提示语「{WARNING}」。新版要求标示在营养成分表下方。",
                 possible, "2025")

    def nrv(self, key):
        return number(self.nvalue(key, "nrv").removesuffix("%").strip())

    def check_nrv(self, version):
        mismatches, missing, matches = [], [], []
        included = []
        bad_keys = []
        basis = self.basis()
        for key, (name, unit, reference) in NUTRIENTS.items():
            if key in self.abnormal:
                # 读数值已判为异常时不参与 NRV 复算，避免用错数去指控客户。
                continue
            value = number(self.nvalue(key))
            if value is None or self.nvalue(key, "unit") != unit or not is_valid_basis(basis):
                continue
            included.append(key)
            expected = value * 100 / reference
            rounded = nrv_options(expected)
            desired = "% 或 ".join(str(option) for option in rounded) + "%"
            reported = self.nrv(key)
            if reported is None:
                missing.append(name)
            elif reported not in rounded:
                mismatches.append(f"{name}标示 {number_text(reported)}%，表内应约为 {desired}")
                bad_keys.append(key)
            else:
                matches.append(f"{name} {number_text(value)} {unit} → {number_text(reported)}%")
        sugar_percent = self.nrv("sugar")
        if sugar_percent is not None:
            included.append("sugar")
        if not included:
            return
        if mismatches or missing or sugar_percent is not None:
            findings = mismatches.copy()
            if missing:
                findings.append("未确认" + "、".join(missing) + "的 NRV%")
            if sugar_percent is not None:
                findings.append(f"糖行识别到 {number_text(sugar_percent)}%，糖没有标准 NRV，请核对列归属")
            if matches:
                findings.append("可复算：" + "、".join(matches))
            # 确定问题必须每一项证据都 verified；"满足几项"只展示，不参与放行。
            check = self.nrv_checklist(bad_keys or included)
            group = "issue" if (self.confirmed and mismatches and check["upgradeAllowed"]) else "review"
            item = self.add("N02", group, "营养素参考值需核对", "对照原表并重算 NRV%",
                            "；".join(findings) + "。证据清单：" + evidence_lib.state_text(check) + "。",
                            self.evidence(keys=("basis",), nutrients=included), version)
            item["evidenceChecklist"] = check
            item["upgradeAllowed"] = check["upgradeAllowed"]
        else:
            # 复算一致也可能是「用一笔有争议的读数算出来的」：读数本身冲突时
            # 只能给待核实，由 report 说明冲突值，而不能写成通过。
            check = self.nrv_checklist(included) if included else None
            conflicted = bool(check) and any(row.get("state") == "conflict"
                                             for row in (check or {}).get("required") or [])
            if conflicted:
                item = self.add("N02", "review", "营养素参考值读数存在冲突",
                                "回看原图确认首次识别与放大重读哪一个正确",
                                "；".join(matches) + "。上述复算基于当前采用值；"
                                "但该值与原图首次识别结果不一致，须人工确认后才能据此下结论。",
                                self.evidence(keys=("basis",), nutrients=included), version)
                item["evidenceChecklist"] = check
            else:
                item = self.add("N02", "pass", "已识别的 NRV 可复算", "复核原图标示值",
                                "；".join(matches) + "。按同基准含量除以参考值并修约为整数百分比。",
                                self.evidence(keys=("basis",), nutrients=included), version)
                # 通过也要把清单带出来：读数用了哪一次、是否被重读改写，客户能看见。
                if check:
                    item["evidenceChecklist"] = check

    def energy(self, version):
        keys = ("energy", "protein", "fat", "carb")
        if any(key in self.abnormal for key in keys):
            # 任一产能营养素读数异常时，能量反算失去意义，只保留 N00 的核对要求。
            return
        values = [number(self.nvalue(key)) for key in keys]
        units = [self.nvalue(key, "unit") for key in keys]
        basis = self.basis()
        if any(value is None for value in values) or units != ["kJ", "g", "g", "g"] or not is_valid_basis(basis):
            return
        shown, protein, fat, carb = values
        total = protein * 17 + fat * 37 + carb * 17
        fat_energy = fat * 37
        rounding = Decimal(".5") + (17 + 37 + 17) * Decimal(".05")
        # Zero can represent an amount within the standard's zero threshold, not an exact zero.
        rounding += sum(factor * Decimal(".45") for value, factor in zip((protein, fat, carb), (17, 37, 17)) if value == 0)
        extras = [block.id for block in self.blocks if re.search(r"^\s*(?:[-—一]\s*)?(?:膳食纤维|糖醇|有机酸|乙醇)", block.text)]
        primary = []
        for key in ("energy", "fat"):
            row = self.evidence(nutrients=(key,))
            if row:
                primary.append(next((ref for ref in row if NUTRIENT_AMOUNT.search(self.blocks_by_id[ref].text)), row[0]))
        evidence = primary + self.evidence(nutrients=("energy", "fat", "protein", "carb"), keys=("basis",)) + extras
        detail = (f"{basis}标示 {number_text(shown)} kJ。"
                  f"蛋白质×17＋脂肪×37＋碳水化合物×17 ≈ {number_text(total)} kJ"
                  f"（约 {int(total.to_integral_value(rounding=ROUND_HALF_UP))} kJ）"
                  f"，仅脂肪折算 {number_text(fat_energy)} kJ。")
        energy_maximum = shown + Decimal(".5") if shown else Decimal(17)
        # 只供报告第 05 节排版能量反算式，不改动任何判定；总量与差值都是上面已算好的数。
        check = {
            "protein": number_text(protein), "fat": number_text(fat), "carb": number_text(carb),
            "energy": number_text(shown), "total": number_text(total),
            "difference": number_text(abs(shown - total)), "within": abs(shown - total) <= rounding,
        }
        check = self.energy_checklist()
        if energy_maximum < (fat - Decimal(".05")) * 37:
            group = "issue" if (self.confirmed and not is_serving_basis(basis) and check["upgradeAllowed"]) else "review"
            item = self.add("N03", group, "标示能量低于脂肪折算值",
                            "回看原图与供能计算表",
                            detail + "证据清单：" + evidence_lib.state_text(check) + "。", evidence, version)
            item["evidenceChecklist"] = check
            item["upgradeAllowed"] = check["upgradeAllowed"]
        elif abs(shown - total) > rounding or extras:
            item = self.add("N03", "review", "能量基础复算有差异", "核对原表和完整供能组成",
                            detail + "请考虑膳食纤维、糖醇、酒精及修约等因素。证据清单："
                            + evidence_lib.state_text(check) + "。", evidence, version)
            item["evidenceChecklist"] = check
            item["upgradeAllowed"] = check["upgradeAllowed"]
        else:
            item = self.add("N03", "pass", "能量基础复算一致", "复核原图和计算表",
                            detail + "差值在本次表内修约范围内。" + self.reading_status_note(()), evidence, version)
            rounded = total.to_integral_value(rounding=ROUND_HALF_UP)
            comparison = (f"修约后与标示的 {number_text(shown)} kJ 一致。" if rounded == shown else
                          f"与标示的 {number_text(shown)} kJ 的差值在本次表内修约范围内。")
            item["customerConclusion"] = f"{basis}基础复算为 {number_text(total)} kJ，{comparison}本项无须仅因复算结果修改能量标示。"
        item["energyCheck"] = check

    def bone_remediation(self, term, flavor, ingredients):
        """Q2：三个分支统一为结构化 options（optionId/condition/text），02 与 07 同源。"""
        if BONE_RAW.search(string(ingredients)):
            branch_a = (f"【分支 A｜确有骨汤】配料表已出现骨类原料：就近补标该原料的添加量或在成品中的含量"
                        f"（GB 7718-2025 4.4.1），「{term}」文案与图案可保留；同时核对复合配料展开与定量口径后定稿。")
        else:
            branch_a = (f"【分支 A｜确有骨汤】在配料表中列明骨汤所用原料（如猪骨、鸡骨或骨汤调味料），"
                        f"并按 GB 7718-2025 4.4.1 标示该原料的添加量或在成品中的含量；「{term}」文案与图案可保留，"
                        f"核对复合配料展开与定量口径后定稿。")
        if flavor:
            pack, words = flavor
            actual = f"汤底实际来自{pack}中的{'与'.join(words)}"
        else:
            actual = "汤底实际来自其它配料与食用香精"
        branch_b = (f"【分支 B｜并无骨汤】{actual}：删去「{term}」二字，按实际改写为「鲜香汤底」等表述，"
                    f"图案改为非真实照片式并在图旁醒目标示「图案仅供口味参考」；同步复核第 03、06 节相关条目后定稿。")
        branch_c = ("按新增分包重算净含量与营养数据，在配料表中列明该包配料，并同步核对第 05 节。"
                    if self.field("net") and any(self.nvalue(key) for key in ("energy", "fat", "carb"))
                    else "按新增分包重算净含量与营养数据，并在配料表中列明该包配料。")
        return [
            {"optionId": "bone-A", "condition": "确有骨汤：有配方、供应商规格或投料记录支撑",
             "text": branch_a.replace("【分支 A｜确有骨汤】", "")},
            {"optionId": "bone-B", "condition": "并无骨汤：实际风味来自其他配料或食用香精",
             "text": branch_b.replace("【分支 B｜并无骨汤】", "")},
            {"optionId": "bone-C", "condition": "拟新增骨汤包：以实际配方与投料为前提",
             "text": branch_c},
        ]

    def capture_action(self, target):
        """样张（刀版）与实拍使用不同的取证动作；刀版报告不出现「补拍」。"""
        kind = self.context.get("sampleKind")
        artwork = (kind == "artwork") if kind in ("artwork", "physical") else bool(self.artwork)
        if artwork:
            special = {
                "日期区域": "请提供喷码方案（位置、字体、格式与相对位置）或打样件",
                "贮存条件区域": "请提供贮存条件区域的完整刀版或矢量稿",
                "完整日期": "请提供完整日期排版或核对原稿日期排布",
                "完整营养表": "请提供完整营养表的刀版或矢量稿",
                "日期证据": "按证据状态核对；请提供喷码方案（位置、字体、格式与相对位置）",
                "该段清晰照片或矢量稿/排版文件": "该段请提供矢量稿或排版文件",
            }
            return special.get(target, "请提供" + target + "的完整刀版或矢量稿")
        special = {
            "日期区域": "请补拍喷码部位",
            "贮存条件区域": "请补拍贮存条件区域或确认标签是否标示",
            "完整日期": "请补拍完整日期或核对原稿日期排布",
            "完整营养表": "请补拍一张完整营养表",
            "日期证据": "按证据状态核对或补拍",
            "该段清晰照片或矢量稿/排版文件": "该段请补拍清晰照片（正对、光线均匀）",
        }
        return special.get(target, "请补拍" + target)

    def bone_claim(self):
        matches = [block for block in self.blocks if re.search(r"骨\s*汤|骨\s*高汤", block.text)]
        if not matches:
            return
        preparation = [block for block in self.blocks if re.search(r"倒入.{0,14}(?:开水|白水)\s*\d+\s*(?:毫升|mL)", block.text, re.I)]
        wording = matches[0].text.strip("：:")
        evidence = self.evidence(keys=("ingredients", "packs"))
        basis = string(self.context.get("boneClaimBasis"))[:1000]
        verification = self.context.get("boneClaimEvidence") if self.confirmed and basis else None
        facts = [f"包装可定位到「{wording}」。"]
        if preparation:
            facts.append(f"食用方法写有「{preparation[0].text.rstrip('，,。')}」。")
        if self.field("ingredients") and not re.search(r"骨", self.field("ingredients")):
            facts.append("当前可读配料表未定位到骨汤、骨类原料或汤包。")
        if verification in ("unsupported", "supported", "flavor"):
            facts.append(f"人工核验记录「{basis}」。")
        requirement = "《食品安全法》第七十一条禁止标签含虚假内容，并禁止销售与标签、说明书内容不符的食品。"
        exceptions = ["分包或复合配料的标签可能省略部分原始配料，须核对实际规格和投料。仅加白水冲调也不能排除配料中已有骨汤原料。",
                      "真实配料、工艺声称与明确的风味描述须结合原版文图语境区分。"]
        new_source = source_for("C02", "2025")
        if new_source:
            exceptions.append("第 4.4.1.2 条及问答第三十五问区分说明风味的图案和特别强调。仅用食用香精、香料调配风味时，使用非真实照片式图案，并在近旁醒目标示「图案仅供口味参考」等字样。")
        options = [
            "确认无骨汤原料或工艺时，删除使人以为实际有骨汤的文案和对应画面，按真实风味改写。",
            "如有支撑，逐项核对配料列示、复合配料例外和特别强调时的定量。拟新增骨汤包时，重算净含量和营养数据。",
        ]
        printed_ingredients = self.field("ingredients")
        if (verification is None and self.artwork and preparation and
                printed_ingredients and evidence and not re.search(r"骨|高汤", printed_ingredients) and
                not any(re.search(r"骨|高汤", self.blocks_by_id[ref].text) for ref in self.evidence(keys=("ingredients",))) and
                any(not re.search(r"风味|口味|风格", block.text) for block in matches)):
            refs = [block.id for block in matches + preparation] + evidence
            hold = self.add("C02", "issue", "「骨汤」声称缺少对应标示支撑", "暂停付印，按核实的来源整改后复审",
                            "完整刀版经核对：「一煨骨汤」一类文字与配图表达实际用料或工艺，配料及料包未见对应标示。"
                            "当前稿件不予通过。", refs)
            hold.update(severity="draft-hold", category="稿件放行", reviewBasis=self.layout_basis)
            # ① 现状→② 推论：骨汤只是文案与图案，配料表里能查到的是呈味原料。
            term = claim_term(wording)
            flavor = flavor_source(printed_ingredients)
            if flavor:
                pack, words = flavor
                facts.append(f"汤底的鲜味来源，从配料表看是{pack} 里的{'与'.join(words)}。")
            # ③ 定性：不依赖「骨汤究竟是被漏标还是有据」这个未定前提。
            reasoning = ("整版复核确认声称与已列配料不相互支撑。审核结论为不予通过，实际配方由生产方材料核定。"
                         f"这一项不依赖未定前提——无论「{term}」是漏标还是无据，两种情形都须整改，因此可以直接定性，不需挂待核实。")
            # ④ 依据改用条款原文；⑤ 整改按证据分三选一。
            requirement = standards_requirements() or ["稿件放行判断以本次核对的标示为限。"]
            remediation = self.bone_remediation(term, flavor, printed_ingredients)
            self.explain(hold, facts, requirement, reasoning, [], [], remediation)
            if new_source:
                hold["guidanceSources"] = [new_source]
            self.add("C02-source", "review", "骨汤来源与具体整改路径",
                     "生产方提供配方、供应商规格和投料、工艺记录",
                     "确有骨汤原料或工艺支撑时，核对配料列示、复合配料例外及定量要求。确认无支撑时，更正文案和配图；若仅用食用香精、香料调配风味，再按新版适用条件核对图案及近旁提示。", refs)
            hold["revisionOptions"] = remediation
            return
        if verification == "unsupported":
            group, title = "issue", "「骨汤」实际配料或工艺声称与核验事实不符"
            reasoning = "原图被核实为实际含骨汤或使用骨汤工艺的声称。完整配方、复合配料规格及工艺核验均无支撑，可确认文案与实际情况不符。"
            missing = []
            options = options[:1]
        elif verification == "supported":
            group, title = "review", "「骨汤」来源已核实，需核对列示和定量"
            reasoning = "核验材料支持声称所指的原料或工艺。现有证据尚未确认这项原料应否单独列出，以及整版是否触发特别强调定量要求。"
            missing = ["完整配料表与投料或成品含量记录", "特别强调的图文语境及适用的复合配料例外"]
            options = options[1:]
        elif verification == "flavor":
            group, title = "review", "「骨汤」风味描述需核对图文表达"
            reasoning = "核验记录确认用语描述风味，仍需核对消费者所见图文和真实风味来源。"
            missing = ["整版图案及相邻说明", "实际风味来源及图案是否为真实照片式"]
            options = ["核对文案和图案能否清楚表达真实风味。"]
            if new_source:
                options.append("若适用新版且仅用食用香精、香料调配风味，按第 4.4.1.2 条同步修改图案及图旁提示。")
        else:
            group, title = "review", "「骨汤」声称与配料及食用方法需核实"
            reasoning = "现有标签文字能定位疑点。配料读数及加白水的步骤不足以判断实际原料、生产工艺或整个画面传递的含义。"
            missing = ["含配图、上下文及整套分包的完整标签", "配方、供应商规格、投料记录和制作工艺"]
        item = self.add("C02", group, title, " ".join(options), " ".join([*facts, reasoning]),
                        [block.id for block in matches + preparation] + evidence,
                        "all", "LAW71")
        item["category"] = "图案与文字声称"
        if self.explain(item, facts, requirement, reasoning, exceptions, missing, options):
            if group == "issue":
                item["severity"] = "redline"
            item["revisionOptions"] = options
            if new_source:
                item["guidanceSources"] = [new_source]

    def claims(self):
        emphasis = [block for block in self.blocks if (
            re.search(r"(?:特别添加|特别选用|精选鲜|特别含有)\s*[\u4e00-\u9fff]{2,6}", block.text)
            and not re.search(r"配料表|含量\s*[:;]|添加量\s*[:;]", block.text)
        )]
        name = self.field("name")
        name_clue = re.search(r"牛肉|草莓|骨汤|榛子|草鱼", name)
        if emphasis or name_clue:
            evidence = [block.id for block in emphasis] + self.evidence(keys=("name", "ingredients"))
            self.add("C01", "review", "配料强调需核对定量", "查完整标示和产品配方",
                     "识别到可能强调配料的用语或名称，请核实是否适用配料定量要求，并对照配料表中的添加量或成品含量。",
                     evidence, "2025")
        matched = [(match.group(), block.id) for block in self.blocks for match in CLAIM.finditer(block.text)]
        if matched:
            wording = "、".join(dict.fromkeys(text for text, _ in matched))
            self.add("F03", "review", "2025 版「不添加」等用语需核对",
                     "确认原字样、声称对象与适用例外",
                     f"识别到「{wording}」。请核对新版条款、实际语境和例外。",
                     [identifier for _, identifier in matched], "2025")

    def field_statuses(self):
        results = []
        for key, label in LABELS.items():
            value = self.field(key)
            refs = self.evidence(keys=(key,))
            if key in {"production", "expiry"} and value and not is_date(value) and (POSITION.search(value) or "见包装" in value):
                state, reason = "无法识别", "已定位日期位置指引，实际日期仍待核对。"
            elif value and refs:
                state, reason = "已标", "已定位原图文字；适用性和内容另审。"
            elif not value and not refs and self.confirmed and self.complete:
                state, reason = "未标", "用户已核对各包装区域且未确认该标示；是否必标另审。"
            else:
                state = "无法识别"
                reason = ("有候选证据但数值、归属或内容待核对。" if refs else
                          "无可定位证据，或包装覆盖、可读性尚未确认。")
            results.append({"key": key, "label": label, "status": state,
                            "value": value, "refs": refs, "reason": reason})
        return results

    def nutrition_rows(self, allow_calculation):
        definition = {**NUTRIENTS, "saturated": ("—饱和脂肪", "g", 20), "sugar": ("—糖", "g", None)}
        rows = []
        for key in ("energy", "protein", "fat", "saturated", "carb", "sugar", "sodium"):
            name, expected_unit, reference = definition[key]
            value, unit, nrv = self.nvalue(key), self.nvalue(key, "unit"), self.nvalue(key, "nrv")
            if not value:
                continue
            row = {"name": name, "value": f"{value} {unit}".strip(), "nrv": nrv, "refs": self.evidence(nutrients=(key,))}
            if key in self.abnormal:
                row.update(calculation=f"识别异常：{self.abnormal[key].rstrip('。')}。请核对原图后复算。",
                           expected="—", match=None)
                rows.append(row)
                continue
            if allow_calculation and unit == expected_unit and number(value) is not None and self.basis():
                if reference is None:
                    row.update(calculation="指定 2025 版附录 A 未列糖的 NRV。", expected="空白", match=not bool(self.nrv(key) is not None))
                else:
                    exact = number(value) * 100 / reference
                    rounded = nrv_options(exact)
                    row.update(calculation=f"{value}÷{reference}×100%≈{exact:.2f}%", expected="% 或 ".join(map(str, rounded)) + "%",
                               match=self.nrv(key) in rounded)
            mark = self.rereaded(key)
            if mark:
                note = f"放大 {mark['scale']} 倍重读确认：原读数 {mark['from']}。"
                row["calculation"] = (row["calculation"].rstrip("。") + "。" + note
                                      if row.get("calculation") else note)
                ref = next((ref for ref in self.evidence(nutrients=(key,))
                            if ref in self.reread and self.reread[ref].get("to")), None)
                block = self.blocks_by_id.get(ref) if ref else None
                row["reread"] = {"from": mark["from"], "to": mark.get("to"), "scale": mark["scale"],
                                 "blockId": ref, "box": list(block.box) if block else [],
                                 "at": mark.get("at")}
            rows.append(row)
        return rows

    def nutrition_crosscheck(self, version):
        guards = anomalies(self.nutrients, self.basis())
        if guards:
            detail = "；".join(f"{g['label']} {g['value']} {g['unit']}：{g['reason'].rstrip('。')}" for g in guards)
            refs = list(dict.fromkeys(
                ref for g in guards for ref in self.evidence(nutrients=(g["key"],))))
            # 只排除读数值异常的项目，其余项目继续复算，避免整张表一起失去结论。
            self.abnormal = {g["key"]: g["reason"] for g in guards}
            self.add("N00", "review", "营养读数异常，该项不参与复算",
                     "请放大原图核对标注数值后重新审核",
                     detail + "。上述项目不参与 NRV 与能量复算，其余项目照常复核。", refs)
        self.check_nrv(version)
        self.energy(version)
        confirmation = self.reread_sentence()
        if confirmation:
            # 重读确认只补充说明读数来源，不改变任何判定结论。
            for item in self.items:
                if item["id"] in {"N02", "N03"}:
                    item["detail"] = item["detail"].rstrip("。") + "。" + confirmation
        nrv = next((item for item in reversed(self.items) if item["id"] == "N02"), None)
        energy = next((item for item in reversed(self.items) if item["id"] == "N03"), None)
        if not nrv or not energy or nrv["outcome"] not in {"issue", "review"} or energy["outcome"] not in {"issue", "review"}:
            return
        if "脂肪标示" not in nrv["detail"] or "请考虑膳食纤维" not in energy["detail"]:
            return
        if any(re.search(r"^\s*(?:[-—一]\s*)?(?:膳食纤维|糖醇|有机酸|乙醇)", block.text)
               for block in self.blocks):
            return
        shown, protein, fat, carb = (number(self.nvalue(key)) for key in ("energy", "protein", "fat", "carb"))
        percent = self.nrv("fat")
        if None in (shown, protein, fat, carb, percent) or min(protein, fat, carb) == 0:
            return
        inferred = ((shown - protein * 17 - carb * 17) / 37).quantize(Decimal(".1"), rounding=ROUND_HALF_UP)
        if inferred <= 0 or inferred == fat:
            return
        inferred_energy = protein * 17 + inferred * 37 + carb * 17
        for rounding in (ROUND_HALF_UP, ROUND_HALF_EVEN):
            if (inferred_energy.to_integral_value(rounding=rounding) == shown and
                    (inferred * 100 / NUTRIENTS["fat"][2]).to_integral_value(rounding=rounding) == percent):
                check = self.energy_checklist()
                item = self.add("N02H", "review", f"反算候选：脂肪可能为 {inferred:.1f} g（不改写印刷值）",
                                "把反算值作为核对线索，回看原图与制表底稿；不据此改写印刷值",
                                f"反算假设其他数值和供能口径不变，脂肪 {inferred:.1f} g 可同时解释两处差异。"
                                "这是反算候选，保留原始标示，不作为修订值，也不作为独立结论升级；不据此改写印刷值。"
                                "证据清单：" + evidence_lib.state_text(check) + "。",
                                list(dict.fromkeys(nrv["refs"] + energy["refs"])), version, rule="N02")
                item["inferred"] = {"field": "fat", "value": f"{inferred:.1f}", "unit": "g",
                                    "note": "不据此改写印刷值"}
                item["evidenceChecklist"] = check
                item["upgradeAllowed"] = False
                break

    def supported_law(self, scope):
        if scope == "imported":
            refs = [block.id for block in self.blocks if re.search(r"原产国|原产地|进口商|境内代理|经销商", block.text)]
            self.add("IMPORT", "review", "进口食品中文标示核对", "核对原产地和境内代理信息",
                     "请核对中文标签、原产地及境内代理的名称、地址与联系方式。请结合完整包装和进口资料确认。",
                     refs, "all")
        if scope == "workshop":
            refs = [block.id for block in self.blocks if re.search(r"小作坊|河北", block.text)]
            self.add("HEBEI", "review", "小作坊属地标示核对", "确认生产地、日期与登记材料",
                     "指定库包含河北小作坊条例。请先确认产品归属该地区、时间及食品类别，再核对登记和标示。",
                     refs, "all")
        claims = [block.id for block in self.blocks if re.search(r"预防.{0,5}疾病|治疗.{0,8}(?:疾病|糖尿病|高血压)|治愈", block.text)]
        if claims and scope in {"regular", "imported", "workshop"}:
            self.add("LAW71", "review", "疾病预防或治疗用语线索", "核对包装原句和语境",
                     "识别到可能涉及疾病预防或治疗的表述。请核对整句，排除否定、提示语或 OCR 错误。",
                     claims, "all")

    def _allergen_lexicon(self):
        lex = registry_mod.default_registry().lexicons("GB7718AL")
        custom = lex.get("allergens") if isinstance(lex, dict) else None
        return custom or ALLERGENS

    def allergens(self):
        ingredients = self.field("ingredients")
        evidence = self.evidence(keys=("ingredients",))
        checks = self.context.get("allergenChecks")
        # 空对象等同「没有提供」：这时应继续走自动加粗检测（B1B / 字重测量）。
        # 此前只要带上这个键就进人工分支，导致网页默认 context 里
        # allergenChecks: {} 会把每个致敏类别都列为待核实（米线多出 3 条）。
        has_manual = isinstance(checks, dict) and bool(checks)
        if has_manual and not self.context.get("allergenBasis") and evidence:
            self.allergen_categories(checks, evidence)
            return
        advisory = [block.id for block in self.blocks if re.search(
            r"致[（(]?过[）)]?敏|致敏|过敏|^(?:本产品)?(?:可能)?含有(?:麸质|花生|大豆|小麦|乳|蛋|坚果|鱼)", block.text)]
        clues = list(dict.fromkeys(
            clue for _, pattern in self._allergen_lexicon().values() for clue in re.findall(pattern, ingredients)))
        optional = list(dict.fromkeys(re.findall(r"芝麻|椰子", ingredients)))
        facts = []
        if clues:
            facts.append("当前配料文字涉及「" + "、".join(clues) + "」。原料名称线索还需区分直接投料、复合配料及深加工来源。")
        if optional:
            facts.append("另读到「" + "、".join(optional) + "」。该等配料不属于第 4.12.1 条规定的八类，可自愿提示。")
        facts.append("当前图像文字" + ("读到疑似致敏提示。仍须核实其位置、可辨识性和覆盖类别。"
                                    if advisory else "未定位到明确的邻近提示。仍须回看加粗、下划线及没有引导词的提示。"))
        requirement = "GB 7718-2025 第 4.12.1 条要求直接作为配料使用的八类致敏物质，在配料表内以易辨识方式标示，或在配料表邻近位置提示。"
        exceptions = [
            "配料表内可采用加粗或下划线。配料表邻近可有或没有引导词。交叉接触或间接带入适用第 4.12.2 条和 D.3 的预防性提示。",
            "核对 D.4 深加工例外，包括精炼大豆油、精炼花生油、淀粉、鱼明胶及乳糖醇等。",
            "核对 D.5 的单一配料且品名已明示、食用酒精和蒸馏酒等例外。小面积包装还须查第 10.3 条许可的销售容器、随附说明书或数字标签。",
        ]
        basis = string(self.context.get("allergenBasis"))[:1000]
        state = self.context.get("allergenReview") if self.confirmed and evidence and basis else None
        selected = self.context.get("allergenMissing")
        names = [name for key, (name, _) in self._allergen_lexicon().items() if isinstance(selected, list) and key in selected]
        missing = []
        replacement = None
        if state == "missing" and names:
            group, title = "issue", "直接配料中的致敏物质缺少适用提示"
            facts.append(f"人工核验记录「{basis}」。")
            reasoning = "人工核验确认" + "、".join(names) + "属于应提示的直接配料，未命中豁免，完整销售包装及适用的补充载体没有覆盖所列类别的合规提示。"
            remediation = ["在配料表中突出相应配料名称，或在邻近位置补充清晰的致敏提示。若已有提示，将缺失类别并入原提示并复审全部类别。"]
            if not advisory:
                replacement = "致敏物质提示：含有" + "、".join(names) + "。"
                remediation.append("以生产方核实的实际致敏配料名称复核提示，并确认覆盖所有应标类别。")
        elif state in ("covered", "exempt"):
            group, title = "pass", "致敏物质标示已人工核验" if state == "covered" else "致敏物质标示适用例外已人工核验"
            facts.append(f"人工核验记录「{basis}」。")
            reasoning = ("已核实所有适用类别均有合规提示。" if state == "covered" else
                         "核验材料确认没有适用的直接配料，或相关配料和产品符合例外。")
            remediation = ["留存配方、规格及版面核验记录。"]
        else:
            group, title = "review", "致敏物质标示待核对"
            reasoning = "OCR 可以提供文字线索，仍缺少完整配方或整版呈现的核验记录，无法确定直接配料、提示覆盖及适用例外。"
            missing = ["生产方完整配方与复合配料规格", "整版原稿或所有相关展示面，包括适用的外销售容器和随附信息",
                       "核验配料表内易辨识标示、邻近提示及可能适用的豁免"]
            remediation = ["确认直接配料且适用义务后，对照每类已核实的致敏物质补齐配料表内或邻近提示。"]
        item = self.add("GB7718AL", group, title, " ".join(remediation), " ".join([*facts, reasoning]),
                        advisory + evidence, "2025")
        item["category"] = "致敏物质"
        if self.explain(item, facts, requirement, reasoning, exceptions, missing, remediation) and replacement:
            item["revisionOptions"] = ["若整版无其他须并入的提示，配料表邻近文案可写为「" + replacement + "」"]

    def allergen_categories(self, checks, evidence):
        sources = [self.blocks_by_id[ref] for ref in evidence]
        marked = {"covered": "已确认该类别有易辨识标示", "bold": "已确认该类别在配料表内以加粗标示",
                  "underline": "已确认该类别在配料表内以下划线标示", "nearby": "已确认该类别在配料表邻近位置有提示"}
        for key, (name, pattern) in self._allergen_lexicon().items():
            matches = [(block, re.findall(pattern, block.text)) for block in sources]
            refs = [block.id for block, terms in matches if terms]
            terms = list(dict.fromkeys(term for _, found in matches for term in found))
            if not terms:
                if key == "gluten":
                    composite = [block for block in sources if re.search(r"郫县豆瓣|黄豆酱|酿造酱油|复合调味料", block.text)]
                    if composite:
                        item = self.add("GB7718AL-gluten", "review", "复合配料中的含麸质谷物来源待核实",
                                        "生产方提供相关豆瓣、酱油及复合调味料的规格和成分清单",
                                        "配料表有复合配料线索，未从中确认小麦等含麸质谷物。请按实际组成及适用例外核对是否需要提示。",
                                        [block.id for block in composite], "2025", "GB7718AL")
                        item["category"] = "致敏物质"
                continue
            observed = "配料表可定位「" + "、".join(terms) + "」。"
            state = checks.get(key) if self.layout_basis else None
            if state == "missing":
                group, title = "issue", f"{name}缺少适用的致敏提示"
                detail = observed + "整版核验确认属于适用的直接配料、无相关例外，且未见配料内醒目标示或邻近提示。"
                action = f"在配料表中加粗或下划线标示对应{name}配料，或在配料表邻近位置提示；复审时核对全部应标类别。"
            elif state in (*marked, "exempt"):
                group, title = "pass", f"{name}标示方式已核验" if state in marked else f"{name}适用例外已核验"
                detail = observed + ("整版核验" + marked[state] + "。" if state in marked else "本次核验确认该项符合适用例外。")
                action = "留存原稿及核验记录；改版时复核对应标示。"
            else:
                group, title = "review", f"{name}致敏标示需核实"
                detail = observed + "该类别的直接配料来源、适用例外或完整版面上的标示尚未确认。"
                action = "生产方按本类别提供相关配方或规格，并对照整版复核配料内突出标示和邻近提示。"
            item = self.add("GB7718AL-" + key, group, title, action, detail, refs, "2025", "GB7718AL")
            item["category"] = "致敏物质"
            if state in (*marked, "missing", "exempt"):
                item["reviewBasis"] = self.layout_basis
            if state == "missing" and item["outcome"] == "issue":
                item["revisionOptions"] = [f"如采用邻近提示，核实完整应标清单后将「{name}」写入该清单。"]
        if not any(item["id"].startswith("GB7718AL-") for item in self.items):
            item = self.add("GB7718AL", "review", "致敏物质配方覆盖待确认",
                            "生产方核对完整配方和复合配料规格后，补充实际涉及的类别",
                            "当前可读配料未定位本工具支持的致敏名称线索。没有读到名称无法证明配方无适用的致敏物质。", evidence, "2025")
            item["category"] = "致敏物质"

    def attribute_near(self, anchors):
        """名称所在展示版面的邻近位置是否另有真实属性名称（产品类型／食品类别）。"""
        for candidate in self.blocks:
            if not TYPE_FIELD.search(candidate.text):
                continue
            for anchor in anchors:
                if self.page_index.get(candidate.id) != self.page_index.get(anchor.id):
                    continue
                if abs(candidate.y - anchor.y) < .06:
                    return True
        return False

    def product_name(self):
        """A3：名称所述配料要能在配料表找到；含年份等新创名称须邻近标示真实属性。"""
        name = self.field("name")
        if not name:
            return
        name_refs = self.evidence(keys=("name",))
        name_blocks = [self.blocks_by_id[ref] for ref in name_refs if ref in self.blocks_by_id]
        years = [block for block in name_blocks if NEW_NAME_YEAR.search(block.text)] if name_blocks else []
        if NEW_NAME_YEAR.search(name) or years or NEW_NAME_YEAR.search(" ".join(block.text for block in name_blocks)):
            if not self.attribute_near(name_blocks):
                quoted = "、".join(dict.fromkeys(block.text.strip() for block in (years or name_blocks))) or f"「{name}」"
                detail = (f"食品名称读到「{name}」；同一展示版面读到含年份的名称式表述「{quoted}」。"
                          "该名称式表述的邻近位置未见产品类型、产品类别或食品类别等真实属性名称。"
                          "依据 GB 7718-2025 第 4.2.2 条：新创名称、奇特名称等可在属性名称同一展示版面同时标示，"
                          "当其中含有易使消费者误解、混淆食品属性的文字或词语时，应在属性名称同一展示版面的临近位置标示。")
                item = self.add("A3", "review", "新创名称需邻近标示真实属性名称",
                                "核对该展示版面是否已标示产品类型或食品类别，未标示时在名称临近位置补标真实属性名称",
                                detail, name_refs, "2025", "A3")
                item["basis"] = "GB 7718-2025 第 4.2.2 条"
                return
        hits = name_ingredient_hits(name, ingredient_words(self.field("ingredients")))
        if not hits:
            return
        quoted = []
        for fragment, word in hits[:3]:
            quoted.append(f"「{fragment}」" if fragment == word else f"「{fragment}」（配料表写作「{word}」）")
        detail = (f"食品名称「{name}」中的" + "、".join(quoted) +
                  "在配料表中可定位对应配料。依据 GB 7718-2025 第 4.2.1 条："
                  "应在食品标签的醒目位置标示反映食品真实属性的名称，名称与配料表应相互对应。")
        item = self.add("A3", "pass", "名称所述配料与配料表一致", "复核名称与配料表的对应关系",
                        detail, name_refs + self.evidence(keys=("ingredients",)), "2025", "A3")
        item["basis"] = "GB 7718-2025 第 4.2.1 条"

    def irradiation(self):
        """R15：经辐照加工的食品或配料，须在名称临近或配料表内／临近位置标示（第 4.15 条）。"""
        matches = [block for block in self.blocks
                   if IRRADIATION.search(block.text) and not IRRADIATION_DENIAL.search(block.text)]
        if not matches:
            return
        block = matches[0]
        name_blocks = [self.blocks_by_id[ref] for ref in self.evidence(keys=("name",)) if ref in self.blocks_by_id]
        ingredient_refs = self.evidence(keys=("ingredients",))
        ingredient_blocks = [self.blocks_by_id[ref] for ref in ingredient_refs if ref in self.blocks_by_id]
        ingredient_text = self.field("ingredients")
        near_name = block.id in self.evidence(keys=("name",)) or any(abs(block.y - other.y) < .02 for other in name_blocks)
        inside = (block.id in ingredient_refs or bool(ingredient_text and block.text and block.text in ingredient_text) or
                  any(other.x <= block.x + .001 and other.right >= block.right - .001 and
                      other.y <= block.y + .001 and other.y + other.height >= block.y + block.height - .001
                      for other in ingredient_blocks))
        below = min((block.y - (other.y + other.height) for other in ingredient_blocks
                     if block.y >= other.y + other.height - .001), default=None)
        under = below is not None and below < .06
        clause = ("GB 7718-2025 第 4.15.2 条" if re.search(r"配料|调味料|原料", block.text)
                  else "GB 7718-2025 第 4.15.1 条")
        requirement = ("经电离辐射线或电离能量处理过的食品，应在食品名称临近位置标示「辐照加工食品」或「经辐照处理」；"
                       "处理过的配料应在配料表中括号内标示「辐照」「辐照加工」，或在配料表临近位置标明「经辐照处理」。")
        if near_name or inside or under:
            position = ("位于食品名称临近位置" if near_name else "位于配料表内部" if inside
                        else f"位于配料表下方相邻行（与配料表末行纵向相距约 {below:.2f} 版心高度）")
            detail = (f"读到「{block.text}」。该表述{position}，属于配料表临近或名称临近位置。"
                      f"依据 {clause}：{requirement}")
            refs = list(dict.fromkeys([block.id, *[other.id for other in name_blocks], *ingredient_refs]))
            item = self.add("R15", "pass", "辐照信息标示位置符合要求", "复核原稿字重要求和标签版面",
                            detail, refs, "2025", "R15")
            item["basis"] = clause
            return
        gaps = []
        if name_blocks:
            gaps.append(f"与食品名称纵向相距约 {min(abs(block.y - other.y) for other in name_blocks):.2f} 版心高度")
        if below is not None:
            gaps.append(f"与配料表末行纵向相距约 {below:.2f} 版心高度")
        detail = (f"读到「{block.text}」。该表述" + ("、".join(gaps) or "不在配料表内") +
                  "，既不在食品名称临近位置，也不在配料表内部或其下方相邻行。"
                  f"依据 {clause}：{requirement}请核对实际工艺并按对应位置标示。")
        item = self.add("R15", "issue", "辐照标示位置不符合要求", "把辐照信息移到食品名称临近位置或配料表内／临近位置",
                        detail, [block.id, *ingredient_refs], "2025", "R15")
        item["basis"] = clause

    def warning_statements(self):
        """R14：4.14 警示语为转致性条款，只对已登记依据下结论。

        判定边界（2026-11-14 第二轮修正，依据独立审查 F1–F4、F8）：
          · **触发只看本产品证据**：食品名称字段与「产品类型／产品类别／食品类别」文字块。
            配料表里出现「植物固体饮料」只是原料，不能据此认定本产品是固体饮料。
          · **否认语只在属性证据内生效**；属性名与否认语冲突记「适用性争议」转人工，不跳过检查。
          · **确定缺失要本项专属证据门槛**：属性已定位 + 标签已核验 + 覆盖完整 + 未读到残缺提示语，
            满足才写「确定缺陷」；否则只写「当前未定位到完整文字，尚不能判定未标」。
          · **结构化来源绑定固体饮料公告第三条**，GB 7718-2025 4.14 只作转致入口，不当缺陷的直接依据。
          · 「三新食品」只作线索检索，约束性质须回溯对应公告，不把整表断定为推荐性。
        """
        book = warning_book()
        book_digest = warning_book_digest()
        if not book:
            item = self.add("R14", "gap", "警示语检查缺少依据表，本条未检查",
                            "恢复 knowledge/warnings.json 后重审",
                            "本次运行读不到警示语依据表，4.14 按未检查／未覆盖处理，不得视为通过。",
                            [], None)
            if item:
                item["uncoveredReason"] = "warning-book-missing"
                item["warningState"] = "uncovered"
            return

        page_texts, page_refs = [], []
        for page in self.pages:
            text, refs = "", []
            for block in page:
                chunk = warning_normalize(block.text)
                if not chunk:
                    continue
                text += chunk
                refs.append(block.id)
            page_texts.append(text)
            page_refs.append(refs)

        # 本产品的属性证据：名称字段 + 产品类型类文字块。配料、广告、其他产品对比不算属性。
        attribute_parts, attribute_refs = [], []
        name_value = self.field("name")
        if name_value:
            attribute_parts.append(name_value)
            attribute_refs.extend(self.evidence(keys=("name",)))
        for block in self.blocks:
            if re.match(r"^\s*(?:产品类型|产品类别|食品类别)", warning_normalize(block.text)):
                attribute_parts.append(block.text)
                attribute_refs.append(block.id)
        attribute_text = "".join(warning_normalize(part) for part in attribute_parts)
        attribute_refs = list(dict.fromkeys(attribute_refs))

        solid = book.get("solidDrink") if isinstance(book.get("solidDrink"), dict) else {}
        attribute = str(solid.get("attributeName") or "")
        required_text = warning_normalize(solid.get("requiredText") or "")
        if attribute and attribute in attribute_text:
            covered = [kind for kind in (solid.get("coveredKinds") or [])
                       if isinstance(kind, str) and attribute in kind]
            matched = [kind for kind in covered if warning_normalize(kind) in attribute_text]
            ingredients_value = self.field("ingredients")
            ingredients_text = warning_normalize(ingredients_value) if ingredients_value else ""
            # 菌种类别要看配料实体，不能凭全文出现「菌种／菌株」就认定。
            species = bool(ingredients_text and SOLID_DRINK_SPECIES.search(ingredients_text))
            denial = SOLID_DRINK_DENIAL.search(attribute_text)

            located_hit, located_refs = False, []
            if required_text:
                for index, text in enumerate(page_texts):
                    if required_text not in text:
                        continue
                    located_hit = True
                    located_refs = [block.id for block in self.pages[index]
                                    if warning_normalize(block.text)
                                    and warning_normalize(block.text) in required_text] or page_refs[index]
                    break
            # 读到「不能代替」但读不到完整句子：只能补图，不能判缺失。
            partial_refs = [block.id for block in self.blocks
                            if "不能代替" in warning_normalize(block.text)
                            and required_text not in warning_normalize(block.text)]
            gate = {
                "productAttributeLocated": bool(attribute_text),
                "declaredSolidDrink": bool(matched or species),
                "labelVerified": bool(self.verified),
                "coverageComplete": bool(self.complete),
                "partialWarningText": bool(partial_refs),
            }
            sufficient = bool(gate["productAttributeLocated"] and gate["declaredSolidDrink"]
                              and gate["labelVerified"] and gate["coverageComplete"]
                              and not gate["partialWarningText"])
            gate["sufficient"] = sufficient
            missing_gate = [name for name in ("productAttributeLocated", "labelVerified", "coverageComplete")
                            if not gate[name]]

            basis = solid.get("basis") if isinstance(solid.get("basis"), dict) else {}
            basis_tail = ("依据 " + str(basis.get("document") or "已登记依据")
                          + (f" {basis.get('clause')}" if basis.get("clause") else "") + "。")
            layout = solid.get("layout") if isinstance(solid.get("layout"), dict) else {}
            layout_note = "；".join(str(layout.get(key)) for key in ("areaRatio", "font", "contrast")
                                    if layout.get(key)) or "公告另有版式规定"
            kind_label = "、".join(matched) or ("添加可食用菌种的固体饮料" if species else "")
            item = None
            if located_hit:
                conflict = "；但属性名同时出现否认表述，命名本身需人工确认" if denial else ""
                item = self.add("R14", "review", "固体饮料警示信息已读到，含义与版式未校验",
                                self.capture_action("警示信息所在展示面完整图"),
                                f"读到「{solid.get('requiredText')}」。{basis_tail}"
                                f"版式要求：{layout_note}。"
                                "文字已读到，只说明未漏标该句；语义是否覆盖本产品、位置是否在同一展示版面、"
                                f"面积比例／字体／色差是否合规，均需矢量稿或实测核对，不能判定合规{conflict}。",
                                located_refs, None)
                state = "located"
            elif denial:
                item = self.add("R14", "review", "固体饮料属性名与否认语冲突，适用性需人工确认",
                                "确认该产品真实属性：属性名为固体饮料还是非固体饮料，并保留判断依据",
                                f"属性证据里同时出现「{attribute}」与否认表述「{denial.group()}」。"
                                "两者冲突时不能跳过检查，也不能直接判缺失；请人工确认产品真实属性后重审。",
                                attribute_refs, None)
                state = "disputed"
            elif matched or species:
                shared = (f"本产品属性证据为「{'、'.join(attribute_parts)[:60] or attribute}」"
                          f"（命中：{kind_label}）。公告第三条要求标示「{solid.get('requiredText')}」，"
                          f"本次没有读到该完整文字。{basis_tail}")
                if sufficient:
                    item = self.add("R14", "issue", "固体饮料警示信息未标",
                                    "按公告在同一展示版面补标完整警示信息，并满足面积、字体、色差要求",
                                    shared + "本项已满足证据门槛（属性已定位、标签已核验、覆盖完整、未见残缺提示语），"
                                    "缺少完整警示信息属于确定缺陷。",
                                    list(dict.fromkeys([*attribute_refs, *page_refs[0]])), None)
                    state = "missing"
                else:
                    lacking = "、".join(missing_gate) or "partialWarningText"
                    item = self.add("R14", "review", "固体饮料警示信息未定位到完整文字，尚不能判定未标",
                                    "补齐完整展示面照片后重审；若确已印出请提供清晰原图或矢量稿",
                                    shared + f"目前证据不足以判定「未标」：缺少 {lacking}"
                                    + ("；另读到残缺提示语，疑似断行或遮挡" if partial_refs else "")
                                    + "。请先补证，不能据此认定缺陷，也不能视为通过。",
                                    list(dict.fromkeys([*attribute_refs, *partial_refs, *page_refs[0]])), None)
                    state = "missing"
            else:
                item = self.add("R14", "review", "固体饮料警示信息适用性需人工确认",
                                "确认该产品是否属于公告第三条列举的类别，再决定是否补标警示信息",
                                f"属性证据出现「{attribute}」，但没有读到公告列举的具体类别。"
                                f"公告只对{'、'.join(covered) or '特定类别'}要求该警示信息；"
                                "类别不明时不得判为缺失，也不得判为通过。",
                                attribute_refs, None)
                state = "human"
            if item:
                item["warningState"] = state
                item["basis"] = basis.get("document") or ""
                item["basisClause"] = basis.get("clause") or ""
                item["gatewayClause"] = "GB 7718-2025 4.14（转致）"
                item["evidenceGate"] = gate
                item["basisDigest"] = book_digest
                source = warning_basis_source(basis)
                if source:
                    item["source"] = source

        three_new = book.get("threeNew") if isinstance(book.get("threeNew"), dict) else {}
        entries = [row for row in (three_new.get("entries") or []) if isinstance(row, dict)]
        ingredients_value = self.field("ingredients")
        ingredients_text = warning_normalize(ingredients_value) if ingredients_value else ""
        hits = []
        if ingredients_text:
            for row in entries:
                name = warning_normalize(row.get("nameNormalized") or row.get("name") or "")
                if len(name) >= 2 and name in ingredients_text and row not in hits:
                    hits.append(row)
        if hits:
            shown = "；".join(
                f"{row.get('name')}（推荐食用量 {row.get('intake') or '公告未列'}，{row.get('suitable') or '公告未列'}）"
                for row in hits[:3])
            more = f" 等 {len(hits)} 种" if len(hits) > 3 else ""
            item = self.add("R14", "review", "配料含「三新食品」原料，适用范围与标示义务需人工核对",
                            "回溯该原料对应公告，核对适用范围、使用条件和需标示内容（含适宜人群、食用量）",
                            f"配料表中命中{shown}{more}。本表供线索检索；"
                            "具体适用范围、使用条件、需标示内容及约束性质须回溯对应公告，当前未完成核定。"
                            "因此本项只转人工核对，不自动判缺失，也不能视为通过。",
                            self.evidence(keys=("ingredients",)), None)
            if item:
                item["warningState"] = "human"
                item["basis"] = (three_new.get("basis") or {}).get("document") or ""
                item["basisDigest"] = book_digest
                source = warning_basis_source(three_new.get("basis"))
                if source:
                    item["source"] = source

        # 没有命中任何已登记依据时不往客户版加卡片：普通预包装食品本来就可能没有警示语义务，
        # 逐份报告固定加一条会污染 06 待核实计数。「本条只覆盖已登记依据、其余未覆盖」
        # 写在 R14 的 coverageScope 与检查点账本里，由 rule_registry 给出
        # 「未覆盖（未命中已登记依据）」，不在这里伪造一条待办。

    def grade(self):
        """R11：执行标准已规定质量等级的必须标示，未规定的不得标示（第 4.11 条）。"""
        standard = self.field("standard")
        code = standard_code(standard) if standard else ""
        if not code:
            return
        known = {standard_code(key): name for key, name in grade_standards().items()}
        marked = [block for block in self.blocks if GRADE_MARKS.search(block.text)]
        wording = "、".join(dict.fromkeys(match.group() for block in marked for match in GRADE_MARKS.finditer(block.text)))
        if code in known and not marked:
            detail = (f"标签产品标准代号为「{standard}」（{known[code]}），该标准已规定质量（品质）等级，"
                      "当前标签未见「等级／一级／特级」等质量等级字样。"
                      "依据 GB 7718-2025 第 4.11 条：食品所执行的标准已明确规定质量（品质）等级的，应标示质量（品质）等级，否则不得标示。")
            item = self.add("R11", "review", "该执行标准规定等级，标签未标等级", "按执行标准核对并补标质量等级，或确认该产品不适用分级要求",
                            detail, self.evidence(keys=("standard",)), "2025", "R11")
            item["basis"] = "GB 7718-2025 第 4.11 条"
            return
        if code not in known and marked:
            detail = (f"标签产品标准代号为「{standard}」，本工具清单未记录该标准规定质量等级；"
                      f"标签同时出现「{wording}」等质量等级字样。"
                      "依据 GB 7718-2025 第 4.11 条：食品所执行的标准未明确规定质量（品质）等级的，不得标示质量（品质）等级。")
            item = self.add("R11", "review", "执行标准是否分级需核对", "索取执行标准文本核对分级规定；标准不分级时删除或更正等级字样",
                            detail, list(dict.fromkeys([*self.evidence(keys=("standard",)), *[block.id for block in marked]])), "2025", "R11")
            item["basis"] = "GB 7718-2025 第 4.11 条"

    def storage_check(self):
        """A4：贮存条件应给出可执行的描述（第 4.8 条、附录 A.3）。"""
        value = self.field("storage")
        refs = self.evidence(keys=("storage",))
        requirement = ("依据 GB 7718-2025 第 4.8 条：预包装食品标签上应标示贮存条件，标示形式见附录 A.3，"
                       "可使用「常温保存」「××－××℃保存」「请置于阴凉干燥处」等写法。")
        if not value:
            detail = ("当前资料未读到或未确认贮存条件的具体描述。" if refs else "当前资料未定位到贮存条件字段。") + requirement
            item = self.add("A4", "review", "贮存条件未给出有效描述", self.capture_action("贮存条件区域"),
                            detail, refs, "2025", "A4")
            item["basis"] = "GB 7718-2025 第 4.8 条"
            return
        placeholder = STORAGE_PLACEHOLDER.search(value)
        if placeholder:
            detail = (f"贮存条件读为「{value}」，其中的「{placeholder.group()}」只是指向另一处信息，未给出可执行的贮存条件。" + requirement)
            item = self.add("A4", "review", "贮存条件未给出有效描述", "把贮存条件写成本身可执行的描述，不要只用位置指引",
                            detail, refs, "2025", "A4")
            item["basis"] = "GB 7718-2025 第 4.8 条"
            return
        conditions = [match.group() for match in STORAGE_DETAIL.finditer(value)]
        if len(value) >= 6 and conditions:
            detail = (f"贮存条件「{value}」已给出" + "、".join(dict.fromkeys(conditions[:3])) +
                      "等具体条件。" + requirement)
            item = self.add("A4", "pass", "贮存条件已给出有效描述", "复核原稿文字与适用贮存条件",
                            detail, refs, "2025", "A4")
            item["basis"] = "GB 7718-2025 第 4.8 条"

    def date_format(self):
        """C2：日期须按年、月、日顺序标示（第 4.7.1 条、附录 A.2）。"""
        requirement = ("依据 GB 7718-2025 第 4.7.1 条：应按照年、月、日的顺序清晰标示生产日期和保质期到期日，"
                       "标示形式见附录 A.2（如 20100320、2010/03/20、2010-03-20）。")
        for key, label in (("production", "生产日期"), ("expiry", "保质期到期日")):
            value = self.field(key)
            blocks = [self.blocks_by_id[ref] for ref in self.evidence(keys=(key,)) if ref in self.blocks_by_id]
            texts = list(dict.fromkeys(text for text in [value, *[block.text for block in blocks]] if text))
            if not texts:
                continue
            refs = [block.id for block in blocks]
            day_first = next((text for text in texts if DATE_DAY_FIRST.search(text)), "")
            leading = next((text for text in texts if DATE_DAY_LEADING.search(text)), "")
            if day_first or leading:
                observed = day_first or leading
                detail = f"{label}读为「{observed}」，日期以日或月开头、年份不在首位。" + requirement
                item = self.add("C2", "review", "日期未按年月日顺序标示", "按年、月、日顺序重排日期后再核对喷码",
                                detail, refs, "2025", "C2")
                item["basis"] = "GB 7718-2025 第 4.7.1 条"
                return
            readings = [reading for reading in (date_reading(text) for text in texts) if reading]
            if any(reading["valid"] for reading in readings):
                continue
            if readings:
                observed = readings[0]["text"]
                detail = f"{label}读为「{observed}」，该日期在日历上不存在或无法按年月日解析。" + requirement
                item = self.add("C2", "review", "日期未按年月日顺序标示", "核对原稿日期喷码并按年、月、日顺序重排",
                                detail, refs, "2025", "C2")
                item["basis"] = "GB 7718-2025 第 4.7.1 条"
                return
            incomplete = re.search(r"(?<!\d)(?:19|20)\d{2}\s*[-/.年]", value) if value else None
            if incomplete:
                detail = f"{label}读为「{value}」，只有年份部分可辨识，读不到完整的年月日三元组。" + requirement
                item = self.add("C2", "review", "日期未按年月日顺序标示", self.capture_action("完整日期"),
                                detail, refs, "2025", "C2")
                item["basis"] = "GB 7718-2025 第 4.7.1 条"
                return

    def producer_completeness(self):
        """B1A：生产者（经营者）名称、地址、联系方式三项齐全（第 4.6 条）。"""
        keys = ("producer", "address", "contact")
        labels = {"producer": "生产者名称", "address": "地址", "contact": "联系方式"}
        evidence = self.evidence(keys=keys)
        missing = [key for key in keys if not self.field(key)]
        requirement = ("依据 GB 7718-2025 第 4.6 条：应当按照法律、法规和国务院食品安全监督管理部门的要求"
                       "标示生产者、经营者的名称、地址和联系方式。")
        if not missing:
            detail = "已定位生产者名称、地址和联系方式。" + requirement
            item = self.add("B1A", "pass", "生产者名称、地址、联系方式齐全", "复核实物与厂家资料",
                            detail, evidence, "2025", "B1A")
            item["basis"] = "GB 7718-2025 第 4.6 条"
            return
        # G01 只回答「一般项目能不能在标签上看到」，缺项的完整性口径在这里单列一次，
        # 说明缺的是生产者信息的哪一项，两处措辞不重复。
        names = "、".join(labels[key] for key in missing)
        detail = (f"生产者信息不完整，缺{names}。G01 只核对一般标示项目是否可见，"
                  "此处按生产者信息完整性单独列示。" + requirement)
        item = self.add("B1A", "review", f"生产者信息不完整，缺{names}", "补齐生产者名称、地址和联系方式",
                        detail, evidence, "2025", "B1A")
        item["basis"] = "GB 7718-2025 第 4.6 条"

    def allergen_advisory(self, pattern):
        """配料表邻近的致敏提示行；「可能含有」属预防性提示，不能当作直接配料的标示。"""
        return [block for block in self.blocks
                if ALLERGEN_ADVISORY.search(block.text) and not ALLERGEN_PRECAUTION.search(block.text)
                and re.search(pattern, block.text)]

    def allergen_coverage(self):
        """B1B：按八类逐个比对配料表，输出一条汇总的逐类结论（第 4.12.1 条、附录 D.2）。"""
        ingredients = self.field("ingredients")
        if not ingredients:
            return
        # 人工核验给出结论时以人为准：B1B 不自动判，改由 GB7718AL 承载该结论，
        # 避免同一份报告里出现「已覆盖」与「未提示」两个相反结论。
        review = string(self.context.get("allergenReview")).strip().lower()
        if self.confirmed and self.context.get("allergenBasis") and review:
            return
        checks = self.context.get("allergenChecks")
        marks = {}
        for key, value in (checks.items() if isinstance(checks, dict) else []):
            alias = ALLERGEN_ALIASES.get(str(key).strip().lower())
            if alias and isinstance(value, str):
                marks[alias] = value.strip().lower()
        # 客户已拍板口径：配料表内复合配料名加粗即按附录 D.2.1 视为已提示。
        # V2.0 起加粗由 /api/ocr 的图像测量给出；人工勾选只在填写时覆盖自动结果。
        words = ingredient_words(ingredients)
        composite = [word for word in words if any(name in word for name in ALLERGEN_BOLD_NAMES)]
        rows, refs = [], list(self.evidence(keys=("ingredients",)))
        for key, (_, pattern) in ALLERGENS.items():
            matched = [word for word in words if re.search(pattern, word)]
            advisory = self.allergen_advisory(pattern)
            composite_gluten = key == "gluten" and not matched and bool(composite)
            if not matched and not advisory and not composite_gluten:
                continue
            refs.extend(block.id for block in advisory)
            mark = marks.get(key)
            shown = matched or (composite if composite_gluten else [])
            # 自动字重：直接命中词与复合配料名分别判断，任一确认为加粗即视为已提示。
            measured = [self.bold_word(word) for word in shown]
            auto_bold = True in measured
            auto_unknown = bool(measured) and not auto_bold and any(value is None for value in measured)
            if mark == "missing":
                state, note = "review", "人工核验记录该类属于应提示的直接配料，完整标示未见合规提示"
            elif mark == "exempt":
                state, note = "pass", "人工核验记录该类适用第 4.12.3 条或附录 D.4、D.5 的例外"
            elif mark in ("bold", "underline"):
                state, note = "pass", f"原稿已以{'加粗' if mark == 'bold' else '下划线'}方式在配料表内标示"
            elif mark in ("covered", "nearby"):
                state, note = "pass", "整版核验记录已确认该类有易辨识标示或配料表邻近提示"
            elif advisory:
                state, note = "pass", "配料表邻近的致敏提示行已覆盖该类（附录 D.2.2）"
            elif matched and all(word in ALLERGEN_EXEMPT.get(key, ()) for word in matched):
                state, note = "pass", "命中配料属于附录 D.4 明列的深加工配料，可免于标示致敏物质提示"
            elif auto_bold:
                state, note = "pass", ("程序在该类配料名上测到加粗笔画，按附录 D.2.1 视为已在配料表内提示"
                                       "（字重由图像测量，人工复核记录优先）")
            elif auto_unknown:
                state, note = "review", "该类的加粗无法从图中判断（字太小或区域不清），需矢量稿或打样件核对"
            else:
                state, note = "review", "配料表命中该类配料，实测未见加粗、下划线或配料表邻近提示，需按配方确认"
            rows.append({"key": key, "name": ALLERGEN_NAMES[key], "state": state,
                         "words": shown, "note": note})
        if not rows:
            return
        lines = []
        for row in rows:
            observed = "配料表读到「" + "、".join(row["words"]) + "」，" if row["words"] else ""
            lines.append(f"{row['name']}：{observed}{row['note']}，判为"
                         f"{'已覆盖（pass）' if row['state'] == 'pass' else '待核实（review）'}")
        missing = [ALLERGEN_NAMES[key] for key in ALLERGENS if key not in {row["key"] for row in rows}]
        detail = "按第 4.12.1 条八类逐个比对：" + "；".join(lines) + "。"
        if missing:
            detail += "其余类别（" + "、".join(missing) + "）未在配料表定位到对应原料词，不产生条目。"
        passed = [row for row in rows if row["state"] == "pass"]
        group = "pass" if len(passed) == len(rows) else "review"
        title = "致敏物质逐类核对已覆盖" if group == "pass" else "致敏物质逐类结论需核实"
        action = ("留存原稿、配方与复合配料规格；改版时复核逐类提示。" if group == "pass" else
                  "按本表逐类提供配方或复合配料规格，确认需要提示的类别后统一补齐提示并复审。")
        item = self.add("B1B", group, title, action, detail, list(dict.fromkeys(refs)), "2025", "B1B")
        item["basis"] = "GB 7718-2025 第 4.12.1 条；配料表内提示方式按附录 D.2.1、D.2.2 核对"
        item["category"] = "致敏物质"
        item["allergenClasses"] = [{"key": row["key"], "name": row["name"], "state": row["state"]} for row in rows]
        item["allergenBold"] = any(row["state"] == "pass" and ("加粗" in row["note"] or "下划线" in row["note"])
                                   for row in rows)
        if group == "pass":
            # B1B 已按八类逐类判定为 pass（字重测量成功且逐类结论完成）时，
            # 自动生成的 GB7718AL「致敏物质标示待核对」退出，避免同一事实两个结论。
            # 人工核验（allergenBasis）产出的 GB7718AL 结论保留，那是人的判断。
            self.items = [row for row in self.items
                          if not (row.get("id") == "GB7718AL" and row.get("outcome") == "review"
                                  and row.get("title") == "致敏物质标示待核对")]

    def material_checks(self):
        """List documentary checks that cannot be settled from label text alone."""
        ingredients = self.field("ingredients")
        refs = self.evidence(keys=("ingredients",))
        standard = self.field("standard")
        if re.match(r"Q\s*/", standard, re.I):
            self.add("M01", "review", "企业执行标准适用性需核实", "向生产方索取执行标准文本及适用版本",
                     f"标签列示「{standard}」。标准代号不能证明其产品范围和实际配方相符。", self.evidence(keys=("standard",)))
        additive_pattern = r"山梨酸钾|苯甲酸钠|三氯蔗糖|胭脂红|乳酸链球菌素|D-异抗坏血酸钠|冰醋酸|焦糖色"
        parts = re.split(r"(?:^|[。；;\n])\s*([\u4e00-\u9fff]{1,10}包[A-Z]?)\s*:", ingredients)
        packaged = [(parts[i], parts[i + 1]) for i in range(1, len(parts), 2)]
        if (not packaged or len({name for name, _ in packaged}) != len(packaged) or
                any(re.search(r"包[A-Z]?\s*:", text) for _, text in packaged)):
            packaged = [("", ingredients)]
        elif parts[0].strip():
            packaged.insert(0, ("", parts[0]))
        per_pack = []
        for name, contents in packaged:
            additives = list(dict.fromkeys(re.findall(additive_pattern, contents)))
            if additives:
                per_pack.append(f"{name or '配料表'}：{'、'.join(additives)}。")
        if per_pack and refs:
            item = self.add("M02", "review", "逐包核对食品添加剂使用条件",
                            "生产方按对应食品或料包一次提供配方、食品分类和投料量。按指定库 GB 2760 逐包核对允许使用条件、实际用量和可能的带入。",
                            " ".join(per_pack) + "仅凭名称和成品净含量不能确认实际投料量。", refs)
            item["analysis"] = {"facts": per_pack, "reasoning": "仅凭名称和成品净含量不能确认实际投料量。"}
        if "复合调味料" in ingredients and refs:
            self.add("M03", "review", "复合配料展开及例外需核实", "索取对应复合配料规格、配方比例和执行标准",
                     "配料表使用「复合调味料」描述部分原料。需核对组成、使用比例及适用例外，再判断是否需要展开标示。", refs)
        net_refs = self.evidence(keys=("net",))
        if self.field("net") and net_refs:
            self.add("M04", "review", "净含量字高需结合实际尺寸核实", "以有尺寸比例的矢量原稿或印刷样核对字符高度",
                     "已定位净含量标示。图像没有经确认的成品尺度，无法据像素判断字符高度。", net_refs)
        saturated_refs = self.evidence(nutrients=("saturated",))
        if self.nvalue("saturated") and saturated_refs:
            self.add("M05", "review", "饱和脂肪检测口径需核实", "索取检测报告与换算底稿，核对检测指标、基准与列示数值",
                     "表内复算无法验证检测报告的项目名称和标示数据来源。", saturated_refs)
        # 各料包内部配料的递减顺序：标签只列名称、不列各料包内的单项用量，无法验证，列为待核。
        inner = [(name, [part for part in re.split(r"[、,，]", contents) if part.strip()])
                 for name, contents in packaged if name]
        inner = [(name, items) for name, items in inner if len(items) >= 3]
        if inner and refs:
            samples = "；".join(
                f"{name}依次列示「{'、'.join(items[:4])}」{'等' if len(items) > 4 else ''}"
                for name, items in inner[:2])
            item = self.add("M06", "review", "各料包内配料的递减顺序需与投料记录核对",
                            "索取各料包的实际投料记录，逐包核对包内配料是否按加入量递减排列。",
                            f"{samples}。标签只列出配料名称，没有列出各料包内单项配料的用量，"
                            "无法验证包内排列是否符合递减顺序要求。"
                            "加入量不超过 2% 的配料可以不按递减顺序排列，这一豁免也要结合投料记录判断。",
                            refs)
            line = clause_line("GB 7718-2025", "gb7718-2025", "4.3.2")
            if line:
                analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
                item["analysis"] = {**analysis, "requirement": [line]}

    def review_2025(self, scope, nutrition_safe):
        if scope == "imported":
            self.add("IMPORT2025", "review", "新版进口标签项目", "核对完整包装和进口资料",
                     "核对原产国或地区、境内进口商或代理商信息、境外生产企业注册编号及外文与中文标示对应关系。",
                     self.evidence(keys=("producer", "address", "contact", "license")), "2025")
        else:
            self.general("2025")
        self.product_name()
        self.irradiation()
        self.warning_statements()
        self.grade()
        self.storage_check()
        self.producer_completeness()
        self.dates()
        self.date_format()
        self.net("2025")
        if fact_qa.is_structural_failure(getattr(self, "fact_qa", None)):
            table = (self.fact_qa or {}).get("nutritionTable") or {}
            self.add("nutrition-extract-failure", "review", "营养表结构提取失败",
                     "核对原图行列对应后再审",
                     fact_qa.failure_message(self.fact_qa),
                     list(table.get("headerRefs") or []))
        elif nutrition_safe:
            self.nutrition()
            self.nutrition_crosscheck("2025")
        else:
            self.add("nutrition-conflict", "review", "营养表归属或列不明确", self.capture_action("完整营养表"),
                     "同一表格、计量基准和整组读数核对后再复算。")
        self.claims()
        self.allergens()
        self.allergen_coverage()

    def customer_label_facts(self, raw_pages):
        shown = self.blocks

        def unique_value(pattern):
            values = {found[1] for block in shown if (found := re.fullmatch(pattern, block.text))}
            return next(iter(values)) if len(values) == 1 else ""

        facts = {
            "type": unique_value(r"(?:产品类型|产品类别|食品类别)\s*:\s*(.{1,50})"),
            "flavor": unique_value(r"口味\s*:\s*(.{1,30})") or
                      unique_value(r"((?!风味$|口味$|.*美味$|.*调味$)[\u4e00-\u9fff]{1,10}味)"),
            "origin": unique_value(r"产地\s*:\s*([\u4e00-\u9fff ]{2,30})"),
            "country": unique_value(r"原产(?:国|地)(?:\(地区\))?\s*:\s*(.{1,50})"),
        }
        origins = {}
        for page in self.pages:
            visible = sorted(label_blocks(page), key=lambda block: block.y)
            for i, block in enumerate(visible):
                heading = re.match(r"^([\u4e00-\u9fff]{1,10}包)生产商\s*:", block.text)
                if not heading:
                    continue
                lines = [block.text]
                for next_block in visible[i + 1:i + 4]:
                    if next_block.y - block.y > .025 or re.match(r"^(?:被委托商|受委托方|[\u4e00-\u9fff]{1,10}包生产商)", next_block.text):
                        break
                    if abs(next_block.x - block.x) < .025:
                        lines.append(next_block.text)
                origin = re.search(r"产地\s*:\s*([\u4e00-\u9fff ]{2,30})$", " ".join(lines))
                if origin:
                    origins.setdefault(heading[1], set()).add(origin[1].strip())
        facts["packOrigins"] = [{"pack": pack, "value": next(iter(values))} for pack, values in origins.items() if len(values) == 1]
        contractors = [block for block in shown if re.match(r"^(?:被委托商|受委托方)\s*[A-Z]\s*:", block.text)]
        facts["otherPackOrigins"] = bool(contractors)
        facts["otherPackOriginsBelow"] = len(self.pages) == 1 and bool(contractors) and all(block.y > .65 for block in contractors)
        net = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(克|g)\s*", self.field("net"), re.I)
        if self.artwork and net:
            for page in raw_pages:
                if len(label_blocks(page)) == len(page):
                    continue
                amounts = {match[1] for block in page if block.y < .15 and
                           (match := re.fullmatch(r"[\u4e00-\u9fff]{2,24}(\d+(?:\.\d+)?)\s*(?:克|g)", block.text, re.I))}
                if len(amounts) == 1:
                    ticket = next(iter(amounts))
                    if number(ticket) != number(net[1]):
                        facts["jobTicketMass"] = ticket + " 克"
                    break
        return facts


def verified_label_date(report, key):
    if not report.confirmed or report.context.get("sampleKind") != "physical":
        return None
    candidates = {target["value"] for ref in report.evidence(keys=(key,))
                  for target in dated_targets("", report.blocks_by_id[ref]) if target["role"] == key}
    if len(candidates) == 1:
        return calendar_date(next(iter(candidates)))
    return None


def applicability(report, version, production, planned, artwork):
    kind = report.context.get("sampleKind")
    if kind not in ("artwork", "physical"):
        kind = "artwork" if artwork else "unknown"
    audit_date = calendar_date(report.context.get("auditDate")) or date.today()
    expiry = verified_label_date(report, "expiry")
    transition = None
    if version == "2025":
        sources = [source_for(key, "2025") for key in ("TRANS7718", "TRANS28050", "TRANS100")]
        if all(sources):
            transition = {"standardsPublished": "2025-03-16", "measurePublished": "2025-03-14",
                          "effective": EFFECTIVE_2025, "sources": sources}
        decision = f"本次按指定知识库中的 {VERSION_LABELS[version]} 审核。"
    else:
        decision = "请核定食品类别及对应的专项审核要求。"
    return {"auditDate": audit_date.isoformat(), "sampleKind": kind,
            "artworkDate": artwork["value"] if artwork else None,
            "productionDate": production.isoformat() if production else None,
            "plannedProductionDate": planned.isoformat() if planned else None,
            "expiryDate": expiry.isoformat() if expiry else None,
            "shelfLife": report.field("shelf"), "decision": decision, "transition": transition}


def reread_marks(payload):
    """收集 /api/ocr 写在 block 上的放大重读标记；只有真的发生过重读才会出现。"""
    marks = {}
    pages = payload.get("pages")
    if not isinstance(pages, list):
        return marks
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("blocks"), list):
            continue
        for block in page["blocks"]:
            if not isinstance(block, dict):
                continue
            identifier, mark = block.get("id"), block.get("reread")
            if not isinstance(identifier, str) or not isinstance(mark, dict):
                continue
            if mark.get("unresolved") is True:
                # 多次重读仍不一致：规则层要据此输出「图上看不清」，而不是替客户推定。
                marks[identifier] = {"unresolved": True, "reason": string(mark.get("reason"))}
                continue
            scale, original = mark.get("scale"), string(mark.get("from"))
            corrected = string(mark.get("to"))
            if type(scale) is not int or scale < 2 or not original or not corrected:
                continue
            entry = {"from": original, "to": corrected, "scale": scale}
            if mark.get("at"):
                entry["at"] = mark.get("at")
            marks[identifier] = entry
    return marks


def typography_marks(payload):
    """收集 /api/ocr 写在 block 上的字重测量结果。"""
    marks = {}
    pages = payload.get("pages")
    if not isinstance(pages, list):
        return marks
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("blocks"), list):
            continue
        for block in page["blocks"]:
            if not isinstance(block, dict):
                continue
            identifier, measured = block.get("id"), block.get("typography")
            if not isinstance(identifier, str) or not isinstance(measured, dict):
                continue
            words = measured.get("words")
            marks[identifier] = {
                "bold": measured.get("bold") if measured.get("bold") in (True, False) else None,
                "stroke_ratio": measured.get("stroke_ratio"),
                "baseline_ratio": measured.get("baseline_ratio"),
                "stroke": measured.get("stroke"),
                "height": measured.get("height"),
                "reason": string(measured.get("reason")),
                "words": {str(key): (value if value in (True, False) else None)
                          for key, value in (words.items() if isinstance(words, dict) else [])},
            }
    return marks


def audit(payload):
    draft = payload.get("draft", {})
    context = payload.get("context", {})
    if not isinstance(draft, dict) or not isinstance(context, dict):
        raise ValueError("审核上下文或字段格式无效。")
    versions = registry_mod.pin_versions(context, payload)
    context = dict(context, versions=versions)
    payload = dict(payload, context=context)
    bundle_token = registry_mod.use_bundle(versions.get("bundleId"))
    try:
        return _audit_body(payload, draft, context, versions)
    finally:
        registry_mod.reset_bundle(bundle_token)


def _audit_body(payload, draft, context, versions):
    fields = draft.get("fields", {})
    nutrients = draft.get("nutrition", {})
    if not isinstance(fields, dict) or not isinstance(nutrients, dict):
        raise ValueError("请核对字段和营养表格式。")
    # A6：verifiedFields 只能来自人工编辑；字段值变化即失效（服务端再校验一次）。
    verified, stale = [], list(context.get("staleVerifications") or [])
    nutrition_by_label = {str(label): key for key, (label, _unit, _ref) in NUTRIENTS.items()}
    for row in context.get("verifiedFields") or []:
        if not isinstance(row, dict) or not row.get("field"):
            continue
        key = str(row["field"])
        if key.startswith("营养表-"):
            verified.append(row)
            continue
        current = (fields.get(key) or {}).get("value")
        if current is None and key not in fields:
            # 营养表字段：按标签（脂肪/能量…）映射到 draft.nutrition，分别比 value/unit/nrv。
            label = next((name for name in nutrition_by_label if name in key), None)
            if label is None and key in ("计量基准", "basis"):
                label = "计量基准"
            if label == "计量基准":
                current = (fields.get("basis") or {}).get("value")
            elif label:
                nut = (draft.get("nutrition") or {}).get(nutrition_by_label[label]) or {}
                if "单位" in key:
                    current = nut.get("unit")
                elif "NRV" in key.upper():
                    current = nut.get("nrv")
                else:
                    current = nut.get("value")
            if label is None:
                stale.append(dict(row, staleReason="字段不存在"))
                continue
        if str(current or "") != str(row.get("value") or ""):
            stale.append(dict(row, staleReason="字段值或证据已变化"))
            continue
        verified.append(row)
    context["verifiedFields"] = verified
    context["staleVerifications"] = stale
    pages, coverage, attribution, anchors = inspect_pages(payload)
    conflicts = attribution["conflicts"]
    if attribution["status"] != "confirmed":
        items = [{"id": "mixed-images", "group": "blocked", "outcome": "blocked", "track": "actual",
                  "title": "图片标示冲突，暂停归并", "action": "移除错图或按销售包装拆单",
                  "detail": row["label"] + "。", "refs": row["refs"], "pages": row["images"]}
                 for row in conflicts]
        if not items:
            signals = attribution["signals"]
            items = [{"id": "attribution", "group": "review", "outcome": "review", "track": "actual",
                      "title": "图片归属待确认", "action": "对照逐图候选与原图",
                      "detail": "；".join(row["label"] for row in signals) or "现有标示不足以把所有图片关联到同一销售包装。",
                      "refs": list(dict.fromkeys(ref for row in signals for ref in row["refs"])),
                      "pages": attribution["pages"]}]
        early = {"version": None, "versionLabel": "图片归属尚未确认", "scopeLabel": "",
                "track": "actual", "blocked": bool(conflicts), "conflicts": conflicts,
                "canPublish": False, "attribution": attribution, "coverage": coverage,
                "anchors": [], "fields": [], "items": items,
                "factQa": draft.get("factQa"),
                "blocking": fact_qa.blocking_state(draft.get("factQa"), attribution,
                                                   context.get("staleVerifications")),
                "versions": versions}
        return early
    refs_in_case = {block.id for page in pages for block in page}
    if (entry(draft, "attribution").get("token") != attribution["token"] or
            any(ref not in refs_in_case for group in (fields, nutrients) for value in group.values()
                if isinstance(value, dict) and isinstance(value.get("refs"), list) for ref in value["refs"]
                if isinstance(ref, str))):
        draft = extract_blocks(pages)
        fields, nutrients = draft["fields"], draft["nutrition"]
        context = {**context, "layoutReviewed": False}
    if not isinstance(draft.get("factQa"), dict) or "nutritionTable" not in (draft.get("factQa") or {}):
        fact_qa.attach_nutrition_qa(draft, pages)
    bound_ok, bound_stale = fact_qa.evaluate_manual_corrections(
        context.get("verifiedFields") or [], draft, pages, fact_qa.pages_digest(pages))
    if bound_stale:
        context["verifiedFields"] = bound_ok
        context["staleVerifications"] = list(context.get("staleVerifications") or []) + bound_stale
    report = Report(context, fields, nutrients, pages, coverage,
                    reread_marks(payload), typography_marks(payload))
    report.fact_qa = draft.get("factQa")
    scope = string(context.get("scope", "unknown"))
    scope = scope if scope in SCOPE_LABELS else "unknown"
    hint = string(draft.get("scopeHint", "unknown"))
    scope_conflict = scope == "regular" and hint in {"imported", "workshop", "other"}
    raw_pages = read_pages(payload)
    artwork = artwork_date(raw_pages)
    report.artwork = context.get("sampleKind") == "artwork" or (context.get("sampleKind") != "physical" and bool(artwork))
    production = calendar_date(context.get("productionDate")) or verified_label_date(report, "production")
    sample_kind = context.get("sampleKind")
    planned = calendar_date(context.get("plannedProductionDate")) if sample_kind == "artwork" or (sample_kind != "physical" and artwork) else None
    version = "2025" if scope in {"regular", "imported"} and not scope_conflict else None
    if scope in {"unknown", "other", "workshop"} or scope_conflict:
        route = hint if scope_conflict else scope
        details = {
            "imported": "按进口食品路径核对原产国、境内进口商或代理商、注册编号、中文标签及专项要求。",
            "workshop": "按生产所在地的小作坊条例核对登记证、产品类别、销售区域及当地标示义务。",
            "other": "请确认食品类别、销售形态和对应专项标准，再进入适用的审核流程。",
            "unknown": "请先确认是否为国内生产、国内销售且直接提供给消费者的普通预包装食品。",
        }
        selected = SCOPE_LABELS[route]
        title = "所选范围与标签线索不一致" if scope_conflict else selected
        report.add("scope", "review", title, "先确认食品类别", details[route])
        if not scope_conflict:
            report.supported_law(scope)
        if scope_conflict:
            scope = "unknown"
    else:
        report.bone_claim()
        report.supported_law(scope)
        report.track = "standard2025"
        report.review_2025(scope, draft.get("nutritionConflict") is not True)
        report.track = "actual"
        report.material_checks()
    report.track = "internal"
    check_consistency(report)
    # 同一项目多处读数的交叉核对（X01／X02）只作对照提示，仍归在标签内对照。
    crosscheck.run(report)
    label_facts = report.customer_label_facts(raw_pages)
    exclusion_notes = exclusions.notes(report, {"labelFacts": label_facts})
    # 覆盖边界要写进客户报告，不能只藏在开发账本里（独立审查 F5/Q2）。
    # 用 exclusionNotes 承载，避免 reviewNotes 非空时把旧版 08 说明整段顶掉。
    if warning_book() is not None:
        exclusion_notes = [*exclusion_notes, {
            "key": "warning-scope",
            "title": "警示语检查范围",
            "text": ("GB 7718-2025 4.14 为转致性条款。本次只对已登记依据出结论："
                     "固体饮料公告（2021 年第 46 号）第三条，限于公告列举类别；"
                     "以及「三新食品」原料线索（只转人工核对）。"
                     "警示信息所占面积、字体、色差等版式要求，"
                     "以及其它法律、法规、食品安全标准和部门公告要求的警示语未覆盖，请另行人工核对。"),
            "clause": "GB 7718-2025 4.14",
            "refs": [],
        }]
    unresolved = [anchor for anchor in anchors if anchor["dateStatus"] != "read"]
    if unresolved:
        report.track = "actual"
        report.add("date-anchor", "review", "日期指引与喷码需要核对",
                   report.capture_action("日期证据"), "；".join(dict.fromkeys(anchor["status"] for anchor in unresolved)),
                   [ref for anchor in unresolved for ref in [anchor["sourceRef"], *[item["ref"] for item in anchor["targets"]]]])
    page_for_ref = {block.id: image_id for blocks, image_id in zip(pages, attribution["pages"]) for block in blocks}
    for item in report.items:
        item["refs"] = [ref for ref in item["refs"] if ref in page_for_ref]
        item["pages"] = list(dict.fromkeys(page_for_ref[ref] for ref in item["refs"]))
    result = {"version": version, "versionLabel": VERSION_LABELS[version],
              "scopeLabel": SCOPE_LABELS[scope], "track": "standard2025" if version else "actual",
              "versionTransition": {"published": "2025-03-16", "effective": EFFECTIVE_2025} if version else None,
              "artworkDate": artwork,
              "labelFacts": label_facts,
              "exclusionNotes": exclusion_notes,
              "applicability": applicability(report, version, production, planned, artwork),
              "nutritionRows": report.nutrition_rows(draft.get("nutritionConflict") is not True and
                                                     any(item["id"] in {"N02", "N03"} and item.get("source") for item in report.items)),
              "blocked": False, "conflicts": [], "canPublish": True, "attribution": attribution,
              "coverage": coverage, "anchors": anchors,
              "fields": report.field_statuses(), "items": report.items,
              "factQa": report.fact_qa, "nutritionExtractFailure": fact_qa.is_structural_failure(report.fact_qa),
              "blocking": fact_qa.blocking_state(report.fact_qa, attribution,
                                                 context.get("staleVerifications"))}
    # 08 节的检查点说明由专用库驱动；01–07 节仍由上面的条目渲染。
    result["reviewNotes"] = notes.build(report, draft, result)
    # A1：唯一分类数据（卡片、正文、章节标题、report_check 都读这一份）。
    try:
        import report_plan as _plan
        result["classification"] = _plan.classify(result["items"])
        # 结构化重读记录：original OCR、当前提取值、重读值、采用值互不覆盖。
        reverse = {label: key for key, label in NUTRIENT_LABELS.items()}
        structured = {}
        adopted_map = context.get("adoptedEvidence")
        if not isinstance(adopted_map, dict):
            adopted_map = context.get("adoptedReadings") if isinstance(context.get("adoptedReadings"), dict) else {}
        for row in result.get("nutritionRows") or []:
            if not isinstance(row, dict) or not row.get("reread"):
                continue
            mark = row["reread"]
            key = reverse.get(str(row.get("name")))
            if not key:
                continue
            box = mark.get("box") or []
            block_id = mark.get("blockId")
            unit = row.get("unit") or ""
            readings = []
            original = mark.get("from")
            if original not in (None, ""):
                readings.append({"value": str(original), "unit": unit, "source": "ocr-original",
                                 "blockId": block_id, "box": box, "at": None})
            current = row.get("value")
            if current not in (None, ""):
                readings.append({"value": str(current), "unit": unit, "source": "ocr",
                                 "blockId": block_id, "box": box, "at": None})
            reread_to = mark.get("to")
            if reread_to not in (None, ""):
                readings.append({"value": str(reread_to), "unit": unit, "source": "reread",
                                 "scale": mark.get("scale"), "blockId": block_id, "box": box,
                                 "at": mark.get("at")})
            adopted = adopted_map.get(key)
            if adopted is None:
                label = NUTRIENT_LABELS.get(key)
                adopted = adopted_map.get(label) if label else None
            numbers = {evidence_lib.normalize(item["value"], item.get("unit")) for item in readings}
            numbers.discard(None)
            # Observations that disagree stay visible. An adopted-value record
            # is stored separately; it must not rewrite or drop 125 vs 12.5.
            structured[key] = {"readings": readings, "agree": len(numbers) <= 1,
                               "adopted": adopted if adopted is not None else None}
        result["autoReread"] = structured
    except Exception:
        result["classification"] = None
    # V2.7 阶段 1：统一整改计划。02/03/06 的条目仍是 report.items，07 节改为读 plan 的 revision。
    try:
        import report_plan
        plan = report_plan.build(report, draft, context)
        result["reportPlan"] = {"schemaVersion": plan["schemaVersion"], "snapshotId": plan["snapshotId"],
                                "planVersion": plan.get("planVersion"),
                                "items": plan["items"], "document": plan["document"],
                                "summary": report_plan.summary(plan)}
        result["revision"] = plan["revision"]
    except Exception:
        # 计划构建失败不改变既有审核结果；07 节回退到旧渲染。
        result["reportPlan"] = None
        result["revision"] = None
    report.close_disabled_rules()
    result["items"] = report.items
    result["versions"] = versions
    # 纪律第 6 条：版本号不虚标。versions 里钉的是发布那一刻的代码哈希；
    # 代码改过但没重新发布时必须能看出来，不能拿旧哈希当成正在跑的代码。
    try:
        live = registry_mod.program_identity()
    except Exception:
        live = {}
    pinned = versions.get("programVersion") if isinstance(versions, dict) else None
    basis_digest = warning_book_digest()
    if live:
        result["runningProgram"] = {
            "programVersion": live.get("programVersion"),
            "auditSha256": live.get("auditSha256"),
            "pinnedProgramVersion": pinned,
            "matchesPinned": bool(pinned) and pinned == live.get("programVersion"),
            # 依据表不在发布包里，必须单独记摘要，否则这次审核无法冻结（F5）。
            "warningBasisSha256": basis_digest or None,
        }
    # 候选配置：与已发布组合分开命名，禁止混成一个「已发布状态」（F5/Q5）。
    result["candidateConfig"] = {
        "published": False,
        "publishedBundleId": versions.get("bundleId") if isinstance(versions, dict) else None,
        "publishedProgramVersion": pinned,
        "runningCode": (live or {}).get("programVersion"),
        "runningAuditSha256": (live or {}).get("auditSha256"),
        "warningBasis": {
            "path": "knowledge/warnings.json",
            "sha256": basis_digest or None,
            "inPublishedBundle": False,
        },
        # programVersion 只覆盖 audit.py，判定链路的其它模块要单独冻结（F5）。
        "codeModules": registry_mod.module_digests(),
        "note": ("本轮 R14 逻辑与依据表均为候选配置，未随 knowledge bundle 发布；"
                 "报告结论按候选配置生成，发布版仍只声明已发布能力。"),
    }
    result["classification"] = result.get("classification")
    # P3: record the 89-checkpoint ledger from the same published registry.
    # Existing item outcomes are not rewritten here.
    try:
        import rule_registry
        product_profile = {
            "scope": scope,
            "nutritionRequired": context.get("nutritionRequired"),
            "sampleKind": context.get("sampleKind"),
            "productionDate": context.get("productionDate") or production,
        }
        audit_profile = {
            "bundleId": versions.get("bundleId"),
            "ruleReleaseId": versions.get("rulesVersion"),
            "rulesVersion": versions.get("rulesVersion"),
            "knowledgeVersion": versions.get("knowledgeVersion"),
            "knowledgeReleaseId": versions.get("knowledgeVersion"),
            "programVersion": versions.get("programVersion"),
            "promptVersion": versions.get("promptVersion"),
            "templateVersion": versions.get("templateVersion"),
        }
        result["checklist"] = rule_registry.record_audit(result, product_profile, audit_profile)
    except Exception as exc:
        result["checklist"] = {"error": str(exc), "checkpoints": [], "ledger": [], "count": 0}
    return result
