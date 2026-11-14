async function request(url, value) {
  const options = value === undefined ? {} : {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(value),
  };
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.message || "请求失败");
  return data;
}

const feedback = document.getElementById("feedback");
function say(message, ok = true) {
  feedback.hidden = !message;
  feedback.textContent = message || "";
  feedback.style.background = ok ? "" : "#f8e6e6";
}

function $(id) { return document.getElementById(id); }

let statusData = null;
let knowledgeData = null;
let rulesData = null;

async function loadStatus() {
  statusData = await request("/api/admin/status");
  const v = statusData.versions || {};
  $("versionLine").textContent =
    `当前发布 ${v.bundleId || "—"}  · 知识库 ${v.knowledgeVersion || "—"}  · 规则 ${v.rulesVersion || "—"}  · 提示词 ${v.promptVersion || "—"}  · 模板 ${v.templateVersion || "—"}  · 程序 ${v.programVersion || "—"}`;
}

function switchTab(name) {
  document.querySelectorAll(".admin-tabs button").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.tab === name));
  });
  ["knowledge", "rules", "interpretations", "agents", "publish"].forEach((tab) => {
    const node = document.getElementById(`tab-${tab}`);
    if (node) node.hidden = tab !== name;
  });
}

async function loadKnowledge() {
  knowledgeData = await request("/api/admin/knowledge");
  const body = $("docTable").querySelector("tbody");
  body.innerHTML = "";
  for (const doc of knowledgeData.documents || []) {
    const tr = document.createElement("tr");
    const conflict = (knowledgeData.fileLinks || []).find((row) =>
      (row.files || []).some((file) => file.documentId === doc.id) && row.conflict);
    tr.innerHTML = `<td>${doc.name || ""}<br><span class="admin-muted">${doc.filename || doc.id}</span></td>
      <td>${doc.verificationStatus || ""}${conflict ? ' <span class="admin-flag">PDF/Word 有差异</span>' : ""}</td>
      <td>${doc.clauseCount || 0}</td>`;
    tr.style.cursor = "pointer";
    tr.addEventListener("click", () => showDocument(doc.id));
    body.appendChild(tr);
  }
}

async function showDocument(id) {
  const data = await request(`/api/admin/knowledge/document?id=${encodeURIComponent(id)}`);
  const qa = data.qa || {};
  const issues = (qa.issues || []).map((item) => `<li>${item.code}：${item.detail}</li>`).join("") || "<li>未发现自动质检问题</li>";
  const slices = (data.slices || []).slice(0, 80).map((item) =>
    `<tr><td><button type="button" class="quiet" data-clause="${item.clause}">${item.clause || ""}</button></td><td>${item.kind || ""}</td><td>${item.title || ""}</td><td>${item.location || ""}</td></tr>`
  ).join("");
  const conflict = data.conflict;
  $("docDetail").innerHTML = `
    <h3>${(data.document || {}).name || id}</h3>
    <p class="admin-muted">${(data.document || {}).filename || ""} · ${(data.document || {}).sha256 || ""}</p>
    <p>质检状态：${qa.status || "待审核"}；解析 ${data.clauseCount || 0} 条。</p>
    ${conflict && !conflict.agree ? `<p class="admin-flag">与成对的 PDF/Word 有 ${conflict.diffCount || 0} 处条款差异，须先核对，不能随机取用。</p>` : ""}
    <ul>${issues}</ul>
    <div class="admin-toolbar">
      <button type="button" class="quiet" id="approveDoc">批准此文件</button>
    </div>
    <table class="admin-table"><thead><tr><th>条款</th><th>类型</th><th>标题</th><th>位置</th></tr></thead><tbody>${slices}</tbody></table>
    <div id="clauseView"></div>
    <h4>解析校订（原文件不改）</h4>
    <div class="admin-side-by-side">
      <label>原文片段<textarea id="corrOriginal" rows="4"></textarea></label>
      <label>校订文本<textarea id="corrCorrected" rows="4"></textarea></label>
    </div>
    <div class="admin-toolbar">
      <input id="corrClause" placeholder="条款号">
      <input id="corrReason" placeholder="校订原因">
      <button type="button" class="quiet" id="saveCorrection">写入草稿</button>
    </div>`;
  $("docDetail").querySelectorAll("[data-clause]").forEach((button) => {
    button.addEventListener("click", async () => {
      const record = await request(`/api/admin/knowledge/clause?document=${encodeURIComponent(id)}&number=${encodeURIComponent(button.dataset.clause)}`);
      $("corrClause").value = button.dataset.clause;
      $("corrOriginal").value = record.originalText || record.text || "";
      const related = (record.relatedSlices || []).map((item) => `${item.clause} ${item.title || ""}`).join("；");
      $("clauseView").innerHTML = `<p><strong>${record.clause}</strong> · ${record.location || ""} · ${record.verificationStatus || ""}</p>
        <pre>${record.text || ""}</pre>
        ${record.conflict && record.conflict.clauseDiff ? `<p class="admin-flag">${record.conflict.message}</p>` : ""}
        ${related ? `<p class="admin-muted">关联：${related}</p>` : ""}`;
    });
  });
  $("approveDoc").addEventListener("click", async () => {
    await request("/api/admin/ingest/approve", { documentId: id, editor: $("editorName")?.value || "admin", reason: "核对后批准" });
    say("已记录批准，发布后才会进入已发布库。");
  });
  $("saveCorrection").addEventListener("click", async () => {
    await request("/api/admin/corrections", {
      documentId: id,
      clause: $("corrClause").value,
      originalText: $("corrOriginal").value,
      correctedText: $("corrCorrected").value,
      reason: $("corrReason").value,
      editor: $("editorName")?.value || "admin",
    });
    say("校订已写入草稿。原文件未改动，发布后生效。");
  });
}

async function searchKnowledge() {
  const q = $("knowledgeQuery").value.trim();
  const mode = $("searchMode").value;
  const data = await request(`/api/admin/knowledge/search?q=${encodeURIComponent(q)}&mode=${encodeURIComponent(mode)}`);
  const items = data.items || [];
  $("searchResults").innerHTML = items.length
    ? `<table class="admin-table"><thead><tr><th>文件</th><th>条款</th><th>摘录</th></tr></thead><tbody>${
      items.map((item) => `<tr><td>${item.documentId || item.name || ""}</td><td>${item.clause || ""}</td><td>${(item.excerpt || item.text || item.message || "").slice(0, 200)}</td></tr>`).join("")
    }</tbody></table>`
    : `<p class="admin-muted">没有检索结果。</p>`;
}

async function loadRules() {
  rulesData = await request("/api/admin/rules");
  const body = $("ruleTable").querySelector("tbody");
  body.innerHTML = "";
  const rules = rulesData.draftRules || rulesData.rules || {};
  for (const [id, rule] of Object.entries(rules)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${id}<br><span class="admin-muted">${rule.title || ""}</span></td>
      <td>${rule.enabled === false ? '<span class="admin-flag">已停用</span>' : "启用"}</td>
      <td>${rule.implementationVersion || ""}（${rule.implementationKind || "code"}）</td>`;
    tr.style.cursor = "pointer";
    tr.addEventListener("click", () => showRule(id, rule));
    body.appendChild(tr);
  }
}

function showRule(id, rule) {
  const cites = (rule.citations || []).map((item) => `${item.document || ""} ${item.clause || ""}`).join("；");
  $("ruleDetail").innerHTML = `
    <h3>${id} ${rule.title || ""}</h3>
    <p>检查：${rule.checks || "—"}</p>
    <p>适用：${rule.appliesWhen || "—"}</p>
    <p>所需证据：${rule.requiredEvidence || "—"}</p>
    <p>判断：${rule.judgment || "—"}</p>
    <p>依据：${cites || "—"}</p>
    <p>实现版本：${rule.implementationVersion || "—"}。${rule.implementationKind === "code" ? "算法变更仍需开发发布。" : ""}</p>
    <label>词表 / 参数（JSON）<textarea id="rulePatch" rows="8">${JSON.stringify({ enabled: rule.enabled !== false, lexicons: rule.lexicons || {}, parameters: rule.parameters || {} }, null, 2)}</textarea></label>
    <div class="admin-toolbar">
      <button type="button" class="quiet" id="disableRule">停用并写入草稿</button>
      <button type="button" class="quiet" id="enableRule">启用并写入草稿</button>
      <button type="button" class="primary" id="saveRule">保存草稿</button>
    </div>
    <p class="admin-muted">保存草稿不会改变当前审核行为。发布后，从正常审核入口运行才会使用新版。停用后对应项目显示「未检查／未覆盖」，不会因此显示通过。</p>`;
  const save = async (patch) => {
    await request("/api/admin/draft", {
      editor: $("editorName")?.value || "admin",
      reason: `修改规则 ${id}`,
      patch: { rules: { rules: { [id]: patch } } },
    });
    say("规则草稿已保存，尚未改变正在使用的审核行为。");
    await loadRules();
  };
  $("saveRule").addEventListener("click", async () => {
    const patch = JSON.parse($("rulePatch").value);
    await save(patch);
  });
  $("disableRule").addEventListener("click", () => save({ enabled: false }));
  $("enableRule").addEventListener("click", () => save({ enabled: true }));
}

async function loadInterpretations() {
  const data = await request("/api/admin/interpretations");
  const entries = data.draft || data.published || {};
  $("interpDetail").innerHTML = Object.keys(entries).length
    ? Object.entries(entries).map(([key, entry]) => `
        <article>
          <h3>${key}</h3>
          <p>状态：${entry.status || "pending"}；核定：${entry.verifiedBy || "—"} ${entry.verifiedAt || ""}</p>
          <p>适用条件：${entry.applicability || ""}</p>
          <pre>${JSON.stringify(entry.requirements || entry, null, 2)}</pre>
        </article>`).join("")
    : "<p>当前没有审核口径条目。</p>";
}

async function loadAgents() {
  const data = await request("/api/admin/agents");
  const roles = (data.draft || data.published || {}).roles || {};
  $("agentDetail").innerHTML = Object.values(roles).map((role) => `
    <article>
      <h3>${role.title || role.id}</h3>
      <p>职责：${role.duties || ""}</p>
      <p>可读知识范围：${role.knowledgeScope || ""}</p>
      <p>工具权限：${(role.toolPermissions || []).join("、")}</p>
      <p>输入输出约定：${role.ioContract || ""}</p>
      <label>提示词<textarea data-role="${role.id}" rows="10">${role.prompt || ""}</textarea></label>
    </article>`).join("") +
    `<div class="admin-toolbar"><button type="button" class="primary" id="saveAgents">保存提示词到草稿</button>
     <p class="admin-muted">修改后须回归并随统一版本发布。</p></div>`;
  const button = $("saveAgents");
  if (button) button.addEventListener("click", async () => {
    const patch = { roles: {} };
    $("agentDetail").querySelectorAll("textarea[data-role]").forEach((node) => {
      patch.roles[node.dataset.role] = { prompt: node.value };
    });
    await request("/api/admin/draft", { editor: $("editorName")?.value || "admin", reason: "修改 Agent 提示词", patch: { agents: patch } });
    say("Agent 配置已写入草稿，发布并回归后生效。");
  });
}

async function loadHistory() {
  const data = await request("/api/admin/history");
  const body = $("historyTable").querySelector("tbody");
  body.innerHTML = (data.history || []).slice().reverse().map((row) => `
    <tr>
      <td>${row.at || ""}</td>
      <td>${row.action || ""}</td>
      <td>${row.bundleId || ""}</td>
      <td>${row.reason || ""}</td>
      <td>${row.bundleId && row.bundleId !== (data.current || {}).bundleId
        ? `<button type="button" class="quiet" data-rollback="${row.bundleId}">回滚到此版本</button>` : "当前"}</td>
    </tr>`).join("");
  body.querySelectorAll("[data-rollback]").forEach((button) => {
    button.addEventListener("click", async () => {
      await request("/api/admin/rollback", {
        bundleId: button.dataset.rollback,
        editor: $("editorName").value || "admin",
        reason: $("publishReason").value || `回滚到 ${button.dataset.rollback}`,
      });
      say("已回滚并形成新的发布记录。历史报告仍保留其原始版本号。");
      await loadStatus();
      await loadHistory();
    });
  });
}

async function showAffected() {
  const data = await request("/api/admin/affected");
  $("affectedBox").innerHTML = `<h3>受影响的规则、口径和模板</h3>
    <p>规则：${(data.rules || []).map((item) => item.id || item).join("、") || "无"}</p>
    <p>审核口径：${(data.interpretations || []).join("、") || "无"}</p>
    <p>模板：${(data.templates || []).join("、") || "无"}</p>
    <p class="admin-muted">修改致敏物质等依据时，应把列出的项目一起核对后发布，避免知识库已更新而 Agent 仍用旧口径。</p>`;
}

document.querySelectorAll(".admin-tabs button").forEach((button) => {
  button.addEventListener("click", async () => {
    switchTab(button.dataset.tab);
    if (button.dataset.tab === "rules") await loadRules();
    if (button.dataset.tab === "interpretations") await loadInterpretations();
    if (button.dataset.tab === "agents") await loadAgents();
    if (button.dataset.tab === "publish") { await showAffected(); await loadHistory(); }
  });
});
$("searchKnowledge").addEventListener("click", () => searchKnowledge().catch((err) => say(err.message, false)));
$("refreshIngest").addEventListener("click", async () => {
  const result = await request("/api/admin/ingest/refresh", {});
  say(result.note || "质检完成，仍为待审核。");
  await loadKnowledge();
});
$("runChecks").addEventListener("click", async () => {
  const draft = await request("/api/admin/draft");
  const result = await request("/api/admin/draft", { patch: {}, reason: "检查草稿", editor: $("editorName").value || "admin" });
  $("trialBox").innerHTML = `<h3>自动检查</h3><pre>${JSON.stringify(result.checks, null, 2)}</pre>`;
  await showAffected();
});
$("runTrial").addEventListener("click", async () => {
  const trial = await request("/api/admin/trial", {});
  $("trialBox").innerHTML = `<h3>修改前后差异</h3><pre>${JSON.stringify(trial.diff || trial.summary, null, 2)}</pre>`;
  say(`试跑完成，变更 ${trial.summary?.changedItems || 0} 项。`);
});
$("publishNow").addEventListener("click", async () => {
  const result = await request("/api/admin/publish", {
    editor: $("editorName").value || "admin",
    reason: $("publishReason").value || "审核确认后发布",
  });
  say(result.message);
  $("trialBox").innerHTML = `<h3>发布结果</h3><pre>${JSON.stringify({ current: result.current, trial: result.trial, affected: result.affected }, null, 2)}</pre>`;
  await loadStatus();
  await loadHistory();
  await loadKnowledge();
});

loadStatus().then(loadKnowledge).catch((err) => say(err.message, false));
