"use strict";

(() => {
  // 原始报告模板的样式，按原文保留。
  const css = `
  :root{
    --ink:#161a1f; --ink2:#3a424d; --muted:#6b7683; --line:#e4e8ee;
    --bg:#f6f7f9; --card:#ffffff;
    --red:#c0392b; --redbg:#fdecea;
    --amber:#9a6b00; --amberbg:#fdf6e3;
    --blue:#1a5fb4; --bluebg:#eaf2fd;
    --gray:#5b6470; --graybg:#f0f2f5;
    --green:#1a7f4b; --greenbg:#e8f5ee;
  }
  *{box-sizing:border-box;}
  body{
    margin:0; background:var(--bg); color:var(--ink);
    font-family:"Microsoft YaHei","PingFang SC","Hiragino Sans GB","Source Han Sans SC",sans-serif;
    font-size:14px; line-height:1.75; -webkit-font-smoothing:antialiased;
  }
  .page{max-width:1000px; margin:0 auto; padding:32px 28px 64px; background:var(--card);
    box-shadow:0 1px 3px rgba(0,0,0,.06),0 12px 32px rgba(20,30,50,.05);}
  /* header */
  .kicker{font-size:12px; letter-spacing:.18em; color:var(--muted); text-transform:uppercase;}
  h1{font-size:25px; line-height:1.4; margin:10px 0 6px; letter-spacing:-.2px;}
  .sub{color:var(--ink2); font-size:14.5px;}
  .meta{display:flex; flex-wrap:wrap; gap:8px 28px; margin:18px 0 0; padding:14px 0 0;
    border-top:1px solid var(--line); font-size:13px; color:var(--muted);}
  .meta b{color:var(--ink); font-weight:600;}
  /* verdict */
  .verdict{margin:24px 0 8px; border:1px solid #f0c9c3; border-left:5px solid var(--red);
    background:var(--redbg); border-radius:8px; padding:18px 20px;}
  .verdict .tag{display:inline-block; background:var(--red); color:#fff; font-size:12px;
    font-weight:700; padding:2px 10px; border-radius:4px; letter-spacing:.05em;}
  .verdict p{margin:10px 0 0; color:#7a2a22; font-size:14.5px;}
  .verdict strong{color:#5e1c16;}
  /* stat cards */
  .stats{display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:22px 0 34px;}
  .stat{border:1px solid var(--line); border-radius:8px; padding:14px 16px; background:#fbfcfd;}
  .stat .n{font-size:26px; font-weight:700; line-height:1.2;}
  .stat .l{font-size:12.5px; color:var(--muted); margin-top:2px;}
  .stat.r .n{color:var(--red);} .stat.a .n{color:var(--amber);}
  .stat.b .n{color:var(--blue);} .stat.g .n{color:var(--gray);}
  /* sections */
  h2{font-size:17px; margin:38px 0 4px; padding-bottom:9px; border-bottom:2px solid var(--ink);
    letter-spacing:.02em;}
  h2 .num{color:var(--muted); font-weight:400; margin-right:8px; font-variant-numeric:tabular-nums;}
  h3{font-size:14.5px; margin:24px 0 8px; color:var(--ink);}
  p{margin:9px 0;}
  .lead{color:var(--ink2);}
  /* timeline */
  .tl{display:grid; grid-template-columns:88px 1fr; gap:0 14px; margin:16px 0 6px;
    border-left:2px solid var(--line); padding-left:0;}
  .tl .d{font-variant-numeric:tabular-nums; font-weight:700; font-size:13px; color:var(--blue);
    padding:10px 0 10px 14px; position:relative;}
  .tl .d::before{content:""; position:absolute; left:-6px; top:18px; width:9px; height:9px;
    border-radius:50%; background:#fff; border:2px solid var(--blue);}
  .tl .c{padding:8px 0 12px; font-size:13.5px; color:var(--ink2);}
  .tl .c b{color:var(--ink);}
  /* tables */
  table{width:100%; border-collapse:collapse; margin:14px 0 6px; font-size:13.5px;}
  th,td{border:1px solid var(--line); padding:9px 11px; text-align:left; vertical-align:top;}
  th{background:#f3f5f8; font-weight:600; font-size:12.5px; color:var(--ink2); white-space:nowrap;}
  td.num,th.num{text-align:center; font-variant-numeric:tabular-nums; white-space:nowrap;}
  tbody tr:nth-child(even){background:#fbfcfd;}
  .ok{color:var(--green); font-weight:700;}
  .no{color:var(--red); font-weight:700;}
  /* chips */
  .chip{display:inline-block; font-size:11.5px; font-weight:700; padding:1px 8px;
    border-radius:20px; white-space:nowrap; letter-spacing:.02em;}
  .chip.r{background:var(--redbg); color:var(--red); border:1px solid #f2c7c1;}
  .chip.a{background:var(--amberbg); color:var(--amber); border:1px solid #ecd9a5;}
  .chip.b{background:var(--bluebg); color:var(--blue); border:1px solid #c6dbf7;}
  .chip.g{background:var(--graybg); color:var(--gray); border:1px solid #dde1e7;}
  /* quote / basis */
  .rule{font-size:12.5px; color:var(--muted); line-height:1.6;}
  .rule b{color:var(--ink2);}
  /* callouts */
  .box{border:1px solid var(--line); border-radius:8px; padding:14px 18px; margin:16px 0;
    background:#fbfcfd;}
  .box.warn{border-color:#f0d9a8; background:var(--amberbg);}
  .box.info{border-color:#cddffb; background:var(--bluebg);}
  .box h4{margin:0 0 6px; font-size:13.5px;}
  /* text block */
  pre{background:#f4f6f8; border:1px solid var(--line); border-left:4px solid var(--green);
    border-radius:6px; padding:14px 16px; font-size:13.5px; line-height:1.9; margin:14px 0;
    white-space:pre-wrap; word-break:break-word;
    font-family:"Microsoft YaHei","PingFang SC",sans-serif; color:var(--ink);}
  code{background:#eef1f5; padding:1px 5px; border-radius:3px; font-size:12.5px;
    font-family:Consolas,"Courier New",monospace;}
  ul,ol{margin:8px 0; padding-left:22px;}
  li{margin:5px 0;}
  .foot{margin-top:40px; padding-top:16px; border-top:1px solid var(--line);
    font-size:12px; color:var(--muted); line-height:1.8;}
  @media print{
    body{background:#fff;} .page{box-shadow:none; max-width:none; padding:0;}
    h2{break-after:avoid;} table{break-inside:auto;} tr{break-inside:avoid;}
  }
  @media (max-width:760px){
    .page{padding:20px 16px 40px;} .stats{grid-template-columns:repeat(2,1fr);}
    .tl{grid-template-columns:70px 1fr;}
  }
`;

  const groups = {
    blocked: ["r", "报告暂停"], issue: ["a", "需处理"], review: ["g", "待核实"],
    gap: ["g", "依据不足"], pass: ["ok", "已检查"], internal: ["a", "标签内对照"],
  };
  const areaLabels = {
    readable: "可读", partial: "部分可读", unreadable: "不可读", missing: "未覆盖",
    covered: "已覆盖", unknown: "待确认", marked: "已标", unmarked: "未标",
    unclear: "无法识别", not_applicable: "不适用", linked: "已定位", pending: "待确认",
  };
  const scalar = (value) => typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
  const rowsOf = (value) => Array.isArray(value) ? value.filter((item) => item && typeof item === "object") : [];
  const unique = (values) => [...new Set(Array.isArray(values) ? values.map(scalar).filter(Boolean) : [])];
  const label = (key) => Object.hasOwn(areaLabels, key) ? areaLabels[key] : scalar(key) || "待确认";

  function escape(value) {
    return scalar(value).replace(/[&<>"']/g, (symbol) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[symbol]);
  }

  // 待核对原因本身以「。」结尾，多条用「；」拼接时会渲染出「。；」「。。」。
  // 拼接前剥掉句末标点，由调用方在整段末尾统一补一个「。」。
  function reasonText(value) {
    return escape(scalar(value).replace(/[。；;]+$/u, ""));
  }

  function dataImage(value) {
    const text = scalar(value);
    return /^data:image\/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}$/i.test(text) ? text : "";
  }

  function sourceUrl(source, base) {
    const value = scalar(source.localUrl || source.url);
    const allowed = /^\/(?:knowledge\/|sources\/|api\/(?:knowledge|source)s?\/)/;
    if (!allowed.test(value)) return "";
    try {
      const origin = new URL(base || window.location.origin);
      if (origin.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(origin.hostname)) return "";
      const parsed = new URL(value, origin.origin);
      if (parsed.origin !== origin.origin || !allowed.test(parsed.pathname)) return "";
      const page = Number(source.page);
      if (!parsed.hash && Number.isInteger(page) && page > 0) parsed.hash = `page=${page}`;
      return parsed.href;
    } catch {
      return "";
    }
  }

  function sourceText(source, base) {
    if (!source || typeof source !== "object") return "";
    const name = [source.filename || source.name || source.standardId, source.clause,
      source.location || (source.page ? `第 ${source.page} 页` : "")]
      .map(scalar).filter(Boolean).join(" · ");
    if (!name) return "";
    const url = sourceUrl(source, base);
    return `<div class="rule"><b>依据</b>　${url
      ? `<a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${escape(name)}</a>`
      : escape(name)}</div>`;
  }

  function citations(item) {
    const primary = [...rowsOf(item.sources), item.source].filter((source) => source && typeof source === "object");
    const sources = primary.flatMap((source) => [source, ...rowsOf(source.related)]);
    return [...new Map(sources.map((source) => [
      [source.documentId || source.filename || source.name || source.standardId, source.clause, source.location || source.page].join("|"),
      source,
    ])).values()];
  }

  function chip(item, standard) {
    const group = item.outcome || item.group;
    const [tone, name] = Object.hasOwn(groups, group) ? groups[group] : groups.review;
    const category = scalar(item.category);
    return `<span class="chip ${tone}">${category ? escape(category) + " · " : ""}${standard ? "2025 版 · " : ""}${name}</span>`;
  }

  function imageIndex(images) {
    const refs = new Map();
    const ids = new Map();
    images.forEach((image, i) => {
      if (scalar(image.id)) ids.set(scalar(image.id), i + 1);
      rowsOf(image.blocks || image.ocr?.blocks).forEach((block, j) => {
        const ref = scalar(block.id);
        if (!ref || refs.has(ref)) return;
        refs.set(ref, { photo: i + 1, number: j + 1, id: `evidence-${i + 1}-${j + 1}`, block });
      });
    });
    return { refs, ids };
  }

  function markStyle(box) {
    if (!Array.isArray(box) || box.length !== 4 || !box.every(Number.isFinite)) return "";
    const [left, top, width, height] = box;
    if (left < 0 || top < 0 || width <= 0 || height <= 0 || left + width > 1.001 || top + height > 1.001) return "";
    const p = (value) => Math.round(value * 10000) / 100;
    return `left:${p(left)}%;top:${p(top)}%;width:${p(width)}%;height:${p(height)}%`;
  }

  function table(headers, rows, empty) {
    const headings = headers.map(([text, width, numeric]) =>
      `<th${width ? ` style="width:${width}px"` : ""}${numeric ? ' class="num"' : ""}>${escape(text)}</th>`).join("");
    const body = rows.join("") || `<tr><td colspan="${headers.length}">${escape(empty)}</td></tr>`;
    return `<table><thead><tr>${headings}</tr></thead><tbody>${body}</tbody></table>`;
  }

  function isoDate(value) {
    const text = scalar(value);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return "";
    const parsed = new Date(text);
    return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === text ? text : "";
  }

  function shownNrv(value) {
    const text = scalar(value);
    return /^\d+(?:\.\d+)?$/.test(text) ? `${text}%` : text || "—";
  }

  // V2.7：07 节优先读统一整改计划（report_plan.py 生成）。定位失败逐条列出，不再静默跳过。
  function escapeWithBold(value, words) {
    const text = scalar(value);
    const marks = unique((Array.isArray(words) ? words : []).map(scalar).filter((word) => word && text.includes(word)))
      .sort((left, right) => right.length - left.length);
    if (!marks.length) return escape(text);
    const pattern = new RegExp(`(${marks.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "g");
    return text.split(pattern).map((piece) => (marks.includes(piece) ? `<b>${escape(piece)}</b>` : escape(piece))).join("");
  }

  function revisionFromPlan(revision) {
    const lines = [];
    for (const section of rowsOf(revision.sections)) {
      const heading = scalar(section.heading);
      const text = scalar(section.text);
      if (!text) {
        if (heading) lines.push(`${escape(heading)}：`);
        continue;
      }
      const body = section.changed ? `<b>${escapeWithBold(text, section.bold)}</b>`
        : escapeWithBold(text, section.bold);
      lines.push(`${heading ? `${escape(heading)}：` : ""}${body}`);
    }
    return {
      copy: lines.join("\n"),
      options: [],
      failures: rowsOf(revision.failures),
      canExport: revision.canExport !== false,
      pendingText: revision.pendingText === true,
    };
  }

  function reviseLabel(fields, product, images, items, artwork) {
    const field = (key) => scalar(fields[key]?.value);
    const entries = new Map();
    const photos = images.map((image) => rowsOf(image.blocks || image.ocr?.blocks));
    const blocks = photos.flat().filter((block) => !artwork || Array.isArray(block.box) && block.box[1] >= .15);
    const textFrom = (pattern) => {
      const values = unique(blocks.flatMap((block) => scalar(block.text).match(pattern)?.[1] || []));
      return values.length === 1 ? values[0] : "";
    };
    function set(key, heading, value, missing = "") {
      if (value || missing) entries.set(key, { heading, value: value || `〔${missing}〕`, changed: false });
    }

    set("type", "产品类型", field("type") || textFrom(/^(?:产品类型|产品类别|食品类别)[：:]\s*(.{1,50})$/), "");
    set("ingredients", "配料表", field("ingredients"), "按确认的完整配方填写，复合配料与各料包按适用口径列示");
    const net = field("net");
    const packs = field("packs");
    set("net", "净含量", [net, packs && `（${packs}）`].filter(Boolean).join(""), "填写确认的净含量与规格");
    set("standard", "产品执行标准", field("standard"));
    set("license", "食品生产许可证编号", field("license"));
    const production = field("production");
    const expiry = field("expiry");
    if (production && production === expiry) {
      set("production", "生产日期／保质期到期日", production);
    } else {
      set("production", "生产日期", production, "按适用规则填写批次日期或标出可定位的实际喷码部位");
      set("expiry", "保质期到期日", expiry);
    }
    set("shelf", "保质期", field("shelf"));
    set("storage", "贮存条件", field("storage"), "填写生产方核定的贮存条件");
    const roleNames = /^(生产商\s*[/／、]\s*委托商|生产企业名称|生产商|委托商|受委托方|生产厂家|制造商|生产者名称|进口商|代理商|经销商)[：:]/;
    const producerRefs = unique(fields.producer?.refs);
    const producerBlock = blocks.find((block) => producerRefs.includes(block.id) && roleNames.test(scalar(block.text)));
    set("producer", field("producer") && scalar(producerBlock?.text).match(roleNames)?.[1] || "责任主体名称", field("producer"), "按生产或进口责任主体的有效资料填写");
    set("address", "地址", field("address"), "按对应责任主体的有效资料填写");
    set("origin", "产地", field("origin") || textFrom(/^产地[：:]\s*(.{1,60})$/));
    set("country", "原产国（地区）", field("country") || textFrom(/^原产(?:国|地)(?:[（(]地区[）)])?[：:]\s*(.{1,60})$/));
    set("contact", "联系方式", field("contact"));
    set("usage", "食用方法", field("usage") || textFrom(/^(?:食用方法|使用方法)[：:]\s*(.{1,180})$/));
    set("warning", "温馨提示", field("warning") || textFrom(/^温馨提示[：:]\s*(.{1,180})$/));
    set("allergen", "致敏物质提示", field("allergen") || textFrom(/^(?:致敏物质提示|致敏物质信息|致敏原提示|过敏原提示)[：:]\s*(.{1,180})$/));

    // Associate a continuation with its own contractor by line position.
    const supplierHeading = /^(?:[^：:]{1,12}包生产商|被委托商\s*[A-Z]|(?:被|受)委托方\s*[A-Z])[：:]/i;
    const supplierRows = [];
    const licenceEdit = items.find((item) => item.id === "I02" && item.replacement);
    for (const photo of photos) {
      for (const block of photo) {
        const line = scalar(block.text);
        if (!supplierHeading.test(line) || !Array.isArray(block.box)) continue;
        const next = photo.filter((candidate) => Array.isArray(candidate.box) &&
          Math.abs(candidate.box[0] - block.box[0]) < .024 && candidate.box[1] > block.box[1] &&
          candidate.box[1] - block.box[1] < .024).sort((a, b) => a.box[1] - b.box[1])[0];
        const continuation = scalar(next?.text);
        const suffix = !/地址[：:]/.test(line) && !supplierHeading.test(continuation) && /地址[：:]/.test(continuation) ? continuation : "";
        const edited = licenceEdit?.refs?.includes(block.id) && /食品许可证编号[：:]\s*SC\d{14}/i.test(line);
        const row = edited ? line.replace(/食品许可证编号[：:]\s*SC\d{14}/i, licenceEdit.replacement) : line;
        const formatted = (edited ? "<b>" + escape(row) + "</b>" : escape(row)) + (suffix ? "\n" + escape(suffix) : "");
        if (!supplierRows.includes(formatted)) supplierRows.push(formatted);
      }
    }

    const inserts = [];
    const changes = new Set();
    const options = [];
    const ingredientEntry = entries.get("ingredients");
    for (const item of items) {
      const revision = item.revision || {};
      const replacement = scalar(revision.replacement || item.replacement);
      const provisional = unique(item.revisionOptions);
      if (provisional.length) options.push({ title: item.title, values: provisional });
      if (["review", "gap", "pass"].includes(item.outcome || item.group) || !replacement) continue;
      if (item.id === "I02" && supplierRows.some((row) => row.includes(escape(replacement)))) continue;
      if (item.id === "I04") {
        const refs = unique(item.refs);
        const original = blocks.map((block) => refs.includes(block.id) && scalar(block.text).normalize("NFKC"))
          .find((value) => value && ingredientEntry.value.includes(value) && ingredientEntry.value.indexOf(value) === ingredientEntry.value.lastIndexOf(value));
        if (original) {
          ingredientEntry.value = ingredientEntry.value.replace(original, () => replacement);
          ingredientEntry.changed = true;
          continue;
        }
      }
      const target = scalar(revision.field) || ({ I03: "warning", GB7718AL: "allergen" })[item.id];
      const entry = entries.get(target);
      const original = scalar(revision.original);
      if (target && (entry || ["warning", "allergen", "claim", "usage"].includes(target))) {
        const uniqueMatch = original && entry?.value.includes(original) &&
          entry.value.indexOf(original) === entry.value.lastIndexOf(original);
        if (uniqueMatch || !original) {
          const heading = entry?.heading || ({ warning: "温馨提示", allergen: "致敏物质提示", claim: "图案与工艺说明", usage: "食用方法" })[target];
          const value = uniqueMatch ? entry.value.replace(original, () => replacement) :
            replacement.replace(new RegExp(`^${heading}[：:]\\s*`), "");
          entries.set(target, { heading, value, changed: true });
          continue;
        }
      }
      if (!changes.has(replacement)) {
        changes.add(replacement);
        inserts.push({ heading: item.title || item.category, value: replacement, changed: true });
      }
    }

    const ingredientText = ingredientEntry.value.replace(/(^|[。；;])\s*(?=[\u4e00-\u9fff]{1,12}包\s*[A-Z]?[：:])/g, "$1\n").trim();
    ingredientEntry.value = ingredientText;
    const ordered = [...entries.entries()].filter(([key]) => key !== "allergen");
    if (entries.has("allergen")) ordered.splice(ordered.findIndex(([key]) => key === "net") + 1, 0, ["allergen", entries.get("allergen")]);
    const asCopy = ({ heading, value, changed }) => {
      const separator = heading === "配料表" && value.includes("\n") ? "\n" : "";
      return `${escape(heading)}：${separator}${changed ? `<b>${escape(value)}</b>` : escape(value)}`;
    };
    const copy = ordered.map(([, entry]) => asCopy(entry));
    if (supplierRows.length) copy.push("", "各分包生产及委托信息（逐项对照原稿、许可证及委托材料）：", ...supplierRows);
    if (inserts.length) copy.push("", ...inserts.map(({heading, value}) => `<b>【${escape(heading)}】</b>\n${escape(value)}`));
    return { copy: [escape(product), "", ...copy].join("\n"), options };
  }

  function render({ result = {}, draft = {}, images = [], context = {}, sourceBase, generatedAt, includeEvidence = false } = {}) {
    if (result.canPublish === false || ["pending", "conflict"].includes(result.attribution?.status) ||
        result.blocked || rowsOf(result.items).some((item) => item.group === "blocked")) {
      const diagnostics = rowsOf(result.items).filter((item) => item.group === "blocked" || item.id === "attribution");
      return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>审核暂停</title>
<h1>审核暂停</h1><p>请在审核台先核对图片归属。</p><ul>${diagnostics.map((item) =>
        `<li>${escape(item.title)}　${escape(item.detail)}</li>`).join("")}</ul></html>`;
    }
    images = rowsOf(images);
    const index = includeEvidence ? imageIndex(images) : null;
    const embedded = includeEvidence ? images.map((image) => dataImage(image.dataUrl || image.url)) : [];
    const used = new Set();
    // V2.7：02/03/06 的条目身份来自统一整改计划（reportPlan.items），渲染数据仍取审核条目。
    const checkErrors = result.checkErrors && result.checkErrors.ok === false ? rowsOf(result.checkErrors.errors) : [];
    // D3：AI 未参与或失败时，页首如实标注，不用旧结果补位。
    const reportStatus = scalar(result.reportMeta?.status);
    const aiStatus = scalar(result.aiReview?.status);
    const aiUnfinished = reportStatus === "ai-failed" || ["failed", "unavailable", "result-unknown"].includes(aiStatus);
    const aiNotice = reportStatus === "rule-only"
      ? '<div class="box warn"><p style="margin:0">本报告为规则版：AI 审核未参与，结论由规则与人工核验记录生成。</p></div>'
      : aiUnfinished
        ? '<div class="box warn"><p style="margin:0">AI 审核未完成，本报告为规则版；可重试生成新的审核报告。</p></div>'
        : "";
    // 检查未通过时的页首纯文本清单；通过时不额外产生空白，保证与旧报告逐字一致。
    const checkBlock = checkErrors.length ? `
  <div class="box warn"><h4>全文一致性检查未通过（${checkErrors.length} 项）</h4><ul>${checkErrors.map((error) =>
    `<li><b>${escape(scalar(error.code))}</b>：${escape(scalar(error.message))}${scalar(error.detail) ? `（${escape(scalar(error.detail))}）` : ""}</li>`).join("")}</ul></div>` : "";
    const planItems = rowsOf(result.reportPlan?.items);
    const items = rowsOf(result.items).map((item) => {
      const plan = planItems.find((row) => row.itemId === item.id);
      return plan ? { ...item, actionId: plan.actionId, decisionState: plan.decisionState,
        clauseRefs: plan.clauseRefs } : item;
    });
    const isStandard = (item) => item.track === "standard2025";
    const actual = items.filter((item) => !isStandard(item));
    const standard = items.filter(isStandard);
    const critical = actual.filter((item) => item.group === "issue" && ["critical", "redline", "draft-hold"].includes(item.severity));
    const draftHolds = critical.filter((item) => item.severity === "draft-hold");
    const legalCritical = critical.length - draftHolds.length;
    const issues = actual.filter((item) => item.group === "issue" && !critical.includes(item));
    const internal = actual.filter((item) => item.group === "internal");
    const normative = [...issues, ...internal];
    const mustChange = standard.filter((item) => item.outcome === "issue");
    const pending = [...actual.filter((item) => !["issue", "pass", "internal"].includes(item.group)),
      ...standard.filter((item) => !["issue", "pass"].includes(item.outcome))];
    const anchor = pending.find((item) => item.id === "date-anchor");
    const dateCheck = anchor && pending.find((item) => ["F04", "G02"].includes(item.id) &&
      unique(item.refs).some((ref) => unique(anchor.refs).includes(ref)));
    if (dateCheck) {
      pending[pending.indexOf(dateCheck)] = { ...dateCheck, detail: scalar(dateCheck.detail) + " " + scalar(anchor.detail),
        refs: unique([...rowsOf(dateCheck.refs), ...rowsOf(anchor.refs)]) };
      pending.splice(pending.indexOf(anchor), 1);
    }
    // A1：卡片、正文、章节标题共用同一份分类数据（服务端 result.classification）。
    const classification = result.classification || {};
    const recheckRequired = Number.isFinite(classification.recheckRequired) ? classification.recheckRequired : critical.length;
    const confirmedRedline = Number.isFinite(classification.confirmedRedline) ? classification.confirmedRedline : legalCritical;
    const draftHoldCount = Number.isFinite(classification.draftHold) ? classification.draftHold : draftHolds.length;
    const normativeCount = Number.isFinite(classification.normative) ? classification.normative : normative.length;
    const mustChangeCount = Number.isFinite(classification.mustChange) ? classification.mustChange : mustChange.length;
    const pendingCount = Number.isFinite(classification.pending) ? classification.pending : pending.length;
    const passed = items.filter((item) => item.group === "pass");
    const applicability = result.applicability || {};
    const fields = { ...draft.fields, ...Object.fromEntries(rowsOf(result.fields).map((field) => [field.key, field])) };
    const field = (key) => scalar(fields[key]?.value);
    const product = field("name") || scalar(result.productName) || "产品名称待核对";
    const labelFacts = result.labelFacts || {};
    const productDescriptor = unique([labelFacts.type, labelFacts.flavor]).join(" · ");
    const packOrigins = rowsOf(labelFacts.packOrigins).filter((entry) => entry?.pack && entry?.value);
    const packOriginText = packOrigins.map(({ pack, value }) => `${pack}：${value}`).join("；");
    const originText = packOriginText ? `产地：${packOriginText}` :
      labelFacts.country ? `原产国（地区）：${labelFacts.country}` :
      labelFacts.origin ? `产地：${labelFacts.origin}` : "";
    const at = new Date(generatedAt || Date.now());
    const date = !generatedAt && isoDate(applicability.auditDate) ||
      (Number.isNaN(at.valueOf()) ? "" : new Intl.DateTimeFormat("sv-SE", {timeZone: "Asia/Shanghai"}).format(at));
    const net = field("net");

    function evidence(values, limit = Infinity) {
      if (!index) return "";
      const links = unique(values).flatMap((ref) => {
        const hit = index.refs.get(ref);
        if (!hit) return [];
        used.add(ref);
        const marked = embedded[hit.photo - 1] && markStyle(hit.block.box);
        return [`<a href="#${marked ? "mark-" : ""}${hit.id}">图 ${hit.photo} · 区域 ${hit.number}</a>`];
      });
      const extra = links.length > limit ? `<details><summary>另有 ${links.length - limit} 处证据</summary>${links.slice(limit).join("　")}</details>` : "";
      return links.slice(0, limit).join("　") + extra;
    }

    const sentences = (values) => unique(values).map((value) => escape(value)).join("<br>");
    function internalReason(item) {
      return ({
        I01: "两处列示顺序可直接对照。净含量反映成品分包重量，配料表列示口径还要查投料记录，不能仅凭成品包重推断每种配料的加入量。",
        I02: "同组编号的引导词不一致。原稿核对时应同时检查文字和许可证凭证，确认补字后的主体与对应编号仍配对。",
        I03: "该段同时给出了产生异常的解释和勿食用的处置要求。文字应让消费者能直接决定如何处理包装破损、胀袋和漏气。",
        I04: "复合配料括号收尾处同时承担分隔作用。请核对配方中相邻内容是独立加入还是属于同一复合配料，确认后再改分隔。",
        I07: "此项比的是各行之间的相对层次。将整表改成相同字重仍无法区分层次，应按适用版本确认需要突出哪些行，并核对打样效果。",
        X01: "几处读数说的是同一项目，可直接对照。各被委托商、各料包生产商的许可证和标准代号本来可以不同，此项只在同一主体名下比对；请核对原稿后统一。",
        X02: "外包装净含量与标签所列各分包重量属于同一销售单元的换算关系，可直接相加对照。",
      })[item.id];
    }

    function cite(item) {
      return citations(item).map((source) => {
        if (includeEvidence) return sourceText(source, sourceBase);
        const name = scalar(source.standardId || source.name);
        return name ? `<div class="rule"><b>依据</b>　${escape(unique([name, source.clause]).join(" · "))}</div>` : "";
      }).join("");
    }

    function finding(item) {
      const refs = evidence(item.refs, 3);
      const basis = cite(item);
      const analysis = item.analysis || {};
      const facts = sentences(analysis.facts) || escape(item.detail);
      const reasoning = scalar(analysis.reasoning || internalReason(item));
      // 依据要点可以是一行，也可以是条款原文列表（C02 引 4.2.1／3.4／4.4.1 与食品安全法）。
      const requirement = Array.isArray(analysis.requirement) ? analysis.requirement : [analysis.requirement];
      const requirements = unique([...requirement, item.basis]);
      const exceptions = sentences(analysis.exceptions);
      return `${facts}${reasoning ? `<br><b>判读</b>　${escape(reasoning)}` : ""}
        ${exceptions ? `<br><b>适用条件</b>　${exceptions}` : ""}${requirements.length ? `<div class="rule"><b>依据要点</b>　${sentences(requirements)}</div>` : ""}${basis}${refs ? `<div class="rule">${refs}</div>` : ""}${includeEvidence && item.reviewBasis ? `<div class="rule">核验记录：${escape(item.reviewBasis)}</div>` : ""}`;
    }

    // AI 生成的整改要求只用于 02/03 节（findings）：程序已校验引用，失败回退模板。
    const remediations = result.remediations && typeof result.remediations === "object" ? result.remediations : {};
    function action(item, pendingItem = false) {
      const analysis = item.analysis || {};
      if (!pendingItem && Array.isArray(item.options) && item.options.length) {
        // Q2：02 与 07 读同一份结构化 options（optionId/condition/text）。
        const rows = item.options.map((option) => {
          const optionId = scalar(option.optionId);
          const condition = scalar(option.condition);
          const text = scalar(option.text);
          return `<li data-option-id="${escape(optionId)}">${condition ? `<b>${escape(condition)}</b>：` : ""}${escape(text)}</li>`;
        }).join("");
        return `<ul>${rows}</ul>`;
      }
      const template = sentences(analysis.remediation) || escape(item.action);
      const missing = pendingItem ? sentences(analysis.missingEvidence) : "";
      const ai = pendingItem ? null : remediations[item.id];
      const clauses = ai && Array.isArray(ai.clauseIds)
        ? unique(ai.clauseIds.map((value) => scalar(value)).filter(Boolean)) : [];
      const basis = ai ? scalar(ai.basis) : "";
      const cite = clauses.length ? clauses.join("、") : basis;
      const remediation = ai && scalar(ai.text)
        ? `${escape(scalar(ai.text))}${cite ? ` <span class="rule">依据 ${escape(cite)}</span>` : ""}`
        : template;
      return `${missing ? `<b>请提供</b>　${missing}<br>` : ""}${remediation || "请核对原稿并在改版后复审。"}`;
    }

    function findings(values, empty) {
      const rows = values.map((item, index) => `<tr><td class="num">${index + 1}</td>
        <td>${item.severity === "draft-hold" ? '<span class="chip r">稿件停版</span>' : chip(item, isStandard(item))}<br><b>${escape(item.title)}</b></td>
        <td>${finding(item)}</td><td>${action(item)}</td></tr>`);
      return table([["#", 34, true], ["问题", 150], ["现状与依据"], ["整改要求", 250]], rows, empty);
    }

    const view = result.reportView || null;
    const itemById = new Map(items.map((item) => [item.id, item]));
    const viewItems = (key, fallback) => Array.isArray(view?.sections?.[key])
      ? view.sections[key].map((id) => itemById.get(id)).filter(Boolean) : fallback;
    const viewCritical = viewItems("02", critical);
    const viewNormative = viewItems("03", normative);
    const viewMustChange = viewItems("04", mustChange);
    const criticalTable = findings(viewCritical, "本次证据未支持确认红线问题。相关条款或材料缺口详见待客户核实。");
    const issuesTable = findings(viewNormative, "当前资料未发现可列示的规范性问题。待核对项目见第 06 节。");
    // 04 的跨章节指引跟着实际待核实事项走：06 没有致敏条目时不得再写「致敏物质的判定见第 06 节」。
    const allergenPending = pending.some((item) => /^(?:GB7718AL|B1B)/.test(scalar(item.id)) ||
      /致敏|过敏/.test(`${scalar(item.title)}${scalar(item.detail)}`));
    const emptyStandard = pending.length
      ? `本次未确认按 2025 版审核的不符合项。${allergenPending ? "致敏物质的判定见第 06 节；" : ""}其余待核实事项亦见第 06 节。`
      : "本次未确认按 2025 版审核的不符合项，亦无待核实事项。";
    const standardTable = findings(viewMustChange, emptyStandard);
    const pendingRows = viewItems("06", pending);
    const materialsLine = view?.materials?.length
      ? `<p class="lead">需企业提供：${view.materials.map((value) => escape(scalar(value))).join("、")}。</p>` : "";
    const pendingTable = table([["#", 34, true], ["事项", 190], ["核实依据与说明"]], pendingRows.map((item, index) =>
      `<tr><td class="num">${index + 1}</td><td><b>${escape(item.title)}</b>${item.aiFlag ? ' <span class="chip">AI 复核提出</span>' : ""}</td><td>${isStandard(item) ? "按 2025 版审核。<br>" : ""}${finding(item)}<br><b>核实动作</b>　${action(item, true)}</td></tr>`
    ), "本次无独立的待核实事项。");

    const nutritionRows = rowsOf(result.nutritionRows);
    const rereadRows = nutritionRows.filter((row) => row.reread);
    const rereadLead = rereadRows.length ? ` 其中${rereadRows.map((row) => `${escape(String(row.name).replace(/^—/, ""))} ${escape(String(row.value))} 经放大 ${escape(String(row.reread.scale))} 倍重读确认（原读数 ${escape(String(row.reread.from))}），由程序自动裁切重读，不是人工改值`).join("；")}。` : "";
    // 能量反算直接引用 audit.py 已算好的 N03 数据（17／37／17 kJ/g 为 GB 28050 表 1 换算系数），
    // JS 只负责排版，不重算总量；任一供能读数异常或缺失时 N03 不会生成，此处也随之不出现反算式。
    const energyCheck = items.find((item) => item.id === "N03")?.energyCheck;
    function energyReverse(row, calculation) {
      if (!energyCheck || scalar(row.name) !== "能量") return "";
      const {protein, fat, carb, energy, total, difference, within} = energyCheck;
      if (![protein, fat, carb, energy, total, difference].every((value) => scalar(value))) return "";
      const formula = `能量反算：蛋白质 ${protein}×17＋脂肪 ${fat}×37＋碳水 ${carb}×17＝${total} kJ` +
        `（标示 ${energy} kJ，差 ${difference}${within ? "，在修约范围内" : ""}）`;
      const nrv = scalar(calculation);
      return escape(nrv ? `${formula}；NRV：${nrv}` : formula);
    }
    const nutritionQa = result.factQa?.nutritionTable || draft.factQa?.nutritionTable || {};
    const extractFailed = result.nutritionExtractFailure || nutritionQa.extractStatus === "structural-failure"
      || items.some((item) => item.id === "nutrition-extract-failure");
    const pendingQa = rowsOf(nutritionQa.pendingReview);
    const qaNote = extractFailed
      ? escape(nutritionQa.failureReason || "已检测到营养成分表并读到数字，但项目与数值未能可靠配对，不能作为空表通过。")
      : (pendingQa.length ? `待核对：${pendingQa.map((row) => reasonText(row.reason || row.label || "")).filter(Boolean).slice(0, 6).join("；")}。` : "");
    const emptyNutrition = extractFailed
      ? (nutritionQa.failureReason || "已检测到营养成分表并读到数字，但项目与数值未能可靠配对，不能作为空表通过。")
      : "当前图像无可列示的营养成分表读数。";
    const nutritionTable = `${extractFailed || qaNote ? `<div class="box warn"><h4>${extractFailed ? "营养表结构提取失败" : "营养表待核对"}</h4><p style="margin:0">${qaNote || escape(emptyNutrition)}</p></div>` : ""}
    <div class="nutri">${table([["项目", 110], ["标示值", 78, true], ["标示 NRV%", 70, true],
      ["复核计算"], ["应为", 70, true], ["判定", 56, true]], nutritionRows.map((row) => {
      const shown = shownNrv(row.nrv);
      const refs = evidence(row.refs, 2);
      const rereadTag = row.reread ? ' <span class="chip b">放大重读</span>' : "";
      const calculation = energyReverse(row, row.calculation) || escape(row.calculation) || "缺少可用的复算条件。";
      return `<tr><td>${escape(row.name)}</td><td class="num">${escape(row.value)}${rereadTag}</td><td class="num">${escape(shown)}</td>
        <td>${calculation}${refs ? `<div class="rule">${refs}</div>` : ""}</td>
        <td class="num">${escape(row.expected) || "—"}</td><td class="num${row.match === true ? " ok" : row.match === false ? " no" : ""}">${row.match === true ? "✓" : row.match === false ? "不一致" : "待核实"}</td></tr>`;
    }), emptyNutrition)}</div>`;
    const calculations = items.filter((item) => ["N02", "N03"].includes(item.id));
    const fieldEvidence = includeEvidence ? rowsOf(result.fields).map((field) => {
      const refs = evidence(field.refs);
      return `<li><b>${escape(field.label || field.key)} · ${escape(label(field.status))}</b>${field.value ? `　${escape(field.value)}` : ""}。${escape(field.reason)}${refs ? `<div class="rule">${refs}</div>` : ""}</li>`;
    }) : [];
    const coverageEvidence = rowsOf(result.coverage).map((area) => {
      const refs = includeEvidence ? unique(area.images).flatMap((id) => {
        const n = index.ids.get(id);
        return n ? [`<a href="#photo-${n}">图 ${n}</a>`] : [];
      }).join("、") : "";
      return `${escape(area.label || area.id)}（${escape(label(area.status))}${refs ? `，${refs}` : ""}）`;
    }).join("、");
    const anchorEvidence = includeEvidence ? rowsOf(result.anchors).map((anchor) => {
      const targets = rowsOf(anchor.targets).map((target) => `${escape(target.label)} ${escape(target.value)} ${evidence([target.ref])}`).join("；");
      return `<li><b>${escape(anchor.label) || "跨图定位"}</b> ${evidence([anchor.sourceRef])}。${targets || evidence([anchor.targetRef]) || "目标待定位"}。${escape(label(anchor.status))}。</li>`;
    }) : [];

    function figure(photo, indexInReport) {
      const n = indexInReport + 1;
      const data = embedded[indexInReport];
      const shown = [...used].map((ref) => index.refs.get(ref)).filter((entry) => entry.photo === n);
      const marks = shown.flatMap((entry) => {
        const style = markStyle(entry.block.box);
        return style ? [`<a id="mark-${entry.id}" style="position:absolute;${style};outline:2px solid var(--red)" href="#${entry.id}" aria-label="区域 ${entry.number}"></a>`] : [];
      }).join("");
      const picture = data ? `<div style="position:relative;width:fit-content;max-width:100%;line-height:0"><img src="${data}" style="display:block;max-width:100%;max-height:420px" alt="图 ${n}，${escape(photo.name)}">${marks}</div>` : "";
      return `<li id="photo-${n}"><b>图 ${n}　${escape(photo.name) || "原始图像"}</b>${picture}
        ${shown.length ? `<ol>${shown.map((entry) => `<li id="${entry.id}"><b>区域 ${entry.number}</b>　${escape(entry.block.text)}
        ${markStyle(entry.block.box) ? `<span class="rule">位置 ${entry.block.box.map((value) => (value * 100).toFixed(1) + "%").join("／")}</span>` : ""}</li>`).join("")}</ol>` : ""}</li>`;
    }

    const sourceNames = (values) => unique(values.flatMap(citations).map((source) => scalar(source.name || source.standardId))).join("｜");
    const citedSources = sourceNames(items) || "待确认";
    const edition = result.version === "2025" ? "GB 7718-2025、GB 28050-2025" : "";
    const transition = applicability.transition || result.versionTransition;
    const effective = scalar(transition?.effective);
    const published = scalar(transition?.standardsPublished || transition?.published);
    const artwork = scalar(applicability.artworkDate || result.artworkDate?.value);
    const isArtwork = applicability.sampleKind === "artwork" ||
      (applicability.sampleKind !== "physical" && Boolean(artwork));
    const production = isoDate(applicability.productionDate || context.productionDate);
    const planned = isoDate(applicability.plannedProductionDate);
    const expiry = isoDate(applicability.expiryDate);
    const productionText = production ? `实际生产日期为 ${escape(production)}。` :
      planned ? `计划投产日期为 ${escape(planned)}，实际批次尚待确认。` :
      (field("production") ? `标签生产日期栏读为「${escape(field("production"))}」，实际生产批次待确认。` : "实际生产日期待确认。");
    const scope = scalar(result.scopeLabel) || "范围待核对";
    const timeline = [
      ...(published ? [[published, "新标准发布。", `GB 7718-2025、GB 28050-2025 发布。${transition.measurePublished ? `《食品标识监督管理办法》于 ${escape(transition.measurePublished)} 公布。` : ""}`]] : []),
      [date || "待核对", "本次审核。", escape(applicability.decision) || `${productionText}${escape(result.versionLabel) || "审核范围待核对"}。`],
      ...(effective ? [[effective, "新标准与新规实施。", `GB 7718-2025、GB 28050-2025 及《食品标识监督管理办法》于该日实施。`]] : []),
    ].map(([day, title, message]) => `<div class="d">${escape(day)}</div><div class="c"><b>${title}</b>${message}</div>`).join("");
    const shelf = scalar(applicability.shelfLife || field("shelf"));
    const gap = items.find((item) => item.group === "gap");
    const focus = critical[0] || mustChange[0] || pending.find((item) => item.id === "C02") || items.find((item) => item.hypothesis) || internal[0] || pending[0];
    const needsReview = Boolean(pending.length);
    // 处置词与客户范文对齐，全文只保留这四种：不予通过／整改后复审／待核实／本次审核记录。
    const verdict = critical.length ? "不予通过"
      : (normative.length || mustChange.length) ? "整改后复审"
      : needsReview ? "待核实" : "本次审核记录";
    const [background, ink] = issues.length || mustChange.length ? ["var(--amberbg)", "var(--amber)"] : ["var(--graybg)", "var(--gray)"];
    const verdictBox = critical.length ? "" : ` style="background:${background};border-color:var(--line);border-left-color:${ink}"`;
    const verdictTag = critical.length ? "" : ` style="background:${ink}"`;
    const verdictText = critical.length ? "" : ' style="color:var(--ink2)"';
    const emphasize = (value) => `<strong${verdictText}>${escape(value)}</strong>`;
    // 四档名称按客户范文固定，不随内容变体；「稿件停版」只出现在第 02 节的 chip 与结论句。
    const totals = [
      ["r", recheckRequired, "须整改后复审", `确认红线 ${confirmedRedline} · 稿件停版 ${draftHoldCount}`],
      ["a", normativeCount, "规范性 · 限期改正", ""],
      ["b", mustChangeCount, "新规必改（2027-03-16）", ""],
      ["g", pendingCount, "待客户核实", ""],
    ].map(([tone, count, title, note]) => `<div class="stat ${tone}"><div class="n">${count}</div><div class="l">${escape(title)}` +
      (note ? `<br><small>${escape(note)}</small>` : "") + "</div></div>").join("");
    const attribution = { single: "单张原图", automatic: "同品证据匹配", manual: "已人工确认同一销售包装" }[result.attribution?.confirmedBy];
    const observation = focus?.hypothesis && draft.nutrition?.fat?.value ?
      `表内脂肪标示 ${escape(draft.nutrition.fat.value)} ${escape(draft.nutrition.fat.unit)}。` : "";
    const summary = focus ? `<br>其中，${emphasize(focus.title)}。${observation}${escape(focus.detail)}` : "";
    const rulesGap = gap ? `<div class="rule"><b>适用依据缺口</b>　${escape(gap.title)}。${escape(gap.detail)}</div>` : "";
    const packs = field("packs");
    const dateEvidence = evidence(result.artworkDate?.refs);
    const checkedList = passed.filter((item) => !calculations.includes(item)).map((item) =>
      `<li><b>${isStandard(item) ? "2025 版 · " : ""}${escape(item.title)}</b>。${finding(item)}</li>`).join("");
    // 致敏逐类结论为 pass、且该结论依据的是配料表内加粗时，08 节固定补一行口径说明。
    const allergenBoldPass = items.find((item) => item.allergenBold === true && item.outcome === "pass");
    const saturated = nutritionRows.find((row) => scalar(row.name).includes("饱和脂肪") && row.match === true);
    const calculationNotes = calculations.map((item) => {
      if (includeEvidence) return `<li><b>${escape(item.title)}</b>。${finding(item)}</li>`;
      if (item.customerConclusion) return `<li><b>能量复算</b>。${escape(item.customerConclusion)}${cite(item)}</li>`;
      if (item.id === "N02" && item.outcome === "pass") return `<li><b>营养素参考值</b>。可复算的行与标示一致，逐项数值见上表。${cite(item)}</li>`;
      return `<li><b>${escape(item.title)}</b>。${escape(item.detail)} ${escape(item.action)}。${cite(item)}</li>`;
    }).join("") + (saturated ? `<li><b>饱和脂肪</b>。以标示的 ${escape(saturated.value)} 复算为 ${escape(saturated.expected)}，与标示一致。检测报告的指标名称及数据口径仍需核对，见第 06 节。</li>` : "");
    const evidencePhotos = includeEvidence ? images.map(figure).join("") : "";
    // 08 节说明由专用检查点库驱动（result.reviewNotes）。旧载荷没有该字段时保留原渲染，
    // 便于工作台与历史报告继续打开；新报告一律走检查点库。
    const reviewNotes = rowsOf(result.reviewNotes);
    const legacyNotes = [
      `<li>审核对象为所提供的标签图片。文字与数值${context.verified === true && (context.verifiedFields || []).length ? "已人工校对" : "仍待人工校对"}。标示位置和字高以完整包装、矢量稿或打样件核验。</li>`,
      includeEvidence ? unique(context.notes).map((note) => `<li>${escape(note)}</li>`).join("") : "",
      packOriginText ? `<li><b>产品名称与分包产地</b>。名称按原稿列示。包装的产地信息对应所列料包（${escape(packOriginText)}），产品名中的地域词不作为整盒或其他料包产地的证据。</li>` : "",
      allergenBoldPass ? "<li><b>致敏物质</b>。配料表内已对相关配料以加粗方式提示，符合 GB 7718-2025 附录 D.2.1；如后续配方调整，须同步复核。</li>" : "",
      labelFacts.jobTicketMass && net ? `<li><b>制版净含量核对</b>。制作表头显示 ${escape(labelFacts.jobTicketMass)}，包装版面标示 ${escape(net)}。请印前统一制作信息并确认最终付印值。制作表头不在印刷范围内时，这一处不作为销售标签的双重标示冲突。</li>` : "",
      packOriginText && packs ? '<li><b>分包与定量标示</b>。各料包重量用于说明销售规格。图文是否特别强调某项配料须逐项核对，触发定量标示时再按适用依据和实际配方核定比例。</li>' : "",
      checkedList,
      rowsOf(result.exclusionNotes).map((note) => `<li><b>${escape(scalar(note.title))}</b>。${escape(scalar(note.text))}　<span class="rule">${escape(scalar(note.clause))}</span></li>`).join(""),
      isArtwork ? '<li><b>刀版与实物核验</b>。本次按提供的印刷版面核查文字与图案，成品字符高度及实际喷码位置仍须以有比例的矢量稿、打样件或首批实物核验。</li>' : "",
    ].join("");
    const noteList = reviewNotes.length ? reviewNotes.map((note) =>
      `<li><b>${escape(scalar(note.title))}</b>。${escape(scalar(note.text))}${scalar(note.clause) ? `<div class="rule">${escape(unique(String(scalar(note.clause)).split(/[、；;]/).map((v) => v.trim())).join("、"))}</div>` : ""}</li>`).join("") : legacyNotes;
    // V2.4：未经程序核验的 AI 候选一律不进客户报告，只在工作台/API 响应里展示。
    // 客户报告只呈现规则引擎与程序核验通过的事实，避免报告自我质疑。
    const agentBlock = "";
    const planRevision = result.revision && typeof result.revision === "object" ? result.revision : null;
    const revision = planRevision ? revisionFromPlan(planRevision) : reviseLabel(fields, product, images, items, isArtwork);
    const revisionFailures = rowsOf(planRevision?.failures);
    const normalizeOption = (value) => typeof value === "object" && value
      ? { id: scalar(value.optionId), condition: scalar(value.condition), text: scalar(value.text) }
      : { id: "", condition: "", text: scalar(value) };
    const planOptions = rowsOf(planRevision?.options).map((option) =>
      ({ title: scalar(option.title), values: (Array.isArray(option.values) ? option.values : rowsOf(option.values)).map(normalizeOption) }));
    const revisionOptions = (planOptions.length ? planOptions : (revision.options || [])).map(({ title, values }) =>
      `<li><b>${escape(title)}</b><ul>${values.map((value) =>
        `<li data-option-id="${escape(value.id)}">${value.condition ? `<b>${escape(value.condition)}</b>：` : ""}${escape(value.text)}</li>`).join("")}</ul></li>`).join("");
    const retainedNutrition = nutritionRows.length ? `<div class="nutri">${table([["营养项目", 150], ["标示值"], ["标示 NRV%", 110, true]],
      nutritionRows.map((row) => `<tr><td>${escape(row.name)}</td><td>${escape(row.value)}</td><td class="num">${escape(shownNrv(row.nrv))}</td></tr>`), "")}</div>` : "";

    return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>预包装食品中文标签合规审核报告 · ${escape(product)}</title>
<style>${css}</style>
<style>
@media print {
  @page { size: A4; margin: 13mm; }
  body { line-height: 1.45; }
  .page { padding: 0; }
  .stats { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .tl { grid-template-columns: 88px 1fr; }
  .box, pre { margin: 8px 0; }
  h2 + .lead { break-after: avoid; }
  thead { display: table-header-group; }
  tr { break-inside: avoid; }
  .nutri { break-inside: avoid; }
  .nutri table { break-inside: avoid; }
  pre { line-height: 1.55; break-inside: auto; }
  li { break-inside: avoid; }
  h4 { break-after: avoid; }
  th, td { padding-top: 6px; padding-bottom: 6px; }
  .foot { margin-top: 8px; padding-top: 5px; line-height: 1.4; break-inside: avoid; }
}
</style>
</head>
<body>
<div class="page">

  <div class="kicker">Food Label Compliance Review</div>
  <h1>预包装食品中文标签合规审核报告</h1>
  <div class="sub">标的产品：<b>${escape(product)}${productDescriptor ? `（${escape(productDescriptor)}）` : ""}</b>${net ? `　净含量 ${escape(net)}` : ""}${packs ? `（${escape(packs)}）` : ""}${originText ? `　${escape(originText)}` : ""}${labelFacts.otherPackOrigins ? `；其余料包产地见包装${labelFacts.otherPackOriginsBelow ? "下方" : ""}受托方信息。` : ""}</div>

  <div class="meta">
    <div>审核对象：${escape(scope)}；${isArtwork ? "设计稿（刀版图），非实物" : `标签原图 ${images.length} 张`}${attribution ? `；${escape(attribution)}` : ""}</div>
    <div>审核日期：<b>${escape(date)}</b></div>
    <div>审核依据：${edition ? `指定知识库中的 ${escape(edition)}` : escape(citedSources)}</div>
    ${result.auditMeta ? `<div>本次审核模型：${escape(scalar(result.auditMeta.model))}　审核时间：${escape(scalar(result.auditMeta.auditedAt))}</div>` : ""}
    ${result.aiReview?.stats ? `<div>AI 审核：检查 ${scalar(result.aiReview.stats.checked)} 项，提出 ${scalar(result.aiReview.stats.proposed)} 条候选；${scalar(result.aiReview.stats.merged)} 条与既有事项重复已合并，${scalar(result.aiReview.stats.new)} 条新增，${scalar(result.aiReview.stats.pendingReview)} 条待人工复核（状态：${escape(scalar(result.aiReview.status))}）</div>` : ""}
  </div>

  <div class="verdict"${verdictBox}>
    <span class="tag"${verdictTag}>审核结论 · ${verdict}</span>
    <p${verdictText}>共列示 ${emphasize(`${recheckRequired} 项须整改后复审`)}，其中确认红线 ${emphasize(`${confirmedRedline} 项`)}、稿件停版 ${emphasize(`${draftHoldCount} 项`)}；${emphasize(`${normativeCount} 项规范性检查或标签内对照`)}、${emphasize(`${mustChangeCount} 项新规必改项`)}、${emphasize(`${pendingCount} 项待核实事项`)}。${summary}</p>
  </div>

  <div class="stats">${totals}</div>${aiNotice}${checkBlock}

  <h2><span class="num">01</span>适用版本与时间轴</h2>
  <p class="lead">${edition ? `本次按指定知识库中的 <b>${escape(edition)}</b> 审核。` : "请按食品类别核对专项审核要求。"}</p>
  <div class="tl">${timeline}</div>
  <div class="box warn">
    <h4>给企业的实务判断</h4>
    <p style="margin:0">${artwork ? `刀版页眉「制作日期」为 <b>${escape(artwork)}</b>，对应制版记录。${dateEvidence} ` : ""}${productionText}${shelf ? `标签保质期为 <b>${escape(shelf)}</b>。` : ""}${expiry ? `已校对的到期日为 <b>${escape(expiry)}</b>。` : ""}${edition ? "定版前请核对日期和报告中列出的标示事项。" : "请先确认生产地、食品类别与适用审核要求。"}</p>
    ${rulesGap}
  </div>

  <h2><span class="num">02</span>须整改后复审 ${recheckRequired} 项（确认红线 ${confirmedRedline} · 稿件停版 ${draftHoldCount}）</h2>
  ${draftHolds.length ? '<p class="lead">标为「稿件停版」的事项是对当前设计稿的放行决定，法定结论另行列示。</p>' : ""}
  ${criticalTable}

  <h2><span class="num">03</span>规范性问题（限期改正，${normativeCount} 项）</h2>
  ${internal.length ? '<p class="lead">「标签内对照」记录同一包装的标示差异。请核对原稿和材料后确定整改要求。</p>' : ""}
  ${issuesTable}

  <h2><span class="num">04</span>新规必改项（2027-03-16 前完成，${mustChangeCount} 项）</h2>
  ${standardTable}

  <h2><span class="num">05</span>营养成分表数值复核</h2>
  <p class="lead">计量基准：${escape(draft.fields?.basis?.value) || "待核对"}。表内复算按本次启用的指定标准口径，依据印刷读数或校对值。${rereadLead}</p>
  ${nutritionTable}
  ${calculationNotes ? `<div class="box info"><h4>补充判读</h4><ul style="margin:6px 0 0">${calculationNotes}</ul></div>` : ""}

  <h2><span class="num">06</span>待客户核实的 ${pendingCount} 项</h2>
  ${pendingTable}

  <h2><span class="num">07</span>${revision.canExport === false
    ? "建议修改后文本（本次定位失败，不可直接交生产方）"
    : (revision.pendingText || pendingCount > 0 || recheckRequired > 0)
      ? "建议修改后文本（部分内容待确认，不可直接交生产方）"
      : "建议修改后文本（可直接交生产方改版）"}</h2>
  <p class="lead">${revision.canExport === false
    ? "以下为根据现有原稿编排的底稿。存在未能定位的整改动作，本稿不可直接交生产方改版，请先核对下列原文。"
    : "以下为在现有原稿副本上应用已确认整改后的改版稿。加粗为建议变更，〔〕内需补充确认。核实项和原稿读数校对完成后再付印。"}</p>
  ${revisionFailures.length ? `<div class="box warn"><h4>定位失败，本稿不可直接改版</h4><ul>${revisionFailures.map((row) =>
    `<li><b>${escape(scalar(row.itemId))}</b>：${escape(scalar(row.reason))}${scalar(row.before) ? `（需核对原文：${escape(scalar(row.before))}）` : ""}</li>`).join("")}</ul></div>` : ""}
  <pre>${revision.copy}</pre>
  ${revisionOptions ? `<div class="box warn"><h4>核实后选择的改法</h4><ul>${revisionOptions}</ul></div>` : ""}
  ${retainedNutrition ? `<p class="lead">营养成分表保留本次标示读数（${escape(field("basis")) || "计量基准待核对"}）。按第 05 节核实后定稿。</p>${retainedNutrition}` : ""}

  <h2><span class="num">08</span>审核说明</h2>
  <ul style="font-size:13.5px; color:var(--ink2)">
    ${noteList}${agentBlock}
    ${includeEvidence && coverageEvidence ? `<li><b>图像覆盖</b>　${coverageEvidence}。</li>` : ""}
    ${fieldEvidence.length ? `<li><b>字段核对</b><ul>${fieldEvidence.join("")}</ul></li>` : ""}
    ${anchorEvidence.length ? `<li><b>跨图定位</b><ul>${anchorEvidence.join("")}</ul></li>` : ""}
    ${evidencePhotos}
  </ul>

  <div class="foot">
    审核依据：<b>本次已引用</b>——${escape(citedSources)}。<br>判定规则：相关依据缺口或资料待核实时，保留核对意见。${includeEvidence ? "可回查的条款链接需本机服务运行。" : ""}
  </div>

</div>
</body>
</html>`;
  }

  async function embedImages(images) {
    return Promise.all(rowsOf(images).map(async (image) => {
      if (dataImage(image.dataUrl)) return image;
      if (typeof image.url !== "string" || !image.url.startsWith("blob:")) return image;
      const response = await fetch(image.url);
      if (!response.ok) throw new Error("无法读取报告所用的照片。");
      const blob = await response.blob();
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.addEventListener("load", () => resolve(reader.result), { once: true });
        reader.addEventListener("error", () => reject(new Error("无法嵌入报告所用的照片。")), { once: true });
        reader.readAsDataURL(blob);
      });
      if (!dataImage(dataUrl)) throw new Error("报告照片需为可嵌入的 PNG、JPEG 或 WebP。");
      return { ...image, dataUrl };
    }));
  }

  window.LabelReport = Object.freeze({ render, embedImages });
})();

