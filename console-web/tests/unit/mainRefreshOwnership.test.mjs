/**
 * 测试职责：锁定控制台注销与迟到刷新之间的认证 owner 顺序，防止旧请求恢复已清理的登录态。
 * 安全边界：测试只读取源码结构，不启动网络、浏览器或真实 Session，也不接触租户业务数据。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";


/** 从主应用源码中提取两个顶层函数之间的固定片段，缺失边界时立即失败。 */
function sourceSection(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(start >= 0, `missing start marker: ${startMarker}`);
  assert.ok(end > start, `missing end marker: ${endMarker}`);
  return source.slice(start, end);
}


test("logout invalidates refresh ownership before cleanup and network await", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const handler = sourceSection(
    source,
    'logoutButton?.addEventListener("click", async () => {',
    "for (const navigationButton",
  );
  const invalidateIndex = handler.indexOf("const logoutGeneration = ++refreshSequence;");
  const lifecycleIndex = handler.indexOf("gatewayDebugLifecycle.leave();");
  const clearAuthIndex = handler.indexOf("clearAuthSession();");
  const awaitIndex = handler.indexOf("await logoutAdmin");
  const errorGateIndex = handler.indexOf("logoutGeneration !== refreshSequence || authSession.token", awaitIndex);
  const errorRenderIndex = handler.indexOf("renderLogin(authErrorMessage(error)", errorGateIndex);
  const finalGateIndex = handler.indexOf(
    "logoutGeneration !== refreshSequence || authSession.token",
    errorGateIndex + 1,
  );
  const finalRenderIndex = handler.lastIndexOf("renderLogin();");

  assert.ok(invalidateIndex > 0);
  assert.ok(invalidateIndex < lifecycleIndex);
  assert.ok(invalidateIndex < clearAuthIndex);
  assert.ok(invalidateIndex < awaitIndex);
  assert.ok(awaitIndex < errorGateIndex && errorGateIndex < errorRenderIndex);
  assert.ok(errorGateIndex < finalGateIndex && finalGateIndex < finalRenderIndex);
});


test("late current-user response is rejected before identity or storage writeback", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const refresh = sourceSection(source, "async function performRefresh()", "function renderLogin(");
  const frozenTokenIndex = refresh.indexOf("const refreshToken = authSession.token;");
  const ownerClosureIndex = refresh.indexOf("const refreshOwnerCurrent = () =>", frozenTokenIndex);
  const currentUserAwaitIndex = refresh.indexOf("await getCurrentUser({ controlApiUrl, token: refreshToken })");
  const ownerGateIndex = refresh.indexOf("if (!refreshOwnerCurrent())", currentUserAwaitIndex);
  const identityWriteIndex = refresh.indexOf("clearBudgetStateOnIdentityChange", currentUserAwaitIndex);
  const storageWriteIndex = refresh.indexOf("saveAuthSession(refreshToken, context)", currentUserAwaitIndex);
  const catchIndex = refresh.indexOf("} catch (error)", storageWriteIndex);
  const catchOwnerGateIndex = refresh.indexOf("if (!refreshOwnerCurrent())", catchIndex);
  const catchClearIndex = refresh.indexOf("clearAuthSession();", catchIndex);

  assert.ok(frozenTokenIndex > 0 && frozenTokenIndex < ownerClosureIndex);
  assert.ok(ownerClosureIndex < currentUserAwaitIndex);
  assert.ok(currentUserAwaitIndex < ownerGateIndex && ownerGateIndex < identityWriteIndex);
  assert.ok(ownerGateIndex < storageWriteIndex);
  assert.ok(catchIndex < catchOwnerGateIndex && catchOwnerGateIndex < catchClearIndex);
});


test("background refresh leaves the unauthenticated login form intact", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const interval = sourceSection(source, "window.setInterval(() => {", "function renderLogin(");
  const invitationGateIndex = interval.indexOf("publicInvitationRoute?.isPublicMode()");
  const authGateIndex = interval.indexOf("if (!authSession.token)");
  const refreshIndex = interval.indexOf("void refresh();");

  // 登录页含密码草稿，必须在任何后台 refresh 之前返回，不能依赖 performRefresh 再重绘登录页。
  assert.ok(invitationGateIndex >= 0);
  assert.ok(invitationGateIndex < authGateIndex && authGateIndex < refreshIndex);
});


test("superseded login success is rejected after each await and before session commit", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const login = sourceSection(source, "function renderLogin(", "/** 生成当前邀请弹窗 owner 输入");
  const submitIndex = login.indexOf('form?.addEventListener("submit", async (event) => {');
  const ownerIndex = login.indexOf("const loginOwner = Object.freeze({ generation: ++loginGeneration });", submitIndex);
  const loginAwaitIndex = login.indexOf("const login = await loginAdmin({", ownerIndex);
  const loginGateIndex = login.indexOf("if (!loginOwnerCurrent())", loginAwaitIndex);
  const currentUserAwaitIndex = login.indexOf("const context = await getCurrentUser", loginGateIndex);
  const currentUserGateIndex = login.indexOf("if (!loginOwnerCurrent())", currentUserAwaitIndex);
  const commitGateIndex = login.indexOf("if (!loginOwnerCurrent())", currentUserGateIndex + 1);
  const storageWriteIndex = login.indexOf("saveAuthSession(loginToken, context);", commitGateIndex);
  const memoryWriteIndex = login.indexOf("authSession = { token: loginToken, context };", storageWriteIndex);
  const ownerInvalidateIndex = login.indexOf("loginGeneration += 1;", memoryWriteIndex);
  const refreshInvalidateIndex = login.indexOf("refreshSequence += 1;", ownerInvalidateIndex);

  assert.ok(submitIndex >= 0 && submitIndex < ownerIndex);
  assert.ok(ownerIndex < loginAwaitIndex && loginAwaitIndex < loginGateIndex);
  assert.ok(loginGateIndex < currentUserAwaitIndex && currentUserAwaitIndex < currentUserGateIndex);
  assert.ok(currentUserGateIndex < commitGateIndex && commitGateIndex < storageWriteIndex);
  assert.ok(storageWriteIndex < memoryWriteIndex && memoryWriteIndex < ownerInvalidateIndex);
  assert.ok(ownerInvalidateIndex < refreshInvalidateIndex);
});


test("superseded login failure cannot clear or redraw a newer session", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const login = sourceSection(source, "function renderLogin(", "/** 生成当前邀请弹窗 owner 输入");
  const ownerClosureIndex = login.indexOf("const loginOwnerCurrent = () => (");
  const generationGateIndex = login.indexOf("loginOwner.generation === loginGeneration", ownerClosureIndex);
  const sessionGateIndex = login.indexOf("&& !authSession.token", generationGateIndex);
  const catchIndex = login.indexOf("} catch (err) {");
  const catchOwnerGateIndex = login.indexOf("if (!loginOwnerCurrent())", catchIndex);
  const catchClearIndex = login.indexOf("clearAuthSession();", catchOwnerGateIndex);
  const catchMemoryClearIndex = login.indexOf("authSession = { token: null, context: null };", catchClearIndex);
  const catchRenderIndex = login.indexOf("renderLogin(authErrorMessage(err)", catchMemoryClearIndex);

  assert.ok(ownerClosureIndex >= 0 && ownerClosureIndex < generationGateIndex);
  assert.ok(generationGateIndex < sessionGateIndex && sessionGateIndex < catchIndex);
  assert.ok(catchIndex < catchOwnerGateIndex && catchOwnerGateIndex < catchClearIndex);
  assert.ok(catchClearIndex < catchMemoryClearIndex && catchMemoryClearIndex < catchRenderIndex);
});


test("login page replacement and each new submit invalidate earlier login owners", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const declarationIndex = source.indexOf("let loginGeneration = 0;");
  const login = sourceSection(source, "function renderLogin(", "/** 生成当前邀请弹窗 owner 输入");
  const renderInvalidateIndex = login.indexOf("loginGeneration += 1;");
  const lifecycleCleanupIndex = login.indexOf("gatewayDebugLifecycle.leave();");
  const pageRenderIndex = login.indexOf("renderLoginPage(app");
  const submitIndex = login.indexOf('form?.addEventListener("submit", async (event) => {');
  const submitInvalidateIndex = login.indexOf("generation: ++loginGeneration", submitIndex);

  assert.ok(declarationIndex >= 0);
  assert.ok(renderInvalidateIndex >= 0 && renderInvalidateIndex < lifecycleCleanupIndex);
  assert.ok(renderInvalidateIndex < pageRenderIndex && pageRenderIndex < submitIndex);
  assert.ok(submitIndex < submitInvalidateIndex);
});
