import importlib
import argparse
import base64
import binascii
import io
import json
import mimetypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from labelreview.audit import audit
from labelreview.extract import extract, NUTRIENT_PATTERN
from labelreview.knowledge import document_metadata, resolve_document, search
from labelreview.reread import choose_blocks, flat, improve, reread_blocks, settle
from labelreview.typography import measure_image


ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}/"
OCR = ROOT / ".runtime" / "ocr"
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
OCR_SLOTS = threading.BoundedSemaphore(2)
STATIC_FILES = {
    "/": "index.html",
    "/app.js": "app.js",
    "/report.js": "report.js",
    "/style.css": "style.css",
    "/tokens.css": "tokens.css",
    "/admin": "admin.html",
    "/admin.html": "admin.html",
    "/admin.js": "admin.js",
}
DEMO_FILES = {
    "synthetic-label.png", "synthetic-ocr.json",
}
APP_VERSION = "1.4.2"


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


OCR_BACKEND = None
OCR_ENGINE = "本机 Apple Vision"
DEMO_MODE = False


def _rapidocr_site_packages():
    venv = ROOT / ".runtime" / "ocr-venv"
    if not venv.is_dir():
        return None
    matches = sorted((venv / "lib").glob("python*/site-packages"))
    return matches[-1] if matches else None


def _ensure_rapidocr():
    try:
        import rapidocr  # noqa: F401
        return True
    except ImportError:
        pass
    site = _rapidocr_site_packages()
    if site and str(site) not in sys.path:
        sys.path.insert(0, str(site))
    try:
        import rapidocr  # noqa: F401
        return True
    except ImportError:
        return False


def prepare_ocr():
    global OCR_BACKEND, OCR_ENGINE
    source = ROOT / "ocr.swift"
    swift = shutil.which("swiftc")
    if swift:
        if not (OCR.is_file() and os.access(OCR, os.X_OK) and OCR.stat().st_mtime_ns >= source.stat().st_mtime_ns):
            OCR.parent.mkdir(exist_ok=True)
            temporary = OCR.with_name(f"ocr-{os.getpid()}")
            try:
                subprocess.run([swift, "-O", str(source), "-o", str(temporary)], check=True)
                temporary.replace(OCR)
            finally:
                temporary.unlink(missing_ok=True)
        OCR_BACKEND = "vision"
        OCR_ENGINE = "本机 Apple Vision"
        return
    if _ensure_rapidocr():
        from labelreview.ocr_rapid import ENGINE_LABEL, get_engine
        get_engine()
        OCR_BACKEND = "rapidocr"
        OCR_ENGINE = ENGINE_LABEL
        return
    raise RuntimeError(
        "未找到可用的本机文字识别。macOS 请先运行 xcode-select --install 安装 Swift 命令行工具；"
        "其他系统请安装：pip install rapidocr onnxruntime"
    )


def recognize(data, media_type):
    signatures = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}
    if media_type not in signatures or not data.startswith(signatures[media_type]):
        raise RequestError(415, "只支持内容格式有效的 JPEG 或 PNG 图片。")
    if not OCR_SLOTS.acquire(timeout=4):
        raise RequestError(503, "正在识别其他图片，请稍后重试。")
    try:
        if OCR_BACKEND == "rapidocr":
            from labelreview.ocr_rapid import recognize_bytes
            try:
                return recognize_bytes(data)
            except ValueError as exc:
                raise RequestError(422, str(exc)) from exc
            except Exception as exc:
                raise RequestError(500, "无法启动本机文字识别，请重新启动审核工具。") from exc
        result = subprocess.run([str(OCR)], input=data, capture_output=True, timeout=45)
        response = json.loads(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise RequestError(504, "图片识别超时，请缩小图片或裁出标签后重试。") from exc
    except RequestError:
        raise
    except (OSError, ValueError) as exc:
        raise RequestError(500, "无法启动本机文字识别，请重新启动审核工具。") from exc
    finally:
        OCR_SLOTS.release()
    if result.returncode:
        raise RequestError(422, response.get("error", "无法识别该图片。"))
    return response


def reread_suspicious(data, blocks):
    """对低置信度或含可疑标点的 block 放大重读，返回 (applied, unresolved, scales)。"""
    chosen = choose_blocks(blocks)
    if not chosen:
        return [], [], []
    candidates = reread_blocks(data, chosen, read=lambda png: recognize(png, "image/png"), scales=(2, 4))
    applied, unresolved, scales = [], [], set()
    position = {id(block): index for index, block in enumerate(blocks)}
    for block, items in zip(chosen, candidates):
        for item in items:
            scales.add(item["scale"])
        settled = settle(items)
        if settled is None:
            block["reread"] = {"unresolved": True, "reason": "多次识别不一致"}
            unresolved.append({"index": position[id(block)], "text": block.get("text", "")[:80],
                               "reason": "多次识别不一致，图上该处看不清"})
            continue
        text, confidence, method = settled
        original = block.get("text", "")
        if flat(text) == flat(original):
            # 重读与整图一致：确认原值，不算改写，但让规则层知道这处已经放大核实过。
            block["reread"] = {"from": original, "to": original,
                               "scale": _scale_of(items, text, confidence), "method": method}
            applied.append({"index": position[id(block)], "from": original, "to": original,
                            "scale": block["reread"]["scale"], "method": method})
            continue
        if confidence > float(block.get("confidence", 0)):
            block["text"] = text
            block["reread"] = {"from": original, "to": text,
                               "scale": _scale_of(items, text, confidence), "method": method}
            applied.append({"index": position[id(block)], "from": original, "to": text,
                            "scale": block["reread"]["scale"], "method": method})
            continue
        block["reread"] = {"unresolved": True, "reason": "重读与整图不一致，且置信度未提升"}
        unresolved.append({"index": position[id(block)], "text": original[:80],
                           "reason": "重读与整图不一致，且置信度未提升"})
    return applied, unresolved, sorted(scales)


def _scale_of(candidates, text, confidence):
    for item in candidates:
        if item["text"].strip() == text and abs(item["confidence"] - confidence) < 1e-9:
            return item["scale"]
    return candidates[-1]["scale"] if candidates else 2


def typography_blocks(data, blocks):
    """只对配料表、营养表相关的块测字重：整页几百块全部测量没有必要。"""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGB")
        chosen = [block for block in blocks if _needs_typography(block)]
        measured = {id(block): measure_image(image, block) for block in chosen}
    ratios = [item["stroke_ratio"] for item in measured.values() if item.get("stroke_ratio")]
    page_ratio = sorted(ratios)[len(ratios) // 2] if len(ratios) >= 3 else None
    for block in chosen:
        item = measured[id(block)]
        if page_ratio and item.get("stroke_ratio"):
            item["baseline_ratio"] = round(item["stroke_ratio"] / page_ratio, 3)
        block["typography"] = item


def _needs_typography(block):
    text = block.get("text", "")
    if NUTRIENT_PATTERN.match(text):
        return True
    if "配料" in text:
        return True
    # 配料表是顿号分隔的枚举串，这比按产品写关键词更通用。
    return text.count("、") >= 2 and len(text) >= 8


def recognize_label(data, media_type):
    """整图识别后自动核对：可疑文字放大重读、营养数值重读、配料与营养表测字重。"""
    response = recognize(data, media_type)
    blocks = response["blocks"]
    payload = {}
    try:
        applied, unresolved, scales = reread_suspicious(data, blocks)
    except Exception as exc:  # 重读是增强步骤，失败时保留整图读数，交给规则层降级。
        print(f"可疑文字重读已跳过：{exc!r}", file=sys.stderr)
        applied, unresolved, scales = [], [], []
    try:
        table = improve(data, blocks, lambda png: recognize(png, "image/png")["blocks"])
    except Exception as exc:
        print(f"营养表重读已跳过：{exc!r}", file=sys.stderr)
        table = None
    if table:
        applied.extend(table["applied"])
        scales = sorted(set(scales) | set(table.get("scales") or []))
        payload["box"] = table["box"]
    if applied or unresolved:
        payload.update({"applied": applied, "unresolved": unresolved, "scales": scales})
    if payload:
        response["reread"] = payload
    try:
        typography_blocks(data, blocks)
    except Exception as exc:
        print(f"字重测量已跳过：{exc!r}", file=sys.stderr)
    return response


def review_crop(payload):
    url = payload.get("image")
    if not isinstance(url, str):
        raise RequestError(400, "请先选择一块图片区域。")
    prefix, comma, content = url.partition(",")
    if not comma or prefix not in {"data:image/jpeg;base64", "data:image/png;base64"}:
        raise RequestError(400, "请提交 JPEG 或 PNG 格式的疑点裁切。")
    try:
        data = base64.b64decode(content, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RequestError(400, "疑点裁切无法解码。") from exc
    media_type = prefix[5:].split(";")[0]
    key = os.getenv("LABEL_REVIEW_API_KEY", "").strip()
    if not key:
        blocks = recognize(data, media_type)["blocks"]
        lines = sorted(blocks, key=lambda block: (round(block["box"][1], 2), block["box"][0]))
        candidate = "\n".join(block["text"] for block in lines)
        return {"candidate": candidate, "engine": OCR_ENGINE,
                "note": "已对裁切区域独立识别。候选读数请对照原图确认，未自动写回。"}
    if payload.get("sendCloud") is not True:
        raise RequestError(400, "请在复核面板确认仅发送所选裁切到已配置的模型。")
    if len(data) > 3 * 1024 * 1024:
        raise RequestError(413, "请选择更小的疑点区域。")
    body = {
        "model": os.getenv("LABEL_REVIEW_MODEL", "deepseek-flash"),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "只逐字抄录图片里清晰可读的食品标签文本。保留小数点、单位和百分号。看不清写〔无法识别〕。不要推算、修改或给出合规结论。"},
            {"type": "image_url", "image_url": {"url": url}},
        ]}],
        "max_tokens": 512,
    }
    request = urllib.request.Request("https://api.deepseek.com/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=18) as response:
            candidate = json.load(response)["choices"][0]["message"]["content"]
        if not isinstance(candidate, str):
            raise ValueError("invalid content")
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise RequestError(502, "模型暂未返回可用读数，请核对模型配置或改用原图手动核对。") from exc
    return {"candidate": candidate[:2000], "engine": "DeepSeek",
            "note": "这是裁切区域的模型候选读数，请对照原图确认。"}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def write_response(self, status, body, content_type, disposition=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, status, payload):
        self.write_response(
            status, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def check_local(self):
        port = self.server.server_port
        permitted_hosts = {f"{HOST}:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "").lower() not in permitted_hosts:
            raise RequestError(403, "请从本机审核地址打开工具。")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in permitted_hosts}:
            raise RequestError(403, "请从本机审核页面提交请求。")

    def read_body(self, maximum):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise RequestError(400, "请求长度无效。") from exc
        if length < 1:
            raise RequestError(400, "请求内容为空。")
        if length > maximum:
            raise RequestError(413, f"上传内容超过 {maximum // 1024 // 1024} MB，请缩小后重试。")
        try:
            data = self.rfile.read(length)
        except TimeoutError as exc:
            raise RequestError(408, "接收内容超时，请重试。") from exc
        if len(data) != length:
            raise RequestError(400, "请求内容未完整接收。")
        return data

    def do_GET(self):
        try:
            self.check_local()
            target = urlsplit(self.path)
            path = target.path
            if path == "/api/health":
                # 纪律第 6 条：APP_VERSION 是人工版本号，代码有没有变看 auditSha256。
                program = importlib.import_module("labelreview.registry").program_identity()
                self.json_response(200, {"app": "food-label-review", "version": APP_VERSION,
                                         "auditSha256": program.get("auditSha256"),
                                         "programVersion": program.get("programVersion"),
                                         "ocrEngine": OCR_ENGINE, "ocrBackend": OCR_BACKEND,
                                         "demoMode": DEMO_MODE,
                                         "cloudReview": not DEMO_MODE and bool(os.getenv("LABEL_REVIEW_API_KEY", "").strip()),
                                         "pdfChrome": importlib.import_module("labelreview.report_api").chrome_status(),
                                         "pdfStamp": importlib.import_module("labelreview.report_api").stamp_status()})
                return
            if path.startswith("/api/report/"):
                report_id = path.removeprefix("/api/report/")
                wanted = ""
                for suffix in (".html", ".pdf.json", ".pdf"):
                    if report_id.endswith(suffix):
                        report_id = report_id[:-len(suffix)]
                        wanted = suffix
                        break
                from labelreview.report_api import load
                try:
                    saved = load(report_id)
                except FileNotFoundError:
                    raise RequestError(404, "指定报告不存在。")
                if path.endswith(".html"):
                    self.write_response(200, saved.get("reportHtml", "").encode(),
                                        "text/html; charset=utf-8")
                elif wanted == ".pdf.json":
                    from labelreview.report_api import pdf_status
                    self.json_response(200, pdf_status(report_id))
                    return
                elif wanted == ".pdf":
                    from labelreview.report_api import _pdf_path, pdf_status
                    target = _pdf_path(report_id)
                    if wanted == ".pdf" and path.endswith(".pdf") and not target.exists():
                        status = pdf_status(report_id)
                        if status.get("status") == "failed":
                            self.json_response(409, {"status": "failed",
                                                     "reason": status.get("reason") or "PDF 生成失败",
                                                     "reportId": report_id,
                                                     "retry": "POST /api/report/%s.pdf" % report_id})
                            return
                        self.json_response(202, {"status": status.get("status", "pending"),
                                                 "reportId": report_id,
                                                 "message": "PDF 生成中，请稍后重试。"})
                        return
                    self.write_response(200, target.read_bytes(), "application/pdf")
                else:
                    self.json_response(200, saved)
                return
            if path == "/api/knowledge":
                query = parse_qs(target.query).get("q", [""])[0]
                self.json_response(200, {"items": search(query)})
                return
            if path.startswith("/api/admin/"):
                from labelreview.admin_api import handle_get, AdminError
                try:
                    self.json_response(200, handle_get(path, target))
                except AdminError as exc:
                    raise RequestError(exc.status, str(exc)) from exc
                return
            if path.startswith("/knowledge/"):
                key = path.removeprefix("/knowledge/")
                source = resolve_document(key)
                if source is None:
                    raise RequestError(404, "指定知识库中未找到原文，或原文完整性检查失败。")
                meta = document_metadata(key)
                mode = "inline" if source.suffix.lower() == ".pdf" else "attachment"
                disposition = f"{mode}; filename*=UTF-8''{quote(meta['filename'], safe='')}"
                kind = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
                self.write_response(200, source.read_bytes(), kind, disposition)
                return
            relative = STATIC_FILES.get(path)
            if path.startswith("/demos/") and path[7:] in DEMO_FILES:
                relative = path[1:]
            if not relative or not (ROOT / relative).is_file():
                raise RequestError(404, "找不到此文件。")
            kind = mimetypes.guess_type(relative)[0] or "application/octet-stream"
            if kind.startswith("text/") or kind == "application/javascript":
                kind += "; charset=utf-8"
            self.write_response(200, (ROOT / relative).read_bytes(), kind)
        except RequestError as exc:
            self.json_response(exc.status, {"error": str(exc)})

    def do_POST(self):
        try:
            self.check_local()
            path = urlsplit(self.path).path
            media_type = self.headers.get_content_type()
            # B2：旧接口退役（410），正常路径统一走 /api/report。
            retired = {"/api/audit": "/api/report", "/api/remediation": "/api/report", "/api/agents": "/api/report"}
            if path in retired:
                self.json_response(410, {"error": "该接口已退役，请使用 /api/report。",
                                          "retired": path, "migrateTo": retired[path]})
                return
            if DEMO_MODE and path in {"/api/ocr", "/api/review"}:
                raise RequestError(503, "当前是预置文字演示。识别自己的图片请去掉 --demo 重启，并配置本机 OCR。")
            if path == "/api/ocr":
                if media_type not in {"image/jpeg", "image/png"}:
                    raise RequestError(415, "只支持 JPEG 或 PNG 图片。")
                response = recognize_label(self.read_body(MAX_IMAGE_BYTES), media_type)
            elif path.startswith("/api/admin/"):
                if media_type != "application/json":
                    raise RequestError(415, "请提交 JSON 格式的数据。")
                try:
                    payload = json.loads(self.read_body(MAX_JSON_BYTES))
                except (ValueError, RecursionError) as exc:
                    raise RequestError(400, "JSON 数据无效，请重新提交。") from exc
                if not isinstance(payload, dict):
                    raise RequestError(400, "数据结构无效，请重新提交。")
                from labelreview.admin_api import handle_post, AdminError
                try:
                    response = handle_post(path, payload)
                except AdminError as exc:
                    raise RequestError(exc.status, str(exc)) from exc
                except ValueError as exc:
                    raise RequestError(400, str(exc)) from exc
            elif path.startswith("/api/report/") and path.endswith(".pdf"):
                report_id = path.removeprefix("/api/report/").removesuffix(".pdf")
                from labelreview.report_api import retry_pdf
                response = retry_pdf(report_id)
                status = 200 if response.get("status") in {"ready", "pending"} else 409
                self.json_response(status, response)
                return
            elif path in {"/api/extract", "/api/audit", "/api/review", "/api/agents", "/api/remediation", "/api/report"}:
                if media_type != "application/json":
                    raise RequestError(415, "请提交 JSON 格式的数据。")
                try:
                    payload = json.loads(self.read_body(MAX_JSON_BYTES))
                except (ValueError, RecursionError) as exc:
                    raise RequestError(400, "JSON 数据无效，请重新提交。") from exc
                if not isinstance(payload, dict):
                    raise RequestError(400, "数据结构无效，请重新提交。")
                if path == "/api/agents":
                    from labelreview.runtime_agents import run_agents
                    response = run_agents(payload)
                elif path == "/api/remediation":
                    from labelreview.runtime_agents import build_remediation
                    response = build_remediation(payload)
                elif path == "/api/report":
                    from labelreview.report_api import build as build_report
                    from labelreview.decision_set import StaleRevisionError
                    if DEMO_MODE:
                        # A demo process must never spend the owner's model key.
                        payload["aiAgents"] = False
                        payload["aiRemediation"] = False
                        payload["sendCloud"] = False
                        context = payload.get("context") or {}
                        if not isinstance(context, dict):
                            raise RequestError(400, "审核设置必须是对象。")
                        payload["context"] = dict(context, demoMode=True)
                    try:
                        response = build_report(payload)
                    except StaleRevisionError as exc:
                        raise RequestError(409, str(exc)) from exc
                else:
                    operation = {"/api/extract": extract, "/api/audit": audit, "/api/review": review_crop}[path]
                    response = operation(payload)
            else:
                raise RequestError(404, "找不到此接口。")
            self.json_response(200, response)
        except RequestError as exc:
            self.json_response(exc.status, {"error": str(exc)})
        except ValueError as exc:
            self.json_response(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self.log_error("后台处理出错，请重试。")
            self.json_response(500, {"error": "本机审核出错，请检查当前填写内容后重试。"})


def running_here(url):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + "api/health", timeout=1) as response:
            health = json.load(response)
            return (health.get("app") == "food-label-review" and health.get("version") == APP_VERSION
                    and health.get("demoMode", False) == DEMO_MODE)
    except (OSError, ValueError, urllib.error.URLError):
        return False


def open_browser(url):
    webbrowser.open(url)


def main():
    global DEMO_MODE, OCR_BACKEND, OCR_ENGINE
    parser = argparse.ArgumentParser(description="本地食品标签审核工具")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--port", type=int, help="指定监听端口")
    parser.add_argument("--demo", action="store_true", help="使用公开合成样例的预置文字，不启动 OCR 或云端 AI")
    options = parser.parse_args()
    DEMO_MODE = options.demo
    ports = [options.port] if options.port is not None else [PORT, PORT + 1, 18866, 18869, 18871]
    for port in ports:
        url = f"http://{HOST}:{port}/"
        if running_here(url):
            print("审核工具已启动 " + url)
            if options.open:
                open_browser(url)
            return
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
        except OSError as exc:
            error = exc
            continue
        try:
            with server:
                if DEMO_MODE:
                    OCR_BACKEND = "demo"
                    OCR_ENGINE = "预置文字演示（未运行 OCR）"
                else:
                    prepare_ocr()
                print("审核工具运行于 " + url + "  ·  Ctrl+C 停止", flush=True)
                if options.open:
                    open_browser(url)
                server.serve_forever()
        except KeyboardInterrupt:
            return
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            error = exc
            break
        return
    print(f"启动失败。{error}", file=sys.stderr)
    raise SystemExit(1) from error


if __name__ == "__main__":
    main()
