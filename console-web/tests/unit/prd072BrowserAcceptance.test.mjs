/**
 * PRD-072 文件工作台浏览器验收合同。
 * 本文件不启动服务或写 artifact，只锁定真实验收必须依赖的双视口、DOM 隐私、权限投影和
 * 生命周期门禁；最终 1280x720/390x844 运行截图由项目收口验收执行。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

/** 双视口必须使用同一稳定工作台，并在移动端把每个表格单元投影为标签化行。 */
test("PRD-072 browser layout contract covers desktop and mobile without horizontal minimums", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  const desktopRule = css.match(/\.file-row\s*\{([\s\S]*?)\n\}/)?.[1] ?? "";
  assert.match(desktopRule, /grid-template-columns:/);
  assert.match(desktopRule, /minmax\(0, 1\.45fr\)/);
  assert.doesNotMatch(desktopRule, /min-width:\s*[1-9][0-9]+px/);
  const mobile = css.slice(css.indexOf("@media (max-width: 720px)"));
  assert.match(mobile, /\.file-upload-form[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(mobile, /\.file-row:not\(\.file-row-head\) > \[data-label\]::before/);
  assert.match(mobile, /content:\s*attr\(data-label\)/);
  assert.match(css, /overflow-wrap:\s*anywhere/);
  assert.equal((css.match(/{/g) ?? []).length, (css.match(/}/g) ?? []).length);
});

/** 文件名只允许走转义文本位置；正文、规则与对象定位字段不得出现在 renderer 合同。 */
test("PRD-072 browser DOM contract excludes extracted text and storage internals", async () => {
  const [renderer, binder] = await Promise.all([
    readFile(new URL("../../src/features/files/renderFiles.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/files/bindFiles.js", import.meta.url), "utf8"),
  ]);
  assert.match(renderer, /escapeHtml\(file\.original_name\)/);
  assert.doesNotMatch(renderer, /file\.(?:extracted_text|original_object_key|extracted_object_key|effective_dlp_rule_id|content_fingerprint)/);
  assert.doesNotMatch(renderer, /data-file-name/);
  assert.match(renderer, /tone-not-configured/);
  assert.match(renderer, /未配置病毒扫描/);
  assert.match(binder, /selection\.textContent = file\?\.name/);
  assert.doesNotMatch(binder, /innerHTML|localStorage|sessionStorage|indexedDB|console\.(?:log|warn|error)/);
});

/** 登录、撤权、导航、离页和上传中的刷新门禁必须全部由主应用统一执行。 */
test("PRD-072 browser lifecycle contract destroys owner state on every strong boundary", async () => {
  const main = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  assert.match(main, /clearFileStateOnIdentityChange\(authSession\.context, context\)/);
  assert.match(main, /window\.addEventListener\("pagehide"[\s\S]*filePageState\.clear\(\)/);
  assert.match(main, /function renderLogin[\s\S]*filePageState\.clear\(\)/);
  assert.match(main, /logoutButton[\s\S]*filePageState\.clear\(\)/);
  assert.match(main, /itemId !== "files"[\s\S]*filePageState\.clear\(\)/);
  assert.match(main, /nextItemId !== "files"[\s\S]*filePageState\.clear\(\)/);
  assert.doesNotMatch(main, /window\.setInterval\(\(\) => \{[\s\S]*?void refresh\(\);/);
  assert.match(main, /case "files":[\s\S]*loadFilesState\(consoleShellState, refreshId\)/);
  assert.match(main, /error\.status === 403[\s\S]*error\.code === "auth\.permission_denied"/);
  assert.match(main, /loadFilesState\(consoleShellState, refreshId\)/);
  assert.match(main, /bindFilesPageInteractions\(consoleShellState\)/);
});
