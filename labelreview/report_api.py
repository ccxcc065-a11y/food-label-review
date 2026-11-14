"""统一报告入口（v2）：规则初审 → AI 对照审核 → 程序分流 → 统一计划 → AI 撰稿
→ reportView → 渲染 HTML → 检查 → 不可变成稿 → PDF 后台生成。"""
import dataclasses
import datetime
import hashlib
import json
import os
import re
import secrets
import threading
from pathlib import Path

from labelreview.audit import audit
import labelreview.decision_set as decision_set
from labelreview.decision_set import (
    DecisionSet,
    RulingValidationError,
    StaleRevisionError,
    UnboundApprovalError,
    approval_matches,
    approval_tries_fact_patch,
    bind_candidate,
    enrich_candidate,
    gate_writer_revisions,
    get_or_create_for_task,
    persist_for_task,
    seed_from_report,
)
from labelreview.extract import extract
import labelreview.html_check as html_check_module
import labelreview.knowledge as knowledge
import labelreview.report_check as report_check
import labelreview.report_plan as report_plan
import labelreview.report_projection as report_projection
import labelreview.report_render as report_render
import labelreview.runtime_agents as runtime_agents
import labelreview.snapshot as snapshot_module
import labelreview.task_store as task_store
from labelreview.timing import TaskTimer

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / ".runtime" / "reports"
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
]
CHROME = CHROME_CANDIDATES[0]  # default hint only; resolve_chrome() picks a real binary
PDF_RENDERER = None  # tests may set a callable(html_text, dest_path)


def resolve_chrome():
    """Return a usable Chrome/Chromium binary, or raise with a diagnostic reason."""
    import shutil
    env = (os.environ.get("LABEL_REPORT_CHROME") or "").strip()
    tried = []
    if env:
        tried.append(env)
        if os.path.isfile(env) and os.access(env, os.X_OK):
            return env
        raise RuntimeError("Chrome 不可用：LABEL_REPORT_CHROME 不是可执行文件（%s）" % env)
    for path in CHROME_CANDIDATES:
        tried.append(path)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        tried.append(name if not found else found)
        if found and os.path.isfile(found) and os.access(found, os.X_OK):
            return found
    raise RuntimeError("Chrome 不可用：未找到可执行文件，已试：" + "、".join(tried))


def chrome_status():
    try:
        path = resolve_chrome()
        return {"available": True, "binary": path}
    except Exception as exc:
        return {"available": False, "reason": str(exc)[:300]}


def stamp_status():
    """Footer stamping needs pypdf (see requirements.txt). Chrome alone is not enough."""
    try:
        import pypdf
    except ModuleNotFoundError:
        return {"available": False, "package": "pypdf",
                "reason": "未安装 pypdf，PDF 页脚盖印不可用。请执行 python3 -m pip install -r requirements.txt"}
    return {"available": True, "package": "pypdf",
            "version": str(getattr(pypdf, "__version__", "") or "")}


def reports_dir():
    override = os.environ.get("LABEL_REVIEW_REPORTS")
    return Path(override) if override else REPORTS


@dataclasses.dataclass
class AuditBundle:
    """D1：本轮不可变审核对象；AI 审核与撰稿都读它，不各自再审。"""
    payload: dict
    pages: list
    context: dict
    draft: dict
    report: dict
    plan: dict
    timer: TaskTimer
    planVersion: str = ""
    decisionSet: object = None

    def __post_init__(self):
        self.planVersion = str(self.plan.get("planVersion") or "")


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id(prefix):
    return "%s%d%s" % (prefix, int(datetime.datetime.now().timestamp() * 1000), secrets.token_hex(3))


def _actual_model(answer):
    for row in (answer or {}).get("provider") or []:
        if isinstance(row, dict) and row.get("model"):
            return str(row["model"])
    return ""


def _status(payload, review, meta):
    requested = bool(payload.get("aiAgents") or payload.get("aiRemediation"))
    if not requested:
        return "rule-only"
    review_status = str(review.get("status") or "")
    rem_status = str(meta.get("aiRemediation") or "")
    if review_status == "result-unknown" or rem_status == "result-unknown":
        return "result-unknown"
    review_ok = review_status == "completed"
    remediation_ok = rem_status in ("completed", "partial")
    # A completed writer must not hide a failed or unparseable review.
    if payload.get("aiAgents") and review_status in {"failed", "unavailable"}:
        return "ai-failed"
    if review_ok or remediation_ok:
        return "ok"
    return "ai-failed"


def _terms(text):
    text = str(text or "")
    words = {word for word in re.findall(r"[\u4e00-\u9fff]{2,8}", text)}
    return {word for word in words if word not in ("本产品", "标签", "包装", "本条", "需要", "应当")}


def _evidence_blocks(context):
    return {str(row.get("id")): str(row.get("text") or "")
            for row in (context or {}).get("_blocks") or [] if isinstance(row, dict)}


def _scope_ok(clause_refs, scope_text):
    text = str(scope_text or "")
    for ref in clause_refs:
        if not (ref.get("clause") or "").strip():
            return False
        entry = runtime_agents.clause_pool([ref], "2025")
        body = next(iter(entry.values()), {}).get("text", "")
        if "进口" in body and "进口" not in text:
            return False
        if "小作坊" in body and "小作坊" not in text:
            return False
        if "营养标签" in body and "营养" not in text:
            return False
    return bool(clause_refs)


def _prefilter(report, candidate, blocks, scope_text):
    """前置过滤：只排除明显错误；不表示事实核验通过。"""
    material = candidate.get("missingMaterial") or {}
    checks = {"missingMaterial": all(material.get(key) for key in ("what", "fromWhom", "affects"))}
    evidence_text = "".join(str(blocks.get(ref) or "") for ref in candidate.get("evidenceIds") or [])
    terms = _terms(candidate.get("claim"))
    hits = [term for term in terms if term in evidence_text]
    absence = bool(re.search(r"未|没有|缺少|未见|无", candidate.get("claim") or ""))
    checks["keyword"] = bool(terms) and bool(hits) and not absence
    checks["scope"] = _scope_ok(candidate.get("clauseRefs") or [], scope_text)
    checks["field"] = None
    try:
        import labelreview.audit_retrieval as audit_retrieval
        cleaned = audit_retrieval.sanitize_review_row(candidate)
        checks["soundConfirmation"] = not audit_retrieval.unsound_fail_confirmation(
            cleaned, original=candidate)
    except Exception:
        checks["soundConfirmation"] = True
    return checks, all(value for value in checks.values() if value is not None)


def _version_pins(report, plan, context):
    context = context if isinstance(context, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    report = report if isinstance(report, dict) else {}
    versions = context.get("versions") if isinstance(context.get("versions"), dict) else {}
    if not versions and isinstance(report.get("versions"), dict):
        versions = report.get("versions") or {}
    return {
        "snapshot_id": str(plan.get("snapshotId") or report.get("snapshotId") or context.get("snapshotId") or ""),
        "audit_profile_id": str(context.get("auditProfileId") or versions.get("auditProfileId")
                                or report.get("auditProfileId") or ""),
        "knowledge_version": str(versions.get("knowledgeVersion") or context.get("knowledgeVersion") or ""),
        "rules_version": str(versions.get("rulesVersion") or context.get("rulesVersion") or ""),
        "evidence_version": str(
            context.get("evidenceVersionId") or report.get("evidenceVersionId")
            or versions.get("evidenceVersion") or ""
        ),
    }


def _bind_live_candidate(candidate, report, plan, context):
    """Attach content-addressed identity. Display ids such as AI-1 are not keys."""
    pins = _version_pins(report, plan, context)
    enriched = enrich_candidate(candidate, **pins)
    bound = bind_candidate(enriched)
    return enriched, bound


def _annotate_candidates(candidates, report, plan, context):
    try:
        import labelreview.audit_retrieval as audit_retrieval
    except Exception:
        audit_retrieval = None
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        if audit_retrieval is not None:
            cleaned = audit_retrieval.sanitize_review_row(candidate)
            for key in ("claim", "fact", "reason", "clauseRefs", "labels", "citationLabel",
                        "droppedClauseRefs"):
                if key in cleaned:
                    candidate[key] = cleaned[key]
        _enriched, bound = _bind_live_candidate(candidate, report, plan, context)
        candidate["candidateId"] = bound["candidateId"]
        if bound.get("snapshotId") and not candidate.get("snapshotId"):
            candidate["snapshotId"] = bound["snapshotId"]
        if bound.get("displayId") and not candidate.get("displayId"):
            candidate["displayId"] = bound["displayId"]
    return candidates


def _decision_set_for(report, plan, context, *, task_id=""):
    context = context if isinstance(context, dict) else {}
    existing = context.get("_decisionSet")
    if isinstance(existing, DecisionSet):
        return existing
    token = str(task_id or context.get("_decisionTaskId") or context.get("taskId") or "")
    pins = _version_pins(report, plan, context)
    versions = (context.get("versions") if isinstance(context.get("versions"), dict) else {}) or {}
    decisions = get_or_create_for_task(
        token,
        snapshot_id=pins["snapshot_id"],
        audit_profile_id=pins["audit_profile_id"],
        rules_version=pins["rules_version"] or str(versions.get("rulesVersion") or ""),
        knowledge_version=pins["knowledge_version"],
        facts_digest=str(context.get("factsDigest") or report.get("factsDigest") or ""),
        registered_exceptions=context.get("registeredExceptions") or [],
    )
    if token:
        context["_decisionTaskId"] = token
    return decisions


def _promote_candidates(report, plan, candidates, context):
    """Promote only candidates with a content-bound approval that still passes the prefilter.

    A previous-round display id such as AI-1 is not an approval key. Approvals
    that try to patch facts are refused. DecisionSet.promote_approved /
    approval_matches is the identity check; this function keeps the existing
    prefilter, wording-overlap skip against already listed items, and report
    item shape.
    """
    context = context if isinstance(context, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    approvals = list(context.get("aiReviewApproved") or [])
    blocks = _evidence_blocks(context)
    existing = " ".join(str(row.get("title") or "") + str(row.get("detail") or "")
                        for row in report.get("items") or [])
    existing_ids = {
        str(row.get("candidateId"))
        for row in report.get("items") or []
        if isinstance(row, dict) and row.get("candidateId")
    }
    decisions = _decision_set_for(report, plan, context)
    expected_revision = (
        context.get("expectedDecisionRevision")
        or context.get("expected_decision_revision")
        or ""
    )
    promoted = []
    seen = set(existing_ids)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        enriched, bound = _bind_live_candidate(candidate, report, plan, context)
        candidate["candidateId"] = bound["candidateId"]
        checks, passed = _prefilter(report, candidate, blocks, report.get("scopeLabel") or "")
        candidate["prefilter"] = checks
        candidate["passesPrefilter"] = passed
        if not passed:
            continue
        matching = [item for item in approvals if approval_matches(item, enriched)]
        if not matching:
            continue
        if any(approval_tries_fact_patch(item) for item in matching):
            continue
        if bound["candidateId"] in seen:
            continue
        claim = str(candidate.get("claim") or "")
        material = candidate.get("missingMaterial") or {}
        if _terms(claim) & _terms(existing):
            continue
        if any(term in claim for term in ("已整改", "已解决", "通过")):
            continue
        try:
            decisions.add_candidate(enriched)
            ruling = {"candidateId": bound["candidateId"], "conclusion": "待核实"}
            for item in matching:
                if isinstance(item, dict):
                    if item.get("snapshotId"):
                        ruling["snapshotId"] = item.get("snapshotId")
                    if item.get("auditProfileId"):
                        ruling["auditProfileId"] = item.get("auditProfileId")
                    if item.get("clauseVersions") or item.get("evidenceVersions"):
                        ruling["clauseVersions"] = item.get("clauseVersions")
                        ruling["evidenceVersions"] = item.get("evidenceVersions")
                    if item.get("conclusion"):
                        ruling["conclusion"] = item.get("conclusion")
                    break
            decisions.record_ruling(
                enriched,
                ruling,
                expected_decision_revision=expected_revision or None,
            )
        except (UnboundApprovalError, RulingValidationError, StaleRevisionError):
            continue
        report.setdefault("items", []).append({
            "id": candidate.get("id"), "candidateId": bound["candidateId"],
            "group": "review", "outcome": "review", "track": "actual",
            "title": claim[:200],
            "detail": (candidate.get("fact") or "") + " " + (candidate.get("reason") or ""),
            "action": "请提供：%s（%s）；影响：%s" % (material.get("what"), material.get("fromWhom"),
                                                    material.get("affects")),
            "refs": candidate.get("evidenceIds") or [], "aiFlag": True})
        promoted.append(candidate.get("id"))
        seen.add(bound["candidateId"])
        existing = existing + claim
    task_id = context.get("_decisionTaskId") or context.get("taskId") or ""
    if task_id:
        persist_for_task(task_id, decisions)
    return promoted


def _reclassify(report):
    try:
        import labelreview.report_plan as report_plan
        return report_plan.classify(report.get("items") or [])
    except Exception:
        return report.get("classification")


def _ai_stats(plan, review, report):
    candidates = review.get("candidates") or []
    coverage = review.get("coverage") or report.get("auditCoverage") or []
    checked = len({str(row.get("checklistId") or row.get("checkpointId") or row.get("id") or i)
                   for i, row in enumerate(coverage)
                   if isinstance(row, dict) and row.get("coverage") == "已查"})
    existing_ids = {str(row.get("candidateId")) for row in report.get("items") or []
                    if isinstance(row, dict) and row.get("candidateId")}
    # Official merge count is same-issue identity, not wording overlap.
    merged = 0
    for row in candidates:
        if not isinstance(row, dict):
            continue
        bound = row.get("candidateId")
        if bound and bound in existing_ids:
            merged += 1
    new = len(review.get("promoted") or [])
    return {"checked": checked, "proposed": len(candidates), "merged": merged,
            "new": new, "pendingReview": max(0, len(candidates) - merged - new),
            "checkpoints": checked}


def _report_view(report, plan, decisions=None):
    """Cards, sections, and materials from the frozen DecisionSet when present."""
    if decisions is None and isinstance((report or {}).get("_decisionSet"), DecisionSet):
        decisions = report.get("_decisionSet")
    projection = report_projection.project(
        decisions, report.get("items") or [],
        plan=plan, coverage=report.get("auditCoverage") or [],
    )
    view = report_projection.view_from_projection(projection)
    if not view.get("classification"):
        view["classification"] = report.get("classification")
    return view


def _parse_utc(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _timing_payload(timer, client_timing, knowledge_stats, pdf_ready_ms=None):
    """⑤ 分阶段计时：客户端点击→受理/OCR/字段，服务端规则/知识库/AI/渲染/保存。"""
    data = timer.snapshot(pdf_ready_ms=pdf_ready_ms, client_timing=client_timing)
    data["knowledge"] = knowledge_stats or {}
    clicked = _parse_utc((client_timing or {}).get("clickedUtc"))
    if clicked:
        accepted = _parse_utc(timer.startedUtc)
        if accepted:
            data["clickToAcceptedMs"] = round((accepted - clicked).total_seconds() * 1000, 1)
        for key in ("ocrStartUtc", "ocrEndUtc", "extractStartUtc", "extractEndUtc"):
            value = _parse_utc((client_timing or {}).get(key))
            if value:
                data.setdefault("client", {})[key] = round((value - clicked).total_seconds() * 1000, 1)
        if pdf_ready_ms is not None:
            data["clickToPdfMs"] = pdf_ready_ms
    return data


def _save_snapshot(bundle, check, review, meta, report_id, status, html_text, content_hash, snap,
                   timing_payload):
    """成稿落盘（HTML 先锁定；PDF 另行后台生成，状态单独存）。"""
    reports_dir().mkdir(parents=True, exist_ok=True)
    path = reports_dir() / ("%s.json" % report_id)
    timer = bundle.timer
    provider = {}
    for row in (review.get("provider") or []) if isinstance(review, dict) else []:
        provider = {"available": bool(row.get("requestId")), "value": row.get("requestId") or None,
                    "source": "response.header" if row.get("requestId") else "unavailable",
                    "reason": None if row.get("requestId") else "服务商未返回"}
        break
    payload = {
        "reportId": report_id, "taskId": timer.taskId,
        "snapshotId": bundle.plan.get("snapshotId"),
        "snapshotHash": snap["snapshotHash"], "snapshotManifest": snap["manifest"],
        "planVersion": bundle.planVersion, "contentHash": content_hash,
        "startedAt": timer.startedUtc, "endedAt": _now(),
        "model": bundle.report.get("auditMeta", {}).get("model"),
        "modelSource": "response", "status": status,
        "providerRequestId": provider or {"available": False, "reason": "服务商未返回"},
        "aiReview": {"status": review.get("status"), "model": review.get("model"),
                     "usage": review.get("usage") or [], "provider": review.get("provider") or [],
                     "stats": review.get("stats") or {}, "candidates": review.get("candidates") or [],
                     "rejected": review.get("rejected") or [], "promoted": review.get("promoted") or [],
                     "message": review.get("message") or "",
                     "finishReason": review.get("finishReason") or ""},
        "aiRemediation": {"status": meta.get("aiRemediation"), "planVersion": bundle.planVersion,
                          "accepted": meta.get("aiAccepted", 0), "rejected": meta.get("aiRejected") or [],
                          "diagnostics": meta.get("aiRemediationDiagnostics") or {}},
        "manualVerification": {"verifiedFields": bundle.context.get("verifiedFields") or [],
                               "autoReread": bundle.report.get("autoReread") or []},
        "check": {"ok": check.get("ok"), "errors": check.get("errors") or [], "html": check.get("html")},
        "reportView": bundle.report.get("reportView"),
        "timing": timing_payload,
        "pdfStatus": "pending",
        "reportHtml": html_text,
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    with os.fdopen(os.open(path, flags), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


def _pdf_path(report_id):
    return reports_dir() / ("%s.pdf" % report_id)


def _pdf_status_path(report_id):
    return reports_dir() / ("%s.pdf.json" % report_id)


def pdf_status(report_id):
    path = _pdf_status_path(report_id)
    if not path.exists():
        return {"status": "missing"}
    return json.loads(path.read_text())


def _write_pdf_status(report_id, data):
    path = _pdf_status_path(report_id)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(temp, path)


def _stamp_pdf(path, report_id, timer):
    """PDF 加页码与报告版本标识（页脚用 ASCII，中文正文不变）。"""
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PDF 已渲染，但页脚盖印失败：未安装 pypdf。请执行 python3 -m pip install -r requirements.txt"
        ) from exc
    reader = PdfReader(str(path))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    font_ref = writer._add_object(font)
    total = len(writer.pages)
    for index, page in enumerate(writer.pages, 1):
        text = "Report %s  |  Page %d / %d" % (report_id, index, total)
        stream = DecodedStreamObject()
        stream.set_data(("BT /RptFont 7 Tf 0.45 0.45 0.45 rg 40 18 Td (%s) Tj ET" % text).encode("latin-1"))
        stream_ref = writer._add_object(stream)
        contents = page.get_contents()
        if contents is None:
            page[NameObject("/Contents")] = stream_ref
        else:
            reference = getattr(contents, "indirect_reference", None)
            page[NameObject("/Contents")] = ArrayObject([reference, stream_ref]) if reference is not None \
                else ArrayObject([page.raw_get("/Contents"), stream_ref])
        resources = page["/Resources"]
        fonts = resources.get("/Font") if resources.get("/Font") is not None else DictionaryObject()
        fonts[NameObject("/RptFont")] = font_ref
        resources[NameObject("/Font")] = fonts
    with open(path, "wb") as handle:
        writer.write(handle)


def _generate_pdf(report_id, html_text, timer, clicked_utc=None):
    status_path = _pdf_status_path(report_id)
    lock = reports_dir() / ("%s.pdf.lock" % report_id)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return
    target = _pdf_path(report_id)
    temp = reports_dir() / ("%s.pdf.tmp" % report_id)
    try:
        if PDF_RENDERER is not None:
            PDF_RENDERER(html_text, temp)
        else:
            import labelreview.chrome_pdf as chrome_pdf
            chrome = resolve_chrome()
            # CLI --print-to-pdf hangs on some Linux Chrome 156 builds; CDP printToPDF does not.
            chrome_pdf.print_html(chrome, html_text, temp, timeout=120)
        if not temp.exists() or temp.stat().st_size == 0:
            raise RuntimeError("PDF 文件为空")
        if PDF_RENDERER is None:
            _stamp_pdf(temp, report_id, timer)
        os.replace(temp, target)
        pdf_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        status = {"status": "ready", "pdfHash": pdf_hash,
                  "pdfReadyMs": timer.ready("pdf-ready"), "reportId": report_id}
        clicked = _parse_utc(clicked_utc)
        if clicked:
            status["clickToPdfMs"] = round(
                (datetime.datetime.now(datetime.timezone.utc) - clicked).total_seconds() * 1000, 1)
        _write_pdf_status(report_id, status)
        _update_json_pdf(report_id, "ready", pdf_hash)
    except Exception as exc:
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass
        _write_pdf_status(report_id, {"status": "failed", "reason": str(exc)[:300],
                                      "reportId": report_id})
        _update_json_pdf(report_id, "failed", None)
    finally:
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass


def _update_json_pdf(report_id, status, pdf_hash):
    """PDF 状态单独文件；成稿 JSON 不再改写（已锁定）。"""
    path = reports_dir() / ("%s.json" % report_id)
    if not path.exists():
        return
    # 只写旁路状态，成稿本体保持逐字节不变；pdfStatus 以 <id>.pdf.json 为准。


def _start_pdf(report_id, html_text, timer, clicked_utc=None):
    thread = threading.Thread(target=_generate_pdf, args=(report_id, html_text, timer, clicked_utc), daemon=True)
    thread.start()
    return thread


def retry_pdf(report_id, *, start_thread=False):
    """Rebuild a PDF from the already-saved HTML. Never calls or bills AI."""
    saved = load(report_id)
    html = saved.get("reportHtml") or ""
    if not html:
        return {"status": "failed", "reason": "没有已保存的 HTML，无法重试 PDF",
                "reportId": report_id, "aiCalled": False}
    existing = pdf_status(report_id)
    if existing.get("status") == "ready" and _pdf_path(report_id).exists():
        result = dict(existing)
        result["aiCalled"] = False
        result["unchanged"] = True
        return result
    lock = reports_dir() / ("%s.pdf.lock" % report_id)
    if lock.exists():
        try:
            lock.unlink()
        except OSError:
            pass
    timer = TaskTimer()
    if saved.get("taskId"):
        timer.taskId = saved["taskId"]
    before_hash = saved.get("contentHash")
    _write_pdf_status(report_id, {"status": "pending", "reportId": report_id, "retry": True})
    if start_thread:
        _start_pdf(report_id, html, timer)
        status = pdf_status(report_id)
    else:
        _generate_pdf(report_id, html, timer)
        status = pdf_status(report_id)
    status["aiCalled"] = False
    again = load(report_id)
    if again.get("reportHtml") != html or again.get("contentHash") != before_hash:
        status["htmlDrift"] = True
    return status


def _is_resume(payload):
    if not isinstance(payload, dict):
        return False
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    task_id = payload.get("taskId") or context.get("taskId") or context.get("_decisionTaskId")
    expected = (payload.get("expectedDecisionRevision")
                or context.get("expectedDecisionRevision")
                or context.get("expected_decision_revision"))
    return bool(task_id and expected)


def _strip_payload(payload):
    if not isinstance(payload, dict):
        return {}
    data = dict(payload)
    data.pop("clientTiming", None)
    context = dict(data.get("context") or {}) if isinstance(data.get("context"), dict) else {}
    for key in ("LABEL_REVIEW_API_KEY", "apiKey", "api_key", "key", "authorization", "token"):
        context.pop(key, None)
        data.pop(key, None)
    data["context"] = context
    return data


def _persist_task(task_id, *, status, decisions=None, bundle=None, report_id="", content_hash="",
                  snapshot_id="", model_request_issued=False, event=None):
    if not task_id:
        return
    version = decisions.decisionVersion if decisions is not None else ""
    task_store.upsert(
        task_id, status=status, decision_version=version, report_id=report_id,
        content_hash=content_hash, snapshot_id=snapshot_id,
        model_request_issued=model_request_issued,
    )
    if bundle is not None:
        task_store.save_bundle(task_id, bundle)
    if event:
        task_store.record_event(task_id, event.get("kind") or status, event.get("payload") or {})
    if decisions is not None:
        persist_for_task(task_id, decisions)


def _apply_projection(report, plan, decisions):
    view = _report_view(report, plan, decisions)
    report["reportView"] = view
    if view.get("classification"):
        report["classification"] = view["classification"]
    if decisions is not None:
        report["decisionVersion"] = decisions.decisionVersion
    if view.get("conditionalEdits") is not None:
        revision = report.get("revision") if isinstance(report.get("revision"), dict) else {}
        if revision and not revision.get("options"):
            revision["options"] = [
                {"itemId": row.get("itemId"), "title": row.get("title"),
                 "values": row.get("options") or [], "pendingEdit": row.get("pendingEdit")}
                for row in view["conditionalEdits"]
            ]
            report["revision"] = revision
    return view


def resume(payload):
    """Continue a saved task after human confirmation. Does not rerun AI."""
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    task_id = str(payload.get("taskId") or context.get("taskId") or context.get("_decisionTaskId") or "")
    expected = str(payload.get("expectedDecisionRevision")
                   or context.get("expectedDecisionRevision")
                   or context.get("expected_decision_revision") or "")
    record = task_store.get(task_id)
    if record is None or not record.get("bundle"):
        raise ValueError("找不到可恢复的任务 %s；请重新发起审核，不能用其他任务的成稿顶替。" % task_id)
    bundle_data = record["bundle"]
    decisions = get_or_create_for_task(task_id)
    current = decisions.decisionVersion
    if expected != current:
        task_store.record_event(task_id, "stale-revision",
                                {"expected": expected, "current": current})
        raise StaleRevisionError(
            "expectedDecisionRevision %s does not match current %s" % (expected, current)
        )
    approvals = list(payload.get("aiReviewApproved") or context.get("aiReviewApproved") or [])
    report = bundle_data.get("report") or {}
    plan = bundle_data.get("plan") or {}
    review = bundle_data.get("review") or {"status": "completed", "candidates": []}
    candidates = list(review.get("candidates") or [])
    frozen_context = dict(bundle_data.get("context") or {})
    frozen_context.update({
        "aiReviewApproved": approvals,
        "_decisionTaskId": task_id,
        "taskId": task_id,
        "expectedDecisionRevision": expected,
        "_blocks": frozen_context.get("_blocks") or (context.get("_blocks") or []),
    })
    if payload.get("registeredExceptions") or context.get("registeredExceptions"):
        frozen_context["registeredExceptions"] = (
            payload.get("registeredExceptions") or context.get("registeredExceptions")
        )
    promoted = _promote_candidates(report, plan, candidates, frozen_context)
    if promoted:
        report["classification"] = _reclassify(report)
    review = dict(review)
    review["promoted"] = list(dict.fromkeys(list(review.get("promoted") or []) + list(promoted)))
    # Reuse the frozen plan and draft. Promoted candidates are 待核实 and do
    # not rewrite formal 07 edits. Rules and AI are not rerun.
    draft = bundle_data.get("draft") or payload.get("draft") or {}
    pages = bundle_data.get("pages") or payload.get("pages") or []
    if not pages:
        raise ValueError("任务 %s 没有保存的页面数据，无法在不重跑审核的情况下继续。" % task_id)
    meta = dict(bundle_data.get("meta") or {})
    meta["resumed"] = True
    meta["aiRerun"] = False
    persist_for_task(task_id, decisions)
    _persist_task(task_id, status="writing", decisions=decisions,
                  snapshot_id=plan.get("snapshotId") or record.get("snapshot_id") or "",
                  event={"kind": "resume", "payload": {"promoted": promoted, "approvals": len(approvals)}})
    timer = TaskTimer()
    timer.taskId = task_id
    saved_payload = bundle_data.get("payload") or {"pages": pages, "context": frozen_context, "draft": draft}
    saved_payload = dict(saved_payload)
    saved_payload["aiRemediation"] = payload.get("aiRemediation", saved_payload.get("aiRemediation", True))
    saved_payload["aiAgents"] = False  # do not rerun the audit agent
    bundle = AuditBundle(payload=saved_payload, pages=pages, context=frozen_context,
                         draft=draft, report=report, plan=plan, timer=timer,
                         decisionSet=decisions)
    return _finalize(bundle, review, meta, saved_payload, resume=True)


def build(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求数据无效。")
    if _is_resume(payload):
        return resume(payload)
    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("缺少标签图片数据。")
    context = payload.get("context")
    if not isinstance(context, dict):
        raise ValueError("审核上下文无效。")

    timer = TaskTimer()
    timer.mark("accepted")
    client_timing = payload.get("clientTiming") if isinstance(payload.get("clientTiming"), dict) else {}
    draft = payload.get("draft")
    if not isinstance(draft, dict):
        case = {"pages": pages}
        if payload.get("confirmation"):
            case["confirmation"] = payload["confirmation"]
        draft = extract(case)
        timer.mark("extract")
    knowledge.reset_timing()
    try:
        import labelreview.registry as registry_mod
        context = dict(context, versions=registry_mod.pin_versions(context, payload))
    except Exception:
        context = dict(context)
    audit_payload = {"pages": pages, "draft": draft, "context": context}
    for key in ("confirmation", "identityRevision"):
        if payload.get(key):
            audit_payload[key] = payload[key]
    report = audit(audit_payload)
    timer.mark("rules")
    knowledge_stats = knowledge.timing()
    plan = report.get("reportPlan")
    if not isinstance(plan, dict) or not plan.get("snapshotId"):
        return {"reportId": None, "snapshotId": None, "snapshotHash": None, "contentHash": None,
                "status": "failed", "report": report,
                "check": {"ok": False, "counts": [], "errors": [
                    {"code": "edit-not-applied", "message": "整改计划未生成，无法生成客户版",
                     "itemIds": [], "detail": ""}]},
                "meta": {"mode": "template", "aiRemediation": "unavailable", "aiAgents": "skipped"},
                "saved": None}
    bundle = AuditBundle(payload=payload, pages=pages, context=context, draft=draft,
                         report=report, plan=plan, timer=timer)
    meta = {"mode": "template", "aiRemediation": "skipped", "aiAgents": "skipped", "items": []}

    context = dict(context, _blocks=[{"id": block.get("id"), "text": block.get("text")}
                                     for page in pages for block in page.get("blocks") or []])
    context["_decisionTaskId"] = timer.taskId
    decisions = _decision_set_for(report, plan, context, task_id=timer.taskId)
    seed_from_report(decisions, report)
    bundle.context = context
    bundle.decisionSet = decisions

    # B/S2：先 AI 对照审核，再由程序分流、形成统一计划，最后才允许撰稿。
    review = {"status": "skipped", "candidates": [], "rejected": [], "promoted": []}
    if payload.get("aiAgents"):
        try:
            answer = runtime_agents.run_ai_review(bundle, recorder=timer)
        except Exception:
            answer = {"status": "failed", "candidates": [], "rejected": [],
                      "message": "AI 审核调用失败（超时或网络异常）"}
        candidates = _annotate_candidates(answer.get("candidates") or [], report, plan, context)
        if answer.get("lockedJudgments"):
            seed_from_report(decisions, {"items": [], "lockedJudgments": answer.get("lockedJudgments")})
        review = {"status": answer.get("status"), "model": _actual_model(answer), "at": _now(),
                  "candidates": candidates, "rejected": answer.get("rejected") or [],
                  "message": answer.get("message"), "usage": answer.get("usage") or [],
                  "provider": answer.get("provider") or [],
                  "coverage": answer.get("coverage") or [],
                  "lockedJudgments": answer.get("lockedJudgments") or [],
                  "evidenceVersionId": answer.get("evidenceVersionId"),
                  "versions": answer.get("versions") or {},
                  "promptComposition": answer.get("promptComposition") or {},
                  "diagnostics": answer.get("diagnostics") or {},
                  "modelRequestIssued": bool(answer.get("modelRequestIssued")
                                             or answer.get("status") == "result-unknown"),
                  "promoted": _promote_candidates(report, plan, candidates, context)}
        report["auditCoverage"] = review["coverage"]
        report["lockedJudgments"] = review["lockedJudgments"]
        report["evidenceVersionId"] = review["evidenceVersionId"]
        report["decisionVersion"] = decisions.decisionVersion
        if review["promoted"]:
            report["classification"] = _reclassify(report)
        persist_for_task(timer.taskId, decisions)
        # 如实记录本次审核状态：此前该字段一直停留在初始的 skipped，
        # 导致报告页首写「AI 审核未参与」，而实际已检查 25 项并提出候选。
        meta["aiAgents"] = review.get("status")
        meta["aiReviewPromptComposition"] = review.get("promptComposition") or {}
        meta["aiReviewDiagnostics"] = review.get("diagnostics") or {}
        timer.mark("ai-review")
    report["agentNotes"] = [{"title": row.get("claim"), "detail": row.get("reason")}
                            for row in review.get("candidates") or []]
    timer.mark("plan-locked")
    review["stats"] = _ai_stats(plan, review, report)
    report["aiReview"] = review
    _persist_task(
        timer.taskId, status="awaiting-human" if (review.get("candidates") and not review.get("promoted")) else "writing",
        decisions=decisions,
        bundle=_task_bundle(payload, bundle, review, meta),
        snapshot_id=plan.get("snapshotId") or "",
        model_request_issued=bool(review.get("modelRequestIssued") or review.get("status") == "result-unknown"),
        event={"kind": "ai-review", "payload": {"status": review.get("status"), "candidates": len(review.get("candidates") or [])}},
    )
    return _finalize(bundle, review, meta, payload, knowledge_stats=knowledge_stats)


def _task_bundle(payload, bundle, review, meta):
    return {
        "payload": _strip_payload(payload),
        "pages": bundle.pages,
        "draft": bundle.draft,
        "context": {k: v for k, v in dict(bundle.context or {}).items() if k != "_decisionSet"},
        "report": bundle.report,
        "plan": bundle.plan,
        "review": review,
        "meta": {k: v for k, v in dict(meta).items() if k != "timing"},
    }



def _ai_remediation_payload(meta):
    """Writer status and accepted count for the HTTP answer, not only inside meta."""
    return {
        "status": meta.get("aiRemediation"),
        "accepted": meta.get("aiAccepted", 0),
        "rejected": meta.get("aiRejected") or [],
        "writerAttempts": (meta.get("aiRemediationDiagnostics") or {}).get("writerAttempts"),
    }


def _finalize(bundle, review, meta, payload, *, resume=False, knowledge_stats=None):
    timer = bundle.timer
    report = bundle.report
    plan = bundle.plan
    draft = bundle.draft
    context = bundle.context
    decisions = bundle.decisionSet
    if decisions is None:
        decisions = _decision_set_for(report, plan, context, task_id=timer.taskId)
        seed_from_report(decisions, report)
        bundle.decisionSet = decisions
    client_timing = payload.get("clientTiming") if isinstance(payload.get("clientTiming"), dict) else {}
    knowledge_stats = knowledge_stats or {}
    if resume:
        meta["resumed"] = True
        meta["aiRerun"] = False
        review = dict(review)
        review["stats"] = _ai_stats(plan, review, report)
        report["aiReview"] = review

    report["remediations"] = {}
    if payload.get("aiRemediation"):
        decisions.freeze()
        persist_for_task(timer.taskId, decisions)
        answer = runtime_agents.build_remediation(bundle, recorder=timer)
        accepted = answer.get("remediations") or {}
        accepted, writer_rejected = gate_writer_revisions(decisions, accepted)
        if writer_rejected:
            answer["rejected"] = list(answer.get("rejected") or []) + writer_rejected
            answer["accepted"] = len(accepted)
            if not accepted:
                if answer.get("status") not in ("unavailable", "failed"):
                    answer["status"] = "failed"
            elif writer_rejected and answer.get("status") == "completed":
                answer["status"] = "partial"
        report["remediations"] = accepted
        report["decisionSet"] = {
            "decisionVersion": decisions.decisionVersion,
            "disputes": decisions.disputes,
            "rejectedRevisions": decisions.rejected_revisions(),
        }
        persist_for_task(timer.taskId, decisions)
        meta["aiRemediation"] = answer.get("status")
        meta["aiAccepted"] = answer.get("accepted", 0)
        meta["aiRejected"] = answer.get("rejected") or []
        # Writer diagnostics: attempts, tolerated normalizations, dropped exact
        # duplicates and a redacted excerpt of the raw model text (T20 17:31 follow-up).
        meta["aiRemediationDiagnostics"] = answer.get("diagnostics") or {}
        meta["planVersion"] = bundle.planVersion
        meta["mode"] = "ai" if accepted else "template"
        meta["decisionVersion"] = decisions.decisionVersion
        for item in plan.get("items") or []:
            if item.get("decisionState") != "confirmed" or not item.get("edit"):
                continue
            meta["items"].append({"itemId": item.get("itemId"), "actionId": item.get("actionId"),
                                  "source": "ai" if item.get("itemId") in accepted else "template"})
        timer.mark("ai-remediation")

    audited_at = timer.startedUtc
    report["auditMeta"] = {"model": _actual_model(review) or os.getenv("LABEL_REVIEW_MODEL", "") or "未配置",
                           "auditedAt": audited_at, "aiReview": review.get("status"),
                           "aiRemediation": meta.get("aiRemediation"),
                           "decisionVersion": decisions.decisionVersion}
    report.setdefault("decisionVersion", decisions.decisionVersion)
    persist_for_task(timer.taskId, decisions)
    report["planVersion"] = bundle.planVersion
    _apply_projection(report, plan, decisions)
    status = _status(payload, review, meta)
    report_id = _new_id("R")
    report["reportMeta"] = {"reportId": report_id, "taskId": timer.taskId, "status": status,
                            "mode": meta["mode"], "planVersion": bundle.planVersion}
    snap = snapshot_module.build(payload, report, plan, context, audited_at, report["auditMeta"]["model"])
    report["snapshotHash"] = snap["snapshotHash"]
    report["snapshotManifest"] = snap["manifest"]
    report["reportMeta"]["snapshotHash"] = snap["snapshotHash"]
    timer.mark("report-view")

    html_text, render_error = "", ""
    timer.mark("render-start")
    try:
        html_text = report_render.render(report, draft, context)
    except Exception as exc:
        render_error = str(exc)[:300]
    timer.mark("html-rendered")
    if html_text:
        html_result = html_check_module.run(
            html_text, report, plan, context,
            dict(review.get("stats") or {}, enabled=bool(payload.get("aiAgents")),
                 status=review.get("status")))
    else:
        html_result = {"ok": False, "errors": [{"code": "render-failed", "message": "最终 HTML 渲染失败",
                                                "detail": render_error}]}
    structural = report_check.run(report, plan, {"snapshot": plan.get("snapshotId")})
    timer.mark("validated")
    combined = list(structural.get("errors") or []) + list(html_result.get("errors") or [])
    check = {"ok": not combined, "errors": combined, "counts": structural.get("counts") or [],
             "html": {"ok": html_result.get("ok"), "errors": html_result.get("errors") or []}}
    report["checkErrors"] = {"ok": check["ok"], "errors": combined, "counts": check["counts"],
                             "html": check["html"]}
    report["reportMeta"]["check"] = check["ok"]

    if not check["ok"]:
        report["reportMeta"]["status"] = "check-failed"
        _persist_task(timer.taskId, status="check-failed", decisions=decisions,
                      bundle=_task_bundle(payload, bundle, review, meta),
                      snapshot_id=plan.get("snapshotId") or "",
                      event={"kind": "check-failed", "payload": {"errors": len(combined)}})
        return {"reportId": None, "snapshotId": None, "snapshotHash": snap["snapshotHash"],
                "contentHash": None, "report": report, "draft": draft, "check": check,
                "status": "check-failed",
                "aiRemediation": _ai_remediation_payload(meta),
                "meta": dict(meta, taskId=timer.taskId, snapshotHash=snap["snapshotHash"],
                             contentHash=None, timing=timer.snapshot(),
                             decisionVersion=decisions.decisionVersion,
                             resumed=bool(resume), aiRerun=False if resume else None),
                "saved": None}

    content_hash = hashlib.sha256(html_text.encode()).hexdigest()
    timer.mark("html-saved")
    timing_payload = _timing_payload(timer, client_timing, knowledge_stats)
    saved = _save_snapshot(bundle, check, review, meta, report_id, status, html_text, content_hash,
                           snap, timing_payload)
    _write_pdf_status(report_id, {"status": "pending", "reportId": report_id})
    _start_pdf(report_id, html_text, timer, client_timing.get("clickedUtc"))
    meta.update(snapshotId=plan.get("snapshotId"), taskId=timer.taskId,
                snapshotHash=snap["snapshotHash"], contentHash=content_hash,
                planVersion=bundle.planVersion, timing=timing_payload,
                decisionVersion=decisions.decisionVersion,
                resumed=bool(resume), aiRerun=False if resume else False)
    _persist_task(timer.taskId, status=status, decisions=decisions,
                  bundle=_task_bundle(payload, bundle, review, meta),
                  report_id=report_id, content_hash=content_hash,
                  snapshot_id=plan.get("snapshotId") or "",
                  model_request_issued=bool(review.get("modelRequestIssued") or status == "result-unknown"),
                  event={"kind": "saved", "payload": {"reportId": report_id, "status": status}})
    return {"reportId": report_id, "snapshotHash": snap["snapshotHash"],
            "contentHash": content_hash, "snapshotId": snap["snapshotHash"],
            "planVersion": bundle.planVersion, "decisionVersion": decisions.decisionVersion,
            "report": report, "draft": draft, "check": check, "status": status,
            "aiRemediation": _ai_remediation_payload(meta),
            "meta": meta, "saved": str(saved)}


def load(report_id):
    path = reports_dir() / ("%s.json" % os.path.basename(str(report_id)))
    if not path.exists():
        raise FileNotFoundError("报告不存在")
    return json.loads(path.read_text(encoding="utf-8"))
