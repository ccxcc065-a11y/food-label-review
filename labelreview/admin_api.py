"""Admin API for the knowledge / rules registry. Execution reads the same store."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import labelreview.clause_index as clause_index
import labelreview.knowledge as knowledge
import labelreview.registry as registry_mod
class AdminError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _reg():
    return registry_mod.default_registry()


def _query(target):
    return {key: values[-1] if values else "" for key, values in parse_qs(target.query).items()}


def handle_get(path, target):
    query = _query(target)
    if path == "/api/admin/status":
        current = _reg().current()
        draft = _reg().get_draft()
        return {
            "current": current,
            "versions": _reg().current_versions(),
            "draft": {"status": draft.get("status"), "updatedAt": draft.get("updatedAt"),
                      "updatedBy": draft.get("updatedBy"), "basedOn": draft.get("basedOn"),
                      "reason": draft.get("reason")},
            "historyCount": len(_reg().history()),
        }
    if path == "/api/admin/knowledge":
        bundle = _reg().load_bundle()
        docs = []
        for item in ((bundle or {}).get("knowledge") or {}).get("manifest", {}).get("documents") or knowledge.library_documents():
            qa = (((bundle or {}).get("knowledge") or {}).get("ingestQa") or {}).get(item["id"]) or {}
            pack = _reg().clauses_for(item["id"]) or {}
            docs.append({
                **item,
                "kept": clause_index.keep_authorized_document(item),
                "wouldBlanket2011Drop": clause_index.is_blanket_2011_exclusion(item),
                "identity": clause_index.identify_standard(item.get("filename"), metadata=item),
                "verificationStatus": qa.get("status") or "待审核",
                "issueCount": qa.get("issueCount") or 0,
                "issues": qa.get("issues") or [],
                "clauseCount": pack.get("clauseCount") or len(pack.get("slices") or []),
            })
        return {
            "versions": _reg().current_versions(),
            "documents": docs,
            "fileLinks": _reg().file_links(),
            "corrections": ((bundle or {}).get("knowledge") or {}).get("corrections") or [],
        }
    if path == "/api/admin/knowledge/clause":
        document = query.get("document") or ""
        number = query.get("number") or ""
        standard = query.get("standard") or ""
        if standard and not document:
            record = knowledge.clause_for_standard(standard, number)
        else:
            record = knowledge.clause(document, number)
        if not record:
            raise AdminError(404, "未找到该条款。")
        return record
    if path == "/api/admin/knowledge/search":
        query_text = query.get("q") or ""
        mode = query.get("mode") or "keyword"
        if mode in {"exact", "keyword", "topic", "related"}:
            items = knowledge.search_clauses(query_text, mode=mode, document=query.get("document") or None)
        else:
            items = knowledge.search(query_text)
        return {"items": items, "mode": mode}
    if path == "/api/admin/knowledge/document":
        key = query.get("id") or ""
        pack = _reg().clauses_for(key)
        meta = knowledge.document_metadata(key)
        if not meta:
            raise AdminError(404, "未找到该文件。")
        slices = (pack or {}).get("slices") or []
        preview = [{k: item.get(k) for k in
                    ("id", "clause", "kind", "title", "location", "page", "paragraph",
                     "verificationStatus", "parent", "children", "related", "textHash")}
                   for item in slices]
        return {"document": meta, "clauseCount": (pack or {}).get("clauseCount"),
                "qa": ((_reg().load_bundle() or {}).get("knowledge") or {}).get("ingestQa", {}).get(key),
                "slices": preview, "conflict": _reg().conflict_for(key)}
    if path == "/api/admin/rules":
        import labelreview.rule_registry as rule_registry
        bundle_id = query.get("bundle") or query.get("bundleId") or None
        listed = rule_registry.list_rules(bundle_id or query.get("rulesVersion") or query.get("ruleReleaseId"))
        # Same published records as audit execution; list_rules only overlays
        # status / evidence / coverage flags. draftRules remain a separate draft.
        rules_dict = {item["id"]: item for item in listed.get("rules") or []}
        book = _reg().rules_bundle(listed.get("bundleId"))
        return {"versions": listed.get("versions") or _reg().current_versions(),
                "rules": rules_dict or (book.get("rules") or {}),
                "catalog": listed,
                "unregisteredCoverageIds": listed.get("unregisteredCoverageIds") or [],
                "citations": book.get("citations") or {},
                "draftRules": ((_reg().get_draft().get("rules") or {}).get("rules"))}
    if path == "/api/admin/interpretations":
        return {"published": _reg().interpretations(),
                "draft": (_reg().get_draft().get("interpretations") or {}).get("entries")}
    if path == "/api/admin/agents":
        bundle = _reg().load_bundle() or {}
        return {"published": bundle.get("agents") or {},
                "draft": _reg().get_draft().get("agents")}
    if path == "/api/admin/templates":
        bundle = _reg().load_bundle() or {}
        return {"published": bundle.get("templates") or {},
                "draft": _reg().get_draft().get("templates")}
    if path == "/api/admin/history":
        return {"current": _reg().current(), "history": _reg().history()}
    if path == "/api/admin/draft":
        draft = _reg().get_draft()
        # Do not ship the full clause pack in the draft payload.
        slim = dict(draft)
        knowledge_part = dict(slim.get("knowledge") or {})
        knowledge_part.pop("documents", None)
        slim["knowledge"] = knowledge_part
        return slim
    if path == "/api/admin/affected":
        return _reg().affected_assets()
    raise AdminError(404, "找不到此管理接口。")


def handle_post(path, payload):
    payload = payload if isinstance(payload, dict) else {}
    editor = str(payload.get("editor") or payload.get("by") or "admin")
    reason = str(payload.get("reason") or "")
    if path == "/api/admin/draft":
        draft = _reg().save_draft(payload.get("patch") or payload, editor=editor, reason=reason)
        checks = _reg().auto_check(draft)
        affected = _reg().affected_assets(draft)
        return {"draft": _slim_draft(draft), "checks": checks, "affected": affected,
                "note": "草稿已保存，尚未改变正在使用的审核规则。须发布后才会生效。"}
    if path == "/api/admin/trial":
        trial = _reg().trial_run(_reg().get_draft())
        return trial
    if path == "/api/admin/publish":
        source = str(payload.get("knowledgeSource") or "published")
        if source not in {"published", "rebuild"}:
            raise AdminError(400, "knowledgeSource 只能是 published 或 rebuild。")
        result = _reg().publish(editor=editor, reason=reason or "后台审核确认后发布",
                                knowledge_source=source)
        return {"ok": True, "current": result["current"], "checks": result["checks"],
                "trial": {"summary": (result.get("trial") or {}).get("summary"),
                          "diff": (result.get("trial") or {}).get("diff")},
                "affected": result.get("affected"),
                "message": "已发布新版本。之后新的审核任务使用该版本；正在执行和历史任务仍使用启动时的版本。"}
    if path == "/api/admin/rollback":
        bundle_id = payload.get("bundleId") or payload.get("version")
        current = _reg().rollback(bundle_id, editor=editor, reason=reason)
        return {"ok": True, "current": current}
    if path == "/api/admin/corrections":
        # Parse typo correction: original file stays intact.
        document = payload.get("documentId") or payload.get("document")
        clause = payload.get("clause")
        original = payload.get("originalText")
        corrected = payload.get("correctedText")
        if not document or not clause or original is None or corrected is None:
            raise AdminError(400, "请同时提供文件、条款号、原文和校订文本。")
        row = {
            "documentId": document,
            "clause": clause,
            "originalText": original,
            "correctedText": corrected,
            "confirmedBy": editor,
            "confirmedAt": payload.get("confirmedAt") or "",
            "reason": reason or "解析校订",
        }
        draft = _reg().save_draft({"knowledge": {
            "corrections": list((_reg().get_draft().get("knowledge") or {}).get("corrections") or []) + [row],
        }}, editor=editor, reason=reason or "解析校订")
        return {"ok": True, "correction": row, "note": "校订已写入草稿，原文件未改动。发布后才会用于审核读取。"}
    if path == "/api/admin/slices":
        draft = _reg().save_draft({"knowledge": {
            "sliceEdits": list((_reg().get_draft().get("knowledge") or {}).get("sliceEdits") or []) + [{
                "documentId": payload.get("documentId"),
                "clause": payload.get("clause"),
                "action": payload.get("action") or "adjust-boundary",
                "parent": payload.get("parent"),
                "related": payload.get("related"),
                "start": payload.get("start"),
                "stop": payload.get("stop"),
                "reason": reason,
                "by": editor,
            }],
        }}, editor=editor, reason=reason or "调整切片")
        return {"ok": True, "note": "切片调整已写入草稿，发布后生效。"}
    if path == "/api/admin/ingest/refresh":
        pack = _reg().build_knowledge_pack(use_cache=False)
        # Refresh stays in QA / 待审核 until a new bundle is published.
        return {"ok": True, "fingerprint": pack.get("fingerprint"),
                "fileLinks": pack.get("fileLinks"),
                "ingestQa": {key: {k: v for k, v in row.items() if k != "pairDiff"}
                             for key, row in (pack.get("ingestQa") or {}).items()},
                "status": "待审核",
                "note": "已重新解析并完成质检，结果仍为待审核，不会自动改写已发布版本。"}
    if path == "/api/admin/ingest/approve":
        document = payload.get("documentId")
        if not document:
            raise AdminError(400, "请指定要批准的文件。")
        approvals = list((_reg().get_draft().get("knowledge") or {}).get("approvals") or [])
        approvals.append({"documentId": document, "approvedBy": editor, "reason": reason,
                          "status": "已审核"})
        _reg().save_draft({"knowledge": {"approvals": approvals}}, editor=editor, reason=reason or "批准入库")
        return {"ok": True, "note": "已记录批准。发布知识库版本后，后续审核任务才会使用该版本。"}
    raise AdminError(404, "找不到此管理接口。")


def _slim_draft(draft):
    slim = dict(draft)
    knowledge_part = dict(slim.get("knowledge") or {})
    knowledge_part.pop("documents", None)
    slim["knowledge"] = knowledge_part
    return slim
