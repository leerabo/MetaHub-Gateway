/**
 * PRD-054 双视口浏览器验收合同测试。
 * 本文件冻结实际浏览器脚本依赖的布局、交互锚点和隐私边界；PNG 与像素验收仍由运行记录证明。
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
const mainSource = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
const rendererSource = await readFile(
  new URL("../../src/features/request-explanations/renderRequestExplanation.js", import.meta.url),
  "utf8",
);

/** 截取 PAGE-079 样式带，避免断言被其他历史页面的同名 CSS 值误满足。 */
function explanationCss() {
  const start = css.indexOf(".request-explanations-panel");
  assert.ok(start >= 0);
  return css.slice(start);
}

test("desktop contract uses bounded two-column evidence layout without clipping", () => {
  const source = explanationCss();
  assert.match(
    source,
    /\.request-explanation-body\s*\{[\s\S]*grid-template-columns:\s*minmax\(240px, 300px\) minmax\(0, 1fr\)/,
  );
  assert.match(source, /\.request-explanation-summary\s*\{[\s\S]*min-width:\s*0/);
  assert.match(source, /\.request-explanation-evidence\s*\{[\s\S]*min-width:\s*0/);
  assert.match(source, /overflow-wrap:\s*anywhere/);
  // PAGE-079 不得通过裁切掩盖长 ID、成本字符串或异常证据。
  const panelRule = source.match(/\.request-explanations-panel\s*\{([^}]*)\}/)?.[1] ?? "";
  assert.doesNotMatch(panelRule, /overflow:\s*hidden/);
});

test("390px contract collapses form body and evidence grids to stable single columns", () => {
  const source = explanationCss();
  const mobile = source.slice(source.indexOf("@media (max-width: 719px)"));
  assert.match(mobile, /\.request-explanation-form,[\s\S]*\.request-explanation-body[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(mobile, /\.request-explanation-actions[\s\S]*grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(mobile, /\.request-explanation-record dl[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(source, /\.request-explanation-actions button\s*\{[\s\S]*height:\s*40px/);
});

test("renderer exposes only fixed interaction anchors for browser focus and disclosure checks", () => {
  assert.match(rendererSource, /data-request-explanation-focus="request-id"/);
  assert.match(rendererSource, /data-request-explanation-focus="query"/);
  assert.match(rendererSource, /data-request-explanation-focus="refresh"/);
  assert.match(rendererSource, /data-request-explanation-focus="clear"/);
  assert.match(rendererSource, /data-request-explanation-section=/);
  assert.match(rendererSource, /section:\\?\$\{escapeAttribute\(definition\.key\)\}/);
  assert.doesNotMatch(rendererSource, /JSON\.stringify\(.*(?:fact|values|explanation)/);
});

test("main keeps source request ids out of URL and browser storage during inline explain", () => {
  const handoffStart = mainSource.indexOf("async function runRequestExplanationRead(");
  const handoffEnd = mainSource.indexOf("\n}", handoffStart);
  assert.ok(handoffStart >= 0 && handoffEnd > handoffStart);
  const handoffBlock = mainSource.slice(handoffStart, handoffEnd + 80);
  assert.doesNotMatch(handoffBlock, /replaceNavigationHash\([^)]*requestId/);
  assert.doesNotMatch(handoffBlock, /localStorage|sessionStorage/);
  assert.doesNotMatch(handoffBlock, /switchConsoleNavigation|location\./);

  const loaderStart = mainSource.indexOf("async function loadRequestExplanationState");
  const loader = mainSource.slice(loaderStart, loaderStart + 5000);
  assert.match(loader, /getRequestExplanation\(\{[\s\S]*requestId: ticket\.requestId[\s\S]*signal: ticket\.signal/);
  assert.doesNotMatch(loader, /tenantId: ticket|includeRaw|include_raw|prompt|response_body/);
});
