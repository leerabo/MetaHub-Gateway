/**
 * PRD-051 Browser 验收器的聚焦纯合同测试。
 *
 * 本文件不启动服务、Chrome 或 Playwright，只锁定登出、证据隐私、视口锚点与截图遮罩
 * 的失败关闭语义，避免真实验收器在页面未被看到或敏感字段未被保护时产生假通过证据。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  BrowserAcceptanceFailure,
  assertProtectedDetailMaskCoverage,
  assertSafeEvidence,
  assertViewportIntersection,
  revokeSessionAfterFailure,
  verifyLogoutEvidence,
} from "../prd051-browser-acceptance.mjs";


// 固定公共链路标识只用于验证登出响应和证据投影，不包含任何真实 Session。
const LOGOUT_REQUEST_ID = "req_prd051_browser_logout_unit";
const LOGOUT_TRACE_ID = "trc_prd051_browser_logout_unit";
// 源码 URL 只用于确认纯验证器确实接入真实 Playwright 路径，读取时不会执行脚本入口。
const BROWSER_SCRIPT_URL = new URL("../prd051-browser-acceptance.mjs", import.meta.url);


test("登出证据必须同时验证服务端结果和两类浏览器认证存储", () => {
  const evidence = verifyLogoutEvidence({
    status: 200,
    headers: {
      "x-request-id": LOGOUT_REQUEST_ID,
      "x-trace-id": LOGOUT_TRACE_ID,
    },
    payload: {
      logged_out: true,
      request_id: LOGOUT_REQUEST_ID,
      trace_id: LOGOUT_TRACE_ID,
    },
    storageState: {
      localStorage: [true, true],
      sessionStorage: [true, true],
    },
  });
  assert.deepEqual(evidence, {
    http_status: 200,
    request_id: LOGOUT_REQUEST_ID,
    trace_id: LOGOUT_TRACE_ID,
    logged_out: true,
    browser_auth_cleared: true,
  });

  for (const override of [
    { payload: { logged_out: false, request_id: LOGOUT_REQUEST_ID, trace_id: LOGOUT_TRACE_ID } },
    { storageState: { localStorage: [true, true], sessionStorage: [true, false] } },
    { headers: { "x-request-id": LOGOUT_REQUEST_ID, "x-trace-id": "trc_other" } },
  ]) {
    assert.throws(
      () => verifyLogoutEvidence({
        status: 200,
        headers: {
          "x-request-id": LOGOUT_REQUEST_ID,
          "x-trace-id": LOGOUT_TRACE_ID,
        },
        payload: {
          logged_out: true,
          request_id: LOGOUT_REQUEST_ID,
          trace_id: LOGOUT_TRACE_ID,
        },
        storageState: {
          localStorage: [true, true],
          sessionStorage: [true, true],
        },
        ...override,
      }),
      BrowserAcceptanceFailure,
    );
  }
});


test("PAGE-075 锚点必须与当前截图视口真实相交", () => {
  assert.deepEqual(
    assertViewportIntersection(
      { x: 20, y: 700, width: 300, height: 80 },
      { width: 1280, height: 720 },
      "unit.viewport",
    ),
    { anchor_intersects_viewport: true },
  );
  for (const box of [
    null,
    { x: 10, y: 845, width: 100, height: 20 },
    { x: 391, y: 10, width: 20, height: 20 },
    { x: 10, y: 10, width: 0, height: 20 },
  ]) {
    assert.throws(
      () => assertViewportIntersection(box, { width: 390, height: 844 }, "unit.viewport"),
      BrowserAcceptanceFailure,
    );
  }
});


test("敏感证据字段失败关闭且固定计数字段不会被误伤", () => {
  assert.doesNotThrow(() => assertSafeEvidence({
    allowed_request_count: 3,
    allowed_response_count: 2,
    browser_error_count: 0,
    http_status: 200,
  }, []));

  for (const key of [
    "provider_key",
    "providerApiKey",
    "virtual_key",
    "prompt_text",
    "response",
    "response_body",
    "body",
    "endpoint",
    "endpoint_url",
  ]) {
    assert.throws(
      () => assertSafeEvidence({ [key]: "must-not-pass" }, []),
      BrowserAcceptanceFailure,
    );
  }
});


test("PAGE-075 原因与操作者遮罩必须各有且仅有一个可见目标", () => {
  assert.deepEqual(
    assertProtectedDetailMaskCoverage(
      { reasonCount: 1, reasonVisibleCount: 1, actorCount: 1, actorVisibleCount: 1 },
      "unit.mask",
    ),
    { protected_detail_masked: true },
  );
  for (const state of [
    { reasonCount: 0, reasonVisibleCount: 0, actorCount: 1, actorVisibleCount: 1 },
    { reasonCount: 2, reasonVisibleCount: 2, actorCount: 1, actorVisibleCount: 1 },
    { reasonCount: 1, reasonVisibleCount: 0, actorCount: 1, actorVisibleCount: 1 },
    { reasonCount: 1, reasonVisibleCount: 1, actorCount: 1, actorVisibleCount: 0 },
  ]) {
    assert.throws(
      () => assertProtectedDetailMaskCoverage(state, "unit.mask"),
      BrowserAcceptanceFailure,
    );
  }
});


test("真实 Playwright 路径必须接入滚动、双存储、遮罩和键盘焦点门禁", async () => {
  const source = await readFile(BROWSER_SCRIPT_URL, "utf8");
  assert.match(source, /await anchor\.scrollIntoViewIfNeeded\(\)/);
  assert.match(source, /sessionStorage: keys\.map/);
  assert.match(source, /await page\.keyboard\.press\("Escape"\)/);
  assert.match(source, /document\.activeElement\?\.id === "finops-open-adjustment"/);
  assert.match(
    source,
    /protected_detail_masked: page075Capture\.protectedDetailMasked/,
  );
});


test("真实 Browser 异常路径必须在关闭 Context 前撤销 Session", async () => {
  const source = await readFile(BROWSER_SCRIPT_URL, "utf8");
  assert.match(source, /async function revokeSessionAfterFailure\(page, config\)/);
  assert.match(
    source,
    /loginThroughConsole\(page, config, \(\) => \{[\s\S]*?sessionNeedsRevocation = true;/,
  );
  assert.match(source, /if \(sessionNeedsRevocation && page\) \{[\s\S]*?revokeSessionAfterFailure\(page, config\)/);
  assert.match(source, /failure\.cleanupCode = "session_revoke_failed"/);
  assert.match(source, /cleanup_code=\$\{safe\.cleanupCode\}/);
  assert.doesNotMatch(source, /return \{[^}]*token/s);
});


test("异常 Session 清理仅在页面上下文使用 Token 并返回布尔结果", async () => {
  const keys = ["metahub.auth_token", "metahub.auth_context"];
  const sessionValues = new Map([
    [keys[0], "unit-browser-session"],
    [keys[1], "{}"],
  ]);
  const localValues = new Map([
    [keys[0], null],
    [keys[1], null],
  ]);
  // 只实现验收器使用的 Storage 最小合同，避免测试依赖真实浏览器进程。
  const sessionStorageStub = {
    getItem(key) {
      return sessionValues.get(key) ?? null;
    },
    removeItem(key) {
      sessionValues.delete(key);
    },
  };
  const localStorageStub = {
    getItem(key) {
      return localValues.get(key) ?? null;
    },
    removeItem(key) {
      localValues.delete(key);
    },
  };
  let requestUrl = null;
  let requestAuthorization = null;
  // 假注销端点只记录固定测试值，不向测试输出或证据打印认证头。
  const fetchStub = async (url, options) => {
    requestUrl = url;
    requestAuthorization = options?.headers?.Authorization ?? null;
    return {
      status: 200,
      async json() {
        return { logged_out: true };
      },
    };
  };
  // Playwright evaluate 的替身临时安装页面全局对象，并在执行后完整恢复 Node 环境。
  const page = {
    async evaluate(callback, argumentsValue) {
      const replacements = new Map([
        ["sessionStorage", sessionStorageStub],
        ["localStorage", localStorageStub],
        ["fetch", fetchStub],
      ]);
      const previous = new Map();
      for (const [name, value] of replacements) {
        previous.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
        Object.defineProperty(globalThis, name, { configurable: true, value });
      }
      try {
        return await callback(argumentsValue);
      } finally {
        for (const [name, descriptor] of previous) {
          if (descriptor) Object.defineProperty(globalThis, name, descriptor);
          else delete globalThis[name];
        }
      }
    },
  };

  const result = await revokeSessionAfterFailure(page, { controlOrigin: "http://127.0.0.1:8001" });
  assert.equal(result, true);
  assert.equal(requestUrl, "http://127.0.0.1:8001/auth/logout");
  assert.equal(requestAuthorization, "Bearer unit-browser-session");
  for (const key of keys) {
    assert.equal(sessionStorageStub.getItem(key), null);
    assert.equal(localStorageStub.getItem(key), null);
  }
});
