"""服务端生成客户版 HTML 快照（与网页同一 report.js 渲染器）。"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NODE = (
    'global.window={location:{origin:"http://127.0.0.1"}};'
    'require("./report.js");'
    'const o=JSON.parse(require("node:fs").readFileSync(0,"utf8"));'
    'process.stdout.write(window.LabelReport.render(o));'
)


def render(result, draft, context):
    payload = {"result": result, "draft": draft, "images": [], "context": context}
    completed = subprocess.run(["node", "-e", NODE], input=json.dumps(payload, ensure_ascii=False).encode(),
                               cwd=ROOT, capture_output=True, timeout=120, check=True)
    return completed.stdout.decode()
