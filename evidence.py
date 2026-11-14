"""Conservative multi-image evidence handling, independent of regulatory rules."""
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
import re

from extract import MASS, field_candidates, label_blocks, net_candidates, read_pages


AREAS = {"front": "正面", "back": "背面", "side": "侧面", "seal": "封口／喷码区"}
LABELS = {
    "name": "食品名称", "ingredients": "配料表", "net": "净含量", "packs": "分包规格",
    "producer": "生产者或经营者", "address": "地址", "contact": "联系方式",
    "license": "许可证／登记编号", "standard": "产品标准代号", "production": "生产日期",
    "shelf": "保质期", "expiry": "保质期到期日", "storage": "贮存条件", "basis": "营养表计量基准",
}
DATE = r"(?<!\d)(?:20\d{2}[./年-]\s*\d{1,2}[./月-]\s*\d{1,2}日?|20\d{6})(?!\d)"
DATE_HINT = re.compile(r"见.{0,32}(?:包装|封口|封边|瓶|袋|罐|背面|底部|喷码)")
DATE_ROLES = {
    "production": ("生产日期", r"生产日期|制造日期"),
    "expiry": ("到期日", r"保质期到期日|保质期至|到期日|食用期限"),
}
DATE_ROLE = re.compile("|".join(f"(?P<{key}>{pattern})" for key, (_, pattern) in DATE_ROLES.items()))
SC = re.compile(r"\bSC\s*(\d{14})\b", re.I)
BARCODE = re.compile(r"^(?:条形码\s*[:：]?\s*)?((?:\d[ -]?){12,14})$")
# Area cues used so a coverage tick cannot invent a panel the photo does not show.
FRONT_AREA = re.compile(r"品名|食品名称|商品名称")
BACK_AREA = re.compile(
    r"配料|原料组成|生产者|生产经营者|经销者|生产商|制造商|"
    r"地址|贮存|储藏|保存条件|保质期|执行标准|产品标准代号|"
    r"生产许可证|营养成分表"
)
SEAL_AREA = re.compile(r"生产日期|制造日期|保质期至|保质期到期|喷码")
# Check 受委托/受托 first; 委托方 is a substring of 受委托方.
ENTRUSTED_ROLE = re.compile(
    r"受委托(?:方|单位|企业|生产(?:企业|者|厂商)?)|受托(?:方|单位|企业|生产(?:企业|者|厂商)?)"
)
COMMISSIONER_ROLE = re.compile(r"委托(?:方|单位|人|企业|生产(?:企业|者|厂商)?)")


def compact(value):
    return re.sub(r"[\W_]+", "", value).lower()


def legal_roles_in(text):
    """Return {commissioner, entrusted} mentioned in text; 受委托 is not also 委托."""
    roles = set()
    if not text:
        return roles
    if ENTRUSTED_ROLE.search(text):
        roles.add("entrusted")
    if COMMISSIONER_ROLE.search(ENTRUSTED_ROLE.sub(" ", text)):
        roles.add("commissioner")
    return roles


def sc_legal_role(blocks, sc_block):
    """Bind an SC number to 委托方 or 受委托方 when the page labels that role."""
    same = legal_roles_in(getattr(sc_block, "text", "") or "")
    if len(same) == 1:
        return next(iter(same))
    nearby = []
    box = getattr(sc_block, "box", None)
    sc_y = box[1] if isinstance(box, (list, tuple)) and len(box) >= 2 else None
    for block in blocks:
        roles = legal_roles_in(getattr(block, "text", "") or "")
        if not roles:
            continue
        other = getattr(block, "box", None)
        dist = 0.0
        if sc_y is not None and isinstance(other, (list, tuple)) and len(other) >= 2:
            dist = abs(float(other[1]) - float(sc_y))
        nearby.append((dist, roles))
    page_roles = set().union(*(roles for _, roles in nearby)) if nearby else set()
    if len(page_roles) == 1:
        return next(iter(page_roles))
    if nearby and sc_y is not None:
        nearby.sort(key=lambda item: item[0])
        closest = nearby[0][1]
        if len(closest) == 1:
            return next(iter(closest))
    return ""


def distinct_sc_roles(first, second):
    """True when the two pages' SC numbers are labelled as different legal roles."""
    roles_a = {first.get("sc_roles", {}).get(num, "") for num in first.get("sc") or {}}
    roles_b = {second.get("sc_roles", {}).get(num, "") for num in second.get("sc") or {}}
    known_a = {role for role in roles_a if role}
    known_b = {role for role in roles_b if role}
    return bool(known_a and known_b and known_a.isdisjoint(known_b))


def supported_areas(blocks):
    """Areas the page content itself can attest to. Empty means unclassifiable."""
    found = set()
    texts = [block.text for block in blocks]
    joined = "\n".join(texts)
    has_name = any(key == "name" and value for key, value, _refs in field_candidates(blocks))
    if has_name or FRONT_AREA.search(joined) or any(barcode(text) for text in texts):
        found.add("front")
    if BACK_AREA.search(joined):
        found.add("back")
    if SEAL_AREA.search(joined):
        found.add("seal")
    return found


def area_flag_is_reliable(area, attested):
    """Honor flags only when they do not invent a panel the photo does not show.

    A sparse page (no area cues) still follows the user's flags, which keeps
    constructed complete-coverage cases working. A front-only pack that also
    ticks back/side/seal does not become 已覆盖 for those areas.
    """
    if not attested:
        return True
    return area in attested


def barcode(value):
    match = BARCODE.fullmatch(value)
    if not match:
        return ""
    digits = re.sub(r"\D", "", match[1])
    if len(digits) not in {12, 13, 14}:
        return ""
    check = sum(int(digit) * (3 if index % 2 == 0 else 1)
                for index, digit in enumerate(reversed(digits[:-1])))
    return digits.zfill(14) if (check + int(digits[-1])) % 10 == 0 else ""


def net_key(value):
    match = MASS.fullmatch(value)
    if not match:
        return ""
    unit = match[2].lower()
    unit, scale = {
        "kg": ("g", 1000), "千克": ("g", 1000), "g": ("g", 1), "克": ("g", 1),
        "mg": ("g", Decimal(".001")), "毫克": ("g", Decimal(".001")),
        "l": ("ml", 1000), "升": ("ml", 1000), "ml": ("ml", 1), "毫升": ("ml", 1),
    }[unit]
    return (Decimal(match[1]) * scale, unit)


def roles(value):
    return {match.lastgroup for match in DATE_ROLE.finditer(value)}


def dated_targets(image_id, block):
    labels = list(DATE_ROLE.finditer(block.text))
    parts = [(label.lastgroup, block.text[label.end():labels[i + 1].start() if i + 1 < len(labels) else None])
             for i, label in enumerate(labels)] if labels else [("unknown", block.text)]
    for role, text in parts:
        match = re.search(DATE, text)
        if not match:
            continue
        raw = match.group()
        numbers = re.findall(r"\d+", raw)
        if len(numbers) == 1:
            numbers = [raw[:4], raw[4:6], raw[6:]]
        try:
            value = date(*(int(part) for part in numbers)).isoformat()
        except (ValueError, TypeError):
            continue
        yield {"ref": block.id, "page": image_id, "box": list(block.box), "role": role,
               "label": DATE_ROLES.get(role, ("日期候选", ""))[0], "value": value}


def attribution_for(fingerprints, raw, payload):
    ids = [fp["id"] for fp in fingerprints]
    snapshot = {"pages": sorted(
        [(fp["id"], page.get("digest"), page.get("packageLevel"),
          sorted((b["id"], b["text"], b["box"]) for b in page["blocks"]))
         for fp, page in zip(fingerprints, raw)],
        key=lambda item: item[0]), "revision": payload.get("identityRevision", 0)}
    token = sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    links = {page_id: set() for page_id in ids}
    conflicts, signals = [], []

    def comparable(first, second, key):
        return len(first[key]) == len(second[key]) == 1

    for index, first in enumerate(fingerprints):
        for second in fingerprints[index + 1:]:
            a, b = first["id"], second["id"]
            pair = [a, b]
            if first["level"] != second["level"] and first["level"] and second["level"]:
                signals.append({"label": "包装层级不同，请分别审核各销售包装", "images": pair, "refs": []})
                continue
            different = []
            for key in ("bar", "sc", "net", "name"):
                if not (comparable(first, second, key) and first[key].keys() != second[key].keys()):
                    continue
                # 委托方 and 受委托方 may legally carry different SC numbers
                # on the same product; that is not an automatic same-field conflict.
                if key == "sc" and distinct_sc_roles(first, second):
                    continue
                different.append(key)
            if different:
                for key in different:
                    label = {"bar": "可校验的商品条码不同", "sc": "生产许可证标示不同",
                             "net": "净含量标示不同", "name": "识别的商品名称不同"}[key]
                    entry = {"label": label, "images": pair, "field": key,
                             "refs": list(dict.fromkeys([*first[key].values(), *second[key].values()]))}
                    (signals if key == "name" else conflicts).append(entry)
                continue
            equal = {key for key in ("bar", "name", "net", "sc") if comparable(first, second, key)
                     and first[key].keys() == second[key].keys()}
            # SC and quantity are shared by many unrelated goods. Require a product identifier
            # or the combination of an explicit name, quantity and production identifier.
            if "bar" in equal or {"name", "net", "sc"} <= equal:
                links[a].add(b)
                links[b].add(a)

    reached, queue = set(), ids[:1]
    while queue:
        page_id = queue.pop()
        if page_id not in reached:
            reached.add(page_id)
            queue.extend(links[page_id] - reached)
    same_level = len({fp["level"] for fp in fingerprints if fp["level"]}) <= 1
    can_confirm = bool(ids) and not conflicts and same_level
    by = None
    if conflicts:
        status = "conflict"
    elif ids and same_level and (len(ids) == 1 or
                                (len(reached) == len(ids) and not signals and not payload.get("identityRevision"))):
        status, by = "confirmed", "single" if len(ids) == 1 else "automatic"
    elif can_confirm and payload.get("confirmation") == token:
        status, by = "confirmed", "manual"
    else:
        status = "pending"
    return {"status": status, "confirmedBy": by, "token": token,
            "canConfirm": status == "pending" and can_confirm,
            "pages": ids, "conflicts": conflicts, "signals": signals}


def page_fingerprint(blocks):
    """页内文字指纹：同一张标签图被重复上传时，两页排序后的文字完全相同。"""
    return sha256("\n".join(sorted(block.text for block in blocks)).encode()).hexdigest()


def inspect_pages(payload):
    pages = [label_blocks(blocks) for blocks in read_pages(payload)]
    raw = payload.get("pages", [])
    clear, uncertain = {key: [] for key in AREAS}, {key: [] for key in AREAS}
    fingerprints, hints, dates = [], [], []
    seen_pages, seen_fingerprints = set(), {}
    # 重复图去重：同一张标签图被重复上传时，按页内文字指纹只保留第一页，
    # 重复页声明的覆盖区域并入第一次出现的那一页。归属判定按去重后的页处理，
    # 重复上传不再需要人工确认归属。
    kept, merged_areas = [], {}
    for index, (page, blocks) in enumerate(zip(raw, pages)):
        image_id = page.get("id")
        if not isinstance(image_id, str) or not image_id:
            image_id = f"image-{index + 1}"
        if image_id in seen_pages:
            raise ValueError("图片编号重复。")
        seen_pages.add(image_id)
        fingerprint = page_fingerprint(blocks)
        if blocks and fingerprint in seen_fingerprints:
            areas = page.get("areas", [])
            if isinstance(areas, list):
                merged_areas.setdefault(seen_fingerprints[fingerprint], []).extend(areas)
            continue
        seen_fingerprints[fingerprint] = image_id
        kept.append((page, blocks, image_id))
    raw = [page for page, _, _ in kept]
    pages = [blocks for _, blocks, _ in kept]
    for page, blocks, image_id in kept:
        attested = supported_areas(blocks)
        areas = page.get("areas", [])
        if isinstance(areas, list):
            areas = [*areas, *merged_areas.get(image_id, [])]
            for area in AREAS:
                if area in areas:
                    readable = page.get("readability") == "clear" and bool(blocks)
                    if readable and area_flag_is_reliable(area, attested):
                        clear[area].append(image_id)
                    else:
                        # Flagged but unreadable, or a tick inventing an unseen panel.
                        uncertain[area].append(image_id)
        names = {compact(value): refs[0].id for key, value, refs in field_candidates(blocks)
                 if key == "name" and value and refs and "应品音称" not in refs[0].text}
        nets = {net_key(value): refs[0].id for key, value, refs in net_candidates(blocks)
                if key == "net" and net_key(value) and refs}
        codes = {code: block.id for block in blocks if (code := barcode(block.text))}
        licenses = {match[1]: block.id for block in blocks if (match := SC.search(block.text))
                    and re.match(r"^(?:食品)?生产许可证|^SC编号", block.text, re.I)}
        sc_roles = {}
        for block in blocks:
            match = SC.search(block.text)
            if not match or match[1] not in licenses:
                continue
            role = sc_legal_role(blocks, block)
            if role:
                sc_roles[match[1]] = role
        level = page.get("packageLevel")
        fingerprints.append({"id": image_id, "name": names, "net": nets, "bar": codes, "sc": licenses,
                             "sc_roles": sc_roles,
                             "level": level if level in ("outer", "inner") else ""})
        for block in blocks:
            if DATE_HINT.search(block.text):
                hints.append((image_id, block))
            if re.search(DATE, block.text):
                dates.extend(dated_targets(image_id, block))

    coverage = [{"id": key, "label": label, "status": "已覆盖" if clear[key] else
                 "不可辨／待核" if uncertain[key] else "缺图",
                 "images": list(dict.fromkeys(clear[key] + uncertain[key]))} for key, label in AREAS.items()]
    attribution = attribution_for(fingerprints, raw, payload)
    anchors = []
    if attribution["status"] != "confirmed":
        return pages, coverage, attribution, anchors
    for image_id, block in hints:
        requested = roles(block.text)
        others = [target for target in dates if target["ref"] != block.id and
                  (not requested or target["role"] in requested or target["role"] == "unknown")]
        typed = [target for target in others if target["role"] != "unknown"]
        candidates = typed or others
        distinct = {}
        for target in sorted(candidates, key=lambda item: (item["page"] == image_id, item["page"], item["ref"])):
            distinct.setdefault((target["role"], target["value"]), target)
        targets = sorted(distinct.values(), key=lambda item: (item["role"] != "production", item["role"], item["value"]))
        ambiguous = any(sum(t["role"] == role for t in targets) > 1 for role in {t["role"] for t in targets})
        missing_roles = sorted(requested - {t["role"] for t in targets}) if typed else []
        status = ("同类日期有多个候选，请核对包装和批次" if ambiguous else
                  "部分日期已读到，请核对缺少的日期类型及物理位置" if missing_roles else
                  "日期已读到 · 物理位置待确认" if targets else "未找到可用喷码，需补充喷码方案或实物照片")
        anchors.append({"label": block.text, "sourceRef": block.id, "sourcePage": image_id,
                        "targetRef": targets[0]["ref"] if len(targets) == 1 else None, "targets": targets,
                        "missingRoles": missing_roles, "dateStatus": "ambiguous" if ambiguous else
                        "partial" if missing_roles else "read" if targets else "missing",
                        "positionStatus": "unverified", "status": status})
    return pages, coverage, attribution, anchors
