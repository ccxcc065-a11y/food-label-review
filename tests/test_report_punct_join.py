"""报告渲染缺陷回归：待核对原因自带句号时，拼接不得渲染出「。；」「；；」「。。」。

report.js 原先对各条 pendingReview.reason 直接 join("；") 后再补「。」；
而原因文本本身以「。」结尾，于是渲染出「。；」，被 html_check 判为
residual-punctuation（status: check-failed），这类真实标签（营养表缺多项）写不成稿。

离线运行（不需要起服务、不需要网络）：
    cd 检测 && python3 -m unittest tests.test_report_punct_join -v
"""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import labelreview.html_check as html_check  # noqa: E402  必须在 sys.path 兜底之后导入

REPORT_JS = ROOT / "report.js"

# 与 tests/test_v15.py 相同的写法：在 Node 里加载 report.js，
# 把标准输入里的 payload 渲染成 HTML 输出到 stdout。
NODE_RENDER_SCRIPT = r"""
const { createRequire } = require("node:module");
const { readFileSync } = require("node:fs");
global.window = { location: { origin: process.env.REPORT_ORIGIN || "http://127.0.0.1:0" } };
createRequire(process.cwd() + "/__punct_join_report_require.js")(process.argv[1]);
const payload = JSON.parse(readFileSync(0, "utf8"));
process.stdout.write(window.LabelReport.render(payload));
"""

# 拼接后不得出现的残留标点组合。
RESIDUAL_TOKENS = ("。；", "；；", "。。")


def payload(*reasons):
    """构造一份最小报告数据，只放待核对的营养成分表原因。"""
    return {
        "result": {
            "factQa": {
                "nutritionTable": {
                    "pendingReview": [
                        {"key": "pending-%d" % index, "reason": reason, "label": "第 %d 项" % index}
                        for index, reason in enumerate(reasons, 1)
                    ]
                }
            }
        },
        "draft": {},
        "images": [],
        "context": {},
    }


class PendingReasonJoinTests(unittest.TestCase):
    """营养表「待核对：」那一段的标点拼接。"""

    def render(self, *reasons):
        node = shutil.which("node")
        if not node:
            self.skipTest("需要 Node.js 才能渲染 report.js")
        if not REPORT_JS.is_file():
            self.fail(f"找不到报告渲染脚本：{REPORT_JS}")
        completed = subprocess.run(
            [node, "-e", NODE_RENDER_SCRIPT, str(REPORT_JS)],
            input=json.dumps(payload(*reasons), ensure_ascii=False),
            capture_output=True, encoding="utf-8", cwd=str(ROOT), timeout=120,
        )
        if completed.returncode != 0:
            self.fail(
                "report.js 渲染失败（node 退出码 %d）：\n%s"
                % (completed.returncode, completed.stderr[-2000:])
            )
        html = completed.stdout
        self.assertTrue(html.strip(), "report.js 未输出任何 HTML")
        return html

    def assert_no_residual_punct(self, html, label):
        for token in RESIDUAL_TOKENS:
            position = html.find(token)
            if position < 0:
                continue
            snippet = html[max(0, position - 60):position + 60]
            self.fail(
                f"{label}：渲染结果不得出现残留标点「{token}」，"
                f"命中位置附近：…{snippet}…"
            )

    def qa_note(self, html):
        marker = "待核对："
        start = html.find(marker)
        self.assertGreaterEqual(start, 0, "报告里找不到「待核对：」段落，无法校验拼接结果")
        end = html.find("</p>", start)
        return html[start:end if end > start else len(html)]

    def test_reasons_ending_with_period_join_without_stray_punct(self):
        """两条以「。」结尾的原因：拼接后必须是「缺少饱和脂肪；缺少糖。」。"""
        html = self.render("缺少饱和脂肪。", "缺少糖。")
        # 整篇报告都不得出现残留标点（html_check 判的就是全文）。
        self.assert_no_residual_punct(html, "两条句号结尾原因")
        self.assertIn(
            "待核对：缺少饱和脂肪；缺少糖。", html,
            "待核对原因应以「；」连接、整段只留一个句末「。」，实际渲染：%r"
            % self.qa_note(html),
        )
        self.assertNotIn(
            "。", self.qa_note(html).removesuffix("。"),
            "待核对整段除末尾「。」外不应再出现句号，实际渲染：%r" % self.qa_note(html),
        )

    def test_edge_case_reasons_are_normalized_before_join(self):
        """尾随空白、尾随「；」、本身无句号的原因同样不能拼出残留标点。"""
        html = self.render("缺少饱和脂肪。", "缺少糖；", "缺少反式脂肪酸", "  缺少钠。  ")
        self.assert_no_residual_punct(html, "边界原因")
        self.assertIn(
            "待核对：缺少饱和脂肪；缺少糖；缺少反式脂肪酸；缺少钠。", html,
            "边界原因拼接结果不符合预期，实际渲染：%r" % self.qa_note(html),
        )

    def test_html_check_reports_no_residual_punctuation(self):
        """html_check 对该 HTML 不得报 residual-punctuation（其余检查项可忽略）。"""
        html = self.render("缺少饱和脂肪。", "缺少糖。")
        check = html_check.run(html, {}, {"items": []}, {"scope": "regular"})
        # 先证明这个 code 在检查器里是真会触发的，避免断言空转。
        corrupted = html_check.run(
            "<html><body><p>待核对：缺少饱和脂肪。；缺少糖。</p></body></html>",
            {}, {"items": []}, {"scope": "regular"},
        )
        corrupted_codes = [row.get("code") for row in corrupted.get("errors") or []]
        self.assertIn(
            "residual-punctuation", corrupted_codes,
            "检查器本身应能识别「。；」，否则本断言没有意义：%r" % corrupted_codes,
        )
        residual = [
            row for row in check.get("errors") or []
            if row.get("code") == "residual-punctuation"
        ]
        self.assertEqual(
            residual, [],
            "修复后 html_check 不应再报 residual-punctuation，实际：%r" % residual,
        )


if __name__ == "__main__":
    unittest.main()
