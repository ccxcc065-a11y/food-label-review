"""V2.8 ①：snapshotHash 清单与版本记录（可追溯，不用于命中复用旧报告）。"""
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(payload, report, plan, context, audited_at, model):
    """返回 {snapshotHash, manifest}；清单里不含密钥。"""
    pages = payload.get("pages") or []
    context = context if isinstance(context, dict) else {}
    images, evidence = [], []
    for page in pages:
        blocks = [{"id": block.get("id"), "x": block.get("x"), "y": block.get("y"),
                   "text": block.get("text")} for block in page.get("blocks") or []]
        images.append({"pageId": page.get("id"), "name": page.get("name"),
                       "blockCount": len(blocks), "blocksDigest": _digest(blocks)})
        evidence.extend({"blockId": block.get("id"), "pageId": page.get("id")} for block in blocks)

    refs = {}
    for item in plan.get("items") or []:
        for ref in item.get("clauseRefs") or []:
            if ref.get("documentId"):
                refs[ref["documentId"]] = True
    try:
        from labelreview.knowledge import document_metadata
        knowledge = []
        for document in sorted(refs):
            meta = document_metadata(document) or {}
            knowledge.append({"documentId": document, "name": meta.get("name"),
                              "filename": meta.get("filename"), "sha256": meta.get("sha256")})
    except Exception:
        knowledge = []

    templates = {
        "reviewNotesSha256": _file_sha(ROOT / "knowledge" / "review_notes.json")
        if (ROOT / "knowledge" / "review_notes.json").exists() else "",
        "rendererSha256": _file_sha(ROOT / "report.js"),
        "renderScriptSha256": _file_sha(ROOT / "report_render.py") if (ROOT / "report_render.py").exists() else "",
        "checkerSha256": _digest([_file_sha(ROOT / name) for name in
                                  ("report_check.py", "html_check.py") if (ROOT / name).exists()]),
        "serverVersion": _server_version(),
    }
    prompts = {}
    for name in ("ai-review.md", "remediation.md"):
        path = ROOT / "agents" / name
        prompts[name] = _file_sha(path) if path.exists() else ""
    try:
        import labelreview.registry as registry_mod
        versions = registry_mod.pin_versions(context, payload)
    except Exception:
        versions = context.get("versions") if isinstance(context.get("versions"), dict) else {}
    manifest = {
        "schema": "V2.8-snapshot-1",
        "versions": {
            "programVersion": versions.get("programVersion") or _server_version(),
            "knowledgeVersion": versions.get("knowledgeVersion") or "",
            "rulesVersion": versions.get("rulesVersion") or "",
            "promptVersion": versions.get("promptVersion") or "",
            "templateVersion": versions.get("templateVersion") or "",
            "bundleId": versions.get("bundleId") or "",
        },
        "auditDate": str(audited_at),
        "images": images,
        "evidenceMap": evidence,
        "ocr": {"engine": "apple-vision", "blockCount": len(evidence),
                "digest": _digest([row["blockId"] for row in evidence])},
        "manual": {"verifiedFields": context.get("verifiedFields") or [],
                   "staleVerifications": context.get("staleVerifications") or []},
        "attribution": {"confirmedBy": (report.get("attribution") or {}).get("confirmedBy"),
                        "pages": (report.get("attribution") or {}).get("pages") or [],
                        "coverage": report.get("coverage") or []},
        "context": {key: context.get(key) for key in
                    ("scope", "sampleKind", "scenario", "productionDate", "nutritionRequired",
                     "verified", "preview", "complete", "version") if key in context},
        "knowledge": knowledge,
        "rules": {"appVersion": _server_version(),
                  "planSchema": _plan_schema(),
                  "decisionContract": "V2.8"},
        "templates": templates,
        "ai": {"model": model or "", "promptSha256": prompts,
               "timeoutSeconds": _timeout(),  # STAGE_TIMEOUT (writer / default stage cap)
               "stageTimeoutSeconds": _timeout(),
               "reviewTimeoutSeconds": _review_timeout(),  # REVIEW_TIMEOUT actually applied to ai-review
               "reviewEnabled": bool(payload.get("aiAgents")),
               "remediationEnabled": bool(payload.get("aiRemediation"))},
    }
    return {"snapshotHash": _digest(manifest), "manifest": manifest}


def _server_version():
    try:
        import labelreview.server as server
        return server.APP_VERSION
    except Exception:
        return ""


def _plan_schema():
    try:
        import labelreview.report_plan as report_plan
        return report_plan.SCHEMA_VERSION
    except Exception:
        return None


def _timeout():
    try:
        import labelreview.runtime_agents as runtime_agents
        return runtime_agents.STAGE_TIMEOUT
    except Exception:
        return None


def _review_timeout():
    try:
        import labelreview.runtime_agents as runtime_agents
        return runtime_agents.REVIEW_TIMEOUT
    except Exception:
        return None
