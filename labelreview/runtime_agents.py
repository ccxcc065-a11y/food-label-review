"""Opt-in three-role suggestions. Never mutate audit facts or publish decisions."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace
import urllib.request

from labelreview.audit import audit
from labelreview.extract import read_pages
from labelreview.knowledge import clause as clause_text, clause_tokens, document_metadata, resolve_document
import labelreview.audit_retrieval as audit_retrieval
import labelreview.decision_set as decision_set
ROOT = Path(__file__).resolve().parent.parent
ROLES = (('review', '审核'), ('write', '撰稿'), ('verify', '复核'))


STAGE_TIMEOUT = 150  # 撰稿等阶段的默认超时；任一阶段失败不影响已完成阶段
# 03:51 official five attempt 5: ai-review atMs=245661.2, just over the previous
# 240s cap, then "The read operation timed out". 300s leaves ~54s margin above
# that measured duration without leaving a hung call to burn quota for many minutes.
# No automatic review retry: a near-limit read timeout needs a higher cap, not a
# second full call. result-unknown is not exactly-once; a retry would double the
# worst-case quota. Truncation / unknown-after-response / content rejection are
# never retried here.
REVIEW_TIMEOUT = 300
REVIEW_RETRY_CAP = 0
_USAGE = []


class ModelResultUnknown(Exception):
    """A model HTTP request was issued; the provider result is unknown.

    Timeouts and disconnects after the request is sent cannot be treated as
    exactly-once success or as a clean 'never called'.
    """

    def __init__(self, message, timeout_seconds=None, timeout_class=None, timeout_kind=None):
        super().__init__(message)
        self.timeout_seconds = timeout_seconds
        self.timeout_class = timeout_class
        self.timeout_kind = timeout_kind


def _timeout_class_for(timeout):
    """Name the cap that was actually passed to urlopen."""
    applied = STAGE_TIMEOUT if timeout is None else timeout
    if applied == REVIEW_TIMEOUT:
        return applied, "REVIEW_TIMEOUT"
    if applied == STAGE_TIMEOUT:
        return applied, "STAGE_TIMEOUT"
    return applied, "CUSTOM"


def _is_transport_read_timeout(exc):
    """True for a urlopen/read timeout, not for truncation or a parse error."""
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, TimeoutError):
        return True
    text = ("%s %s" % (exc, reason or "")).lower()
    return "timed out" in text or "timeout" in text


def review_prompt_composition(data, api_prompt_tokens=None):
    """Per-section character counts of the user message actually sent to the model.

    Counts use the same json.dumps(data, ensure_ascii=False) path as _call.
    Scaled token figures, when present, are proportional estimates and must not
    be treated as provider-returned token counts. Raw user text and secrets
    are not stored. Sections are not dropped to save tokens.
    """
    data = data if isinstance(data, dict) else {}
    user = json.dumps(data, ensure_ascii=False)
    section_chars = {str(key): len(json.dumps(value, ensure_ascii=False))
                     for key, value in data.items()}
    record = {
        "userChars": len(user),
        "sectionChars": section_chars,
        "sectionsSent": [str(key) for key in data.keys()],
        "jsonWrapperChars": len(user) - sum(section_chars.values()),
        "note": ("sectionChars are character counts of json.dumps(value, "
                 "ensure_ascii=False) for each user-message key. "
                 "scaledTokenEstimates are proportional estimates, not API tokens."),
    }
    if isinstance(api_prompt_tokens, int) and api_prompt_tokens > 0 and user:
        record["apiPromptTokens"] = api_prompt_tokens
        record["scaledTokenEstimates"] = {
            key: int(round(api_prompt_tokens * chars / len(user)))
            for key, chars in section_chars.items()
        }
        record["scaledTokenEstimatesNote"] = (
            "Estimates only: apiPromptTokens * sectionChars / userChars; "
            "not provider token counts.")
    return record


def _review_call_diagnostics(exc=None, truncated=False):
    """Record the review timeout that actually applied (REVIEW_TIMEOUT, not STAGE_TIMEOUT)."""
    diagnostics = {
        "timeoutSeconds": REVIEW_TIMEOUT,
        "timeoutClass": "REVIEW_TIMEOUT",
        "stageTimeoutSeconds": STAGE_TIMEOUT,
        "reviewAttempts": 1,
        "retryCap": REVIEW_RETRY_CAP,
        "retry": None,
    }
    if truncated:
        diagnostics["failureClass"] = "truncation"
    elif exc is not None:
        diagnostics["timeoutClass"] = getattr(exc, "timeout_class", None) or "REVIEW_TIMEOUT"
        if getattr(exc, "timeout_seconds", None) is not None:
            diagnostics["timeoutSeconds"] = exc.timeout_seconds
        kind = getattr(exc, "timeout_kind", None)
        if not kind and _is_transport_read_timeout(exc):
            kind = "transport-read"
        diagnostics["timeoutKind"] = kind
        diagnostics["timeoutError"] = str(exc)[:200]
        diagnostics["failureClass"] = kind or "result-unknown"
    return diagnostics


def _api_prompt_tokens(usage_rows):
    for row in usage_rows or []:
        usage = row.get("usage") if isinstance(row, dict) and isinstance(row.get("usage"), dict) else row
        if isinstance(usage, dict) and isinstance(usage.get("prompt"), int):
            return usage.get("prompt")
    return None

def _sources_of(row):
    """遍历一条审核条目携带的全部来源（source / sources / guidanceSources，含 related）。"""
    for key in ("source", "sources", "guidanceSources"):
        value = row.get(key)
        items = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
        for source in items:
            yield from _walk_source(source)


def _walk_source(source):
    if not isinstance(source, dict):
        return
    yield source
    for related in source.get("related") or []:
        yield from _walk_source(related)


DECISIONS = [
    {"id": "version-2025", "text": "统一按 GB 7718-2025、GB 28050-2025 审核；禁止比较 2011 版，禁止把结论写成预检。"},
    {"id": "allergen-bold", "text": "配料表内对复合配料名加粗，视为已作致敏物质提示，符合附录 D.2.1，判 pass；不再列为待核实。"},
    {"id": "irradiate-near", "text": "辐照信息位于配料表下方相邻行内（约 3 个块高、0.06 版心内），视为「配料表临近位置」，符合 4.15.2。"},
    {"id": "date-no-pad", "text": "日期月份不补零（如 2026-9-15）不构成问题；4.7.1 只要求年月日顺序，未要求两位数字。"},
    {"id": "grade-whitelist", "text": "质量等级只对 knowledge/grades.json 清单内的执行标准下结论；清单外的标准不判，不得推定其是否分级。"},
    {"id": "marketing-compliant", "text": "「匠心」「好味道」「Q弹」「10分钟」这类营销与感官用语判为合规并说明理由，不判违规。"},
    {"id": "absolute-hint-only", "text": "绝对化用语只提示不判定；不得据此把结论升级为红线或规范性。"},
    {"id": "unread-not-problem", "text": "图上看不清、多次重读仍不一致的位置，只能列为待核实，不得进入规范性章节。"},
    {"id": "artwork-no-date", "text": "设计稿（刀版图）上没有实际生产日期属正常状态，不因此判问题。"},
    {"id": "coverage-thin", "text": "小作坊、进口类产品本次未覆盖专项规则，如实声明，不得用通用条款凑篇幅或推定结论。"},
    {"id": "agent-candidate-only", "text": "AI 角色的任何输出都是候选建议，不改变判定、不进统计卡。"},
    {"id": "draft-hold", "text": "声称与配料表不支撑时按既定结论判红线并暂停付印；不得以「可能是风味描述」为由降级——"
                                 "问答第三十四豁免的只是定量标示义务，不豁免 3.4 的真实性要求。"},
]


def _decisions():
    """本系统已定的口径。模型不得质疑或推翻，只能据此核对。"""
    return list(DECISIONS)


def _library_context(report):
    """把审核库喂给模型：条款原文（并入 sources）、检查点清单、条款覆盖状态。"""
    clauses, checkpoints, coverage = {}, [], []
    for row in report.get("items", []):
        if not isinstance(row, dict):
            continue
        for source in _sources_of(row):
            document = source.get("documentId")
            if not document or not resolve_document(document):
                continue
            name = (document_metadata(document) or {}).get("name") or document
            for number in clause_tokens(source.get("clause")):
                if (document, number) in clauses:
                    continue
                record = clause_text(document, number)
                if not record:
                    continue
                token = document + ":" + number
                text = record["text"]
                clauses[(document, number)] = {"id": token, "name": name, "clause": number,
                                               "text": text[:600] + ("…（后略）" if len(text) > 600 else "")}
    try:
        from labelreview.notes import definitions as note_definitions
        catalogue, _ = note_definitions()
        fired = {note.get("id") for note in report.get("reviewNotes") or [] if isinstance(note, dict)}
        for key, entry in sorted(catalogue.items(), key=lambda pair: pair[1].get("order", 99)):
            if isinstance(entry, dict):
                variants = entry.get("templates") if isinstance(entry.get("templates"), dict) else {}
                body = " ".join(str(v) for v in variants.values() if isinstance(v, str))
                checkpoints.append({"id": key, "title": entry.get("title"),
                                    "clause": entry.get("clause"), "trigger": entry.get("trigger"),
                                    "text": body[:700] + ("…（后略）" if len(body) > 700 else ""),
                                    "fired": key in fired})
    except Exception:
        checkpoints = []
    try:
        loaded = json.loads((ROOT / "knowledge" / "coverage.json").read_text())
        for number, row in sorted((loaded.get("clauses") or {}).items()):
            if isinstance(row, dict):
                coverage.append({"clause": number, "title": row.get("title"), "rule": row.get("rule"),
                                 "status": row.get("status"), "note": row.get("note")})
    except (OSError, ValueError):
        coverage = []
    return clauses, checkpoints, coverage


def _system(role, report):
    system = (ROOT / 'agents' / (role + '.md')).read_text()
    if report.get('version') == '2025':
        system += '\n标签审核统一依据指定库中的 GB 7718-2025、GB 28050-2025。按此口径表达结论，禁止比较旧版、切换版本或将结论描述为预检。'
    return system


def _guidance(role, checkpoints):
    """三个角色的追加约束：审核只找、撰稿只写、复核只删。"""
    if role == 'review':
        return ('\n本次审核已有 %d 个确定性检查点（见 checkpoints，含判定条件与文案）、'
                '条款覆盖表（见 coverage）与已定口径（见 decisions）。\n'
                'decisions 中已明确的口径不得质疑、不得建议推翻；checkpoints 中 fired 为 true 的检查点，其判断已由本地规则完成，只核对不重判。\n'
                '你的任务只有三项：\n'
                '① 核对：逐条检查 items 的事实陈述是否与 ocr 原文一致、所引条款是否与 sources 原文对得上；\n'
                '② 矛盾：两条规则对同一事实给出不同结论；\n'
                '③ 补漏：coverage 中 status 为 missing 的条款对应的、本标签可能存在的问题。\n'
                '不要提出「判定路径可以另走」这类不改变结论的讨论，也不要建议降低已定结论的等级。\n'
                '找不到就返回空的 notes 数组，不要为了凑数而写；detail 控制在 600 字以内，只写哪里不对、为什么不对。\n'
                '所有 itemIds / evidenceIds / sourceIds 必须来自给定数据，不得虚构。' % len(checkpoints))
    if role == 'write':
        return ('\n输入 previousNotes 是审核角色的发现。每条按「看到了什么 → 依据哪条 → 具体怎么改」三段写；'
                '只能加工已有发现，不得新增问题，条数不得超过 previousNotes 的条数；'
            '每条候选的 itemIds 组合必须与 previousNotes 中某一条完全相同（可以改标题和措辞，不能换目标事项）；'
                'previousNotes 为空时返回空数组；detail 控制在 900 字以内。\n'
                '所有 itemIds / evidenceIds / sourceIds 必须来自给定数据，不得虚构。')
    return ('\n输入 previousNotes 是撰稿角色的候选。逐条回查三件事：'
            '① 读错（数字、配料名、日期与 ocr 原文是否一致）；'
            '② 引错（条款号与 sources 里的原文是否对得上）；'
            '③ 把待核实写成确定问题（条目仍是 review，文案却写成肯定结论）。'
            '只能删不能加，条数不得超过输入；每条输出的 itemIds 组合必须在输入中存在且不得重复占用同一目标；全部不合格返回空数组。\n'
            '所有 itemIds / evidenceIds / sourceIds 必须来自给定数据，不得虚构。')


_RAW = []
_PROVIDER = []


def _call(role, system, data, key, model, sink=None, timeout=None, raw_sink=None,
         timeout_class=None):
    applied, inferred_class = _timeout_class_for(timeout)
    timeout_class = timeout_class or inferred_class
    request = urllib.request.Request('https://api.deepseek.com/chat/completions',
        data=json.dumps({'model': model, 'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}],
            'max_tokens': 32000, 'response_format': {'type': 'json_object'}}, ensure_ascii=False).encode(),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=applied) as response:
            raw = response.read(400001)
            headers = {name.lower(): value for name, value in response.headers.items()}
    except Exception as exc:
        # The request object was built and urlopen was entered; treat
        # timeout / transport failure as result-unknown, not a clean miss.
        if isinstance(exc, (TimeoutError, urllib.error.URLError, OSError)):
            kind = "transport-read" if _is_transport_read_timeout(exc) else "transport"
            raise ModelResultUnknown(str(exc) or exc.__class__.__name__,
                                     timeout_seconds=applied, timeout_class=timeout_class,
                                     timeout_kind=kind) from exc
        raise
    if len(raw) > 400000:
        raise ValueError('模型响应过长')
    payload = json.loads(raw)
    _PROVIDER.append({"role": role, "model": payload.get("model") or model,
                      "requestId": next((headers[name] for name in
                                         ("x-request-id", "request-id", "x-ds-request-id", "trace-id")
                                         if headers.get(name)), ""),
                      "headers": {name: headers.get(name) for name in ("x-request-id", "request-id")
                                  if headers.get(name)}})
    choice = (payload.get('choices') or [{}])[0] if isinstance(payload, dict) else {}
    message = choice.get('message') if isinstance(choice, dict) else {}
    content = str((message or {}).get('content') or '')
    finish_reason = str(choice.get('finish_reason') or '')
    _RAW.append({"role": role, "content": content[:8000], "finishReason": finish_reason})
    if raw_sink is not None:
        # Per-request copy of the model text, taken before the truncation
        # check so a failed writer call can still be diagnosed afterwards.
        raw_sink.append({"role": role, "content": content[:20000], "chars": len(content),
                         "finishReason": finish_reason})
    usage = payload.get('usage') if isinstance(payload.get('usage'), dict) else {}
    usage_row = {'role': role, 'prompt': usage.get('prompt_tokens'),
                 'completion': usage.get('completion_tokens'), 'total': usage.get('total_tokens')}
    _USAGE.append(usage_row)
    if sink is not None:
        sink.append({"role": role, "usage": usage_row, "finishReason": finish_reason,
                     "provider": _PROVIDER[-1] if _PROVIDER else {}, "model": payload.get("model") or model})
    if finish_reason == 'length' or (
            isinstance(usage_row.get('completion'), int) and usage_row['completion'] >= 32000):
        raise ValueError('模型输出被截断（达到输出上限），结果不可用')
    return content


def _repair_notes(text, marker='"notes"'):
    """模型输出被截断（JSON 不完整）时，逐条提取已经写完的 note 对象。"""
    if not isinstance(text, str):
        return None
    body = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M)
    found = body.find(marker)
    if found < 0:
        return None
    start = body.find('[', found)
    if start < 0:
        return None
    notes, index, size = [], start + 1, len(body)
    while index < size:
        while index < size and body[index] in ' \t\r\n,':
            index += 1
        if index >= size or body[index] != '{':
            break
        depth, in_string, escape, cursor = 0, False, False, index
        while cursor < size:
            char = body[cursor]
            if in_string:
                if escape:
                    escape = False
                elif char == '\\':
                    escape = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        if cursor >= size:
            break
        try:
            notes.append(json.loads(body[index:cursor + 1]))
        except ValueError:
            break
        index = cursor + 1
    return {'notes': notes}


def _targets(note):
    """候选的稳定身份 = 它指向的审核条目集合。标题可以被改写，目标事项不能。"""
    return tuple(sorted(note.get('itemIds') or []))


def _validate(value, ids, refs, sources, limit=None, previous=None):
    if isinstance(value, str):
        if len(value) > 18000:
            raise ValueError('候选输出过长')
        try:
            value = json.loads(value)
        except ValueError:
            value = _repair_notes(value)
            if not value:
                raise ValueError('候选输出格式无效')
    if not isinstance(value, dict) or set(value) != {'notes'} or not isinstance(value['notes'], list) or len(value['notes']) > 12:
        raise ValueError('候选输出格式无效')
    for note in value['notes']:
        if not isinstance(note, dict) or set(note) != {'title', 'detail', 'itemIds', 'evidenceIds', 'sourceIds'}:
            raise ValueError('候选条目格式无效')
        for field, cap in [('title', 120), ('detail', 1800)]:
            text = note[field]
            if not isinstance(text, str) or not text.strip() or len(text) > cap or re.search(r'https?://|file://|/Users/|/etc/|<[^>]*>', text):
                raise ValueError('候选包含无效文字或外部来源')
        for field, allowed in [('itemIds', ids), ('evidenceIds', refs), ('sourceIds', sources)]:
            if not isinstance(note[field], list) or len(note[field]) > 30 or any(not isinstance(x, str) or x not in allowed for x in note[field]):
                raise ValueError('候选引用不存在')
        if not note['itemIds']:
            raise ValueError('候选必须关联已有审核条目')
    if limit is not None and len(value['notes']) > limit:
        raise ValueError('候选条数超过上一环节')
    if previous is not None:
        # 只比条数不够：模型可以换掉一条，只要数量不变就能通过。
        # 要求每条候选的目标事项必须来自上一环节，且不得重复占用同一目标。
        allowed = {_targets(note) for note in previous}
        seen = set()
        for note in value['notes']:
            key = _targets(note)
            if key not in allowed:
                raise ValueError('候选指向了上一环节没有的目标事项，视为新增')
            if key in seen:
                raise ValueError('同一目标事项出现重复候选')
            seen.add(key)
    if len(json.dumps(value, ensure_ascii=False)) > 18000:
        raise ValueError('候选输出过长')
    return value['notes']


def run_agents(payload, call=None):
    """三个角色是一条流水线：质检员只找问题，撰稿人只加工，审校只删。

    call injection is for offline tests; transport receives no raw payload.
    """
    result = {'notes': [], 'stages': [], 'status': 'unavailable', 'candidateOnly': True}
    local_usage = []

    def invoke(role, system, data, key, model):
        if call is not None:
            return call(role, system, data, key, model)
        return _call(role, system, data, key, model, sink=local_usage)
    if payload.get('sendCloud') is not True:
        result['message'] = '未授权发送文字，未调用模型。'
        return result
    key, model = (os.getenv(name, '').strip() for name in ('LABEL_REVIEW_API_KEY', 'LABEL_REVIEW_MODEL'))
    if not key or not model:
        result['message'] = '须明确配置模型及密钥；当前仅本地规则，未执行 AI 审核。'
        return result
    try:
        report = audit(payload)
        if report.get('blocked') or not report.get('canPublish'):
            result['message'] = '图片归属或范围尚未确认，未发送模型。'
            return result
        # 每条发现必须自带「实际引用了哪些条款」：否则模型只能从共用的 sources 里猜，
        # 会把「知识库里有 4.1」误当成「这条引用了 4.1」（V2.4 修复的误报根因）。
        items = [{k: row[k] for k in ('id', 'title', 'detail', 'action', 'outcome', 'track', 'refs') if k in row}
                 for row in report.get('items', [])]
        for row, item in zip(report.get('items', []), items):
            cited = []
            for source in _sources_of(row):
                cited.extend(clause_tokens(source.get('clause')))
            requirement = (row.get('analysis') or {}).get('requirement')
            for line in (requirement if isinstance(requirement, list) else [requirement] if requirement else []):
                cited.extend(clause_tokens(str(line)))
            item['citedClauses'] = list(dict.fromkeys(cited))
        ids = {row['id'] for row in items}
        blocks = [{'id': b.id, 'text': b.text} for page in read_pages(payload) for b in page]
        refs = {b['id'] for b in blocks}
        # 引用一律落到逐条条款原文（见 _library_context），不再塞整页段落。
        citations = {}
        fields = [{k: row[k] for k in ('key', 'label', 'value', 'status') if k in row}
                  for row in report.get('fields', [])]
        clauses, checkpoints, coverage = _library_context(report)
        for entry in clauses.values():
            citations.setdefault(entry['id'], entry)
        shared = {'ocr': blocks, 'items': items, 'sources': list(citations.values()),
                  'version': report.get('version'), 'decisions': _decisions()}
        # 检查点清单、覆盖表与已定口径只给质检员：撰稿人、审校不需要重复读库。
        library = dict(shared, fields=fields, checkpoints=checkpoints, coverage=coverage)
        if len(json.dumps(library, ensure_ascii=False)) > 90000:
            raise ValueError('文字资料过长，请缩小本次审核范围')
    except Exception:
        result.update(status='failed', notes=[], usage=list(local_usage),
                      message='文字资料未能准备完成；本地审核结果未改变。')
        return result

    notes, previous, status = [], [], 'completed'
    for index, (role, label) in enumerate(ROLES):
        stage = {'role': role, 'label': label, 'status': 'running'}
        result['stages'].append(stage)
        data = dict(library) if role == 'review' else dict(shared)
        data['previousNotes'] = previous
        limit = len(previous) if role in ('write', 'verify') else None
        before = len(local_usage)
        try:
            system = _system(role, report) + _guidance(role, checkpoints)
            if role == 'review':
                system += '\n条款原文已并入 sources：id 形如「文档:条款号」的条目就是逐条原文，可直接作为 sourceIds 引用。'
            produced = _validate(invoke(role, system, data, key, model), ids, refs,
                                 set(citations), limit, previous if role in ('write', 'verify') else None)
        except Exception:
            stage['status'] = 'failed'
            status = 'failed' if index == 0 else 'partial'
            break
        if len(local_usage) > before:
            stage['usage'] = local_usage[before:]
            stage['usage'] = [row.get('usage') for row in stage['usage']]
        stage['status'] = 'completed'
        stage['count'] = len(produced)
        notes, previous = produced, produced
        if role == 'review' and not produced:
            # 质检员没找到新问题：短路，不跑撰稿和复核，省 2/3 token。
            result.update(notes=[], status='completed', usage=local_usage,
                          message='质检角色未发现新问题，未运行撰稿与复核；本地审核结果未变。')
            return result
    if status == 'completed':
        message = '三个角色已完成；仅为候选建议，未修改事实或判定。'
    elif status == 'partial':
        done = [stage['label'] for stage in result['stages'] if stage['status'] == 'completed']
        message = ('已保留' + '、'.join(done) + '阶段结果；后续角色未返回可验证内容。'
                   '仅为候选建议，本地审核结果未改变。')
    else:
        message = '模型未返回可验证的完整候选；可重试，本地审核结果未改变。'
    result.update(notes=notes, status=status, usage=local_usage, message=message)
    return result


# ---------------------------------------------------------------- 整改要求（V2.7 阶段 3：文档级条款绑定 + 服务端重建输入）
REMEDIATION_DOCUMENTS = ("gb7718-2025", "gb28050-2025", "gb7718-2025-qa", "gb28050-2025-qa",
                         "food-safety-law", "advertising-law", "import-food", "metrology",
                         "hebei-workshop", "samr100")
PROMISE = re.compile(r"已放行|无风险|符合要求|可以上市|无问题|无需整改|不影响销售")
FORBIDDEN = re.compile(r"https?://|file://|/Users/|/etc/|<[^>]*>")
# 方案 4.3 第 5 条：模型不得凭空新增义务；这些说法在来源中没有依据时整条拒绝。
UNSUPPORTED = re.compile(r"(?:所有|全部|一律)[^。；;]{0,12}(?:必须|应当|须|统一)|字号[^。；;]{0,8}(?:必须|一律)|必须(?:统一|加粗|使用)|统一(?:字体|字号|字重)|一律加粗")
NUMBER = re.compile(r"\d+(?:[./-]\d+)*")
TRIPLE_KEYS = {"documentId", "edition", "clause", "textHash"}
REMEDIATION_ENTRY_KEYS = {"snapshotId", "planVersion", "itemId", "actionId", "clauseRefs", "explanation"}
STRUCTURE_REASON = "结构无效（V2.8 只接受 explanation 自由解释）"
# Input fields the writer sometimes copies back verbatim. They are ignored by
# the composer (facts are injected by the program), so an exact echo is
# dropped; a changed echo is still a structure rejection.
REMEDIATION_ECHO_KEYS = ("title", "detail", "edit", "evidence")
WRITER_RAW_EXCERPT = 6000
WRITER_RETRY_TIMEOUT = 75   # seconds; keeps review + writer + one retry inside the 300 s client budget


def _hash_text(text):
    return hashlib.sha256(str(text or "").encode()).hexdigest()[:16]


def clause_pool(refs, edition="2025"):
    """(documentId, edition, clause) → 条款原文与正文摘要。

    GB 7718-2025 的 4.1 与 GB 28050-2025 的 4.1 是两条不同条款，键必须带文档与版本。
    refs 接受 {"documentId","clause"[,"edition"]} 或 (documentId, clause) 二元组。
    """
    pool = {}
    for ref in refs or []:
        if isinstance(ref, (tuple, list)) and len(ref) >= 2:
            document, clause = ref[0], ref[1]
            ref_edition = edition
        elif isinstance(ref, dict):
            document, clause = ref.get("documentId"), ref.get("clause")
            ref_edition = str(ref.get("edition") or edition)
        else:
            continue
        if not isinstance(document, str) or not isinstance(clause, str) or not document or not clause:
            continue
        if not resolve_document(document):
            continue
        key = (document, ref_edition, clause)
        if key in pool:
            continue
        record = clause_text(document, clause)
        if not record:
            continue
        pool[key] = {"text": record["text"], "textHash": _hash_text(record["text"])}
    return pool


def _clause_pool(refs, edition="2025"):
    """兼容旧调用名；语义见 clause_pool。"""
    return clause_pool(refs, edition)


def _refs_of(item):
    """条目允许引用的条款三元组；只认计划里的 clauseRefs（已带文档、版本、摘要）。"""
    refs = []
    for ref in item.get("clauseRefs") or []:
        if not isinstance(ref, dict) or set(ref) - {"documentId", "edition", "clause", "textHash"}:
            continue
        if not ref.get("documentId") or not ref.get("clause"):
            continue
        refs.append({"documentId": ref["documentId"], "edition": str(ref.get("edition") or "2025"),
                     "clause": ref["clause"], "textHash": ref.get("textHash")})
    return refs


def _triple(ref):
    # 模型可能把输入的 textHash 一并回填，允许但只取三元组；其余键判为结构无效。
    if not isinstance(ref, dict) or not set(ref) <= TRIPLE_KEYS:
        return None
    document, edition, clause = ref.get("documentId"), ref.get("edition"), ref.get("clause")
    if not all(isinstance(value, str) and value for value in (document, edition, clause)):
        return None
    return document, edition, clause



def _unexpected_numbers(text, item):
    """Flag rewritten identifiers, not incidental counts such as “5 家被委托商”.

    A short integer in the writer's explanation is only a defect when the
    planned before/after text itself contains numbers (so a net-content or
    licence rewrite can still be caught). Identifier-like tokens (licence
    numbers, years, dotted clause ids, SC… codes) are always checked.
    """
    allowed = _allowed_numbers(item)
    found = set(NUMBER.findall(text))
    extra = found - allowed
    if not extra:
        return set()
    planned = " ".join([
        str((item.get("edit") or {}).get("before") or ""),
        str((item.get("edit") or {}).get("after") or ""),
    ])
    planned_has_numbers = bool(NUMBER.findall(planned))
    significant = set()
    for token in extra:
        if re.fullmatch(r"\d{1,3}", token) and not planned_has_numbers:
            continue
        significant.add(token)
    return significant


def _allowed_numbers(item):
    """程序认定的数字集合：整改动作前后的原文、条目事实、证据块、条款原文。"""
    chunks = [str((item.get("edit") or {}).get("before") or ""),
              str((item.get("edit") or {}).get("after") or ""),
              str(item.get("detail") or ""), str(item.get("title") or "")]
    chunks.extend(str(text) for text in item.get("evidence") or [])
    for ref in item.get("clauseRefs") or []:
        chunks.append(str((item.get("clauseTexts") or {}).get(_triple_string(ref)) or ""))
        chunks.append(str(ref.get("clause") or ""))
        chunks.append(str(ref.get("edition") or ""))
        # documentId such as gb7718-2025 must allow a writer to cite 「GB 7718-2025」.
        chunks.append(str(ref.get("documentId") or ""))
    return {match for chunk in chunks for match in NUMBER.findall(chunk)}


def _triple_string(ref):
    if not isinstance(ref, dict):
        return ""
    document, edition, clause = ref.get("documentId"), ref.get("edition"), ref.get("clause")
    return "%s@%s %s" % (document, edition, clause) if all((document, edition, clause)) else ""


def _remediation_reason(entry, item, pool, snapshot=None, plan_version=None):
    """契约校验；返回拒绝原因（空字符串表示通过）。顺序与方案 4.3 的七条一致。"""
    if not isinstance(entry, dict):
        return STRUCTURE_REASON + "：条目不是对象"
    if set(entry) != REMEDIATION_ENTRY_KEYS:
        missing = sorted(REMEDIATION_ENTRY_KEYS - set(entry))
        extra = sorted(str(key)[:40] for key in set(entry) - REMEDIATION_ENTRY_KEYS)
        detail = "；".join(part for part in (
            ("缺少 " + "、".join(missing)) if missing else "",
            ("多出 " + "、".join(extra[:8])) if extra else "") if part)
        return STRUCTURE_REASON + ("：" + detail if detail else "")
    if snapshot is not None and entry.get("snapshotId") != snapshot:
        return "快照与本次不一致（过期结果）"
    if plan_version is not None and entry.get("planVersion") != plan_version:
        return "planVersion 与当前计划不一致（旧撰稿）"
    if entry.get("itemId") != item.get("itemId"):
        return "itemId 不属于本次条目"
    if entry.get("actionId") != item.get("actionId"):
        return "actionId 与该条目不匹配"
    refs = entry.get("clauseRefs")
    if not isinstance(refs, list):
        return "clauseRefs 不是数组"
    allowed = {(ref["documentId"], ref["edition"], ref["clause"]) for ref in _refs_of(item)}
    for ref in refs:
        triple = _triple(ref)
        if triple is None:
            return "clauseRefs 结构无效"
        if triple not in allowed:
            return "引用了该条没有的条款"
        record = pool.get(triple)
        if not record:
            return "条款原文取不到"
        plan_ref = next(ref for ref in _refs_of(item)
                        if (ref["documentId"], ref["edition"], ref["clause"]) == triple)
        if plan_ref.get("textHash") and plan_ref["textHash"] != record["textHash"]:
            return "条款正文摘要不匹配"
    text = entry.get("explanation")
    if not isinstance(text, str):
        return "explanation 不是字符串"
    if not 40 <= len(text) <= 600:
        return "explanation 长度不在 40–600 字"
    if FORBIDDEN.search(text):
        return "含网址、路径或 HTML"
    if PROMISE.search(text):
        return "含承诺性表述"
    if UNSUPPORTED.search(text):
        return "新增了来源未支持的义务"
    extra = _unexpected_numbers(text, item)
    if extra:
        return "数字或编号与整改计划不一致：" + "、".join(sorted(extra))
    return ""


def verify_remediation(entry, item, pool, snapshot=None):
    """校验一条 AI 整改要求；任何一条不满足都不采用，回退模板文案。"""
    return _remediation_reason(entry, item, pool, snapshot) == ""


def _remediation_guidance():
    return ("\n本次只写「整改要求」：读 clauses 的条款原文与 items 的事实，给每条写一段生产方能直接执行的整改方法。\n"
            "要求：\n"
            "① 只写怎么改、改成什么、改完怎么复核；不复述问题、不改结论等级、不新增问题；\n"
            "② 每条只能引用该条 items[i].clauseRefs 里的（documentId, edition, clause）三元组，不得引用其他条款；"
            "该条 clauseRefs 为空（标签内对照）时 clauseRefs 返回空数组；\n"
            "③ explanation 40–600 字，只写自由解释（为什么这样改、改完怎么复核）；事实、依据、数字、条款与替换文本由程序注入，不得改写；不要写「建议核实」「另行确认」这类空话；\n"
            "④ 不得出现「已放行」「无风险」「符合要求」「可以上市」等承诺，也不得写网址、路径或 HTML；\n"
            "⑤ 不得新增来源未支持的义务（例如「所有字号必须一致」）；数字、单位、许可证编号必须与 items 一致，不得改写；\n"
            "⑥ snapshotId、planVersion、itemId、actionId 必须原样回填，不得虚构；\n"
            "⑦ 严格 JSON：{\"remediations\":[{\"snapshotId\":\"...\",\"planVersion\":\"...\",\"itemId\":\"...\","
            "\"actionId\":\"...\",\"clauseRefs\":[{\"documentId\":\"...\",\"edition\":\"...\",\"clause\":\"...\"}],"
            "\"explanation\":\"...\"}]}\n"
            "⑧ 每条恰好这 6 个键：snapshotId、planVersion、itemId、actionId、clauseRefs、explanation；"
            "自由解释只放在 explanation（字符串），不要用 text、content、remediation 等其他键名；"
            "不要回填 title、detail、edit、evidence；每个 actionId 只输出一条，clauseRefs 内同一条款只写一次。")


def _checkpoint_brief():
    """只给整改需要的检查点摘要：id、标题、条款。"""
    try:
        from labelreview.notes import definitions as note_definitions
        catalogue, _ = note_definitions()
        return [{"id": key, "title": entry.get("title"), "clause": entry.get("clause")}
                for key, entry in sorted(catalogue.items(), key=lambda pair: pair[1].get("order", 99))
                if isinstance(entry, dict)]
    except Exception:
        return []


def _bundle_of(payload_or_bundle):
    """D1：优先使用 report_api 已构建的 AuditBundle；旧调用（直接传 payload）回退为单次构建。

    注意：本函数是唯一允许 audit() 的入口；两个模型输入构建函数不得各自再审。
    """
    if hasattr(payload_or_bundle, "report") and hasattr(payload_or_bundle, "plan"):
        return payload_or_bundle
    report = audit(payload_or_bundle)
    return SimpleNamespace(payload=payload_or_bundle, report=report,
                           plan=report.get("reportPlan") or {},
                           context=payload_or_bundle.get("context") or {},
                           pages=payload_or_bundle.get("pages") or [])


def _remediation_inputs(payload_or_bundle):
    """服务端只读 AuditBundle：取本轮计划，只把 confirmed 动作交给模型。"""
    bundle = _bundle_of(payload_or_bundle)
    report, payload = bundle.report, bundle.payload
    if report.get("blocked") or not report.get("canPublish"):
        return None, report, "图片归属或范围尚未确认，未发送模型。"
    plan = report.get("reportPlan")
    if not isinstance(plan, dict) or not plan.get("items"):
        return None, report, "整改计划未生成，未发送模型。"
    items = [item for item in plan["items"] if item.get("decisionState") == "confirmed" and item.get("edit")]
    if not items:
        return None, report, "本次没有需要 AI 撰写整改要求的已确认动作。"
    return plan, report, ""



def _writer_decision_set(payload_or_bundle, plan, report):
    """Reuse the task DecisionSet when the report path already created one."""
    bundle = payload_or_bundle if hasattr(payload_or_bundle, "payload") else None
    existing = getattr(bundle, "decisionSet", None) if bundle is not None else None
    if isinstance(existing, decision_set.DecisionSet):
        decision_set.seed_from_report(existing, report)
        return existing
    context = {}
    if bundle is not None and isinstance(getattr(bundle, "context", None), dict):
        context = bundle.context
    elif isinstance(payload_or_bundle, dict) and isinstance(payload_or_bundle.get("context"), dict):
        context = payload_or_bundle.get("context") or {}
    timer = getattr(bundle, "timer", None) if bundle is not None else None
    task_id = ""
    if timer is not None:
        task_id = str(getattr(timer, "taskId", "") or "")
    task_id = task_id or str(context.get("_decisionTaskId") or context.get("taskId") or "")
    pins = {
        "snapshot_id": str((plan or {}).get("snapshotId") or (report or {}).get("snapshotId") or context.get("snapshotId") or ""),
        "audit_profile_id": str(context.get("auditProfileId") or ""),
        "rules_version": str((context.get("versions") or {}).get("rulesVersion") or ""),
        "knowledge_version": str((context.get("versions") or {}).get("knowledgeVersion") or ""),
        "facts_digest": str(context.get("factsDigest") or (report or {}).get("factsDigest") or ""),
        "registered_exceptions": context.get("registeredExceptions") or [],
    }
    decisions = decision_set.get_or_create_for_task(task_id, **pins)
    decision_set.seed_from_report(decisions, report)
    if bundle is not None:
        bundle.decisionSet = decisions
    return decisions


def build_remediation(payload_or_bundle, call=None, recorder=None):
    """撰稿：只读当前 planVersion 的已确认动作，程序校验计划版本、条款、数字与结构。"""
    result = {"remediations": {}, "accepted": 0, "rejected": [], "status": "unavailable",
              "stages": [], "candidateOnly": True, "usage": [], "seconds": 0.0, "snapshotId": None,
              "planVersion": None}
    local_usage = []
    key, model = (os.getenv(name, '').strip() for name in ('LABEL_REVIEW_API_KEY', 'LABEL_REVIEW_MODEL'))
    if not key or not model:
        result['message'] = '须明确配置模型及密钥；本轮不生成 AI 整改要求，报告使用模板文案。'
        return result
    started = time.time()
    plan, report, reason = _remediation_inputs(payload_or_bundle)
    payload = getattr(payload_or_bundle, "payload", payload_or_bundle)
    if plan is None:
        result.update(status="failed" if "计划" in reason else "unavailable", seconds=round(time.time() - started, 1),
                      message=reason)
        return result
    snapshot = plan.get("snapshotId")
    plan_version = str(plan.get("planVersion") or "")
    result["planVersion"] = plan_version
    report_items = {item.get("id"): item for item in report.get("items", []) if isinstance(item, dict)}
    blocks = {block.get("id"): str(block.get("text") or "")
              for page in payload.get("pages") or [] for block in page.get("blocks") or []}
    clauses = clause_pool([ref for item in plan["items"] for ref in _refs_of(item)], "2025")
    items = []
    for item in plan["items"]:
        if item.get("decisionState") != "confirmed" or not item.get("edit"):
            continue
        row = report_items.get(item.get("itemId"), {})
        refs = _refs_of(item)
        item_clauses = {_triple_string(ref): clauses.get((ref["documentId"], ref["edition"], ref["clause"]), {}).get("text", "")
                        for ref in refs}
        items.append({
            "snapshotId": snapshot, "itemId": item.get("itemId"), "actionId": item.get("actionId"),
            "title": str(row.get("title") or item.get("title") or "")[:200],
            "detail": str(row.get("detail") or "")[:1200],
            "clauseRefs": refs, "clauseTexts": item_clauses,
            "edit": {"operation": (item.get("edit") or {}).get("operation"),
                     "before": str((item.get("edit") or {}).get("before") or "")[:400],
                     "after": str((item.get("edit") or {}).get("after") or "")[:400]},
            "evidence": [blocks[ref][:200] for ref in list(dict.fromkeys(row.get("refs") or []))[:4]
                         if ref in blocks],
        })
    if not items:
        result.update(status="completed", accepted=0, seconds=round(time.time() - started, 1),
                      message="本次没有需要 AI 撰写整改要求的已确认动作。")
        return result
    data = {"snapshotId": snapshot, "planVersion": plan_version,
            "items": [{k: item[k] for k in ("snapshotId", "itemId", "actionId", "title", "detail",
                                             "clauseRefs", "edit", "evidence")}
                      for item in items],
            "clauses": {"%s@%s %s" % key: entry["text"] for key, entry in clauses.items()},
            "checkpoints": _checkpoint_brief(), "decisions": [entry["text"] for entry in _decisions()]}
    system = (ROOT / "agents" / "remediation.md").read_text() + _remediation_guidance()
    stage = {"role": "remediation", "label": "整改", "status": "running"}
    result["stages"].append(stage)
    sent_items = {item["actionId"]: item for item in data["items"]}
    by_action = {item["actionId"]: item for item in items}
    raw_records = []
    normalized, duplicates = [], []   # every tolerated change is logged here, per attempt
    retry_cap = _writer_retry_cap()

    def run_writer(attempt, request_data, request_system, timeout=None):
        """One writer call → (entries, top-level dict); raw text is kept per attempt."""
        raw_sink = []
        _t0 = time.monotonic()
        try:
            if call:
                produced = call("remediation", request_system, request_data, key, model)
            else:
                produced = _call("remediation", request_system, request_data, key, model,
                                 sink=local_usage, timeout=timeout, raw_sink=raw_sink)
                produced = raw_sink[-1]["content"] if raw_sink else produced
        except Exception as exc:
            raw_records.append(_writer_raw_record(attempt, raw_sink[-1] if raw_sink else None,
                                                  error=str(exc)[:200]))
            raise
        if recorder is not None:
            recorder.model("remediation", attempt, (local_usage or [{}])[-1].get("usage", {}), "ok",
                           (local_usage or [{}])[-1].get("provider", {}), model,
                           duration_ms=(time.monotonic() - _t0) * 1000)
        record = _writer_raw_record(attempt, raw_sink[-1] if raw_sink else {"content": produced})
        raw_records.append(record)
        try:
            parsed_entries, parsed_top = _remediation_parsed(produced)
        except Exception as exc:
            record["parseError"] = str(exc)[:200]
            raise
        record["parseNotes"] = list(parsed_top.get("parseNotes") or [])
        for note in record["parseNotes"]:
            normalized.append({"attempt": attempt, "itemId": None, "actionId": None,
                               "notes": [note], "disposition": "parse"})
        return parsed_entries, parsed_top

    def diagnostics(attempts, normalized, duplicates, retry=None):
        return {"writerAttempts": attempts, "retryCap": retry_cap, "retry": retry,
                "normalized": normalized, "duplicatesDropped": duplicates, "raw": raw_records}

    attempts = 1
    retry_info = None
    try:
        entries, top = run_writer(1, data, system)
    except (ModelResultUnknown, TimeoutError):
        stage["status"] = "result-unknown"
        result.update(status="result-unknown", modelRequestIssued=True,
                      seconds=round(time.time() - started, 1), usage=list(local_usage),
                      diagnostics=diagnostics(attempts, normalized, duplicates),
                      message="撰稿模型请求已发出，结果未知；不能视为成功，也不承诺恰好一次。整批回退模板。")
        return result
    except Exception as exc:
        truncated = "截断" in str(exc)
        if truncated or retry_cap < 1:
            stage["status"] = "failed"
            result.update(status="failed", seconds=round(time.time() - started, 1), usage=list(local_usage),
                          diagnostics=diagnostics(attempts, normalized, duplicates),
                          message="模型未返回可解析的整改要求（可能被截断）；整批回退模板，诊断已保留。")
            return result
        # Unparseable but not truncated: one bounded retry for the whole batch.
        entries, top = [], {}
        retry_info = {"trigger": "unparseable", "actionIds": sorted(by_action)}
    accepted, rejected, first_normalized, first_duplicates = _validate_writer_entries(
        entries, top, by_action, sent_items, clauses, snapshot, plan_version, attempt=1)
    normalized += first_normalized
    duplicates += first_duplicates
    if retry_info is None and retry_cap >= 1:
        structural = sorted({row["actionId"] for row in rejected
                             if row.get("actionId") in by_action and row["actionId"] not in
                             {value["actionId"] for value in accepted.values()}
                             and str(row.get("reason") or "").startswith(STRUCTURE_REASON)})
        if structural:
            retry_info = {"trigger": "structure", "actionIds": structural}
    if retry_info is not None:
        attempts = 2
        retry_data = dict(data, items=[sent_items[action] for action in retry_info["actionIds"]])
        retry_system = system + _writer_retry_note()
        try:
            retry_entries, retry_top = run_writer(2, retry_data, retry_system, timeout=WRITER_RETRY_TIMEOUT)
            retry_info["status"] = "returned"
        except (ModelResultUnknown, TimeoutError):
            retry_entries, retry_top = [], {}
            retry_info["status"] = "result-unknown"
        except Exception:
            retry_entries, retry_top = [], {}
            retry_info["status"] = "failed"
        retry_by_action = {action: by_action[action] for action in retry_info["actionIds"]}
        more, more_rejected, more_normalized, more_duplicates = _validate_writer_entries(
            retry_entries, retry_top, retry_by_action, sent_items, clauses, snapshot, plan_version, attempt=2)
        retried = set(retry_info["actionIds"])
        # Earlier structure rejections for retried actions are replaced by the retry's verdict.
        rejected = [row for row in rejected if row.get("actionId") not in retried]
        rejected += [dict(row, reason="重试后：" + str(row.get("reason") or "")) for row in more_rejected]
        for item_id, value in more.items():
            accepted.setdefault(item_id, value)
        for action in sorted(retried - {value["actionId"] for value in accepted.values()}):
            if not any(row.get("actionId") == action for row in rejected):
                rejected.append({"itemId": by_action[action]["itemId"], "actionId": action,
                                 "reason": "重试后仍未返回该动作的整改要求"})
        normalized += more_normalized
        duplicates += more_duplicates
        retry_info["accepted"] = len(more)
    stage["status"] = "completed"
    stage["count"] = len(accepted)
    status = "completed" if accepted and not rejected else "partial" if accepted else "failed"
    message = ("已校验采用 %d 条；未采用的条目回退模板文案。" % len(accepted)) if rejected else \
              ("已校验采用 %d 条。" % len(accepted)) if accepted else \
              "没有整改要求通过校验；报告全部回退模板文案。"
    result["diagnostics"] = diagnostics(attempts, normalized, duplicates, retry_info)
    decisions = _writer_decision_set(payload_or_bundle, plan, report)
    accepted, writer_rejected = decision_set.gate_writer_revisions(decisions, accepted)
    if writer_rejected:
        rejected = list(rejected) + writer_rejected
        status = "completed" if accepted and not rejected else "partial" if accepted else "failed"
        message = ("已校验采用 %d 条；未采用的条目回退模板文案。" % len(accepted)) if rejected else                   ("已校验采用 %d 条。" % len(accepted)) if accepted else                   "没有整改要求通过校验；报告全部回退模板文案。"
    task_id = ""
    timer = getattr(payload_or_bundle, "timer", None)
    if timer is not None:
        task_id = str(getattr(timer, "taskId", "") or "")
    if not task_id:
        context = getattr(payload_or_bundle, "context", None)
        if isinstance(context, dict):
            task_id = str(context.get("_decisionTaskId") or context.get("taskId") or "")
    if task_id:
        decision_set.persist_for_task(task_id, decisions)
    result.update(status=status, accepted=len(accepted), rejected=rejected, remediations=accepted,
                  usage=list(local_usage), seconds=round(time.time() - started, 1), message=message,
                  snapshotId=snapshot, decisionVersion=decisions.decisionVersion,
                  disputes=list(decisions.disputes),
                  writerRejected=writer_rejected)
    return result


def _writer_retry_cap():
    """At most one writer retry; LABEL_REVIEW_WRITER_RETRY=0 disables it. Never more than 1."""
    raw = os.getenv("LABEL_REVIEW_WRITER_RETRY", "1").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 1
    return max(0, min(1, value))


def _writer_retry_note():
    return ("\n【重试】上一次输出的结构无效，本次只重写下列 items。每条必须恰好包含 snapshotId、planVersion、itemId、"
            "actionId、clauseRefs、explanation 六个键；自由解释写在 explanation 字符串里，不得使用 text 等其他键名，"
            "不得回填 title/detail/edit/evidence；每个 actionId 只输出一条。其余要求不变。")


_SECRETISH = re.compile(r"(?i)(?:sk-[A-Za-z0-9]{8,}|bearer\s+[A-Za-z0-9._-]{8,}|api[_-]?key\s*[:=]\s*\S+)")


def _writer_raw_record(attempt, raw, error=None):
    """Redacted excerpt of the writer's raw text for diagnosis (no secrets, no paths/URLs/HTML)."""
    record = {"attempt": attempt}
    if raw is not None:
        content = raw.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        full_chars = raw.get("chars") if isinstance(raw.get("chars"), int) else len(content)
        excerpt = _SECRETISH.sub("[redacted]", FORBIDDEN.sub("[redacted]", content[:WRITER_RAW_EXCERPT]))
        record.update(chars=full_chars, sha256=hashlib.sha256(content.encode()).hexdigest(),
                      excerpt=excerpt, excerptTruncated=full_chars > WRITER_RAW_EXCERPT,
                      finishReason=str(raw.get("finishReason") or ""))
    if error:
        record["error"] = _SECRETISH.sub("[redacted]", FORBIDDEN.sub("[redacted]", error))
    return record


def _normalize_remediation_entry(entry, sent_item, top):
    """Tolerate near-valid writer output only where the meaning is unambiguous.

    Returns (entry, notes, reason). reason != "" means a structure rejection.
    Never fills itemId/actionId/clauseRefs/explanation from thin air and never
    turns an invalid explanation into a valid one; the contract check still runs.
    """
    if not isinstance(entry, dict):
        return entry, [], STRUCTURE_REASON + "：条目不是对象"
    value = dict(entry)
    notes = []
    # agents/remediation.md up to 2026-11-12 showed "text" in its example while
    # the guidance demanded "explanation"; "text" is the same free-text field.
    if "text" in value:
        if "explanation" not in value and isinstance(value["text"], str):
            value["explanation"] = value.pop("text")
            notes.append("text→explanation")
        elif isinstance(value.get("explanation"), str) and isinstance(value["text"], str) \
                and value["text"].strip() == value["explanation"].strip():
            value.pop("text")
            notes.append("dropped duplicate text")
        else:
            return value, notes, STRUCTURE_REASON + "：text 与 explanation 同时出现且不一致"
    wrapped = value.get("explanation")
    if isinstance(wrapped, dict) and len(wrapped) == 1:
        inner_key, inner = next(iter(wrapped.items()))
        if inner_key in ("explanation", "text") and isinstance(inner, str):
            value["explanation"] = inner
            notes.append("unwrapped explanation object")
    for key in REMEDIATION_ECHO_KEYS:
        if key in value:
            if isinstance(sent_item, dict) and value[key] == sent_item.get(key):
                value.pop(key)
                notes.append("dropped echoed " + key)
            else:
                return value, notes, STRUCTURE_REASON + "：回填了被改动的输入字段 " + key
    for key in ("snapshotId", "planVersion"):
        if key not in value and isinstance(top, dict) and isinstance(top.get(key), str) and top.get(key):
            value[key] = top[key]
            notes.append(key + " from top level")
    refs = value.get("clauseRefs")
    if isinstance(refs, list):
        seen, unique = set(), []
        for ref in refs:
            token = json.dumps(ref, ensure_ascii=False, sort_keys=True)
            if token in seen:
                continue
            seen.add(token)
            unique.append(ref)
        if len(unique) != len(refs):
            value["clauseRefs"] = unique
            notes.append("dropped duplicate clauseRefs")
    return value, notes, ""


def _validate_writer_entries(entries, top, by_action, sent_items, clauses, snapshot, plan_version, attempt=1):
    """Normalize → contract-check each entry. Exact duplicates are dropped, not counted as failures.

    Every entry whose content was changed by normalization is logged in
    `normalized` with its notes and final disposition (accepted / rejected /
    duplicate-dropped / duplicate-rejected), whatever happens to it next.
    """
    accepted, rejected, normalized, duplicates = {}, [], [], []
    used, seen = set(), {}
    for entry in entries:
        action_id = entry.get("actionId") if isinstance(entry, dict) else None
        item = by_action.get(action_id) if isinstance(action_id, str) else None
        if item is None:
            rejected.append({"itemId": entry.get("itemId") if isinstance(entry, dict) else None,
                             "actionId": action_id if isinstance(action_id, str) else None,
                             "reason": "actionId 不属于本次条目"})
            continue
        value, notes, reason = _normalize_remediation_entry(entry, sent_items.get(action_id), top)
        log = None
        if notes:
            log = {"attempt": attempt, "itemId": item["itemId"], "actionId": action_id,
                   "notes": list(notes), "disposition": None}
            normalized.append(log)
        token = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if token in seen.setdefault(action_id, set()):
            duplicates.append({"attempt": attempt, "itemId": item["itemId"], "actionId": action_id,
                               "kind": "exact"})
            if log:
                log["disposition"] = "duplicate-dropped"
            continue
        seen[action_id].add(token)
        if action_id in used:
            rejected.append({"itemId": item["itemId"], "actionId": action_id,
                             "reason": "同一动作重复输出（内容不同，未采用后一条）"})
            if log:
                log["disposition"] = "duplicate-rejected"
            continue
        reason = reason or _remediation_reason(value, item, clauses, snapshot, plan_version)
        if reason:
            rejected.append({"itemId": item["itemId"], "actionId": action_id, "reason": reason})
            if log:
                log["disposition"] = "rejected"
            continue
        if log:
            log["disposition"] = "accepted"
        accepted[item["itemId"]] = {
            "text": _compose_remediation(item, value),
            "clauseIds": [ref["clause"] for ref in value["clauseRefs"]],
            "clauseRefs": [dict(ref) for ref in value["clauseRefs"]],
            "actionId": action_id, "snapshotId": snapshot, "planVersion": plan_version,
        }
        used.add(action_id)
    return accepted, rejected, normalized, duplicates


def _remediation_parsed(value):
    """(entries, top-level dict) from writer output; raises ValueError when unusable."""
    if isinstance(value, dict):
        parsed = value
    else:
        text = re.sub(r"^```(?:json)?|```$", "", str(value or "").strip(), flags=re.M)
        repaired = False
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = _repair_notes(text, '"remediations"')
            repaired = True
        if not parsed:
            raise ValueError('候选输出格式无效')
    entries = parsed.get("remediations") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        raise ValueError('候选输出格式无效')
    top = {key: parsed.get(key) for key in ("snapshotId", "planVersion") if isinstance(parsed, dict)}
    objects = [entry for entry in entries if isinstance(entry, dict)]
    notes = []
    if not isinstance(value, dict) and repaired:
        notes.append("repaired incomplete JSON")
    if len(objects) != len(entries):
        notes.append("dropped %d non-object entries" % (len(entries) - len(objects)))
    if len(objects) > 12:
        notes.append("kept first 12 of %d entries" % len(objects))
    top["parseNotes"] = notes
    return objects[:12], top


def _compose_remediation(item, entry):
    """③ 关键句由程序注入：动作（before→after）+ 依据；AI 只贡献 explanation。"""
    edit = item.get("edit") or {}
    before = str(edit.get("before") or "").strip()
    after = str(edit.get("after") or "").strip()
    if edit.get("operation") == "suggest_reorder":
        action = "按投料记录确认后调整料包顺序"
    elif before and after:
        shown_before = before if len(before) <= 120 else before[:120] + "…"
        shown_after = after if len(after) <= 120 else after[:120] + "…"
        action = "将「%s」改为「%s」" % (shown_before, shown_after)
    else:
        action = "按核实结果整改"
    refs = entry.get("clauseRefs") or []
    basis = "、".join(sorted({"%s %s" % ((ref.get("documentId") or "").upper(), ref.get("clause") or "")
                              for ref in refs if ref.get("clause")}))
    text = action + "。" + entry.get("explanation", "").strip()
    return text + ("（依据：%s）" % basis if basis else "")


def _remediation_entries(value):
    """Compatibility wrapper: entries only (see _remediation_parsed)."""
    return _remediation_parsed(value)[0]


# ---------------------------------------------------------------- AI 参与审核（V2.7 阶段 C）
AI_REVIEW_CLAIM = re.compile(r"^[^\n]{4,200}$")


def _ai_review_inputs(payload_or_bundle):
    """冻结本任务版本与证据，生成检查清单，并从已发布知识库检索相关条款。

    不再把输入限制在规则已经引用的条款，也不再按每条最多 5 块、每块前 300 字裁剪证据。
    """
    bundle = _bundle_of(payload_or_bundle)
    report, payload = bundle.report, bundle.payload
    plan = report.get("reportPlan") or {}
    pages = getattr(bundle, "pages", None) or payload.get("pages") or []
    context = getattr(bundle, "context", None) or payload.get("context") or {}
    draft = getattr(bundle, "draft", None) or payload.get("draft") or {}
    prep = audit_retrieval.prepare_audit_task(
        report=report, payload=payload, plan=plan, context=context, pages=pages, draft=draft)
    data = audit_retrieval.model_payload(prep)
    data["version"] = report.get("versionLabel") or report.get("version") or ""
    data["checkpoints"] = _checkpoint_brief()
    data["decisions"] = [entry["text"] for entry in _decisions()]
    blocks = {block.get("id"): str(block.get("text") or "")
              for block in prep.get("evidenceCatalog") or []}
    return report, plan, blocks, data, prep


def _ai_review_candidates(value):
    if isinstance(value, dict):
        parsed = value
    else:
        text = re.sub(r"^```(?:json)?|```$", "", str(value or "").strip(), flags=re.M)
        parsed = json.loads(text)
    rows = parsed.get("candidates") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        raise ValueError("候选输出格式无效")
    return [row for row in rows if isinstance(row, dict)][:12]


def run_ai_review(payload_or_bundle, call=None, recorder=None):
    """审核 Agent：按已发布知识库检索并留下覆盖记录。合格候选进工作台，不直接进客户报告。

    程序准备在调用模型之前完成。模型未配置或失败时，检查清单仍保留，未作答项目记为未查，
    不得视为通过。停用规则保持未检查／未覆盖。
    """
    result = {"status": "unavailable", "candidates": [], "rejected": [], "proposed": 0,
              "snapshotId": None, "usage": [], "model": os.getenv("LABEL_REVIEW_MODEL", ""), "message": "",
              "coverage": [], "lockedJudgments": [], "evidenceVersionId": None, "versions": {}}
    local_usage = []
    report, plan, blocks, data, prep = _ai_review_inputs(payload_or_bundle)
    result["snapshotId"] = plan.get("snapshotId")
    result["evidenceVersionId"] = prep.get("evidenceVersionId")
    result["versions"] = prep.get("versions") or {}
    result["promptComposition"] = review_prompt_composition(data)
    result["diagnostics"] = _review_call_diagnostics()

    def _finish(usage_rows=None, exc=None, truncated=False):
        rows = usage_rows if usage_rows is not None else result.get("usage") or []
        result["promptComposition"] = review_prompt_composition(
            data, api_prompt_tokens=_api_prompt_tokens(rows))
        result["diagnostics"] = _review_call_diagnostics(exc=exc, truncated=truncated)
        return result

    key, model = (os.getenv(name, "").strip() for name in ("LABEL_REVIEW_API_KEY", "LABEL_REVIEW_MODEL"))
    if not plan.get("snapshotId"):
        applied = audit_retrieval.apply_coverage(prep, None, rejected_reason="规则结果未形成快照，未发起 AI 审核。")
        result.update(applied, status="unavailable", message="规则结果未形成快照，未发起 AI 审核。")
        return _finish()
    if not key or not model:
        applied = audit_retrieval.apply_coverage(prep, None, rejected_reason="未配置模型或密钥，未发起 AI 审核。")
        result.update(applied, status="unavailable", message="未配置模型或密钥，未发起 AI 审核。")
        return _finish()
    system = (ROOT / "agents" / "ai-review.md").read_text()
    try:
        _t0 = time.monotonic()
        produced = call("ai-review", system, data, key, model) if call else \
            _call("ai-review", system, data, key, model, sink=local_usage,
                  timeout=REVIEW_TIMEOUT, timeout_class="REVIEW_TIMEOUT")
        if recorder is not None:
            recorder.model("ai-review", 1, (local_usage or [{}])[-1].get("usage", {}), "ok",
                           (local_usage or [{}])[-1].get("provider", {}), model,
                           duration_ms=(time.monotonic() - _t0) * 1000)
        applied = audit_retrieval.apply_coverage(prep, produced)
        last = (local_usage or [{}])[-1] if local_usage else {}
        usage_row = last.get("usage") or {}
        truncated = last.get("finishReason") == "length" or (
            isinstance(usage_row.get("completion"), int) and usage_row.get("completion") >= 32000)
        if applied.get("parseError") or truncated:
            if truncated:
                applied = audit_retrieval.apply_coverage(
                    prep, None, rejected_reason="模型输出被截断（达到输出上限），结果不可用。")
            result.update(applied, status="failed", usage=[row.get("usage", {}) for row in local_usage],
                          provider=[row.get("provider", {}) for row in local_usage],
                          message="AI 审核未返回可解析结果；候选不进入报告。检查覆盖已按未查记录。")
            return _finish(result.get("usage"), truncated=truncated)
    except ModelResultUnknown as exc:
        applied = audit_retrieval.apply_coverage(
            prep, None, rejected_reason="模型请求已发出但结果未知（超时或中断）。")
        result.update(applied, status="result-unknown", modelRequestIssued=True,
                      usage=[row.get("usage", {}) for row in local_usage],
                      provider=[row.get("provider", {}) for row in local_usage],
                      message="模型请求已发出，结果未知；不能视为成功，也不承诺恰好一次。候选不进入报告。检查覆盖已按未查记录。 %s" % str(exc)[:200])
        return _finish(result.get("usage"), exc=exc)
    except TimeoutError as exc:
        applied = audit_retrieval.apply_coverage(
            prep, None, rejected_reason="模型请求已发出但结果未知（超时）。")
        result.update(applied, status="result-unknown", modelRequestIssued=True,
                      message="模型请求已发出，结果未知；不能视为成功，也不承诺恰好一次。候选不进入报告。检查覆盖已按未查记录。")
        return _finish(result.get("usage"), exc=exc)
    except Exception as exc:
        last = (local_usage or [{}])[-1] if local_usage else {}
        usage_row = last.get("usage") or {}
        truncated = "截断" in str(exc) or last.get("finishReason") == "length" or (
            isinstance(usage_row.get("completion"), int) and usage_row.get("completion") >= 32000)
        if truncated:
            reason = "模型输出被截断（达到输出上限），结果不可用。"
        else:
            detail = str(exc).strip()[:200]
            reason = ("AI 审核失败：%s" % detail) if detail else "AI 审核未返回可解析结果；候选不进入报告。"
        applied = audit_retrieval.apply_coverage(prep, None, rejected_reason=reason)
        result.update(applied, status="failed",
                      usage=[row.get("usage", {}) for row in local_usage],
                      provider=[row.get("provider", {}) for row in local_usage],
                      model=model,
                      finishReason=last.get("finishReason") or "",
                      message=reason + " 候选不进入报告。检查覆盖已按未查记录。")
        return _finish(result.get("usage"), exc=None if not truncated else None, truncated=truncated)
    # Preserve legacy candidate validation for rows that still use the old shape
    # and were not already turned into locked findings (e.g. missing fact/reason).
    legacy_rejected = []
    try:
        entries = _ai_review_candidates(produced)
    except Exception:
        entries = []
    covered_ids = {row.get("checklistId") for row in applied.get("candidates") or []}
    for entry in entries:
        claim = str(entry.get("claim") or "").strip()
        fact = str(entry.get("fact") or "").strip()
        reason_text = str(entry.get("reason") or "").strip()
        evidence_ids = [ref for ref in (entry.get("evidenceIds") or []) if isinstance(ref, str)]
        targets = [str(x) for x in (entry.get("checklistIds") or entry.get("itemIds") or []) if x]
        if targets and any(t in covered_ids for t in targets):
            continue
        if claim and fact and reason_text and evidence_ids and all(ref in blocks for ref in evidence_ids):
            continue
        if not claim or not fact or not reason_text:
            legacy_rejected.append({"claim": claim[:120], "reason": "claim/fact/reason 缺失"})
        elif not evidence_ids:
            legacy_rejected.append({"claim": claim[:120], "reason": "没有 evidenceIds"})
        elif any(ref not in blocks for ref in evidence_ids):
            legacy_rejected.append({"claim": claim[:120], "reason": "evidenceIds 不属于当前快照"})
    rejected = list(applied.get("rejected") or []) + legacy_rejected
    result.update(applied, status="completed", proposed=len(applied.get("candidates") or []),
                  rejected=rejected, usage=[row.get("usage", {}) for row in local_usage],
                  provider=[row.get("provider", {}) for row in local_usage],
                  message="候选仅进工作台，需人工复核后才可能进入待核实。检查覆盖已记录；未作答项目为未查，不等于通过。")
    return _finish(result.get("usage"))
