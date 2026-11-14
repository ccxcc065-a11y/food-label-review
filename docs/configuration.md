# 配置与运行

## 依赖

- Python 3.9+：用项目独立虚拟环境；`pip install -r requirements.txt` 安装固定版本的 Pillow 和 pypdf。
- Node.js 20+：运行 report.js，HTML 成稿需要它。
- 实际 OCR：macOS 使用 Apple Vision 和 `swiftc`；其他平台可另装 `rapidocr onnxruntime`。公开演示不需要这两条 OCR 路径。
- PDF：另外需要 Chrome/Chromium。可在 `/api/health` 查看本机 PDF 条件，缺失时仍可下载 HTML。

正常启动 `python server.py --open`；无 OCR 演示使用 `python server.py --demo --open`。服务只绑定 127.0.0.1，不是公网多租户部署方案。若端口占用使用 `--port`。

`--demo` 不清除或隔离已有知识库；使用过授权语料的目录仍采用其当前发布版。公开冒烟脚本限定无语料干净检出，发现原文件或其他已发布版本会拒绝导出预览。

## 可选 AI

在启动服务的同一终端设置：

```bash
export LABEL_REVIEW_API_KEY='你的密钥'
export LABEL_REVIEW_MODEL='服务商实际可用的模型标识'
python server.py
```

默认调用地址在模型实现中配置为 DeepSeek 的兼容接口。请核对模型能力；纯文本模型不能据此核验图片的字高和字重。密钥不要提交到 Git、报告或截图中。

正常模式下网页审核可以发送标签文字、校对字段和检索到的依据。启用前确认这些资料允许发送到所配置服务商。不配置 key/model 时不发起模型请求；`--demo` 即使有配置也禁用模型。

活跃角色文件在 `agents/ai-review.md` 与 `agents/remediation.md`。审核候选不会自动成为对外确定问题；撰稿不应改变决定。失败、截断和结果未知不是成功。公开仓库不提供模型稳定性保证。

## 授权知识库

1. 自行合法取得所需 PDF/DOCX；本仓库不提供下载或分发它们。
2. 元数据在 `knowledge/manifest.json`；把对应文件放入 `knowledge/documents/`。文件名和摘要要匹配，不能用另一版本冒充登记文件。
3. 在仓库根目录、已激活虚拟环境中执行：

```bash
python -c "from labelreview import registry; registry.default_registry().publish(editor='local-owner', reason='local authorized corpus', knowledge_source='rebuild')"
```

4. 重启服务，核对管理页、发布版本和条款原文后再使用。发布动作会更改本地活动版本，先保存需要保留的配置。

自行更换语料清单还需要检查切片、引用和规则适用范围；把文件放进目录不等于知识库已核验。无语料体验路径不会替你下载标准，也不会用模型常识补齐缺少的依据。

## 本地数据

图片和公开演示输入不经云端 OCR；正常模式的可选 AI 可能发送文字或在明确操作后发送裁切。成稿、任务和编译产物在 `.runtime/`，本地发布包在 `knowledge/registry/`，均已加入 Git 忽略清单。
