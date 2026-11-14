"use strict";

const $ = (id) => document.getElementById(id);
const samples = [
  { name: "徐州肉酱米线", file: "rice-noodles.jpg", tag: "组合包装", scope: "regular" },
  { name: "香辣烤鱼", file: "grilled-fish.png", tag: "营养复算", scope: "regular" },
  { name: "鑫选康牛肉酱", file: "beef-sauce.png", tag: "声称核对", scope: "regular" },
  { name: "米醋风味调味料", file: "seasoning.jpg", tag: "小作坊", scope: "workshop" },
  { name: "榛子提拉米苏口味蛋糕", file: "cake.png", tag: "进口食品", scope: "imported" },
  { name: "运康小米锅巴", files: ["guoba-front.jpg", "guoba-back.jpg"], tag: "实拍正反面", scope: "regular" },
];
const groups = { blocked: "已阻断", issue: "需处理", internal: "标签内对照", gap: "依据不足", review: "待核实", pass: "已检查" };
const areas = { front: "正面", back: "背面", side: "侧面", seal: "封口或喷码区", other: "其他区域" };
const fields = {
  name: "食品名称", ingredients: "配料表", net: "净含量", packs: "分包规格",
  producer: "生产者或经营者", address: "地址", contact: "联系方式",
  license: "生产许可证编号", standard: "产品标准代号", production: "生产日期标示",
  shelf: "保质期", expiry: "保质期到期日标示", storage: "贮存条件", basis: "营养表计量基准",
};
const nutrition = {
  energy: "能量", protein: "蛋白质", fat: "脂肪", saturated: "饱和脂肪",
  carb: "碳水化合物", sugar: "糖", sodium: "钠",
};
const scopeLabels = {
  unknown: "食品范围待确认", regular: "国产普通预包装", imported: "进口预包装",
  workshop: "食品小作坊", other: "其他或特殊食品",
};
const defaultContext = () => ({
  scope: "unknown", sampleKind: "unknown", productionDate: "", plannedProductionDate: "",
  nutritionRequired: "unknown", verified: false,
  boneClaimEvidence: "unknown", boneClaimBasis: "",
  layoutReviewed: false, layoutReviewBasis: "", boneArtworkReview: "unknown", allergenChecks: {},
});
const allergenLabels = {
  gluten: "含麸质谷物", crustaceans: "甲壳类", fish: "鱼类", egg: "蛋类",
  peanut: "花生", soy: "大豆", milk: "乳", nuts: "坚果",
};

let images = [];
let draft = null;
let result = null;
// B1：网页改走统一入口后，预览/下载/打印都绑定同一个 reportId。
let reportAnswer = null;
// V2.8：每次审核一个序号；旧任务晚返回时不得覆盖当前显示。
let auditSeq = 0;
let context = defaultContext();
let scopeSelected = false;
let sample = null;
let busy = false;
let stale = false;
let activeGroup = "review";
let activeMode = "results";
let selectedImage = 0;
let selectedRef = null;
let zoom = 1;
let nextImageId = 1;
const editedFields = new Set();
const editedRows = new Set();
// A6：人工核验记录只能由真实编辑/确认产生；字段值或证据变化即失效。
const verifiedFields = [];
const staleVerifications = [];

function currentImageDigest() {
  return images.map((image) => image.digest || image.id).join("|");
}

function bindManualCorrection(field, adopted, evidenceIds, extras = {}) {
  const refs = [...(evidenceIds || [])];
  const found = refs.map((ref) => findBlock(ref)).find(Boolean);
  const image = found ? images[found.index] : images[0];
  const block = found?.block;
  const nutrientKey = String(field).startsWith("营养表-") ? String(field).split("-")[1] : "";
  const nutrient = nutrientKey && draft?.nutrition ? draft.nutrition[nutrientKey] : null;
  const unit = extras.unit ?? (nutrient ? (nutrient.unit || "") : "");
  const original = extras.originalReading ?? (block?.originalText || block?.text || "");
  return {
    field,
    value: adopted,
    adoptedValue: adopted,
    evidenceIds: refs,
    confirmedAt: new Date().toISOString(),
    imageId: image?.id || "",
    imageDigest: image?.digest || "",
    evidenceDigest: "",
    region: block ? { blockId: block.id, box: [...(block.box || [])] } : null,
    originalReading: String(original ?? ""),
    unit,
    basis: String(draft?.fields?.basis?.value || extras.basis || ""),
    by: "人工",
    ...extras,
  };
}

function recordVerification(field, value, evidenceIds, extras) {
  const text = String(value ?? "");
  const refs = [...(evidenceIds || [])];
  if (!field || !text) return;
  const existing = verifiedFields.find((row) => row.field === field);
  if (existing && existing.value === text && JSON.stringify(existing.evidenceIds || []) === JSON.stringify(refs)
      && existing.imageDigest === (extras?.imageDigest || existing.imageDigest)
      && existing.basis === (draft?.fields?.basis?.value || existing.basis)) return;
  if (existing) staleVerifications.push({ ...existing, staleReason: "字段被重新编辑" });
  const kept = verifiedFields.filter((row) => row.field !== field);
  kept.push(bindManualCorrection(field, text, refs, extras || {}));
  verifiedFields.splice(0, verifiedFields.length, ...kept);
}

function currentVerifiedValue(row) {
  if (draft?.fields?.[row.field]) return String(draft.fields[row.field].value ?? "");
  const parts = String(row.field || "").split("-");
  if (parts[0] === "营养表" && parts[1] && draft?.nutrition?.[parts[1]]) {
    const item = draft.nutrition[parts[1]];
    if (parts[2] === "unit") return String(item.unit ?? "");
    if (parts[2] === "nrv") return String(item.nrv ?? "");
    if (parts[2] === "value") return String(item.value ?? "");
    return String(item.value ?? "") + String(item.unit ?? "");
  }
  return String(row.value ?? "");
}

function pruneVerifications() {
  if (!draft || !draft.fields) return;
  const digest = currentImageDigest();
  const basis = String(draft.fields.basis?.value || "");
  for (const row of [...verifiedFields]) {
    const field = draft.fields[row.field];
    const current = field ? String(field.value ?? "") : currentVerifiedValue(row);
    const refs = field ? [...(field.refs || [])] : [...(row.evidenceIds || [])];
    const imageChanged = (row.imageId && images.length && !images.some((image) => image.id === row.imageId))
      || (row.imageDigest && row.imageDigest.length === 64 && images.length
        && !images.some((image) => image.digest === row.imageDigest));
    const sessionChanged = row.evidenceDigest && digest && row.evidenceDigest !== digest
      && !images.some((image) => image.id === row.imageId);
    const basisChanged = row.basis != null && row.basis !== "" && row.basis !== basis;
    const unitChanged = row.unit != null && String(row.field).startsWith("营养表-") && row.unit !== String(
      draft.nutrition?.[String(row.field).split("-")[1]]?.unit || row.unit);
    if (current !== row.value || JSON.stringify(refs) !== JSON.stringify(row.evidenceIds || [])
        || imageChanged || sessionChanged || basisChanged || unitChanged) {
      verifiedFields.splice(verifiedFields.indexOf(row), 1);
      const staleReason = imageChanged || sessionChanged ? "原图或识别内容已变化"
        : basisChanged ? "计量基准已变化"
        : unitChanged ? "单位已变化"
        : "字段值或证据已变化";
      staleVerifications.push({ ...row, staleReason });
    }
  }
}
let reviewSelection = null;
let knowledgeQueryId = 0;
let confirmation = "";
let identityRevision = 0;

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function button(className, value, onClick) {
  const node = element("button", className, value);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

function feedback(message = "", error = false) {
  $("feedback").hidden = !message;
  $("feedback").textContent = message;
  $("feedback").classList.toggle("error", error);
}

function setBusy(value) {
  busy = value;
  for (const node of document.querySelectorAll("[data-upload], .sample-button, #reset, #audit, #runAgents, #confirmAttribution, #fieldEditor input, #fieldEditor textarea, #contextControls, #coverageControls input, #coverageControls select")) {
    node.disabled = value;
  }
  $("audit").textContent = value ? "处理中…" : "重新审核";
  $("workspace").setAttribute("aria-busy", String(value));
  updateActions();
}

async function request(url, value, contentType = "application/json") {
  const options = value === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": contentType },
    body: contentType === "application/json" ? JSON.stringify(value) : value,
  };
  const response = await fetch(url, options).catch(() => {
    throw new Error("无法连接本地服务。请重新打开「启动审核.command」。");
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok || !payload) {
    throw new Error(payload?.error || "本地服务暂时无法处理请求，请重试。");
  }
  return payload;
}

function pages() {
  return images.map(({ id, name, blocks, areas, readability, digest, packageLevel }) =>
    ({ id, name, blocks, areas, readability, digest, packageLevel }));
}

function casePayload() {
  return { pages: pages(), confirmation, identityRevision };
}

function updateActions() {
  $("report").disabled = busy || stale || result?.canPublish !== true;
  // 阶段 2：全文一致性检查未通过时，工作台可看底稿，但不能下载客户版。
  const checkFailed = result?.checkErrors?.ok === false;
  $("downloadReport").disabled = $("report").disabled || checkFailed;
  if ($("downloadPdf")) $("downloadPdf").disabled = $("downloadReport").disabled;
  $("report").title = result?.canPublish === false ? "先处理图片归属，再生成报告。" : "";
  $("downloadReport").title = checkFailed
    ? `全文一致性检查未通过：${(result.checkErrors.errors || []).map((error) => error.code).join("、")}`
    : "";
  $("reviewEvidence").disabled = busy || !selectedRef;
  $("removeImage").disabled = busy || !images.length;
}

async function refreshDraft() {
  const previous = draft;
  draft = null;
  const extracted = await request("/api/extract", casePayload());
  if (previous?.attribution?.status === "confirmed" && extracted.attribution?.status === "confirmed") {
    for (const [group, keys] of [["fields", editedFields], ["nutrition", editedRows]]) {
      for (const key of keys) {
        const item = previous[group][key];
        if (item?.refs?.length && item.refs.every(findBlock)) extracted[group][key] = item;
        else keys.delete(key);
      }
    }
  }
  draft = extracted;
  resetExpertReview();
  if (!scopeSelected) {
    const sampleScope = images.find((image) => image.sampleScope)?.sampleScope;
    context.scope = sampleScope || (Object.hasOwn(scopeLabels, draft.scopeHint) ? draft.scopeHint : "unknown");
  }
}

async function imageDigest(file) {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

function wipe() {
  for (const image of images) URL.revokeObjectURL(image.url);
  images = [];
  draft = null;
  result = null;
  sample = null;
  stale = false;
  confirmation = "";
  identityRevision = 0;
  activeMode = "results";
  activeGroup = "review";
  selectedImage = 0;
  selectedRef = null;
  zoom = 1;
  context = defaultContext();
  scopeSelected = false;
  editedFields.clear();
  editedRows.clear();
  if (verifiedFields.length) {
    staleVerifications.push(...verifiedFields.map((row) => ({ ...row, staleReason: "原图或识别内容已变化" })));
    verifiedFields.splice(0, verifiedFields.length);
  }
  $("previewImage").removeAttribute("src");
  $("fieldEditor").replaceChildren();
  $("settings").open = false;
  $("photoCoverage").open = false;
  feedback();
  renderWorkspace();
}

async function addImages(files, preset = null) {
  if (busy || !files.length) return;
  setBusy(true);
  const notes = [];
  let hasError = false;
  try {
    if (preset) {
      sample = preset;
      context.scope = preset.scope;
    }
    const before = images.length;
    for (const [index, file] of files.entries()) {
      feedback(`正在本地识别第 ${index + 1}/${files.length} 张图…`);
      try {
        if (file.size > 16 * 1024 * 1024) throw new Error("单张图片超过 16 MB。");
        if (!/\.(?:jpe?g|jfif|png)$/i.test(file.name)) throw new Error("请选择 JPG、JFIF 或 PNG。");
        const digest = await imageDigest(file);
        if (images.some((image) => image.digest === digest)) {
          notes.push(`${file.name} · 已在本次审核中`);
          continue;
        }
        if (images.length >= 8) throw new Error("一份审核最多可添加 8 张图。");
        const mime = /\.png$/i.test(file.name) ? "image/png" : "image/jpeg";
        const data = await request("/api/ocr", file, mime);
        if (!data.blocks.length) notes.push(`${file.name} · 未识别到文字，已保留原图供核对`);
        const id = `image-${nextImageId++}`;
        const blocks = data.blocks.map((block, offset) => ({ ...block, id: `${id}-${offset + 1}` }));
        images.push({
          id, name: file.name, blocks, digest, areas: [], readability: "unknown",
          sampleScope: preset?.scope, url: URL.createObjectURL(file),
        });
      } catch (error) {
        hasError = true;
        notes.push(`${file.name} · ${error.message}`);
      }
    }
    if (images.length === before) {
      if (!before) {
        sample = null;
        context = defaultContext();
      }
      feedback(notes.join("；") || "没有可用图片，请重试。", hasError || !before);
      return;
    }
    if (before === 0) selectedImage = 0;
    confirmation = "";
    identityRevision = 0;
    context.verified = false;
    selectedRef = null;
    zoom = 1;
    stale = true;
    await refreshDraft();
    $("settings").open = context.scope === "unknown" || context.scope === "regular";
    renderWorkspace(true);
    await audit();
    feedback(notes.join("；"), hasError);
  } catch (error) {
    feedback(error.message, true);
    renderWorkspace();
  } finally {
    setBusy(false);
  }
}

async function removeImage() {
  if (busy || !images[selectedImage]) return;
  const [removed] = images.splice(selectedImage, 1);
  URL.revokeObjectURL(removed.url);
  if (!images.length) {
    wipe();
    $("dropArea").querySelector("button").focus();
    return;
  }
  setBusy(true);
  selectedImage = Math.min(selectedImage, images.length - 1);
  selectedRef = null;
  zoom = 1;
  sample = null;
  confirmation = "";
  identityRevision = 0;
  context.verified = false;
  stale = true;
  try {
    await refreshDraft();
    renderWorkspace(true);
    await audit();
    feedback(`已移除 ${removed.name}，请重新核对原图与覆盖区域。`);
  } catch (error) {
    feedback(error.message, true);
    renderWorkspace(true);
  } finally {
    setBusy(false);
  }
}

async function openSample(preset) {
  if (busy) return;
  setBusy(true);
  feedback(`正在载入${preset.name}原图…`);
  let files = [];
  try {
    for (const name of preset.files || [preset.file]) {
      const response = await fetch(`/demos/${name}`);
      if (!response.ok) throw new Error("无法载入样例原图。");
      const blob = await response.blob();
      files.push(new File([blob], name, { type: blob.type }));
    }
  } catch (error) {
    files = [];
    feedback(error.message, true);
  } finally {
    setBusy(false);
  }
  if (files.length) await addImages(files, preset);
}

function renderWorkspace(renderFields = false) {
  const hasImages = images.length > 0;
  $("home").hidden = hasImages;
  $("workspace").hidden = !hasImages;
  $("reset").hidden = !hasImages;
  if (!hasImages) return;
  const product = draft?.fields.name?.value || sample?.name || "未命名标签";
  $("caseTitle").textContent = product;
  $("caseMeta").textContent = `${images.length} 张标签图 · ${context.verified ? "识别值已校对" : "识别值待校对"}`;
  for (const [key, value] of Object.entries(context)) {
    const control = $(key);
    if (!control) continue;
    if (control.type === "checkbox") control.checked = value;
    else control.value = value;
  }
  for (const node of document.querySelectorAll("[data-allergen]")) {
    node.value = context.allergenChecks[node.dataset.allergen] || "unknown";
  }
  $("plannedProductionDateLabel").hidden = context.sampleKind === "physical" ||
    (context.sampleKind !== "artwork" && !result?.artworkDate);
  const edition = ["regular", "imported"].includes(context.scope) ? " · 按 2025 版审核" : "";
  $("settingsStatus").textContent = `${scopeLabels[context.scope]}${edition}`;
  renderViewer();
  if (renderFields) renderEditor();
  renderMode();
  renderResults();
}

async function audit() {
  if (!images.length) {
    feedback("请先导入标签图片。", true);
    return;
  }
  const outerBusy = busy;
  const seq = ++auditSeq;
  if (!outerBusy) setBusy(true);
  try {
    if (!draft) {
      await refreshDraft();
      renderWorkspace(true);
    }
    pruneVerifications();
    const auditContext = { ...context, verifiedFields: [...verifiedFields], staleVerifications: [...staleVerifications] };
    const answer = await request("/api/report", {
      draft, context: auditContext, ...casePayload(), aiRemediation: true, aiAgents: true,
    });
    if (seq !== auditSeq) return;   // 已有更新的审核任务，丢弃这次晚返回的结果
    reportAnswer = answer;
    result = answer.report;
    stale = false;
    activeGroup = Object.keys(groups).find((group) => result.items.some((item) => item.group === group)) || "review";
    activeMode = "results";
    renderWorkspace(true);
    renderCandidateConfirm();
    if (!outerBusy) {
      feedback();
      $("resultSummary").scrollIntoView({ block: "nearest" });
    }
  } catch (error) {
    feedback(error.message, true);
    if (outerBusy) throw error;
  } finally {
    if (!outerBusy) setBusy(false);
  }
}

async function confirmAttribution() {
  if (busy || stale || !result?.attribution?.canConfirm) return;
  confirmation = result.attribution.token;
  stale = true;
  setBusy(true);
  try {
    await refreshDraft();
    renderWorkspace(true);
    await audit();
  } catch (error) {
    confirmation = "";
    feedback(error.message, true);
  } finally {
    setBusy(false);
  }
}

function resetExpertReview() {
  if ($("agentOutput")) $("agentOutput").replaceChildren();
  Object.assign(context, { boneClaimEvidence: "unknown", boneClaimBasis: "",
    layoutReviewed: false, layoutReviewBasis: "", boneArtworkReview: "unknown", allergenChecks: {} });
  for (const key of ["boneClaimEvidence", "boneClaimBasis", "layoutReviewBasis", "boneArtworkReview"]) {
    $(key).value = context[key];
  }
  $("layoutReviewed").checked = false;
  for (const node of document.querySelectorAll("[data-allergen]")) node.value = "unknown";
}

function changed(updateField = false) {
  stale = true;
  $("agentOutput").replaceChildren();
  if (updateField) {
    context.verified = false;
    resetExpertReview();
  }
  $("verified").checked = context.verified;
  $("caseMeta").textContent = `${images.length} 张标签图 · ${context.verified ? "识别值已校对" : "识别值待校对"}`;
  renderResults();
  updateActions();
}

function renderMode() {
  const isResults = activeMode === "results";
  $("resultsMode").setAttribute("aria-pressed", String(isResults));
  $("fieldsMode").setAttribute("aria-pressed", String(!isResults));
  $("results").hidden = !isResults;
  $("fieldEditor").hidden = isResults;
}

function renderResults() {
  const summary = $("resultSummary");
  const groupList = $("groups");
  const output = $("items");
  summary.replaceChildren();
  summary.classList.remove("has-issues");
  groupList.replaceChildren();
  output.replaceChildren();
  $("resultEvidence").replaceChildren();
  updateActions();
  if (!result || stale) {
    summary.append(element("h2", "", stale && result ? "审核资料已更新" : "准备审核"));
    summary.append(element("p", "", stale && result ? "点击「重新审核」，按当前设置和校对值生成结果。" : "确认审核设置后，点击「重新审核」。"));
    return;
  }
  const counts = Object.fromEntries(Object.keys(groups).map((group) => [group, result.items.filter((item) => item.group === group).length]));
  const outside = context.scope !== "regular" && context.scope !== "unknown";
  const pending = result.attribution?.status === "pending";
  const draftHold = result.items.some((item) => item.severity === "draft-hold");
  const title = result.blocked ? "图片标示冲突，已暂停生成报告"
    : pending ? "图片归属待确认，已暂停归并"
    : draftHold ? "审核结论 · 不予通过"
    : result.items.some((item) => item.track === "standard2025" && item.outcome === "issue") ? "按 2025 版审核发现不符合项"
    : counts.gap ? "有检查项缺少适用依据"
    : outside ? "专项审核结果"
    : !result.version ? "需要确认审核范围或口径"
    : counts.issue ? `${counts.issue} 项需处理`
    : counts.review ? `${counts.review} 项待核实`
    : counts.pass ? "本次已执行的检查未见明确问题"
    : "本次审核等待补充资料";
  if (counts.issue || result.blocked) summary.classList.add("has-issues");
  summary.append(element("h2", "", title));
  const meta = [result.versionLabel, result.scopeLabel].filter(Boolean).join(" · ");
  if (meta) summary.append(element("p", "", meta));
  if (result.blocked) summary.append(element("p", "", "请核对冲突证据，将不同商品或不同销售包装分开审核。"));
  if (pending) {
    summary.append(element("p", "", "请对照逐图证据，确认所有图片属于同一商品及同一销售包装。"));
    if (result.attribution.canConfirm) {
      const photos = element("div", "attribution-photos");
      images.forEach((photo, index) => {
        const card = button("attribution-photo", "", () => selectImage(index));
        const thumbnail = element("img");
        thumbnail.src = photo.url;
        thumbnail.alt = `查看图 ${index + 1}`;
        card.append(thumbnail, element("span", "", `图 ${index + 1}`));
        photos.append(card);
      });
      const confirm = button("primary", "确认这些图属于同一销售包装", confirmAttribution);
      confirm.id = "confirmAttribution";
      confirm.disabled = busy;
      summary.append(photos, confirm);
    }
  }
  renderResultEvidence();
  for (const [group, label] of Object.entries(groups)) {
    const control = button("", label, () => {
      activeGroup = group;
      renderResults();
      $("groups").querySelector(`[data-group="${group}"]`).focus({ preventScroll: true });
    });
    control.dataset.group = group;
    control.setAttribute("aria-pressed", String(activeGroup === group));
    control.append(element("span", "count", String(counts[group])));
    groupList.append(control);
  }
  const visible = result.items.filter((item) => item.group === activeGroup);
  if (!visible.length) {
    output.append(element("p", "group-empty", `本次没有「${groups[activeGroup]}」记录。`));
    return;
  }
  for (const item of visible) output.append(renderItem(item));
}

function renderItem(item) {
  const disclosure = element("details", "issue");
  disclosure.dataset.issue = item.id;
  const heading = element("summary");
  const lines = element("span");
  lines.append(element("strong", "issue-title", item.title));
  if (item.action) lines.append(element("span", "issue-action", item.action));
  heading.append(lines);
  disclosure.append(heading);
  const body = element("div", "issue-body");
  if (item.track === "standard2025") body.append(element("p", "track-note", "按 2025 版标准审核"));
  if (item.basis) body.append(element("p", "track-note", item.basis));
  if (item.detail) body.append(element("p", "issue-detail", item.detail));
  const refs = [...new Set(item.refs || [])].filter(findBlock);
  if (refs.length) {
    const controls = element("div", "evidence-buttons");
    const additional = element("div", "evidence-buttons");
    for (const [index, ref] of refs.entries()) {
      const control = button("", refs.length === 1 ? "定位原图" : `证据 ${index + 1}`, () => locate(ref));
      control.title = findBlock(ref).block.text;
      control.setAttribute("aria-label", `定位证据 ${index + 1} · ${control.title}`);
      (index < 3 ? controls : additional).append(control);
    }
    body.append(controls);
    if (additional.childElementCount) {
      const more = element("details", "more-evidence");
      more.append(element("summary", "", `另有 ${additional.childElementCount} 项证据`), additional);
      body.append(more);
    }
  }
  if (item.source || item.sources?.length) {
    const source = element("p", "issue-source");
    const citations = item.sources?.length ? item.sources : [item.source];
    for (const citation of citations.flatMap((entry) => [entry, ...(entry.related || [])])) source.append(sourceLink(citation));
    body.append(source);
  }
  disclosure.append(body);
  return disclosure;
}

function sourceLink({ name, filename, clause, location, page, localUrl, url }) {
  const label = [name || filename, clause, location || (page ? `第 ${page} 页` : "")].filter(Boolean).join(" · ") || "查看依据";
  const path = url || localUrl;
  if (typeof path !== "string" || !/^\/knowledge\/[A-Za-z0-9._-]+(?:#page=\d+)?$/.test(path)) {
    return element("span", "", label);
  }
  const link = element("a", "", label);
  link.href = path;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function renderResultEvidence() {
  const area = $("resultEvidence");
  if (result.canPublish === false && draft?.pageDrafts?.length) {
    const detail = element("details", "evidence-section");
    detail.open = true;
    detail.append(element("summary", "", "逐图识别候选"));
    for (const page of draft.pageDrafts) {
      const index = images.findIndex((photo) => photo.id === page.id);
      const line = element("div", "evidence-row");
      line.append(element("strong", "", `图 ${index + 1}`));
      for (const key of ["name", "net", "producer", "license", "production", "expiry"]) {
        const field = page.fields[key];
        if (!field?.value) continue;
        const ref = field.refs.find(findBlock);
        const title = `${fields[key]} · ${field.value}`;
        line.append(ref ? button("locate", title, () => locate(ref)) : element("span", "", title));
      }
      detail.append(line);
    }
    area.append(detail);
  }
  const coverage = result.coverage || [];
  if (coverage.length) {
    const detail = element("details", "evidence-section");
    detail.append(element("summary", "", "查看拍摄覆盖（人工标注）"));
    for (const entry of coverage) {
      const line = element("div", "evidence-row");
      const status = { clear: "已拍且可读", unclear: "已拍但看不清", missing: "尚未确认拍到", unknown: "待确认" }[entry.status] || entry.status;
      line.append(element("strong", "", entry.label), element("span", "", status));
      for (const id of entry.images || []) {
        const index = images.findIndex((photo) => photo.id === id);
        if (index >= 0) line.append(button("locate", `图 ${index + 1}`, () => selectImage(index)));
      }
      detail.append(line);
    }
    area.append(detail);
  }
  if (result.fields?.length) {
    const detail = element("details", "evidence-section");
    const unreadable = result.fields.filter((field) => field.status === "无法识别").length;
    detail.append(element("summary", "", `查看标示状态 · ${unreadable} 项无法识别`));
    for (const field of result.fields) {
      const line = element("div", "evidence-row");
      line.append(element("strong", "", field.label), element("span", "field-status", field.status));
      if (field.value) line.append(element("span", "evidence-value", field.value));
      const ref = field.refs?.find(findBlock);
      if (ref) line.append(button("locate", "定位", () => locate(ref)));
      if (field.reason) line.append(element("small", "evidence-reason", field.reason));
      detail.append(line);
    }
    area.append(detail);
  }
  if (result.anchors?.length) {
    const detail = element("details", "evidence-section");
    detail.open = true;
    detail.append(element("summary", "", "日期指引与喷码"));
    for (const anchor of result.anchors) {
      const line = element("div", "evidence-row");
      line.append(element("strong", "", anchor.label), element("span", "", anchor.status));
      for (const [ref, label] of [[anchor.sourceRef, "查看位置指引"],
        ...(anchor.targets || []).map((target) => [target.ref, `${target.label} · ${target.value}`])]) {
        if (ref && findBlock(ref)) line.append(button("locate", label, () => locate(ref)));
      }
      detail.append(line);
    }
    area.append(detail);
  }
}

function renderEditor() {
  if (!draft) return;
  const editor = $("fieldEditor");
  if (result?.canPublish === false || draft.attribution?.status !== "confirmed") {
    editor.replaceChildren(element("p", "field-note", "请先在审核结果中核对逐图证据并处理归属，再校对合并字段。"));
    return;
  }
  const head = element("div", "editor-head");
  head.append(element("h2", "", "校对识别字段"));
  head.append(element("p", "field-note", "按原图修正错字或小数点，再在审核设置中确认已校对。"));
  const identity = fieldSet("商品与包装");
  for (const key of ["name", "ingredients", "net", "packs", "producer", "address", "contact", "license", "standard"]) {
    identity.append(renderField(key));
  }
  const dates = fieldSet("日期与贮存");
  for (const key of ["production", "shelf", "expiry", "storage"]) dates.append(renderField(key));
  const nutrients = fieldSet("营养成分表", true);
  const tableQa = draft.factQa?.nutritionTable || result?.factQa?.nutritionTable;
  if (tableQa?.extractStatus === "structural-failure" || draft.nutritionExtractFailure) {
    nutrients.append(element("p", "field-note",
      tableQa?.failureReason || "已检测到营养成分表并读到数字，但项目与数值未能可靠配对，不能作为空表通过。"));
  }
  nutrients.append(renderField("basis"));
  const grid = element("div", "nutrition-grid");
  for (const caption of ["项目", "含量", "单位", "NRV%", "证据"]) grid.append(element("strong", "", caption));
  for (const [key, label] of Object.entries(nutrition)) {
    const row = draft.nutrition[key] ||= { value: "", unit: "", nrv: "", refs: [] };
    grid.append(element("span", "", label));
    for (const [part, caption] of [["value", "含量"], ["unit", "单位"], ["nrv", "NRV%"]]) {
      const input = element("input");
      input.id = `nutrient-${key}-${part}`;
      input.value = row[part] ?? "";
      input.maxLength = 30;
      input.autocomplete = "off";
      input.disabled = busy;
      if (part !== "unit") input.inputMode = "decimal";
      input.setAttribute("aria-label", `${label}${caption}`);
      input.addEventListener("change", () => {
        recordVerification(`营养表-${key}-${part}`,
          (draft.nutrition?.[key]?.value || "") + (draft.nutrition?.[key]?.unit || ""), row.refs);
      });
      input.addEventListener("input", () => {
        row[part] = input.value;
        editedRows.add(key);
        pruneVerifications();
        changed(true);
      });
      grid.append(input);
    }
    grid.append(locateButton(row.refs, label));
  }
  nutrients.append(grid);
  editor.replaceChildren(head, nutrients, identity, dates);
}

function fieldSet(title, open = false) {
  const node = element("details", "editor-set");
  node.open = open;
  node.append(element("summary", "", title));
  return node;
}

function locateButton(refs, label) {
  const ref = (refs || []).find(findBlock);
  if (!ref) return element("span", "", "—");
  const node = button("locate", "定位", () => locate(ref));
  node.setAttribute("aria-label", `定位${label}`);
  return node;
}

function renderField(key) {
  const field = draft.fields[key] ||= { value: "", refs: [] };
  const wrap = element("div", "editor-field");
  const labelRow = element("div", "editor-label");
  const label = element("label", "", fields[key]);
  const input = element(["ingredients", "packs"].includes(key) ? "textarea" : "input");
  input.id = `field-${key}`;
  input.value = field.value || "";
  input.maxLength = key === "ingredients" ? 8000 : 600;
  input.autocomplete = "off";
  input.disabled = busy;
  label.htmlFor = input.id;
  input.addEventListener("change", () => recordVerification(key, field.value, field.refs));
  input.addEventListener("input", () => {
    field.value = input.value;
    editedFields.add(key);
    pruneVerifications();
    if (images.length > 1 && ["name", "net", "packs", "producer", "license"].includes(key)) {
      identityRevision += 1;
      confirmation = "";
    }
    changed(true);
  });
  labelRow.append(label, locateButton(field.refs, fields[key]));
  wrap.append(labelRow, input);
  return wrap;
}

function findBlock(ref) {
  for (const [index, image] of images.entries()) {
    const block = image.blocks.find((candidate) => candidate.id === ref);
    if (block) return { index, block };
  }
  return null;
}

function selectImage(index) {
  selectedImage = index;
  selectedRef = null;
  zoom = 1;
  renderViewer();
}

function recordCoverage(change) {
  change();
  changed(true);
}

function renderCoverageControls(active) {
  const wrap = $("coverageControls");
  wrap.replaceChildren();
  const note = element("p", "field-note", "选出这张图完整拍到的区域；只拍到局部时选「其他区域」。");
  const choices = element("div", "coverage-options");
  for (const [key, label] of Object.entries(areas)) {
    const option = element("label", "check");
    const input = element("input");
    input.type = "checkbox";
    input.checked = active.areas.includes(key);
    input.disabled = busy;
    input.addEventListener("change", () => recordCoverage(() => {
      active.areas = input.checked ? [...active.areas, key] : active.areas.filter((area) => area !== key);
    }));
    option.append(input, document.createTextNode(label));
    choices.append(option);
  }
  const label = element("label", "readability-label", "区域文字清晰度");
  const select = element("select");
  select.disabled = busy;
  for (const [value, text] of [["unknown", "尚未核对"], ["clear", "所选区域可读"], ["unclear", "所选区域部分看不清"]]) {
    const option = element("option", "", text);
    option.value = value;
    select.append(option);
  }
  select.value = active.readability;
  select.addEventListener("change", () => recordCoverage(() => { active.readability = select.value; }));
  label.append(select);
  const packageLabel = element("label", "readability-label", "包装层级");
  const packageSelect = element("select");
  packageSelect.disabled = busy;
  for (const [value, text] of [["", "尚未区分"], ["outer", "外包装"], ["inner", "内包装"]]) {
    const option = element("option", "", text);
    option.value = value;
    packageSelect.append(option);
  }
  packageSelect.value = active.packageLevel || "";
  packageSelect.addEventListener("change", () => recordCoverage(() => {
    active.packageLevel = packageSelect.value;
    confirmation = "";
  }));
  packageLabel.append(packageSelect);
  wrap.append(note, choices, label, packageLabel);
}

function renderViewer() {
  const tabList = $("imageTabs");
  tabList.replaceChildren();
  for (const [index, image] of images.entries()) {
    const tab = button("", images.length === 1 ? "图 1" : `图 ${index + 1}`, () => {
      selectImage(index);
      $("imageTabs").children[index]?.focus({ preventScroll: true });
    });
    tab.title = image.name;
    tab.setAttribute("aria-label", `图 ${index + 1} · ${image.name}`);
    tab.setAttribute("aria-pressed", String(index === selectedImage));
    tabList.append(tab);
  }
  const photo = $("previewImage");
  const active = images[selectedImage];
  if (photo.getAttribute("src") !== active.url) photo.src = active.url;
  photo.alt = `标签图 ${selectedImage + 1} · ${sample?.name || active.name}`;
  const evidence = selectedRef && findBlock(selectedRef);
  $("viewerCaption").textContent = evidence
    ? `定位到 · ${evidence.block.text}`
    : `图 ${selectedImage + 1} / ${images.length} · ${active.blocks.length ? "点击审核证据可放大并定位。" : "OCR 未读到文字，请检查拍摄范围与清晰度。"}`;
  renderCoverageControls(active);
  updateActions();
  if (photo.complete) layoutPhoto(Boolean(evidence));
}

function layoutPhoto(centerEvidence = false, center = null) {
  const photo = $("previewImage");
  const viewport = $("canvasViewport");
  if (!photo.naturalWidth || !viewport.clientWidth || !images.length) return;
  const stage = $("photoStage");
  const fit = Math.min(viewport.clientWidth / photo.naturalWidth, viewport.clientHeight / photo.naturalHeight, 1);
  const width = Math.max(1, Math.round(photo.naturalWidth * fit * zoom));
  const height = Math.max(1, Math.round(photo.naturalHeight * fit * zoom));
  stage.style.width = `${width}px`;
  stage.style.height = `${height}px`;
  stage.style.marginTop = `${Math.max(0, (viewport.clientHeight - height) / 2)}px`;
  const evidence = selectedRef && findBlock(selectedRef);
  const marker = $("evidenceBox");
  marker.hidden = !evidence;
  if (evidence) {
    const [x, y, w, h] = evidence.block.box;
    Object.assign(marker.style, { left: `${x * 100}%`, top: `${y * 100}%`, width: `${w * 100}%`, height: `${h * 100}%` });
    if (centerEvidence) center = [x + w / 2, y + h / 2];
  }
  if (center) {
    viewport.scrollLeft = stage.offsetLeft + width * center[0] - viewport.clientWidth / 2;
    viewport.scrollTop = stage.offsetTop + height * center[1] - viewport.clientHeight / 2;
  } else if (zoom === 1) {
    viewport.scrollTo(0, 0);
  }
  $("zoomOut").disabled = zoom <= 1;
  $("zoomIn").disabled = zoom >= 14;
}

function changeZoom(factor) {
  const viewport = $("canvasViewport");
  const stage = $("photoStage");
  if (!stage.clientWidth || !stage.clientHeight) return;
  const center = [
    (viewport.scrollLeft + viewport.clientWidth / 2 - stage.offsetLeft) / stage.clientWidth,
    (viewport.scrollTop + viewport.clientHeight / 2 - stage.offsetTop) / stage.clientHeight,
  ];
  zoom = factor === 0 ? 1 : Math.min(14, Math.max(1, zoom * factor));
  layoutPhoto(false, center);
}

function locate(ref) {
  const evidence = findBlock(ref);
  if (!evidence) return;
  selectedImage = evidence.index;
  selectedRef = ref;
  const [, , w, h] = evidence.block.box;
  zoom = Math.min(14, Math.max(2.5, Math.min(.62 / Math.max(w, .001), .25 / Math.max(h, .001))));
  renderViewer();
  if (window.matchMedia("(max-width: 57.99rem)").matches) {
    $("viewerTitle").scrollIntoView({ block: "start", behavior: "instant" });
  }
}

async function imageCanvas(url, box, maxSide) {
  const original = new Image();
  original.src = url;
  await original.decode();
  const [x, y, w, h] = box;
  const left = Math.floor(x * original.naturalWidth);
  const top = Math.floor(y * original.naturalHeight);
  const width = Math.max(1, Math.min(original.naturalWidth - left, Math.ceil(w * original.naturalWidth)));
  const height = Math.max(1, Math.min(original.naturalHeight - top, Math.ceil(h * original.naturalHeight)));
  const scale = Math.min(1, maxSide / Math.max(width, height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(width * scale));
  canvas.height = Math.max(1, Math.round(height * scale));
  const drawing = canvas.getContext("2d");
  drawing.fillStyle = "white";
  drawing.fillRect(0, 0, canvas.width, canvas.height);
  drawing.drawImage(original, left, top, width, height, 0, 0, canvas.width, canvas.height);
  return canvas;
}

async function sha256Hex(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

async function exportReport(download = false) {
  if ($("report").disabled) return;
  if (!window.LabelReport?.render) {
    feedback("报告组件尚未载入，请刷新页面后重试。", true);
    return;
  }
  // V2.7：07 节存在未能定位的整改动作时，工作台可以预览底稿，但不得导出「可直接改版」的客户报告。
  if (download && result?.checkErrors?.ok === false) {
    const summary = (result.checkErrors.errors || []).map((error) => `${error.code}：${error.message}`).join("；");
    feedback(`全文一致性检查未通过，不能导出客户版：${summary}`, true);
    return;
  }
  const revision = result?.revision;
  if (download && revision && revision.canExport === false) {
    const failed = (revision.failures || []).map((row) => `${row.itemId}：${row.reason}`).join("；");
    feedback(`完整改版稿有 ${revision.failures?.length || 0} 处未能定位，请先核对原文后重新审核。${failed}`, true);
    return;
  }
  const tab = download ? null : window.open("about:blank", "_blank");
  if (!download && !tab) {
    feedback("浏览器拦截了报告窗口，请允许此网页打开新标签页。", true);
    return;
  }
  if (tab) {
    tab.opener = null;
    tab.document.body.textContent = "正在生成审核报告…";
  }
  setBusy(true);
  try {
    const reportImages = images.map(({ id, name, blocks }) => ({ id, name, blocks }));
    // V2.8 ④：预览/下载读成稿文件字节，并核对 contentHash；不在前端另行拼装。
    let html = "";
    if (reportAnswer?.reportId) {
      const response = await fetch(`/api/report/${encodeURIComponent(reportAnswer.reportId)}.html`);
      if (!response.ok) throw new Error("成稿读取失败");
      html = await response.text();
      const digest = await sha256Hex(html);
      if (reportAnswer.contentHash && digest !== reportAnswer.contentHash) {
        throw new Error(`成稿校验失败：${digest.slice(0, 12)} ≠ ${reportAnswer.contentHash.slice(0, 12)}`);
      }
    }
    if (!html) html = window.LabelReport.render({ result, draft, images: reportImages, context });
    const url = URL.createObjectURL(new Blob([html], { type: "text/html;charset=utf-8" }));
    if (download) {
      const link = element("a");
      link.href = url;
      const name = draft.fields?.name?.value || "食品标签";
      link.download = `${name.replace(/[\\/:*?"<>|]/g, "_").slice(0, 80)}-标签审核报告.html`;
      link.click();
    } else if (!tab.closed) {
      tab.location.replace(url);
    }
    setTimeout(() => URL.revokeObjectURL(url), 120_000);
  } catch {
    tab?.close();
    feedback("报告生成失败，请检查标签图片后重试。", true);
  } finally {
    setBusy(false);
  }
}

async function searchKnowledge(event) {
  event.preventDefault();
  const query = $("knowledgeQuery").value.trim();
  if (!query) return;
  const requestId = ++knowledgeQueryId;
  const output = $("knowledgeResults");
  output.textContent = "正在查询…";
  try {
    const { items } = await request(`/api/knowledge?q=${encodeURIComponent(query)}`);
    if (requestId !== knowledgeQueryId) return;
    output.replaceChildren();
    for (const item of items || []) {
      const article = element("article");
      article.append(sourceLink(item));
      if (item.excerpt) article.append(element("p", "", item.excerpt));
      output.append(article);
    }
    if (!output.childElementCount) output.textContent = "指定知识库中没有找到匹配内容。";
  } catch (error) {
    if (requestId === knowledgeQueryId) output.textContent = error.message;
  }
}

function fieldForRef(ref) {
  for (const [data, labels] of [[draft?.fields, fields], [draft?.nutrition, nutrition]]) {
    for (const [key, value] of Object.entries(data || {})) {
      if (value.refs?.includes(ref)) return labels[key] || "标签文字";
    }
  }
  return "标签文字";
}

function reviewButtons(enabled) {
  $("reviewLocal").disabled = !enabled;
  $("reviewSend").disabled = !enabled || !reviewSelection?.cloud;
}

async function openReview() {
  if ($("reviewEvidence").disabled) return;
  const evidence = findBlock(selectedRef);
  if (!evidence) return;
  const selection = { field: fieldForRef(selectedRef), image: null };
  reviewSelection = selection;
  const photo = images[evidence.index];
  $("reviewCrop").removeAttribute("src");
  $("reviewCaption").textContent = `${selection.field} · 图 ${evidence.index + 1}`;
  $("reviewAvailability").textContent = "正在检查 AI 复核配置…";
  $("reviewOutput").textContent = "正在生成原图裁切…";
  reviewButtons(false);
  $("reviewDialog").showModal();
  const [x, y, w, h] = evidence.block.box;
  const left = Math.max(0, x - Math.min(.025, w / 4));
  const top = Math.max(0, y - Math.min(.018, h / 2));
  const right = Math.min(1, x + w + Math.min(.025, w / 4));
  const bottom = Math.min(1, y + h + Math.min(.018, h / 2));
  try {
    const [canvas, health] = await Promise.all([
      imageCanvas(photo.url, [left, top, right - left, bottom - top], 1400),
      request("/api/health").catch(() => ({})),
    ]);
    if (reviewSelection !== selection) return;
    selection.cloud = health.cloudReview === true;
    selection.image = canvas.toDataURL("image/jpeg", .94);
    selection.canvas = canvas;
    $("reviewCrop").src = selection.image;
    $("reviewOutput").textContent = "";
    $("reviewAvailability").textContent = selection.cloud
      ? "AI 复核会将上方裁切发送至已配置服务。结果仅供校对，是否更正由你确认。"
      : "未配置 AI 服务，可以用本机 OCR 复读裁切并对照原图。";
    reviewButtons(true);
  } catch {
    if (reviewSelection === selection) $("reviewOutput").textContent = "无法裁切选中区域，请重新选择证据。";
  }
}

async function rereadEvidence(useAI) {
  const selection = reviewSelection;
  if (!selection?.image || (useAI && !selection.cloud)) return;
  reviewButtons(false);
  const output = $("reviewOutput");
  output.textContent = useAI ? "AI 正在复读所选裁切…" : "本机 OCR 正在复读…";
  try {
    let candidate, note;
    if (useAI) {
      ({ candidate, note } = await request("/api/review", {
        image: selection.image, field: selection.field, sendCloud: true,
      }));
    } else {
      const blob = await new Promise((resolve) => selection.canvas.toBlob(resolve, "image/jpeg", .94));
      if (!blob) throw new Error("无法读取裁切，请重新选择证据。");
      const recognized = await request("/api/ocr", blob, "image/jpeg");
      candidate = recognized.blocks.map(({ text }) => text).join(" / ");
    }
    if (reviewSelection !== selection) return;
    output.replaceChildren(element("strong", "", candidate ? `候选读数 · ${candidate}` : "此区域没有得到可用候选读数"));
    if (note) output.append(element("p", "", note));
  } catch (error) {
    if (reviewSelection === selection) output.textContent = error.message;
  } finally {
    if (reviewSelection === selection) reviewButtons(true);
  }
}

for (const [index, preset] of samples.entries()) {
  const row = element("li");
  const control = button("sample-button", "", () => openSample(preset));
  const end = element("span", "sample-end");
  const arrow = element("span", "sample-arrow", "↗");
  arrow.setAttribute("aria-hidden", "true");
  end.append(element("span", "sample-type", preset.tag), arrow);
  control.append(element("span", "sample-number", String(index + 1).padStart(2, "0")), element("span", "sample-name", preset.name), end);
  row.append(control);
  $("samples").append(row);
}

for (const node of document.querySelectorAll("[data-upload]")) node.addEventListener("click", () => $("files").click());
$("files").addEventListener("change", (event) => {
  const files = [...event.target.files];
  event.target.value = "";
  void addImages(files);
});
const dropArea = $("dropArea");
dropArea.addEventListener("dragover", (event) => {
  event.preventDefault();
  if (!busy) dropArea.classList.add("dragging");
});
dropArea.addEventListener("dragleave", (event) => {
  if (!dropArea.contains(event.relatedTarget)) dropArea.classList.remove("dragging");
});
dropArea.addEventListener("drop", (event) => {
  event.preventDefault();
  dropArea.classList.remove("dragging");
  void addImages([...event.dataTransfer.files]);
});
for (const type of ["dragover", "drop"]) {
  window.addEventListener(type, (event) => {
    if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
  });
}
$("reset").addEventListener("click", () => {
  wipe();
  $("dropArea").querySelector("button").focus();
});
$("audit").addEventListener("click", audit);
function reviewCandidates() {
  const review = result?.aiReview || reportAnswer?.report?.aiReview || {};
  return (review.candidates || []).filter((row) => row && (row.candidateId || row.id));
}

function renderCandidateConfirm() {
  const output = $("agentOutput");
  if (!output) return;
  const candidates = reviewCandidates();
  if (!candidates.length) return;
  output.replaceChildren(element("p", "", "AI 复核候选需绑定内容编号确认，不能只用轮次编号（如 AI-1）："));
  for (const row of candidates) {
    const id = row.candidateId || "";
    const label = element("label", "");
    const box = element("input");
    box.type = "checkbox";
    box.disabled = !id;
    if (id) box.dataset.candidateId = id;
    if (row.snapshotId) box.dataset.snapshotId = row.snapshotId;
    label.append(box, document.createTextNode(` ${row.displayId || row.id || ""} ${row.claim || row.title || ""}`.trim()));
    if (id) label.append(element("span", "muted", `  ${id}`));
    else label.append(element("span", "muted", "（缺少 candidateId，无法批准）"));
    output.append(label, element("p", "", row.reason || row.detail || ""));
  }
  const taskId = reportAnswer?.meta?.taskId || result?.reportMeta?.taskId;
  const revision = reportAnswer?.decisionVersion || reportAnswer?.meta?.decisionVersion || result?.decisionVersion;
  if (taskId && revision && candidates.some((row) => row.candidateId)) {
    const buttonEl = button("primary", "确认已勾选的候选并继续撰稿", confirmApprovedCandidates);
    buttonEl.id = "confirmCandidates";
    output.append(buttonEl);
  }
}

async function confirmApprovedCandidates() {
  const taskId = reportAnswer?.meta?.taskId || result?.reportMeta?.taskId;
  const revision = reportAnswer?.decisionVersion || reportAnswer?.meta?.decisionVersion || result?.decisionVersion;
  const approved = [...document.querySelectorAll("[data-candidate-id]:checked")].map((node) => {
    const record = { candidateId: node.dataset.candidateId };
    if (node.dataset.snapshotId) record.snapshotId = node.dataset.snapshotId;
    return record;
  });
  if (!taskId || !revision) {
    feedback("没有可恢复的任务版本，请重新审核。", true);
    return;
  }
  if (!approved.length) {
    feedback("请先勾选要确认的候选。显示编号 AI-1 不能作为批准依据。", true);
    return;
  }
  setBusy(true);
  try {
    const answer = await request("/api/report", {
      taskId,
      expectedDecisionRevision: revision,
      aiRemediation: true,
      context: { taskId, expectedDecisionRevision: revision, aiReviewApproved: approved },
    });
    reportAnswer = answer;
    result = answer.report;
    stale = false;
    renderWorkspace(true);
    renderCandidateConfirm();
    feedback(answer.meta?.resumed ? "已按同一任务继续，未重跑 AI 审核。" : "已提交确认。");
  } catch (error) {
    feedback(error.message, true);
  } finally {
    setBusy(false);
  }
}

$("runAgents").addEventListener("click", () => {
  // B1：三角色候选已并入 /api/report 的 aiAgents，结果从本次返回体读取，不再单独调旧接口。
  const output = $("agentOutput");
  const agents = reportAnswer?.meta?.aiAgents || result?.reportMeta?.aiAgents;
  if (!result) {
    feedback("请先完成一次审核。", true);
    return;
  }
  const candidates = reviewCandidates();
  if (candidates.length) {
    renderCandidateConfirm();
    return;
  }
  const notes = result?.agentNotes || [];
  if (notes.length) {
    output.replaceChildren(element("p", "", "AI 复核候选（未核验，仅供工作台参考）："));
    for (const note of notes) {
      output.append(element("strong", "", note.title || "复核建议"), element("p", "", note.detail || ""));
    }
    return;
  }
  output.textContent = `本次无 AI 复核候选（${agents || result.reportMeta?.aiStatus || "未返回"}）。`;
});
$("report").addEventListener("click", () => exportReport());
$("downloadReport").addEventListener("click", () => exportReport(true));
// PDF 下载：读服务端由同一份成稿生成的 PDF，保证与 HTML 同源。
$("downloadPdf")?.addEventListener("click", async () => {
  const id = reportAnswer?.reportId;
  if (!id) { feedback("请先生成报告。", true); return; }
  try {
    let response = await fetch(`/api/report/${encodeURIComponent(id)}.pdf`);
    if (response.status === 202) {
      // D2：HTML 已可读，PDF 在后台生成；轮询状态，失败明确提示可重试。
      for (let attempt = 0; attempt < 20; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        const status = await (await fetch(`/api/report/${encodeURIComponent(id)}.pdf.json`)).json();
        if (status.status === "ready") {
          response = await fetch(`/api/report/${encodeURIComponent(id)}.pdf`);
          break;
        }
        if (status.status === "failed") {
          const retried = await fetch(`/api/report/${encodeURIComponent(id)}.pdf`, { method: "POST" });
          const retryBody = await retried.json().catch(() => ({}));
          if (retryBody.status === "ready") {
            response = await fetch(`/api/report/${encodeURIComponent(id)}.pdf`);
            break;
          }
          throw new Error(`PDF 导出失败：${retryBody.reason || status.reason || "未知原因"}（已从已保存 HTML 重试，未重跑审核）`);
        }
      }
      if (response.status === 202) throw new Error("PDF 仍在生成，请稍后重试");
    }
    if (!response.ok) throw new Error("PDF 尚未生成");
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const name = draft?.fields?.name?.value || "食品标签";
    const link = element("a");
    link.href = url;
    link.download = `${name.replace(/[\\/:*?"<>|]/g, "_").slice(0, 80)}-标签审核报告.pdf`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  } catch (error) { feedback(`PDF 下载失败：${error.message}`, true); }
});
$("removeImage").addEventListener("click", removeImage);
$("reviewEvidence").addEventListener("click", openReview);
$("reviewLocal").addEventListener("click", () => rereadEvidence(false));
$("reviewSend").addEventListener("click", () => rereadEvidence(true));
$("reviewDialog").addEventListener("close", () => { reviewSelection = null; });
$("openKnowledge").addEventListener("click", () => {
  $("knowledgeDialog").showModal();
  $("knowledgeQuery").focus();
});
$("knowledgeForm").addEventListener("submit", searchKnowledge);
for (const [key, label] of Object.entries(allergenLabels)) {
  const field = element("label", "", label);
  const input = element("select");
  input.dataset.allergen = key;
  for (const [value, text] of [
    ["unknown", "待核实"],
    ["covered", "已核对适用标示"],
    ["bold", "配料表内加粗"],
    ["underline", "配料表内下划线"],
    ["nearby", "配料表邻近位置有提示"],
    ["missing", "直接配料，无例外，完整标示均未覆盖"],
    ["exempt", "已核对适用例外"],
  ]) {
    const option = element("option", "", text);
    option.value = value;
    input.append(option);
  }
  field.append(input);
  $("allergenChecks").append(field);
}
$("contextControls").addEventListener("change", (event) => {
  const input = event.target;
  if (input.dataset.allergen) {
    context.allergenChecks[input.dataset.allergen] = input.value;
    changed();
    return;
  }
  if (!(input.id in context)) return;
  if (input.id === "scope") scopeSelected = input.value !== "unknown";
  context[input.id] = input.type === "checkbox" ? input.checked : input.value;
  changed();
  renderWorkspace();
});
for (const key of ["layoutReviewBasis", "boneClaimBasis"]) {
  $(key).addEventListener("input", (event) => {
    context[key] = event.target.value;
    changed();
  });
}
$("resultsMode").addEventListener("click", () => { activeMode = "results"; renderMode(); });
$("fieldsMode").addEventListener("click", () => { activeMode = "fields"; renderMode(); });
$("zoomOut").addEventListener("click", () => changeZoom(.75));
$("zoomIn").addEventListener("click", () => changeZoom(1.33));
$("zoomReset").addEventListener("click", () => {
  selectedRef = null;
  zoom = 1;
  renderViewer();
});
$("previewImage").addEventListener("load", () => layoutPhoto(Boolean(selectedRef)));
$("previewImage").addEventListener("error", () => {
  if (images.length) $("viewerCaption").textContent = "图片无法预览，请换用 JPG 或 PNG。";
});
new ResizeObserver(() => layoutPhoto(Boolean(selectedRef))).observe($("canvasViewport"));
