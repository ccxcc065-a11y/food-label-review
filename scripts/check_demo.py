"""Smoke-test a running --demo instance, including saved report bytes.

Usage: python scripts/check_demo.py http://127.0.0.1:8765 [--write-preview]
This tests distribution and the no-corpus path, not legal accuracy or live OCR.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def check(base, write_preview=False):
    # The no-pass assertions and publishable preview only apply to a clean,
    # corpus-free checkout. Never export a user's licensed local corpus.
    documents = ROOT / "knowledge" / "documents"
    assert not documents.exists() or not any(p.is_file() for p in documents.rglob("*")), \
        "此脚本仅验证无语料检出，请在不含授权正文的临时副本运行。"
    current = ROOT / "knowledge" / "registry" / "current.json"
    if current.exists():
        assert json.loads(current.read_text(encoding="utf-8")).get("bundleId") == "bundle-bootstrap", \
            "当前已有本地发布版，请使用无语料临时副本，避免导出演示之外的资料。"
    # Never route local labels through a system HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(base.rstrip("/") + path, data=body,
                                     headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=90) as response:
            return response.read()

    health = json.loads(request("/api/health"))
    assert health["demoMode"] is True and health["cloudReview"] is False, health
    for path in ["/", "/admin", "/style.css", "/tokens.css", "/app.js", "/report.js",
                 "/demos/synthetic-label.png", "/demos/synthetic-ocr.json"]:
        assert request(path), path
    fixture = json.loads(request("/demos/synthetic-ocr.json"))
    assert fixture["synthetic"] is True
    pages = [{"id": "demo-page", "name": "synthetic-label.png", "blocks": fixture["blocks"],
              "areas": [], "readability": "unknown"}]
    draft = json.loads(request("/api/extract", {"pages": pages}))
    assert draft["fields"]["name"]["value"] == "虚构原味饼干"
    assert draft["fields"]["basis"]["value"] == "每100克"
    assert set(draft["nutrition"]) == {"energy", "protein", "fat", "saturated", "carb", "sugar", "sodium"}
    # Deliberately request AI: --demo must disable it on the server as well.
    started = time.monotonic()
    answer = json.loads(request("/api/report", {
        "pages": pages, "draft": draft,
        "context": {"scope": "regular", "sampleKind": "artwork", "nutritionRequired": "required", "verified": False},
        "aiAgents": True, "aiRemediation": True, "sendCloud": True,
    }))
    elapsed = round(time.monotonic() - started, 3)
    assert answer["check"]["ok"] is True, answer["check"]
    assert answer["meta"]["aiAgents"] == "skipped", answer["meta"]
    assert answer["meta"]["aiRemediation"] == "skipped", answer["meta"]
    assert all(row.get("outcome") != "pass" for row in answer["report"]["items"])
    assert all(not row.get("source") for row in answer["report"]["items"]), \
        "发现实际来源，不能将此结果作为无语料公开样例导出。"
    report_id = answer["reportId"]
    html = request("/api/report/%s.html" % report_id)
    digest = hashlib.sha256(html).hexdigest()
    assert digest == answer["contentHash"]
    assert request("/api/report/%s.html" % report_id) == html
    try:
        request("/api/ocr", {})
    except urllib.error.HTTPError as exc:
        assert exc.code == 503, exc.code
    else:
        raise AssertionError("--demo must not run OCR")
    evidence = {"kind": "synthetic-no-corpus-smoke-test", "reportId": report_id,
                "contentHash": digest, "htmlReadySeconds": elapsed,
                "liveOcr": False, "modelCalls": 0, "nutritionRows": len(draft["nutrition"]),
                "checkOk": True, "businessAccuracyEvaluated": False}
    if write_preview:
        folder = ROOT / "docs"
        folder.mkdir(exist_ok=True)
        (folder / "demo-report.html").write_bytes(html)
        (folder / "demo-run.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:8765")
    parser.add_argument("--write-preview", action="store_true")
    args = parser.parse_args()
    check(args.base, args.write_preview)
