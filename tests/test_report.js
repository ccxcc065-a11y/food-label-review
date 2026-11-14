"use strict";

const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const test = require("node:test");

global.window = { location: { origin: "http://127.0.0.1:8765" } };
require("../report.js");

const { render } = window.LabelReport;
const imageData = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/6VUAAAAASUVORK5CYII=";

test("报告按八节组织并保留跨图证据、来源和 2025 版审核结果", () => {
  const html = render({
    includeEvidence: true,
    draft: {
      fields: { name: { value: "验证样本" }, basis: { value: "每 100 克" } },
      nutrition: { fat: { value: "20.0", unit: "g", nrv: "35", refs: ["b-2"] } },
    },
    images: [
      { id: "a", name: "正面.jpg", dataUrl: imageData, blocks: [{ id: "a-1", text: "日期见封口", box: [0.1, 0.2, 0.3, 0.1] }] },
      { id: "b", name: "封口.jpg", dataUrl: imageData, blocks: [{ id: "b-2", text: "脂肪 20.0", box: [0.5, 0.5, 0.25, 0.15] }] },
    ],
    result: {
      version: "2025",
      versionTransition: { published: "2025-03-16", effective: "2027-03-16" },
      items: [
        { id: "G01", group: "pass", track: "actual", title: "已定位名称", refs: ["a-1"] },
        { id: "N02", group: "issue", outcome: "issue", track: "standard2025", title: "NRV 复算需核对",
          detail: "20.0 ÷ 60 × 100%", refs: ["b-2"],
          source: { filename: "GB 28050-2025.pdf", clause: "A.3", page: 4, localUrl: "/knowledge/doc-01" } },
        { id: "version", group: "gap", track: "actual", title: "指定依据不足" },
      ],
      fields: [{ key: "name", status: "marked", value: "验证样本", refs: ["a-1"] }],
      coverage: [{ id: "front", label: "正面", status: "covered", images: ["a"] }],
      anchors: [{ label: "喷码", sourceRef: "a-1", targetRef: "b-2", status: "linked" }],
    },
    context: { verified: true },
    generatedAt: "2026-11-07T18:20:00Z",
  });
  assert.deepEqual([...html.matchAll(/<h2><span class="num">(\d+)<\/span>/g)].map((match) => match[1]),
    ["01", "02", "03", "04", "05", "06", "07", "08"]);
  assert.match(html, /2026-11-08/);
  assert.match(html, /<div class="tl">[\s\S]*2025-03-16[\s\S]*2026-11-08[\s\S]*2027-03-16/);
  // A1：章节标题与卡片改为读取唯一分类数据，标题带条目数，卡片 1 带副行。
  assert.match(html, /须整改后复审 \d+ 项（确认红线 \d+ · 稿件停版 \d+）/);
  assert.match(html, /规范性问题（限期改正，\d+ 项）/);
  assert.match(html, /新规必改项（2027-03-16 前完成，\d+ 项）/);
  assert.deepEqual([...html.matchAll(/<div class="l">([^<]*)<br><small>([^<]*)<\/small><\/div>|<div class="l">([^<]*)<\/div>/g)]
    .map((match) => match[1] || match[3]),
    ["须整改后复审", "规范性 · 限期改正", "新规必改（2027-03-16）", "待客户核实"]);
  assert.match(html, /确认红线 0 · 稿件停版 0/);
  assert.match(html, /审核结论 · 整改后复审/);
  assert.match(html, /<th>现状与依据<\/th><th style="width:250px">整改要求/);
  assert.match(html, /2025 版 · 需处理/);
  assert.match(html, /20\.0 ÷ 60 × 100%/);
  assert.match(html, /GB 28050-2025\.pdf · A\.3 · 第 4 页/);
  assert.match(html, /href="http:\/\/127\.0\.0\.1:8765\/knowledge\/doc-01#page=4"/);
  assert.match(html, /id="mark-evidence-1-1" style="position:absolute;left:10%;top:20%;width:30%;height:10%/);
  assert.match(html, /href="#mark-evidence-2-1"/);
  assert.match(html, /<a href="#photo-1">图 1<\/a>/);
  assert.equal((html.match(/<img src="data:image\/png;/g) || []).length, 2);
  const css = html.match(/<style>([\s\S]*?)<\/style>/)[1];
  assert.equal(createHash("sha256").update(css).digest("hex"), "d9bce3c34e8d5d6bd95175efba2b96e98c83f64215fe1ec3c15b13d17346b776");
});

test("客户报告保留完整改版稿并省略工作台图片和区域跳转", () => {
  const html = render({ draft: { fields: {
    name: { value: "验证样本" }, ingredients: { value: "小麦粉、食用盐" },
    net: { value: "200 克" }, storage: { value: "置于阴凉干燥处" },
  } }, images: [{ id: "a", dataUrl: imageData, blocks: [{ id: "a-1", text: "配料表：小麦粉、食用盐" }] }],
  result: { items: [{ id: "GB7718AL", group: "issue", track: "standard2025", outcome: "issue",
    replacement: "致敏物质提示：含有小麦", refs: ["a-1"] }] } });
  const copy = html.split('<h2><span class="num">07</span>')[1].split("<h2>")[0];
  assert.match(copy, /配料表：小麦粉、食用盐/);
  assert.match(copy, /净含量：200 克/);
  assert.match(copy, /贮存条件：置于阴凉干燥处/);
  assert.match(copy, /致敏物质提示：<b>含有小麦<\/b>/);
  assert.doesNotMatch(html, /href="#(?:photo|mark-evidence)|<img |区域 1/);
});

test("阻断结果隐藏已有适用审核结论", () => {
  const html = render({ result: { items: [
    { group: "issue", track: "actual", title: "过期的合规结论" },
    { group: "blocked", track: "actual", title: "疑似混图" },
  ] } });
  assert.match(html, /审核暂停/);
  assert.match(html, /疑似混图/);
  assert.doesNotMatch(html, /过期的合规结论/);
});

test("稿件停版独立于法定红线，客户版只显示标准条款和核实任务", () => {
  const html = render({ draft: { fields: { name: { value: "测试米线" }, net: { value: "328 克" } } },
    result: { applicability: { sampleKind: "artwork" }, coverage: [{ id: "front", status: "missing" }],
      labelFacts: { type: "方便米线", flavor: "酱香味", jobTicketMass: "315 克",
        packOrigins: [{ pack: "米线包", value: "山东 临沂" }] },
      items: [
        { id: "C02", group: "issue", track: "actual", severity: "draft-hold", title: "骨汤缺少对应标示" },
        { id: "C02-source", group: "review", track: "actual", title: "骨汤来源与具体整改路径", action: "请生产方提供配方" },
        { id: "N03", group: "pass", track: "standard2025", outcome: "pass", title: "能量基础复算一致",
          customerConclusion: "1227.5 kJ 修约后与 1228 kJ 一致。",
          source: { name: "GB 28050-2025", filename: "内部资料.pdf", clause: "2.3", location: "正文 XML 第 3 段" } },
      ] } });
  assert.match(html, /审核结论 · 不予通过/);
  assert.doesNotMatch(html, /不予放行|当前稿件须改后复审|稿件须改后复审/);
  assert.match(html, /<span class="chip r">稿件停版<\/span>/);
  // A1：正文改为同一分类数据的完整表述。
  assert.match(html, /共列示 <strong[^>]*>1 项须整改后复审<\/strong>，其中确认红线 <strong[^>]*>0 项<\/strong>、稿件停版 <strong[^>]*>1 项<\/strong>/);
  assert.match(html, /方便米线 · 酱香味/);
  assert.match(html, /制作表头显示 315 克，包装版面标示 328 克/);
  assert.match(html, /1227.5 kJ 修约后与 1228 kJ 一致。/);
  assert.match(html, /GB 28050-2025 · 2.3/);
  assert.doesNotMatch(html, /正文 XML|内部资料\.pdf|图像覆盖/);
  assert.equal((html.match(/骨汤来源与具体整改路径/g) || []).length, 1);
});

test("四档计数与正文一致，红线和 2025 版疑点只列在待核实表", () => {
  const result = {
    version: "2025",
    versionTransition: { published: "2025-03-16", effective: "2027-03-16" },
    artworkDate: { value: "2026-09-17" },
    items: [
      { id: "C02", group: "review", track: "actual", title: "骨汤需核实", detail: "350 毫升开水" },
      { id: "GB7718AL", group: "review", outcome: "review", track: "standard2025", title: "致敏提示需核实", detail: "花生碎" },
      { id: "I01", group: "internal", track: "internal", title: "分包次序" },
    ],
  };
  const counts = (html) => [...html.matchAll(/<div class="n">(\d+)<\/div>/g)].map((match) => Number(match[1]));
  const bodyOf = (html, number) => html.split(`<h2><span class="num">${number}</span>`)[1].split("<h2>")[0];
  const tableCounts = (html) => ["02", "03", "04", "06"].map((n) =>
    [...bodyOf(html, n).matchAll(/<tr><td class="num">\d+<\/td>/g)].length);
  const html = render({ result, generatedAt: "2026-11-08T04:00:00Z" });
  assert.deepEqual(counts(html), [0, 1, 0, 2]);
  assert.deepEqual(tableCounts(html), counts(html));
  const emptyStandard = bodyOf(html, "04");
  assert.match(emptyStandard, /<th style="width:34px" class="num">#<\/th><th style="width:150px">问题<\/th><th>现状与依据<\/th><th style="width:250px">整改要求<\/th>/);
  assert.match(emptyStandard, /<tbody><tr><td colspan="4">本次未确认按 2025 版审核的不符合项。致敏物质的判定见第 06 节；其余待核实事项亦见第 06 节。<\/td><\/tr><\/tbody>/);
  const pending = bodyOf(html, "06");
  assert.equal([...pending.matchAll(/骨汤需核实/g)].length, 1);
  assert.equal([...pending.matchAll(/致敏提示需核实/g)].length, 1);
  assert.match(pending, /350 毫升开水/);
  assert.match(pending, /花生碎/);
  assert.match(html, /class="verdict" style="background:var\(--graybg\)/);
  assert.match(html, /设计稿（刀版图），非实物/);
  assert.match(html, /GB 7718-2025、GB 28050-2025/);
  assert.doesNotMatch(html, /GB 7718-2011|GB 28050-2011|预检|现行版对照/);

  const onlyStandard = render({ result: { ...result, items: [
    { group: "issue", outcome: "issue", track: "standard2025", title: "已确认 2025 版不符合" },
  ] } });
  assert.match(onlyStandard, /审核结论 · 整改后复审/);
  assert.deepEqual(tableCounts(onlyStandard), [0, 0, 1, 0]);

  result.items.push(
    { group: "issue", severity: "redline", track: "actual", title: "已确认红线" },
    { group: "issue", outcome: "issue", track: "standard2025", title: "已确认 2025 版不符合" },
  );
  const confirmed = render({ result });
  assert.deepEqual(counts(confirmed), [1, 1, 1, 2]);
  assert.deepEqual(tableCounts(confirmed), counts(confirmed));
  assert.match(confirmed, /class="verdict">\s*<span class="tag">审核结论 · 不予通过/);
});

test("能量行补 N03 反算式，读数异常时不出现", () => {
  const rows = [
    { name: "能量", value: "1228 kJ", nrv: "15", calculation: "1228÷8400×100%≈14.62%", expected: "15%", match: true },
    { name: "蛋白质", value: "4.8 g", nrv: "8", calculation: "4.8÷60×100%≈8.00%", expected: "8%", match: true },
    { name: "脂肪", value: "12.5 g", nrv: "21", calculation: "12.5÷60×100%≈20.83%。放大 2 倍重读确认：原读数 125。",
      expected: "21%", match: true, reread: { from: "125", scale: 2 } },
    { name: "碳水化合物", value: "40.2 g", nrv: "13", calculation: "40.2÷300×100%≈13.40%", expected: "13%", match: true },
  ];
  const energyCheck = { protein: "4.8", fat: "12.5", carb: "40.2", energy: "1228", total: "1227.5", difference: "0.5", within: true };
  const html = render({ result: { nutritionRows: rows, items: [
    { id: "N03", group: "pass", track: "standard2025", outcome: "pass", title: "能量基础复算一致", energyCheck },
  ] } });
  assert.match(html,
    /能量反算：蛋白质 4\.8×17＋脂肪 12\.5×37＋碳水 40\.2×17＝1227\.5 kJ（标示 1228 kJ，差 0\.5，在修约范围内）；NRV：1228÷8400×100%≈14\.62%/);

  const outside = render({ result: { nutritionRows: rows, items: [
    { id: "N03", group: "review", track: "standard2025", outcome: "review", title: "能量与基础复算需核对",
      energyCheck: { ...energyCheck, difference: "12.3", within: false } },
  ] } });
  assert.match(outside, /＝1227\.5 kJ（标示 1228 kJ，差 12\.3）；NRV：/);
  assert.doesNotMatch(outside, /在修约范围内。/);

  const abnormal = render({ result: { nutritionRows: [
    { name: "能量", value: "1228 kJ", nrv: "15", calculation: "识别异常：读数待核对。", expected: "—", match: null },
  ], items: [] } });
  assert.doesNotMatch(abnormal, /能量反算/);
  assert.match(abnormal, /识别异常：读数待核对/);
});

test("图片待确认时隐藏拼接字段和发布入口", () => {
  const html = render({
    draft: { fields: { name: { value: "另一商品名称" } } },
    result: { attribution: { status: "pending" }, canPublish: false, items: [
      { id: "attribution", title: "归属待确认" },
      { group: "issue", title: "拼接产生的结论" },
    ] },
  });
  assert.match(html, /归属待确认/);
  assert.doesNotMatch(html, /另一商品名称|拼接产生的结论|id="(?:print|download)"/);
});

test("动态文本已转义，报告页面无需执行脚本", () => {
  const html = render({
    draft: { fields: { name: { value: '</title><img src=x onerror="alert(1)">' } } },
    result: { items: [{ group: "review", title: "<script>alert(1)</script>",
      source: { name: "外链源", url: "javascript:alert(1)" } }] },
    images: [{ id: "bad", name: "未知图", dataUrl: "data:image/svg+xml;base64,PHN2Zy8+" }],
  });
  assert.match(html, /&lt;\/title&gt;&lt;img/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /javascript:|data:image\/svg\+xml/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /default-src 'none'/);
});



test("A2 部分内容待确认时 07 标题降级", () => {
  const html = render({
    draft: { fields: {}, nutrition: {} }, images: [], context: {},
    result: { items: [], pages: [], canPublish: true, revision: {
      snapshotId: "s", sections: [], applied: [], failures: [], canExport: true, pendingText: true } },
  });
  assert.match(html, /07<\/span>建议修改后文本（部分内容待确认，不可直接交生产方）/);
});

test("Q4 同一依据不重复列出", () => {
  const html = render({
    includeEvidence: true,
    draft: { fields: { name: { value: "测试" } }, nutrition: {} }, images: [], context: {},
    result: { items: [], pages: [], canPublish: true, reviewNotes: [
      { title: "审核依据与对象", text: "说明文字。", clause: "GB 7718-2025 4.1、GB 7718-2025 4.1、GB 28050-2025 3.1" }] },
  });
  const clause = html.match(/GB 7718-2025 4\.1、GB 28050-2025 3\.1/g) || [];
  assert.equal(clause.length, 1);
  assert.equal((html.match(/GB 7718-2025 4\.1/g) || []).length, 1);
});

test("07 有待核实项时不得声称可直接交生产方改版", () => {
  const run = (pending) => render({
    draft: { fields: { name: { value: "测试米线" }, basis: { value: "每 100 克" } } },
    result: { items: pending.map((n) => ({
      id: "P" + n, group: "review", outcome: "review", track: "actual", title: "待核实事项" + n,
    })) },
  });
  const html = run([1, 2, 3]);
  assert.ok(html.includes("部分内容待确认，不可直接交生产方"), "有待核实项时标题应写部分内容待确认");
  assert.ok(!html.includes("（可直接交生产方改版）"), "有待核实项时不得写可直接交");
});

