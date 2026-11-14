"""Unified published registry for knowledge, rules, interpretations, prompts and templates.

Admin display and audit execution read the same published bundle. Saving a
draft never changes live behaviour. A new publish leaves in-flight tasks and
historical snapshots on the bundle they started with.
"""

from __future__ import annotations

import contextvars
import copy
import datetime
import hashlib
import json
import re
import os
import shutil
import threading
import uuid
from pathlib import Path

import labelreview.clause_index as clause_index
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STORE = ROOT / "knowledge" / "registry"
KNOWLEDGE_DIR = ROOT / "knowledge"

_ACTIVE_BUNDLE = contextvars.ContextVar("registry_active_bundle", default=None)
_LOCK = threading.RLock()

UNCOVERED = "未检查／未覆盖"
UNCOVERED_ASCII = "uncovered"

# Rule metadata that describes the code-backed checks in audit.py.
# Citations are overlaid from knowledge/rules.json when bootstrapping.
RULE_CATALOG = {
    "G01": {"title": "一般标示项目", "checks": "食品名称、配料、生产者、地址、联系方式、许可证编号、产品标准和贮存条件是否可见",
            "appliesWhen": "国产普通预包装食品，按 2025 版审核", "requiredEvidence": "对应字段的原图文字块",
            "judgment": "缺必要项则待核或须整改；齐全则仅确认可见，不代替符合性判断",
            "implementationVersion": "audit.py:general", "methods": ["general"]},
    "F04": {"title": "日期标示", "checks": "生产日期、保质期到期日及位置指引",
            "appliesWhen": "2025 版日期要求", "requiredEvidence": "日期文字或喷码区域",
            "judgment": "按 4.7、10.1–10.3 及附录 A.2 判断缺项、指引或豁免",
            "implementationVersion": "audit.py:dates", "methods": ["dates", "date_format"]},
    "G03": {"title": "净含量和规格", "evidenceNeeds": {"pass": ["value", "unit"], "issue": ["value", "unit"], "review": ["value"]}, "checks": "净含量是否标示",
            "appliesWhen": "2025 版", "requiredEvidence": "净含量文字块",
            "judgment": "未见净含量则待核；字高需结合尺寸另列核实",
            "implementationVersion": "audit.py:net", "methods": ["net"]},
    "N01": {"title": "营养标签强制项目", "evidenceNeeds": {"pass": ["value", "unit", "basis", "printedNrv", "reference", "rounding"], "issue": ["value", "unit", "basis", "printedNrv", "reference", "rounding"], "review": ["value", "unit", "basis"]}, "checks": "能量、蛋白质、脂肪、饱和脂肪、碳水化合物、糖、钠及计量基准",
            "appliesWhen": "需要标示营养标签且结构提取成功", "requiredEvidence": "营养成分表原图",
            "judgment": "缺强制项待核；结构提取失败不得改判通过",
            "implementationVersion": "audit.py:nutrition", "methods": ["nutrition"]},
    "F01": {"title": "NRV% 修约与空白", "evidenceNeeds": {"pass": ["value", "unit", "basis", "printedNrv", "reference", "rounding"], "issue": ["value", "unit", "basis", "printedNrv", "reference", "rounding"], "review": ["value", "unit", "basis"]}, "checks": "NRV% 修约及未规定 NRV 的成分不做计算",
            "appliesWhen": "营养表已提取", "requiredEvidence": "营养成分表 NRV 列",
            "judgment": "对照 GB 28050-2025 A.3.2；糖无 NRV 时应保持空白",
            "implementationVersion": "audit.py:check_nrv", "methods": ["check_nrv"]},
    "F02": {"title": "盐油糖警示语", "checks": "是否标示儿童青少年盐油糖警示语",
            "appliesWhen": "2025 版营养标签", "requiredEvidence": "营养表附近文字",
            "judgment": "缺警示语则待核或须整改",
            "implementationVersion": "audit.py:warning", "methods": ["warning"]},
    "N02": {"title": "能量表内复算", "evidenceNeeds": {"pass": ["value", "unit", "basis", "reference", "rounding"], "issue": ["value", "unit", "basis", "reference", "rounding"], "review": ["value", "unit", "basis"]}, "checks": "能量与蛋白质、脂肪、碳水化合物的表内计算是否相符",
            "appliesWhen": "营养表读数可靠", "requiredEvidence": "能量及三大供能成分数值",
            "judgment": "按附录 A.1、A.3 计算；争议读数只卡住依赖它的结论",
            "implementationVersion": "audit.py:energy", "methods": ["energy"]},
    "N03": {"title": "营养数据交叉核对", "evidenceNeeds": {"pass": ["value", "unit", "basis"], "issue": ["value", "unit", "basis"], "review": ["value", "unit", "basis"]}, "checks": "营养表内部及多处读数是否一致",
            "appliesWhen": "营养表已提取", "requiredEvidence": "营养表行值",
            "judgment": "不一致则待核，不直接判通过",
            "implementationVersion": "audit.py:nutrition_crosscheck", "methods": ["nutrition_crosscheck"]},
    "C01": {"title": "配料强调与定量标示", "checks": "名称或强调配料是否需要定量",
            "appliesWhen": "2025 版 4.4", "requiredEvidence": "食品名称、配料表、图案说明",
            "judgment": "关键词只触发复核，需结合例外和配方",
            "implementationVersion": "audit.py:claims", "methods": ["claims", "bone_claim"]},
    "C02": {"title": "风味图案与香精例外", "checks": "风味图案是否属于特别强调及香精例外",
            "appliesWhen": "标签含风味/图案线索", "requiredEvidence": "名称、配料、图案",
            "judgment": "按 4.4.1.2 及例外判断，不能仅凭图案定论",
            "implementationVersion": "audit.py:bone_claim", "methods": ["bone_claim"]},
    "F03": {"title": "无/不含声称", "checks": "“无”“不含”等声称是否有含量依据",
            "appliesWhen": "标签出现该类用语", "requiredEvidence": "声称文字及配料或检测数据",
            "judgment": "不能把所有“无”“不含”一律禁用，须按 4.4.2 核对",
            "implementationVersion": "audit.py:claims", "methods": ["claims"]},
    "IMPORT2025": {"title": "进口预包装 2025 项目", "checks": "原产国、境内责任主体、注册编号、中外文对应",
            "appliesWhen": "食品范围为进口预包装", "requiredEvidence": "生产者、地址、联系方式等",
            "judgment": "列出应核对项目，转入专项资料审核",
            "implementationVersion": "audit.py:review_2025", "methods": ["review_2025"]},
    "GB7718AL": {"title": "致敏物质提示", "checks": "八类致敏配料是否按 4.12 提示或符合免标条件",
            "appliesWhen": "2025 版普通预包装", "requiredEvidence": "配料表及致敏提示区域",
            "judgment": "对照 4.12、附录 D；例外须有依据",
            "implementationVersion": "audit.py:allergens", "methods": ["allergens", "allergen_coverage"]},
    "SAMR100": {"title": "食品标识监督管理办法衔接", "checks": "办法中与标签相关的禁止性或强制性要求",
            "appliesWhen": "2025 版审稿", "requiredEvidence": "标签全文",
            "judgment": "依据不足时不得写成确定通过",
            "implementationVersion": "audit.py:material_checks", "methods": ["material_checks"]},
    "A3": {"title": "食品名称与属性名称", "checks": "属性名称及易误解名称的字高、字体、颜色和位置",
            "appliesWhen": "同一展示版面出现属性名称与其他名称", "requiredEvidence": "名称区域原图及字号测量",
            "judgment": "按 4.2.1、4.2.2；方向不得写反",
            "implementationVersion": "audit.py:product_name", "methods": ["product_name"]},
    "A4": {"title": "贮存条件", "checks": "是否标示贮存条件",
            "appliesWhen": "2025 版", "requiredEvidence": "贮存条件文字",
            "judgment": "未见则待核",
            "implementationVersion": "audit.py:storage_check", "methods": ["storage_check"]},
    "B1A": {"title": "生产者信息完整性", "checks": "生产者或经营者名称、地址和联系方式是否完整",
            "appliesWhen": "2025 版 4.1、4.6", "requiredEvidence": "生产者信息区域",
            "judgment": "缺项待核",
            "implementationVersion": "audit.py:producer_completeness", "methods": ["producer_completeness"]},
    "B1B": {"title": "配料表内致敏提示方式", "checks": "配料表内提示或邻近提示是否符合附录 D.2",
            "appliesWhen": "需要致敏提示", "requiredEvidence": "配料表及邻近文字、加粗等排版",
            "judgment": "按 4.12.1 与附录 D.2",
            "implementationVersion": "audit.py:allergen_coverage", "methods": ["allergen_coverage"]},
    "C2": {"title": "日期标示形式", "checks": "日期格式及附录 A.2 示例形式",
            "appliesWhen": "已见到日期文字", "requiredEvidence": "日期文字块",
            "judgment": "形式不清则待核",
            "implementationVersion": "audit.py:date_format", "methods": ["date_format"]},
    "R11": {"title": "质量（品质）等级", "checks": "执行标准规定等级时是否标示，未规定时是否误标",
            "appliesWhen": "标签或标准涉及等级", "requiredEvidence": "等级文字及产品标准代号",
            "judgment": "依据 grades.json 与 4.11，无法确认则待核",
            "implementationVersion": "audit.py:grade", "methods": ["grade"]},
    "R15": {"title": "辐照食品标示", "checks": "辐照食品或辐照配料是否按规定标示",
            "appliesWhen": "名称或配料出现辐照线索，或需要确认未辐照", "requiredEvidence": "名称及配料表",
            "judgment": "按 4.15.1、4.15.2",
            "implementationVersion": "audit.py:irradiation", "methods": ["irradiation"]},
    "TRANS7718": {"title": "GB 7718-2025 实施过渡", "checks": "发布日期与实施日期",
            "appliesWhen": "涉及版本适用", "requiredEvidence": "生产日期或计划投产日期",
            "judgment": "只引用封面日期，不作额外推断",
            "implementationVersion": "audit.py:applicability", "methods": ["applicability"]},
    "TRANS28050": {"title": "GB 28050-2025 实施过渡", "checks": "发布日期与实施日期",
            "appliesWhen": "涉及版本适用", "requiredEvidence": "生产日期或计划投产日期",
            "judgment": "只引用封面日期",
            "implementationVersion": "audit.py:applicability", "methods": ["applicability"]},
    "TRANS100": {"title": "食品标识监督管理办法实施过渡", "checks": "办法实施日期",
            "appliesWhen": "涉及版本适用", "requiredEvidence": "生产日期",
            "judgment": "引用办法文本中的实施日期",
            "implementationVersion": "audit.py:applicability", "methods": ["applicability"]},
    "LAW67": {"title": "食品安全法第六十七条", "checks": "预包装食品标签应标明的事项",
            "appliesWhen": "需要引用法律依据或非 2025 路径", "requiredEvidence": "标签字段",
            "judgment": "只在有法律文本依据时引用",
            "implementationVersion": "audit.py:supported_law", "methods": ["supported_law", "general"]},
    "LAW71": {"title": "食品安全法第七十一条", "checks": "标签不得含有虚假内容、不得涉及疾病预防治疗功能",
            "appliesWhen": "出现相关声称", "requiredEvidence": "声称文字",
            "judgment": "有依据才引用",
            "implementationVersion": "audit.py:supported_law", "methods": ["supported_law"]},
    "ADV17": {"title": "广告法第十七条", "checks": "食品广告不得涉及疾病治疗功能等",
            "appliesWhen": "标签含广告性用语", "requiredEvidence": "相关文字",
            "judgment": "有依据才引用",
            "implementationVersion": "audit.py:supported_law", "methods": ["supported_law"]},
    "METRO": {"title": "定量包装商品计量监督管理办法", "checks": "净含量计量要求",
            "appliesWhen": "涉及净含量计量", "requiredEvidence": "净含量标示",
            "judgment": "图像无法完成计量判定时列为核实",
            "implementationVersion": "audit.py:net", "methods": ["net"]},
    "IMPORT": {"title": "进出口食品安全管理办法 / 食品安全法进口条款", "checks": "进口食品标签专项要求",
            "appliesWhen": "进口食品路径", "requiredEvidence": "进口主体及中文标签",
            "judgment": "分流专项审核",
            "implementationVersion": "audit.py:supported_law", "methods": ["supported_law"]},
    "HEBEI": {"title": "河北省食品小作坊小餐饮小摊点管理条例", "checks": "小作坊属地标示义务",
            "appliesWhen": "食品范围为小作坊", "requiredEvidence": "登记及销售区域资料",
            "judgment": "分流属地审核",
            "implementationVersion": "audit.py:supported_law", "methods": ["supported_law"]},
    "M01": {"title": "企业标准适用性", "checks": "企标代号与产品范围是否相符",
            "appliesWhen": "产品标准代号以 Q/ 开头", "requiredEvidence": "标准代号及标准文本",
            "judgment": "仅凭代号不能认定适用，列待核",
            "implementationVersion": "audit.py:material_checks", "methods": ["material_checks"],
            "implementationKind": "code"},
    "M02": {"title": "添加剂使用条件", "checks": "逐包核对食品添加剂使用条件",
            "appliesWhen": "配料表出现常见添加剂名称", "requiredEvidence": "配方、分类和投料量",
            "judgment": "仅凭名称不能确认用量，列待核",
            "implementationVersion": "audit.py:material_checks", "methods": ["material_checks"]},
    "M03": {"title": "复合配料展开", "checks": "复合调味料等是否需要展开",
            "appliesWhen": "配料表使用复合配料描述", "requiredEvidence": "复合配料规格和比例",
            "judgment": "列待核",
            "implementationVersion": "audit.py:material_checks", "methods": ["material_checks"]},
    "M04": {"title": "净含量字高", "checks": "净含量字符高度是否符合要求",
            "appliesWhen": "已定位净含量", "requiredEvidence": "带尺寸的原稿或实物测量",
            "judgment": "图像无成品尺度时列待核",
            "implementationVersion": "audit.py:material_checks", "methods": ["material_checks"]},
    "M05": {"title": "饱和脂肪检测口径", "checks": "饱和脂肪检测与标示口径",
            "appliesWhen": "表中有饱和脂肪", "requiredEvidence": "检测报告",
            "judgment": "表内复算无法替代检测口径核实",
            "implementationVersion": "audit.py:material_checks", "methods": ["material_checks"]},
    "M06": {"title": "料包内配料递减顺序", "checks": "各料包内配料是否按加入量递减排列",
            "appliesWhen": "配料表分包装列示", "requiredEvidence": "各料包投料记录",
            "judgment": "标签无用量时列待核",
            "implementationVersion": "audit.py:material_checks", "methods": ["material_checks"]},
    # P3: consistency / allergen methods already in code, previously missing from the catalog.
    # Citations follow what consistency.py / audit.py already quote; no new clause numbers.
    "I01": {"title": "料包顺序与净含量明细一致性",
            "checks": "分包装列示时，包与包的顺序不得用包内 2% 豁免解释倒置",
            "appliesWhen": "净含量明细与配料表均列出多个料包",
            "requiredEvidence": "料包名称与重量",
            "judgment": "程序比较料包重量列示顺序与配料排版顺序；缺数据则待核",
            "implementationVersion": "consistency.py:pack_order", "methods": ["pack_order"],
            "implementationKind": "code",
            "citations": [{"edition": "2025", "document": "gb7718-2025", "clause": "4.3.2"}]},
    "I02": {"title": "许可证用语内部一致性",
            "checks": "同一标签多处许可证用语应一致",
            "appliesWhen": "标签多处出现许可证编号用语",
            "requiredEvidence": "许可证文字块",
            "judgment": "仅覆盖已实现的混用模式；不是许可数据库核验",
            "implementationVersion": "consistency.py:license_wording", "methods": ["license_wording"],
            "implementationKind": "code",
            "citations": [{"edition": "2025", "document": "gb7718-2025", "clause": "4.9、3.2"}]},
    "I03": {"title": "提示语内部自相矛盾",
            "checks": "贮存或温馨提示等同一段文字不得给出相互冲突的读法",
            "appliesWhen": "出现温馨提示或处置说明",
            "requiredEvidence": "提示语文字块",
            "judgment": "仅覆盖已实现的矛盾模式，不是通用语义检查",
            "implementationVersion": "consistency.py:warning_wording", "methods": ["warning_wording"],
            "implementationKind": "code",
            "citations": [{"edition": "2025", "document": "gb7718-2025", "clause": "3.2、3.4"}]},
    "I04": {"title": "配料表分隔符",
            "checks": "配料分隔符是否完整、清晰",
            "appliesWhen": "已定位配料文字",
            "requiredEvidence": "配料表文字块",
            "judgment": "看不清则待核，不升为确定违法",
            "implementationVersion": "consistency.py:ingredient_punctuation",
            "methods": ["ingredient_punctuation"], "implementationKind": "code",
            "citations": [{"edition": "2025", "document": "gb7718-2025", "clause": "4.3.3"}]},
    "I06": {"title": "保质期时长与到期日算术",
            "checks": "保质期时长与起算、到期日的算术关系",
            "appliesWhen": "已读到生产日期、到期日和保质期时长",
            "requiredEvidence": "生产日期、到期日、保质期时长",
            "judgment": "程序做日历加减并提示相差天数；口径不明则待核。本项不编造未引用的条款号",
            "implementationVersion": "consistency.py:shelf_arithmetic", "methods": ["shelf_arithmetic"],
            "implementationKind": "code"},
    "I07": {"title": "4.1 以外成分时强制项醒目性",
            "checks": "标示了 4.1 以外营养成分时，4.1 强制项应更醒目",
            "appliesWhen": "营养表出现 4.1 以外成分",
            "requiredEvidence": "营养表版面及可比较字重或矢量稿",
            "judgment": "测不到则待核；纯文本模型不能声称已经看过字号或加粗",
            "implementationVersion": "consistency.py:nutrition_prominence",
            "methods": ["nutrition_prominence"], "implementationKind": "code",
            "citations": [{"edition": "2025", "document": "gb28050-2025", "clause": "6.2"}]},
    "GB7718AL-gluten": {"title": "含麸质谷物致敏分支",
            "checks": "含麸质谷物等已单独实现的致敏分支",
            "appliesWhen": "配料出现含麸质谷物或复合配料来源待核实",
            "requiredEvidence": "配料表及致敏提示区域",
            "judgment": "程序有专项分支，仍受配料识别完整性限制；不得把未覆盖写成通过",
            "implementationVersion": "audit.py:allergens", "methods": ["allergens"],
            "implementationKind": "code"},
    "N00": {"title": "营养读数异常隔离",
            "checks": "读数异常的营养项不参与复算，只保留该项核对",
            "appliesWhen": "营养表已提取且出现异常读数",
            "requiredEvidence": "异常项的值、单位与计量基准",
            "judgment": "异常项待核实，不牵连其他已核验项目的确定结论",
            "implementationVersion": "audit.py:nutrition", "methods": ["nutrition"],
            "implementationKind": "code",
            "evidenceNeeds": {"pass": ["value", "unit", "basis"], "issue": ["value", "unit", "basis"],
                              "review": ["value", "unit", "basis"]}},
    # R14 已从「未实现」升级为「限已登记依据的部分实现」。
    # 只对 knowledge/warnings.json 登记的两类依据下结论，其余规定保持未覆盖。
    "R14": {"title": "警示语",
            "checks": "法律、法规、食品安全标准或部门公告要求的警示语是否按规定标示；当前只覆盖已登记依据",
            "appliesWhen": "标签出现固体饮料属性名、配料含「三新食品」原料，或相关规定要求标示警示语",
            "requiredEvidence": "警示语文字及所依据的规定",
            "judgment": ("转致性条款。程序只对已登记依据出结论：固体饮料警示信息可判读到／读不到（版式比例、字体、色差一律标未校验）；"
                         "「三新食品」属推荐性资料，命中只转人工核对。其余规定未登记，按未检查／未覆盖处理。"
                         "盐油糖提示语由 F02 单独处理，不能代替本条。不得将未覆盖写成通过"),
            "implementationVersion": "audit.py:warning_statements",
            "methods": ["warning_statements"],
            "implementationKind": "code",
            # 转致性条款：没命中已登记依据不等于合规，检查点要如实标「未覆盖」而不是「无记录」。
            "coverageScope": {
                "kind": "registered-bases-only",
                "note": ("本条只覆盖 knowledge/warnings.json 登记的两类依据（固体饮料公告第三条的警示信息、"
                         "「三新食品」推荐食用量及适宜人群）。本样本未命中任何已登记依据，"
                         "其余法律、法规、食品安全标准和部门公告要求的警示语仍未覆盖，不得视为通过。"),
            },
            "evidenceNeeds": {"pass": ["requiredFacts"], "issue": ["requiredFacts"], "review": ["requiredFacts"]}},
}


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _digest(value):
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    else:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _file_sha(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def _read_json(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _fsync_dir(path):
    """Flush a directory entry (rename / create) to disk. Best effort on platforms without O_DIRECTORY."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_bytes_durable(path, data):
    """Write to a sibling temp file, fsync it, atomically replace the target, fsync the directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    with open(staging, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staging, path)
    _fsync_dir(path.parent)


def _json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_json(path, data):
    _write_bytes_durable(path, _json_bytes(data))


class PublishAborted(RuntimeError):
    """Raised by a fault hook to simulate a crash at a publish stage (tests only)."""


class KnowledgePackMissing(RuntimeError):
    """A bundle's pinned knowledge pack cannot be found; never silently serve another version."""


PUBLISH_STAGES = ("staging-created", "mid-copy", "after-index-build", "staged-complete",
                  "after-rename", "before-pointer-swap", "after-pointer-swap")
BUNDLE_MANIFEST = "bundle-manifest.json"


def _app_version():
    """读取人工版本号，不依赖任何可选第三方库。

    曾经因为 `import server` 会连带 `import PIL`，没装 Pillow 的机器上
    版本号会静默变成空串，programVersion 只剩 `+hash`。版本号不实等于
    报告不可追溯，所以这里直接读源码里的常量。
    """
    try:
        import labelreview.server as server
        return getattr(server, "APP_VERSION", "") or ""
    except Exception:
        try:
            source = Path(__file__).with_name("server.py").read_text(encoding="utf-8")
        except OSError:
            return ""
        match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', source, re.M)
        return match.group(1) if match else ""


def _program_version():
    version = _app_version()
    audit = Path(__file__).with_name("audit.py")
    return {"appVersion": version, "auditSha256": _file_sha(audit),
            "programVersion": version + ("+" + _file_sha(audit)[:12] if audit.is_file() else "")}


def program_identity():
    """当前**正在运行**的代码身份，含 audit.py 的 sha256。

    纪律第 6 条：版本号不虚标。已发布组合里钉住的 programVersion 是发布那一刻的
    代码哈希；代码改过但没重新发布时，报告和 /api/health 必须能看出
    「跑的不是已发布那一版」，不能拿旧哈希当真。
    """
    return _program_version()


# 判定链路上会改变结论的模块。programVersion 只取 audit.py 的哈希，
# 不够代表整条判定链路，所以候选配置里再单独列出这些模块的摘要。
JUDGEMENT_MODULES = ("audit.py", "audit_retrieval.py", "rule_registry.py", "registry.py",
                     "decision_set.py", "report_check.py", "html_check.py")


def module_digests(names=JUDGEMENT_MODULES):
    """判定模块的 sha256 摘要表，用于在候选配置里冻结「跑的是哪一版代码」。"""
    digests = {}
    package = Path(__file__).resolve().parent
    for name in names:
        path = package / name
        try:
            digests[name] = _file_sha(path)
        except OSError:
            digests[name] = ""
    return digests


class Registry:
    def __init__(self, store=None, knowledge_dir=None):
        self.store = Path(store) if store else DEFAULT_STORE
        self.knowledge_dir = Path(knowledge_dir) if knowledge_dir else KNOWLEDGE_DIR
        self.store.mkdir(parents=True, exist_ok=True)
        # Test hook: callable(stage) invoked at each publish stage; may raise to simulate a crash.
        self.fault_hook = None

    def _stage(self, stage):
        hook = self.fault_hook
        if hook:
            hook(stage)

    # ----- paths -----
    def _current_path(self):
        return self.store / "current.json"

    def _history_path(self):
        return self.store / "history.json"

    def _draft_path(self):
        return self.store / "drafts" / "current.json"

    def _bundle_dir(self, bundle_id):
        return self.store / "bundles" / bundle_id

    def _cache_path(self):
        return self.store / "cache" / "knowledge-pack.json"

    def _pack_path(self, fingerprint):
        """Immutable, content-addressed pack pinned by published bundles."""
        return self.store / "cache" / "packs" / f"{fingerprint}.json"

    # ----- current / history -----
    def current(self):
        self.ensure_bootstrap()
        return _read_json(self._current_path(), {}) or {}

    def current_versions(self, bundle_id=None):
        data = self.bundle_meta(bundle_id) if bundle_id else self.current()
        versions = dict(data.get("versions") or {})
        if not versions and data:
            versions = {key: data.get(key) for key in
                        ("programVersion", "knowledgeVersion", "rulesVersion",
                         "promptVersion", "templateVersion", "bundleId") if data.get(key)}
        if "bundleId" not in versions:
            versions["bundleId"] = data.get("bundleId") or bundle_id
        return versions

    def history(self):
        self.ensure_bootstrap()
        return list(_read_json(self._history_path(), []) or [])

    def bundle_meta(self, bundle_id=None):
        bundle_id = bundle_id or (self.current() or {}).get("bundleId")
        if not bundle_id:
            return {}
        return _read_json(self._bundle_dir(bundle_id) / "meta.json", {}) or {}

    def load_bundle(self, bundle_id=None):
        self.ensure_bootstrap()
        bundle_id = bundle_id or (self.current() or {}).get("bundleId")
        if not bundle_id:
            return None
        folder = self._bundle_dir(bundle_id)
        if str(bundle_id).startswith(".") or not folder.is_dir():
            return None
        return {
            "id": bundle_id,
            "meta": _read_json(folder / "meta.json", {}) or {},
            "rules": _read_json(folder / "rules.json", {}) or {},
            "interpretations": _read_json(folder / "interpretations.json", {}) or {},
            "agents": _read_json(folder / "agents.json", {}) or {},
            "templates": _read_json(folder / "templates.json", {}) or {},
            "knowledge": {
                "manifest": _read_json(folder / "knowledge" / "manifest.json", {}) or {},
                "clauses": self._load_clauses(bundle_id),
                "fileLinks": _read_json(folder / "knowledge" / "file-links.json", []) or [],
                "ingestQa": _read_json(folder / "knowledge" / "ingest-qa.json", {}) or {},
                "corrections": _read_json(folder / "knowledge" / "corrections.json", []) or [],
            },
            "trial": _read_json(folder / "trial.json", {}) or {},
        }

    def _load_clauses(self, bundle_id):
        folder = self._bundle_dir(bundle_id)
        direct = folder / "knowledge" / "clauses.json"
        if direct.is_file():
            return json.loads(direct.read_text(encoding="utf-8"))
        ref = _read_json(folder / "knowledge" / "index-ref.json", {}) or {}
        fingerprint = ref.get("packFingerprint") or ref.get("fingerprint")
        if fingerprint:
            pinned = self._pack_path(fingerprint)
            if pinned.is_file():
                return (_read_json(pinned, {}) or {}).get("documents") or {}
        cache = self._cache_path()
        if cache.is_file():
            cached = _read_json(cache, {}) or {}
            if fingerprint and cached.get("fingerprint") == fingerprint:
                return cached.get("documents") or {}
        # Rebuild from the live knowledge corpus only when it reproduces the pinned fingerprint.
        pack = self.build_knowledge_pack()
        if pack.get("fingerprint") == fingerprint:
            return pack.get("documents") or {}
        # Never serve a different knowledge version under this bundle's id.
        raise KnowledgePackMissing(
            f"{bundle_id} 绑定的知识包 {fingerprint} 不可用（当前语料指纹 {pack.get('fingerprint')}），拒绝改用其他版本。")

    # ----- knowledge pack -----
    def manifest_documents(self):
        data = _read_json(self.knowledge_dir / "manifest.json", {}) or {}
        return list(data.get("documents") or [])

    def source_index(self):
        return _read_json(self.knowledge_dir / "index.json", {}) or {}

    def build_knowledge_pack(self, use_cache=True):
        index_path = self.knowledge_dir / "index.json"
        manifest_path = self.knowledge_dir / "manifest.json"
        fingerprint = _digest({
            "index": _file_sha(index_path),
            "manifest": _file_sha(manifest_path),
            "parseVersion": clause_index.PARSE_VERSION,
        })
        if use_cache and self._cache_path().is_file():
            cached = _read_json(self._cache_path(), {}) or {}
            if cached.get("fingerprint") == fingerprint:
                return cached
        documents = {}
        qa = {}
        index = self.source_index()
        for meta in self.manifest_documents():
            if not clause_index.keep_authorized_document(meta):
                continue
            key = meta["id"]
            built = clause_index.build_slices(key, meta, index.get(key) or [])
            documents[key] = built
            path = self.knowledge_dir / meta.get("path", "")
            qa[key] = clause_index.ingest_qa(path if path.is_file() else None, meta, index.get(key) or [])
        links = []
        for group in clause_index.group_standard_files(self.manifest_documents()):
            files = group["files"]
            pair = None
            pdf = next((item for item in files if str(item.get("format")) == "pdf"), None)
            word = next((item for item in files if str(item.get("format")) in {"docx", "doc"}), None)
            if pdf and word and pdf["id"] in documents and word["id"] in documents:
                pair = clause_index.compare_file_pair(documents[pdf["id"]], documents[word["id"]])
            links.append({
                "standardId": group["standardId"],
                "files": [{"documentId": item["id"], "filename": item.get("filename"),
                           "format": item.get("format"), "sha256": item.get("sha256")}
                          for item in files],
                "pairDiff": pair,
                "conflict": bool(pair and not pair.get("agree")),
            })
            if pair:
                for key in (pdf["id"], word["id"]):
                    qa.setdefault(key, {})["pairDiff"] = {
                        "agree": pair["agree"], "diffCount": pair["diffCount"],
                    }
        pack = {
            "fingerprint": fingerprint,
            "parseVersion": clause_index.PARSE_VERSION,
            "builtAt": _now(),
            "documents": documents,
            "fileLinks": links,
            "ingestQa": qa,
            "manifest": {"documents": self.manifest_documents()},
        }
        if use_cache:
            # Never overwrite a published/pinned cache of a different fingerprint.
            # Official publish pins cache/packs/<fp>.json; this working cache is
            # only for the current parser rebuild.
            existing = _read_json(self._cache_path(), {}) or {}
            if not existing or existing.get("fingerprint") == pack.get("fingerprint"):
                _write_json(self._cache_path(), pack)
            packed = self._pack_path(pack.get("fingerprint"))
            if pack.get("fingerprint") and not packed.is_file():
                packed.parent.mkdir(parents=True, exist_ok=True)
                _write_json(packed, pack)
        return pack

    # ----- bootstrap -----
    def ensure_bootstrap(self):
        with _LOCK:
            current = _read_json(self._current_path())
            if current and current.get("bundleId") and self._bundle_dir(current["bundleId"]).is_dir():
                return current
            return self._publish_bundle(self._build_initial_draft(),
                                        publisher="system",
                                        reason="由已授权的 35 份文件清单生成初始发布版本",
                                        trial=None,
                                        bundle_id="bundle-bootstrap")

    def _citation_book(self):
        return _read_json(self.knowledge_dir / "rules.json", {"2025": {}, "all": {}}) or {"2025": {}, "all": {}}

    def _build_rules(self, citations=None, overrides=None):
        citations = citations or self._citation_book()
        overrides = overrides or {}
        rules = {}
        # Include catalog + any citation-only ids.
        ids = list(dict.fromkeys(list(RULE_CATALOG) +
                                 list((citations.get("2025") or {})) +
                                 list((citations.get("all") or {}))))
        for rule_id in ids:
            catalog = dict(RULE_CATALOG.get(rule_id) or {})
            over = dict(overrides.get(rule_id) or {})
            citation_editions = []
            citation_records = []
            for edition, book in citations.items():
                if rule_id in (book or {}):
                    citation_editions.append(str(edition))
                    citation_records.append({"edition": str(edition), **(book[rule_id] or {})})
            if not citation_records and catalog.get("citations"):
                citation_records = list(catalog.get("citations") or [])
                citation_editions = list(dict.fromkeys(str(row.get("edition") or "2025")
                                                       for row in citation_records))
            item = {
                "id": rule_id,
                "title": over.get("title") or catalog.get("title") or rule_id,
                "checks": over.get("checks") or catalog.get("checks") or "",
                "appliesWhen": over.get("appliesWhen") or catalog.get("appliesWhen") or "",
                "requiredEvidence": over.get("requiredEvidence") or catalog.get("requiredEvidence") or "",
                "judgment": over.get("judgment") or catalog.get("judgment") or "",
                "editions": over.get("editions") or citation_editions or [],
                "citations": over.get("citations") or citation_records,
                "enabled": over.get("enabled") if "enabled" in over else True,
                "implementationKind": over.get("implementationKind") or catalog.get("implementationKind") or "code",
                "implementationVersion": over.get("implementationVersion") or catalog.get("implementationVersion") or "",
                "parameters": over.get("parameters") or catalog.get("parameters") or {},
                "lexicons": over.get("lexicons") or catalog.get("lexicons") or {},
                "changeLog": over.get("changeLog") or [],
                "linkedClauses": [f"{row.get('document')}:{row.get('clause')}"
                                  for row in (over.get("citations") or citation_records)
                                  if row.get("document") and row.get("clause")],
            }
            rules[rule_id] = item
        return {"citations": citations, "rules": rules}

    def _build_agents(self, prompt_overrides=None):
        overrides = prompt_overrides or {}
        roles = {
            "review": {
                "id": "review",
                "title": "审核 Agent",
                "duties": "按产品情况逐项检索相关条款、定义、例外和附表，读取相关原图证据，提出判断及依据",
                "knowledgeScope": "published-only",
                "toolPermissions": ["clause-lookup", "keyword-search", "topic-match", "related-expand"],
                "ioContract": "输出判断、依据条款、证据引用；不得修改知识库",
                "file": "ai-review.md",
            },
            "remediation": {
                "id": "remediation",
                "title": "撰稿 Agent",
                "duties": "根据已确认的决定写现状、依据和整改建议；可提交有依据的异议，但不得把审核结论改成通过",
                "knowledgeScope": "published-only",
                "toolPermissions": ["read-decisions", "read-clauses"],
                "ioContract": "只根据已确认决定成文；异议须附出处",
                "file": "remediation.md",
            },
        }
        for key, role in roles.items():
            path = ROOT / "agents" / role["file"]
            role["prompt"] = overrides.get(key) or (path.read_text(encoding="utf-8") if path.is_file() else "")
            role["promptSha256"] = _digest(role["prompt"])
            role.update(overrides.get(f"{key}Config") or {})
        return {"roles": roles}

    def _build_templates(self, override=None):
        notes_path = self.knowledge_dir / "review_notes.json"
        notes = override if override is not None else (_read_json(notes_path, {}) or {})
        return {
            "reviewNotes": notes,
            "reviewNotesSha256": _digest(notes),
            "rendererSha256": _file_sha(ROOT / "report.js"),
        }

    def _build_interpretations(self, override=None):
        if override is not None:
            entries = override
        else:
            try:
                import labelreview.interpretations as interpretations
                entries = interpretations.entries()
            except Exception:
                entries = {}
        return {"entries": entries}

    def _build_initial_draft(self):
        pack = self.build_knowledge_pack()
        program = _program_version()
        rules = self._build_rules()
        agents = self._build_agents()
        templates = self._build_templates()
        interpretations = self._build_interpretations()
        return {
            "reason": "初始发布",
            "program": program,
            "knowledge": pack,
            "rules": rules,
            "agents": agents,
            "templates": templates,
            "interpretations": interpretations,
        }

    def _versions_of(self, draft, bundle_id):
        program = (draft.get("program") or _program_version())
        knowledge_fp = (draft.get("knowledge") or {}).get("fingerprint") or _digest("empty-knowledge")
        rules_fp = _digest(draft.get("rules") or {})
        prompt_fp = _digest(draft.get("agents") or {})
        template_fp = _digest(draft.get("templates") or {})
        return {
            "bundleId": bundle_id,
            "programVersion": program.get("programVersion") or program.get("appVersion") or "",
            "knowledgeVersion": "kb-" + knowledge_fp[:16],
            "rulesVersion": "rules-" + rules_fp[:16],
            "promptVersion": "prompt-" + prompt_fp[:16],
            "templateVersion": "tpl-" + template_fp[:16],
        }

    def _knowledge_identity(self, knowledge):
        """Return (packFingerprint, knowledgeFingerprint, documents-or-None).

        packFingerprint names the clause pack the bundle pins. When the draft
        carries edited documents or corrections, the knowledge fingerprint is
        derived from that content so knowledgeVersion changes with it.
        """
        knowledge = knowledge or {}
        documents = knowledge.get("documents")
        corrections = knowledge.get("corrections") or []
        base = knowledge.get("fingerprint")
        if not documents:
            pack = self.build_knowledge_pack()
            if not base or base == pack.get("fingerprint"):
                pack_fp = pack.get("fingerprint")
            elif self._pack_path(base).is_file():
                pack_fp = base
            elif (_read_json(self._cache_path(), {}) or {}).get("fingerprint") == base:
                _write_json(self._pack_path(base), _read_json(self._cache_path(), {}))
                pack_fp = base
            elif knowledge.get("documentsFrom") == "pack":
                raise KnowledgePackMissing(f"草稿指向的知识包 {base} 不可用，拒绝改用当前语料 {pack.get('fingerprint')}。")
            else:
                # Legacy drafts carry a version label (kb-...) here, not a pack fingerprint.
                pack_fp = pack.get("fingerprint")
            documents = None
        else:
            pack_fp = _digest({"base": base, "documents": _digest(documents)})
        knowledge_fp = pack_fp if not corrections else _digest({"pack": pack_fp, "corrections": _digest(corrections)})
        return pack_fp, knowledge_fp, documents

    def _cleanup_staging(self):
        bundles = self.store / "bundles"
        if not bundles.is_dir():
            return []
        removed = []
        for child in bundles.iterdir():
            if child.name.startswith(".staging-") or child.name.startswith(".trash-"):
                shutil.rmtree(child, ignore_errors=True)
                removed.append(child.name)
        return removed

    def _publish_bundle(self, draft, publisher, reason, trial, bundle_id=None):
        """Atomic publish.

        1. Write every file into bundles/.staging-<id>/ (temp file + fsync each).
        2. Pin the clause pack as an immutable cache/packs/<fingerprint>.json.
        3. Write bundle-manifest.json (sha256 of every file) last, fsync, then
           rename the staging dir to bundles/<id> (atomic on one filesystem).
        4. Swap current.json via temp + fsync + os.replace (the pointer swap).
        5. Append history.

        A crash before step 4 leaves current.json on the previous complete
        bundle; a staging dir is never served (load_bundle ignores dot dirs
        and requires the final name). Leftover staging is removed on retry.
        """
        with _LOCK:
            self._cleanup_staging()
            self._reconcile_history()
            history = list(_read_json(self._history_path(), []) or [])
            previous = _read_json(self._current_path(), {}) or {}
            bundle_id = bundle_id or (f"bundle-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
                                      f"-{len(history)+1}-{uuid.uuid4().hex[:6]}")
            if bundle_id.startswith("."):
                raise ValueError("bundle id 不能以点开头。")
            final = self._bundle_dir(bundle_id)
            if final.exists() and previous.get("bundleId") == bundle_id:
                raise ValueError(f"{bundle_id} 正在使用，不能覆盖。")
            staging = self.store / "bundles" / f".staging-{bundle_id}-{uuid.uuid4().hex[:8]}"
            (staging / "knowledge").mkdir(parents=True, exist_ok=False)
            self._stage("staging-created")

            knowledge = dict(draft.get("knowledge") or {})
            pack_fp, knowledge_fp, documents = self._knowledge_identity(knowledge)
            if documents is None:
                pack = self.build_knowledge_pack()
                if pack.get("fingerprint") == pack_fp:
                    if not knowledge.get("manifest"):
                        knowledge["manifest"] = pack.get("manifest")
                    if not knowledge.get("fileLinks"):
                        knowledge["fileLinks"] = pack.get("fileLinks")
                    if not knowledge.get("ingestQa"):
                        knowledge["ingestQa"] = pack.get("ingestQa")
                    parse_version = pack.get("parseVersion")
                else:
                    parse_version = knowledge.get("parseVersion") or clause_index.PARSE_VERSION
            else:
                parse_version = knowledge.get("parseVersion") or clause_index.PARSE_VERSION
            versions = self._versions_of({**draft, "knowledge": {"fingerprint": knowledge_fp}}, bundle_id)
            meta = {
                "bundleId": bundle_id,
                "publishedAt": _now(),
                "publishedBy": publisher,
                "reason": reason,
                "versions": versions,
                "previousBundleId": previous.get("bundleId"),
                "trialSummary": (trial or {}).get("summary") if trial else None,
            }
            files = [
                ("meta.json", meta),
                ("rules.json", draft.get("rules") or self._build_rules()),
                ("interpretations.json", draft.get("interpretations") or self._build_interpretations()),
                ("agents.json", draft.get("agents") or self._build_agents()),
                ("templates.json", draft.get("templates") or self._build_templates()),
                ("trial.json", trial or {}),
                ("knowledge/manifest.json", knowledge.get("manifest") or {"documents": self.manifest_documents()}),
                ("knowledge/file-links.json", knowledge.get("fileLinks") or []),
                ("knowledge/ingest-qa.json", knowledge.get("ingestQa") or {}),
                ("knowledge/corrections.json", knowledge.get("corrections") or []),
                ("knowledge/coverage.json", knowledge.get("coverage")
                 or _read_json(self.knowledge_dir / "coverage.json", {}) or {}),
            ]
            digests = {}
            for index, (name, data) in enumerate(files):
                blob = _json_bytes(data)
                _write_bytes_durable(staging / name, blob)
                digests[name] = hashlib.sha256(blob).hexdigest()
                if index == len(files) // 2:
                    self._stage("mid-copy")

            # Pin the clause pack (immutable, content-addressed).
            pinned = self._pack_path(pack_fp)
            if documents is not None:
                if not pinned.is_file():
                    _write_json(pinned, {"fingerprint": pack_fp, "parseVersion": parse_version,
                                         "documents": documents, "derived": True})
            elif not pinned.is_file():
                pack = self.build_knowledge_pack()
                if pack.get("fingerprint") != pack_fp:
                    raise KnowledgePackMissing(f"无法固定知识包 {pack_fp}。")
                _write_json(pinned, pack)
            index_ref = {"fingerprint": knowledge_fp, "packFingerprint": pack_fp,
                         "parseVersion": parse_version,
                         "packSha256": _file_sha(pinned)}
            blob = _json_bytes(index_ref)
            _write_bytes_durable(staging / "knowledge" / "index-ref.json", blob)
            digests["knowledge/index-ref.json"] = hashlib.sha256(blob).hexdigest()
            self._stage("after-index-build")

            # Completion marker last: a bundle without it is incomplete by definition.
            _write_json(staging / BUNDLE_MANIFEST, {"bundleId": bundle_id, "complete": True,
                                                   "files": dict(sorted(digests.items()))})
            _fsync_dir(staging / "knowledge")
            _fsync_dir(staging)
            self._stage("staged-complete")

            if final.exists():
                # Not the live bundle (checked above): move aside, then swap in.
                trash = self.store / "bundles" / f".trash-{bundle_id}-{uuid.uuid4().hex[:8]}"
                os.replace(final, trash)
                shutil.rmtree(trash, ignore_errors=True)
            os.replace(staging, final)
            _fsync_dir(final.parent)
            self._stage("after-rename")

            current = {**meta, "status": "published"}
            self._stage("before-pointer-swap")
            _write_json(self._current_path(), current)
            self._stage("after-pointer-swap")
            history.append({"at": meta["publishedAt"], "action": "publish", "bundleId": bundle_id,
                            "by": publisher, "reason": reason, "versions": versions,
                            "trialSummary": meta.get("trialSummary")})
            _write_json(self._history_path(), history)
            return current

    def _reconcile_history(self):
        """If a crash hit after the pointer swap but before history was written,
        record the live bundle in history so the audit trail is not lost."""
        current = _read_json(self._current_path(), {}) or {}
        bundle_id = current.get("bundleId")
        if not bundle_id or not self._bundle_dir(bundle_id).is_dir():
            return False
        history = list(_read_json(self._history_path(), []) or [])
        if any(row.get("bundleId") == bundle_id for row in history):
            return False
        history.append({"at": current.get("publishedAt"), "action": "publish", "bundleId": bundle_id,
                        "by": current.get("publishedBy"), "reason": current.get("reason"),
                        "versions": current.get("versions"), "trialSummary": current.get("trialSummary"),
                        "recovered": True})
        _write_json(self._history_path(), history)
        return True

    def verify_bundle(self, bundle_id=None):
        """Check a bundle against its bundle-manifest.json and its pinned pack."""
        bundle_id = bundle_id or (self.current() or {}).get("bundleId")
        folder = self._bundle_dir(bundle_id)
        problems = []
        manifest = _read_json(folder / BUNDLE_MANIFEST)
        if not manifest:
            return {"bundleId": bundle_id, "ok": False, "legacy": True,
                    "problems": ["缺少 bundle-manifest.json（旧格式或未完成的发布版本）"]}
        for name, digest in (manifest.get("files") or {}).items():
            if _file_sha(folder / name) != digest:
                problems.append(f"{name} 校验不符")
        ref = _read_json(folder / "knowledge" / "index-ref.json", {}) or {}
        pinned = self._pack_path(ref.get("packFingerprint") or ref.get("fingerprint") or "missing")
        if not pinned.is_file():
            problems.append("固定知识包缺失")
        elif ref.get("packSha256") and _file_sha(pinned) != ref.get("packSha256"):
            problems.append("固定知识包校验不符")
        return {"bundleId": bundle_id, "ok": not problems, "legacy": False, "problems": problems}

    # ----- draft workflow -----
    def get_draft(self):
        self.ensure_bootstrap()
        draft = _read_json(self._draft_path())
        if not draft:
            draft = self._draft_from_published()
            _write_json(self._draft_path(), draft)
        return draft

    def _draft_from_published(self, bundle_id=None):
        bundle = self.load_bundle(bundle_id)
        return {
            "status": "draft",
            "basedOn": bundle["id"],
            "updatedAt": _now(),
            "reason": "",
            "program": _program_version(),
            "knowledge": {
                "fingerprint": (bundle.get("meta") or {}).get("versions", {}).get("knowledgeVersion"),
                "sourceFingerprint": None,
                "manifest": bundle["knowledge"]["manifest"],
                "fileLinks": bundle["knowledge"]["fileLinks"],
                "ingestQa": bundle["knowledge"]["ingestQa"],
                "corrections": list(bundle["knowledge"]["corrections"] or []),
                "sliceEdits": [],
                "documents": None,  # resolve from published unless edited
            },
            "rules": copy.deepcopy(bundle["rules"]),
            "interpretations": copy.deepcopy(bundle["interpretations"]),
            "agents": copy.deepcopy(bundle["agents"]),
            "templates": copy.deepcopy(bundle["templates"]),
        }

    def save_draft(self, patch, editor="admin", reason=""):
        draft = self.get_draft()
        patch = patch if isinstance(patch, dict) else {}
        if "rules" in patch:
            draft["rules"] = self._merge_rules(draft.get("rules") or {}, patch["rules"])
        if "interpretations" in patch:
            draft["interpretations"] = patch["interpretations"]
        if "agents" in patch:
            draft["agents"] = _deep_merge(draft.get("agents") or {}, patch["agents"])
        if "templates" in patch:
            draft["templates"] = _deep_merge(draft.get("templates") or {}, patch["templates"])
        if "knowledge" in patch:
            draft["knowledge"] = _deep_merge(draft.get("knowledge") or {}, patch["knowledge"])
        if reason:
            draft["reason"] = reason
        draft["updatedAt"] = _now()
        draft["updatedBy"] = editor
        draft["status"] = "draft"
        _write_json(self._draft_path(), draft)
        return draft

    def _merge_rules(self, current, patch):
        current = copy.deepcopy(current or {"citations": {}, "rules": {}})
        if "citations" in patch:
            current["citations"] = _deep_merge(current.get("citations") or {}, patch["citations"])
        if "rules" in patch:
            for rule_id, body in (patch["rules"] or {}).items():
                current.setdefault("rules", {})
                if body is None:
                    continue
                existing = current["rules"].get(rule_id) or {"id": rule_id, "enabled": True}
                merged = _deep_merge(existing, body)
                merged["id"] = rule_id
                log = list(existing.get("changeLog") or [])
                log.append({"at": _now(), "change": body, "fromEnabled": existing.get("enabled")})
                merged["changeLog"] = log
                # Configurable parameters only; algorithm changes stay code-released.
                if merged.get("implementationKind") == "code" and body.get("algorithm"):
                    raise ValueError(f"{rule_id} 的算法仍需开发发布，不能只在后台改实现。")
                _validate_parameters(merged)
                current["rules"][rule_id] = merged
        return current

    def auto_check(self, draft=None):
        draft = draft or self.get_draft()
        issues = []
        rules = (draft.get("rules") or {}).get("rules") or {}
        citations = (draft.get("rules") or {}).get("citations") or {}
        for rule_id, rule in rules.items():
            if not rule.get("title"):
                issues.append({"code": "missing-title", "level": "error", "ruleId": rule_id,
                               "detail": "规则缺少标题。"})
            if rule.get("enabled") is False and not rule.get("title"):
                issues.append({"code": "disabled-untitled", "level": "warning", "ruleId": rule_id,
                               "detail": "停用规则仍需保留名称，以便显示未检查／未覆盖。"})
            for citation in rule.get("citations") or []:
                doc = citation.get("document")
                clause = citation.get("clause")
                if doc and not self.document_metadata(doc, draft=draft):
                    issues.append({"code": "missing-document", "level": "error", "ruleId": rule_id,
                                   "detail": f"引用了知识库中不存在的文件 {doc}。"})
            _validate_parameters(rule, issues=issues)
        # Changing an allergen basis should list dependents — computed later in affected().
        errors = [item for item in issues if item.get("level") == "error"]
        return {"ok": not errors, "issues": issues, "errorCount": len(errors)}

    def affected_assets(self, draft=None):
        """When a clause basis changes, list affected rules, interpretations and templates."""
        draft = draft or self.get_draft()
        published = self.load_bundle()
        affected = {"rules": [], "interpretations": [], "templates": []}
        old_rules = (published.get("rules") or {}).get("rules") or {}
        new_rules = (draft.get("rules") or {}).get("rules") or {}
        for rule_id, rule in new_rules.items():
            prev = old_rules.get(rule_id) or {}
            if _digest(prev.get("citations")) != _digest(rule.get("citations")) \
                    or prev.get("enabled") != rule.get("enabled") \
                    or _digest(prev.get("lexicons")) != _digest(rule.get("lexicons")) \
                    or _digest(prev.get("parameters")) != _digest(rule.get("parameters")):
                affected["rules"].append({
                    "id": rule_id,
                    "title": rule.get("title"),
                    "changes": _changed_fields(prev, rule, ("enabled", "citations", "lexicons",
                                                              "parameters", "judgment", "linkedClauses")),
                })
        old_i = (published.get("interpretations") or {}).get("entries") or {}
        new_i = (draft.get("interpretations") or {}).get("entries") or {}
        for key in set(old_i) | set(new_i):
            if _digest(old_i.get(key)) != _digest(new_i.get(key)):
                affected["interpretations"].append(key)
        old_t = published.get("templates") or {}
        new_t = draft.get("templates") or {}
        if _digest(old_t.get("reviewNotes")) != _digest(new_t.get("reviewNotes")):
            affected["templates"].append("reviewNotes")
        # If allergen clause 4.12 / GB7718AL is involved, always surface the cluster.
        allergen_hit = any(item["id"] in {"GB7718AL", "B1B"} or "4.12" in str(item)
                           for item in affected["rules"])
        if allergen_hit:
            for rule_id in ("GB7718AL", "B1B"):
                if rule_id not in {item["id"] for item in affected["rules"]} and rule_id in new_rules:
                    affected["rules"].append({"id": rule_id, "title": new_rules[rule_id].get("title"),
                                              "changes": ["linked"]})
            notes = (new_t.get("reviewNotes") or {}).get("notes") or {}
            for key, body in notes.items():
                blob = json.dumps(body, ensure_ascii=False)
                if "4.12" in blob or "致敏" in blob:
                    if key not in affected["templates"]:
                        affected["templates"].append(f"reviewNotes:{key}")
        return affected

    def trial_run(self, draft=None, sample=None):
        """Run fixed samples with the draft rules vs the currently published bundle."""
        draft = draft or self.get_draft()
        sample = sample or default_trial_sample()
        published = self.load_bundle()
        before = self._audit_with_bundle(published, sample)
        virtual = self._virtual_bundle(draft, published)
        after = self._audit_with_bundle(virtual, sample)
        diff = diff_audit_results(before, after)
        return {
            "at": _now(),
            "sampleId": sample.get("id") or "fixed-sample",
            "before": {"versions": (published.get("meta") or {}).get("versions"),
                       "items": _brief_items(before)},
            "after": {"items": _brief_items(after)},
            "diff": diff,
            "summary": {
                "changedItems": len(diff.get("changed") or []),
                "addedItems": len(diff.get("added") or []),
                "removedItems": len(diff.get("removed") or []),
                "uncoveredAfter": [item["id"] for item in (after.get("items") or [])
                                   if item.get("outcome") == UNCOVERED_ASCII
                                   or item.get("status") == UNCOVERED],
            },
        }

    def _virtual_bundle(self, draft, published):
        knowledge = dict(published["knowledge"])
        knowledge["documents"] = knowledge.get("documents") or knowledge.get("clauses") or {}
        if (draft.get("knowledge") or {}).get("documents"):
            knowledge["documents"] = draft["knowledge"]["documents"]
        knowledge["corrections"] = (draft.get("knowledge") or {}).get("corrections") or knowledge.get("corrections")
        return {
            "id": "draft-virtual",
            "meta": {"bundleId": "draft-virtual",
                     "versions": self._versions_of({**draft, "knowledge": published.get("knowledge")}, "draft-virtual")},
            "rules": draft.get("rules") or published["rules"],
            "interpretations": draft.get("interpretations") or published["interpretations"],
            "agents": draft.get("agents") or published["agents"],
            "templates": draft.get("templates") or published["templates"],
            "knowledge": knowledge,
            "trial": {},
        }

    def _audit_with_bundle(self, bundle, sample):
        payload = copy.deepcopy(sample.get("payload") or sample)
        context = dict(payload.get("context") or {})
        versions = (bundle.get("meta") or {}).get("versions") or {"bundleId": bundle.get("id")}
        context["versions"] = versions
        payload["context"] = context
        token = _ACTIVE_BUNDLE.set(bundle)
        try:
            from labelreview.audit import audit
            return audit(payload)
        finally:
            _ACTIVE_BUNDLE.reset(token)

    def publish(self, editor="admin", reason="", trial=None, require_trial=True, knowledge_source="published"):
        """Publish the draft. knowledge_source="published" keeps the live bundle's clause
        pack; "rebuild" publishes the pack rebuilt from the authorized manifest/index with
        the current parser (a formal knowledge release)."""
        draft = self.get_draft()
        checks = self.auto_check(draft)
        if not checks["ok"]:
            raise ValueError("自动检查未通过：" + "；".join(item["detail"] for item in checks["issues"] if item["level"] == "error"))
        trial = trial or self.trial_run(draft)
        if require_trial and trial is None:
            raise ValueError("发布前需要用固定样例试跑。")
        # Resolve knowledge documents for the published pack.
        draft = copy.deepcopy(draft)
        if knowledge_source not in {"published", "rebuild"}:
            raise ValueError("knowledge_source 只能是 published 或 rebuild。")
        if not (draft.get("knowledge") or {}).get("documents"):
            corrections = (draft.get("knowledge") or {}).get("corrections") or []
            if knowledge_source == "rebuild":
                pack = self.build_knowledge_pack()
                draft["knowledge"] = {
                    "fingerprint": pack.get("fingerprint"),
                    "documentsFrom": "pack",
                    "documents": None,
                    "manifest": pack.get("manifest"),
                    "fileLinks": pack.get("fileLinks"),
                    "ingestQa": pack.get("ingestQa"),
                    "parseVersion": pack.get("parseVersion"),
                    "corrections": corrections,
                }
            else:
                published = self.load_bundle()
                ref = _read_json(self._bundle_dir(published["id"]) / "knowledge" / "index-ref.json", {}) or {}
                draft["knowledge"] = {
                    "fingerprint": ref.get("packFingerprint") or ref.get("fingerprint")
                    or (self.build_knowledge_pack() or {}).get("fingerprint"),
                    "documentsFrom": "pack",
                    "documents": None,
                    "manifest": published["knowledge"]["manifest"],
                    "fileLinks": published["knowledge"]["fileLinks"],
                    "ingestQa": published["knowledge"]["ingestQa"],
                    "parseVersion": ref.get("parseVersion") or clause_index.PARSE_VERSION,
                    "corrections": corrections,
                }
        current = self._publish_bundle(draft, publisher=editor, reason=reason or draft.get("reason") or "后台发布",
                                       trial=trial)
        # Reset draft to the new published snapshot.
        _write_json(self._draft_path(), self._draft_from_published(current["bundleId"]))
        return {"current": current, "checks": checks, "trial": trial, "affected": self.affected_assets(draft)}

    def rollback(self, bundle_id, editor="admin", reason=""):
        if not bundle_id or not self._bundle_dir(bundle_id).is_dir():
            raise ValueError("指定版本不存在，无法回滚。")
        # Re-publish a copy so history records the rollback explicitly.
        bundle = self.load_bundle(bundle_id)
        ref = _read_json(self._bundle_dir(bundle_id) / "knowledge" / "index-ref.json", {}) or {}
        draft = {
            "program": _program_version(),
            "knowledge": {
                "fingerprint": ref.get("packFingerprint") or ref.get("fingerprint"),
                "documentsFrom": "pack",
                "parseVersion": ref.get("parseVersion") or clause_index.PARSE_VERSION,
                "manifest": bundle["knowledge"]["manifest"],
                "documents": None,
                "fileLinks": bundle["knowledge"]["fileLinks"],
                "ingestQa": bundle["knowledge"]["ingestQa"],
                "corrections": bundle["knowledge"]["corrections"],
            },
            "rules": bundle["rules"],
            "interpretations": bundle["interpretations"],
            "agents": bundle["agents"],
            "templates": bundle["templates"],
        }
        current = self._publish_bundle(draft, publisher=editor,
                                       reason=reason or f"回滚到 {bundle_id}",
                                       trial=bundle.get("trial") or {"summary": {"rollbackFrom": bundle_id}})
        history = self.history()
        if history:
            history[-1]["action"] = "rollback"
            history[-1]["rolledBackTo"] = bundle_id
            _write_json(self._history_path(), history)
        _write_json(self._draft_path(), self._draft_from_published(current["bundleId"]))
        return current

    # ----- queries used by knowledge.py / audit.py -----
    def document_metadata(self, key, bundle_id=None, draft=None):
        if draft:
            docs = ((draft.get("knowledge") or {}).get("manifest") or {}).get("documents") or []
        else:
            bundle = self.load_bundle(bundle_id) if bundle_id else None
            docs = ((bundle or {}).get("knowledge") or {}).get("manifest", {}).get("documents") if bundle else None
            if not docs:
                docs = self.manifest_documents()
        for item in docs or []:
            if item.get("id") == key:
                return dict(item)
        return None

    def _clause_map(self, bundle):
        knowledge = (bundle or {}).get("knowledge") or {}
        return knowledge.get("documents") or knowledge.get("clauses") or {}

    def clauses_for(self, document_id, bundle_id=None):
        bundle = _ACTIVE_BUNDLE.get()
        if bundle and (bundle_id is None or bundle.get("id") == bundle_id):
            return self._clause_map(bundle).get(document_id)
        data = self.load_bundle(bundle_id)
        if not data:
            return None
        return self._clause_map(data).get(document_id)

    def all_clauses(self, bundle_id=None):
        bundle = _ACTIVE_BUNDLE.get()
        if bundle and (bundle_id is None or bundle.get("id") == bundle_id):
            return self._clause_map(bundle)
        data = self.load_bundle(bundle_id)
        return self._clause_map(data) if data else {}

    def file_links(self, bundle_id=None):
        data = (_ACTIVE_BUNDLE.get() if _ACTIVE_BUNDLE.get() and bundle_id in (None, (_ACTIVE_BUNDLE.get() or {}).get("id")) else None) \
            or self.load_bundle(bundle_id)
        return ((data or {}).get("knowledge") or {}).get("fileLinks") or []

    def conflict_for(self, document_id, bundle_id=None):
        for group in self.file_links(bundle_id):
            ids = {item.get("documentId") for item in group.get("files") or []}
            if document_id in ids and group.get("conflict"):
                return group.get("pairDiff")
        return None

    def rules_bundle(self, bundle_id=None):
        active = _ACTIVE_BUNDLE.get()
        if active and (bundle_id is None or active.get("id") == bundle_id):
            return active.get("rules") or {}
        data = self.load_bundle(bundle_id)
        return (data or {}).get("rules") or {}

    def rule(self, rule_id, bundle_id=None):
        return ((self.rules_bundle(bundle_id).get("rules")) or {}).get(str(rule_id))

    def is_enabled(self, rule_id, bundle_id=None):
        record = self.rule(rule_id, bundle_id)
        if not record:
            return True  # unregistered procedural items (scope, mixed-images, …) still run
        return bool(record.get("enabled", True))

    def citation_record(self, rule_id, edition, bundle_id=None):
        citations = (self.rules_bundle(bundle_id).get("citations")) or {}
        return (citations.get(str(edition), {}) or {}).get(rule_id) or (citations.get("all") or {}).get(rule_id)

    def parameters(self, rule_id, bundle_id=None):
        record = self.rule(rule_id, bundle_id) or {}
        return record.get("parameters") or {}

    def lexicons(self, rule_id, bundle_id=None):
        record = self.rule(rule_id, bundle_id) or {}
        return record.get("lexicons") or {}

    def interpretations(self, bundle_id=None):
        active = _ACTIVE_BUNDLE.get()
        if active and (bundle_id is None or active.get("id") == bundle_id):
            return (active.get("interpretations") or {}).get("entries") or {}
        data = self.load_bundle(bundle_id)
        return ((data or {}).get("interpretations") or {}).get("entries") or {}

    def registered_rule_ids(self, bundle_id=None):
        return list(((self.rules_bundle(bundle_id).get("rules")) or {}).keys())

    def corrections(self, document_id=None, bundle_id=None):
        data = _ACTIVE_BUNDLE.get() if _ACTIVE_BUNDLE.get() and bundle_id in (None, (_ACTIVE_BUNDLE.get() or {}).get("id")) else self.load_bundle(bundle_id)
        rows = ((data or {}).get("knowledge") or {}).get("corrections") or []
        if document_id:
            return [row for row in rows if row.get("documentId") == document_id]
        return list(rows)


def _deep_merge(base, patch):
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _changed_fields(old, new, keys):
    changed = []
    for key in keys:
        if _digest(old.get(key)) != _digest(new.get(key)):
            changed.append(key)
    return changed


def _validate_parameters(rule, issues=None):
    issues = issues if issues is not None else []
    params = rule.get("parameters") or {}
    for name, spec in params.items():
        if not isinstance(spec, dict) or "value" not in spec:
            continue
        unit = spec.get("unit")
        bounds = spec.get("range") or spec.get("bounds")
        value = spec.get("value")
        if bounds and isinstance(value, (int, float)):
            lo, hi = bounds[0], bounds[1] if len(bounds) > 1 else None
            if lo is not None and value < lo or hi is not None and value > hi:
                issues.append({"code": "param-range", "level": "error", "ruleId": rule.get("id"),
                               "detail": f"参数 {name}={value}{unit or ''} 超出允许范围 {bounds}。"})
        if spec.get("basis") is None and rule.get("implementationKind") != "code" and name in params:
            # Config parameters should declare a basis; warn only.
            issues.append({"code": "param-basis", "level": "info", "ruleId": rule.get("id"),
                           "detail": f"参数 {name} 未填写依据。"})
    return issues


def _brief_items(report):
    items = []
    for item in (report or {}).get("items") or []:
        items.append({
            "id": item.get("id"),
            "outcome": item.get("outcome") or item.get("group"),
            "status": item.get("status"),
            "title": item.get("title"),
        })
    return items


def diff_audit_results(before, after):
    old = {item.get("id"): item for item in _brief_items(before)}
    new = {item.get("id"): item for item in _brief_items(after)}
    changed, added, removed = [], [], []
    for key, item in new.items():
        if key not in old:
            added.append(item)
        elif old[key].get("outcome") != item.get("outcome") or old[key].get("status") != item.get("status"):
            changed.append({"id": key, "from": old[key], "to": item})
    for key, item in old.items():
        if key not in new:
            removed.append(item)
    return {"changed": changed, "added": added, "removed": removed}


def default_trial_sample():
    """A fixed, OCR-free sample used by the publish flow."""
    fixture = ROOT / "tests" / "fixtures" / "chips_nutrition_table.json"
    pages = []
    draft = {"fields": {}, "nutrition": {}, "factQa": {}}
    if fixture.is_file():
        raw = json.loads(fixture.read_text(encoding="utf-8"))
        pages = raw.get("pages") or raw.get("payload", {}).get("pages") or []
        if raw.get("draft"):
            draft = raw["draft"]
        elif raw.get("fields") or raw.get("nutrition"):
            draft = {"fields": raw.get("fields") or {}, "nutrition": raw.get("nutrition") or {},
                     "factQa": raw.get("factQa") or {}}
        elif pages:
            try:
                from labelreview.extract import extract
                draft = extract({"pages": pages})
            except Exception:
                draft = {"fields": {}, "nutrition": {}, "factQa": {}}
    if not pages:
        pages = [{"id": "p1", "name": "sample.jpg", "blocks": [
            {"id": "b1", "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.05, "text": "食品名称：示例薯片"},
            {"id": "b2", "x": 0.1, "y": 0.2, "w": 0.6, "h": 0.08, "text": "配料：马铃薯、植物油、食用盐"},
            {"id": "b3", "x": 0.1, "y": 0.35, "w": 0.4, "h": 0.04, "text": "净含量：100克"},
        ]}]
        draft = {"fields": {
            "name": {"value": "示例薯片", "refs": ["b1"]},
            "ingredients": {"value": "马铃薯、植物油、食用盐", "refs": ["b2"]},
            "net": {"value": "100克", "refs": ["b3"]},
        }, "nutrition": {}, "attribution": {"token": ""}}
    return {
        "id": "fixed-chips-or-minimal",
        "payload": {
            "pages": pages,
            "draft": draft,
            "context": {"scope": "regular", "sampleKind": "artwork", "scenario": "auto",
                        "productionDate": "", "nutritionRequired": "required"},
        },
    }


# Process-wide default registry (the app store). Tests may construct their own.
_DEFAULT = None


def default_registry():
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Registry()
    return _DEFAULT


def set_default_registry(registry):
    global _DEFAULT
    _DEFAULT = registry
    return _DEFAULT


def reset_default_registry():
    global _DEFAULT
    _DEFAULT = None


def current_versions(bundle_id=None):
    return default_registry().current_versions(bundle_id)


def pin_versions(context=None, payload=None):
    """Return the versions a task should freeze. An existing pin wins."""
    context = context if isinstance(context, dict) else {}
    if payload and isinstance(payload.get("context"), dict) and not context:
        context = payload["context"]
    existing = context.get("versions") if isinstance(context.get("versions"), dict) else {}
    if existing.get("bundleId"):
        # Keep the frozen bundle; fill any missing component ids from that bundle.
        frozen = default_registry().current_versions(existing.get("bundleId"))
        return {**frozen, **{k: v for k, v in existing.items() if v}}
    return default_registry().current_versions()


def use_bundle(bundle_or_id):
    if isinstance(bundle_or_id, dict):
        bundle = bundle_or_id
    else:
        bundle = default_registry().load_bundle(bundle_or_id)
    return _ACTIVE_BUNDLE.set(bundle)


def reset_bundle(token):
    _ACTIVE_BUNDLE.reset(token)


def active_bundle():
    return _ACTIVE_BUNDLE.get()


def is_enabled(rule_id, bundle_id=None):
    return default_registry().is_enabled(rule_id, bundle_id)


def rule(rule_id, bundle_id=None):
    return default_registry().rule(rule_id, bundle_id)
