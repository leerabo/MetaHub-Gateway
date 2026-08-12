/** PRD-077 Browser 驱动纯合同测试，不启动服务或浏览器。 */

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { EventEmitter, once } from "node:events";
import { existsSync } from "node:fs";
import { mkdir, readFile, readdir, rename, rm, symlink, writeFile } from "node:fs/promises";
import { PassThrough, Writable } from "node:stream";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  buildBrowserEnvironment,
  createPageTargetParameters,
} from "../prd066-browser-acceptance.mjs";
import {
  BrowserAcceptanceFailure,
  CHROME,
  OWNER_CLAIM_FILENAME,
  SOURCE_PROVENANCE_NAMES,
  VIEWPORTS,
  assistantResponseSerializations,
  answerOracleForQuery,
  assertBrowserExecutable,
  assertBrowserExecutableImage,
  assertCleanupFailurePriority,
  assertDirectoryIdentity,
  assertEvidenceKeys,
  assertLayoutMetrics,
  assertOwnershipClaim,
  assertSafeEvidence,
  assertRuntimeSummary,
  assertSourceProvenance,
  assertSourceProvenanceUnchanged,
  assertTracker,
  browserRequestAllowed,
  browserStorageFactsValid,
  bindBrowserGuards,
  buildLiveWorkPrivacyScope,
  buildManifest,
  canonicalSourceIndex,
  classifyPageRequest,
  closeBrowserGracefully,
  cleanupFailedRunDirectories,
  captureDirectoryIdentity,
  createOwnershipClaim,
  createPrivateConfiguration,
  createPrivatePageSession,
  createTracker,
  deleteVerifiedDirectoryTreeAfterRelease,
  disposePrivatePageSession,
  harnessEnvironment,
  inspectConsoleObjectGraph,
  inspectProcessChunk,
  ownedRunDirectory,
  ownedStagingDirectory,
  parseHarnessFailureLine,
  parseHealthAttemptRequestId,
  privateAssistantHeadersValid,
  publishStagingAtomically,
  quiesceHarness,
  recordNetworkLoadingFailure,
  recordNetworkResponse,
  removeOwnedDirectory,
  registerAssistantResponseBody,
  registerAnswerLeafValues,
  registerDelayedAssistantRequest,
  revokeFinancePermission,
  runWindowsHandleHelper,
  safeRunId,
  scanArtifacts,
  scanPrivateTree,
  stopHarness,
  terminateBrowserTree,
  terminateHarnessTree,
  terminateWindowsProcessTree,
  verifyPublishedManifest,
  waitForBrowserExit,
  waitForConsoleInspections,
  waitForHarnessClose,
  waitForHarnessExit,
  waitForRequestOwnersDrain,
} from "../prd077-browser-acceptance.mjs";

// 合同测试的临时目录全部锚定仓库根目录，禁止测试数据写入工作区之外。
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");


/** 构造最小有效 Windows PE 头，测试只关注 Machine 字段而不依赖本机浏览器文件。 */
function windowsPeFixture(machine) {
  const image = Buffer.alloc(0x100);
  image.writeUInt16LE(0x5a4d, 0);
  image.writeUInt32LE(0x80, 0x3c);
  image.writeUInt32LE(0x00004550, 0x80);
  image.writeUInt16LE(machine, 0x84);
  return image;
}


// Node 会按 Windows 版本把 sharing violation 映射成三种 errno，合同统一接受该受控集合。
const WINDOWS_SHARING_ERROR_CODES = new Set(["EACCES", "EBUSY", "EPERM"]);


/** 断言句柄锁确实阻止目录替换或删除，不接受其他异常冒充锁保护。 */
async function assertWindowsSharingViolation(operation) {
  await assert.rejects(
    operation,
    (error) => error instanceof Error && WINDOWS_SHARING_ERROR_CODES.has(error.code),
  );
}


test("PRD-077 pins the repository ARM64 Chrome and rejects mismatched PE images", async () => {
  assert.equal(
    CHROME,
    path.join(ROOT, ".tools", "chrome-portable", "chrome.exe"),
  );
  assert.equal(assertBrowserExecutableImage(windowsPeFixture(0xaa64), "arm64"), true);
  assert.equal(assertBrowserExecutableImage(windowsPeFixture(0x8664), "x64"), true);
  assert.equal(assertBrowserExecutableImage(windowsPeFixture(0x014c), "ia32"), true);
  for (const [image, architecture] of [
    [windowsPeFixture(0x8664), "arm64"],
    [windowsPeFixture(0xaa64), "x64"],
    [Buffer.alloc(32), "arm64"],
    [windowsPeFixture(0xaa64), "unknown"],
  ]) {
    assert.throws(
      () => assertBrowserExecutableImage(image, architecture),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "config.chrome"
        && error.code === "architecture",
    );
  }
  assert.equal(await assertBrowserExecutable(CHROME, process.arch), true);
});


test("PRD-077 Chrome compatibility omits only USERPROFILE from writable roots", () => {
  const artifact = path.join(ROOT, ".tmp", "prd077-browser-environment-contract");
  const rejected = `PRD077_BROWSER_HOST_${randomUUID()}`;
  const environment = buildBrowserEnvironment({
    PATH: "C:\\safe-bin",
    USERPROFILE: rejected,
    OPENAI_API_KEY: rejected,
    GITHUB_TOKEN: rejected,
  }, artifact, { omitUserProfile: true });
  const browserHome = path.join(artifact, "browser-home");
  assert.equal(Object.hasOwn(environment, "USERPROFILE"), false);
  assert.equal(Object.values(environment).includes(rejected), false);
  assert.equal(environment.TEMP, artifact);
  assert.equal(environment.TMP, artifact);
  assert.equal(environment.HOME, browserHome);
  assert.equal(environment.LOCALAPPDATA, path.join(browserHome, "local"));
  assert.equal(environment.APPDATA, path.join(browserHome, "roaming"));
  assert.equal(environment.HOMEDRIVE, path.parse(browserHome).root.replace(/[\\/]$/, ""));
  assert.equal(
    environment.HOMEPATH,
    browserHome.slice(path.parse(browserHome).root.length - 1),
  );
  assert.equal(
    buildBrowserEnvironment({}, artifact).USERPROFILE,
    browserHome,
  );
});


/** 构造固定长度的源码摘要夹具，用于验证发布前 provenance 全量复核。 */
function sourceProvenanceFixture() {
  return Object.fromEntries(
    SOURCE_PROVENANCE_NAMES.map((name, index) => [
      name,
      index.toString(16).padStart(64, "0"),
    ]),
  );
}


// Windows helper 夹具模拟锁定、完成和永久阻塞三个阶段，验证每段 deadline 与排空。
const WINDOWS_TEST_HELPER = String.raw`
import sys
import time

mode = sys.argv[1]
if mode == "before_lock":
    time.sleep(60)
print("LOCKED", flush=True)
if sys.stdin.readline() != "GO\n":
    raise SystemExit(2)
if mode == "completion":
    time.sleep(60)
print("DONE", flush=True)
`;


/** 模拟句柄 helper 子进程的 stdout、stdin、kill 与 close 生命周期。 */
class HandleHelperChild extends EventEmitter {
  /** 配置 kill 或成功完成后是否发出 close，用于覆盖句柄排空失败边界。 */
  constructor({ closeOnKill = true, closeAfterSuccess = true } = {}) {
    super();
    this.exitCode = null;
    this.pid = 7077;
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
    this.stdin = new Writable({
      write: (chunk, _encoding, callback) => {
        if (String(chunk) === "GO\n") {
          this.stdout.write("DONE\n");
          if (closeAfterSuccess) queueMicrotask(() => this.close(0));
        }
        callback();
      },
    });
    this.closeOnKill = closeOnKill;
    queueMicrotask(() => this.stdout.write("LOCKED\n"));
  }

  close(code) {
    if (this.exitCode !== null) return;
    this.exitCode = code;
    this.stdout.end();
    this.stderr.end();
    queueMicrotask(() => this.emit("close", code));
  }

  kill() {
    if (this.closeOnKill) this.close(1);
    return true;
  }
}


/** 构造不含 Prompt/Response 明文的只读助手成功响应契约夹具。 */
function assistantResponsePayload(queryType = "usage_statistics") {
  const suffix = randomUUID().replaceAll("-", "");
  return {
    assistant_query_id: `asq_${suffix}`,
    assistant_audit_id: `asa_${suffix}`,
    query_type: queryType,
    answer: {
      summary: "基于可验证来源生成。",
      claims: [],
      recommendations: [],
    },
    sources: [],
    redactions: ["content_omitted"],
    request_id: `req_${suffix}`,
    trace_id: `trc_${suffix}`,
  };
}


test("cleanup failure does not hide an earlier safe failure coordinate", () => {
  const primary = new BrowserAcceptanceFailure("browser.answer", "contract");
  assert.equal(assertCleanupFailurePriority(true, primary), primary);
  assert.equal(assertCleanupFailurePriority(false, null), null);
  assert.throws(
    () => assertCleanupFailurePriority(true, null),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "cleanup"
      && error.code === "failed",
  );
});


test("released browser profile retries only pre-delete handle lock failures", async () => {
  const calls = [];
  let deleteAttempts = 0;
  const result = await deleteVerifiedDirectoryTreeAfterRelease(
    "C:\\owned-browser-profile",
    { device: "1", inode: "2", birthtime_ns: "3" },
    "cleanup.browser",
    {
      retryDelays: [5, 10],
      verifyOwner: async () => calls.push("verify"),
      delayOperation: async (delayMs) => calls.push(`delay:${delayMs}`),
      deleteOperation: async () => {
        deleteAttempts += 1;
        calls.push(`delete:${deleteAttempts}`);
        if (deleteAttempts === 1) {
          throw new BrowserAcceptanceFailure("cleanup.browser", "handle_lock");
        }
        if (deleteAttempts === 2) {
          throw new BrowserAcceptanceFailure("cleanup.browser", "handle_lock_timeout");
        }
        return "deleted";
      },
    },
  );
  assert.equal(result, "deleted");
  assert.deepEqual(calls, [
    "verify",
    "delete:1",
    "verify",
    "delay:5",
    "verify",
    "delete:2",
    "verify",
    "delay:10",
    "verify",
    "delete:3",
  ]);
});


test("released browser profile never retries an operation or ownership failure", async () => {
  for (const failure of [
    new BrowserAcceptanceFailure("cleanup.browser", "handle_operation"),
    new BrowserAcceptanceFailure("cleanup.other", "handle_lock"),
    new Error("unknown"),
  ]) {
    let deleteAttempts = 0;
    let delayAttempts = 0;
    await assert.rejects(
      deleteVerifiedDirectoryTreeAfterRelease(
        "C:\\owned-browser-profile",
        { device: "1", inode: "2", birthtime_ns: "3" },
        "cleanup.browser",
        {
          retryDelays: [5],
          verifyOwner: async () => undefined,
          delayOperation: async () => { delayAttempts += 1; },
          deleteOperation: async () => {
            deleteAttempts += 1;
            throw failure;
          },
        },
      ),
      (error) => error === failure,
    );
    assert.equal(deleteAttempts, 1);
    assert.equal(delayAttempts, 0);
  }

  let deleteAttempts = 0;
  let ownerChecks = 0;
  const ownerFailure = new BrowserAcceptanceFailure("cleanup.browser", "identity");
  await assert.rejects(
    deleteVerifiedDirectoryTreeAfterRelease(
      "C:\\owned-browser-profile",
      { device: "1", inode: "2", birthtime_ns: "3" },
      "cleanup.browser",
      {
        retryDelays: [5],
        verifyOwner: async () => {
          ownerChecks += 1;
          if (ownerChecks === 2) throw ownerFailure;
        },
        delayOperation: async () => assert.fail("delay must follow owner verification"),
        deleteOperation: async () => {
          deleteAttempts += 1;
          throw new BrowserAcceptanceFailure("cleanup.browser", "handle_lock");
        },
      },
    ),
    (error) => error === ownerFailure,
  );
  assert.equal(deleteAttempts, 1);
  assert.equal(ownerChecks, 2);
});


test("Windows handle helper bounds lock callback completion close and drain", async () => {
  const base = {
    script: WINDOWS_TEST_HELPER,
    stage: "test.handle_helper",
    command: "GO",
    successWord: "DONE",
    // 非目标阶段需容纳 Windows 上 Python 冷启动与进程回收抖动；每个目标阶段
    // 仍在对应分支单独压缩为 20ms，确保五段有界合同没有被放宽。
    lockTimeoutMs: 2_000,
    callbackTimeoutMs: 2_000,
    completionTimeoutMs: 2_000,
    closeTimeoutMs: 2_000,
    drainTimeoutMs: 2_000,
  };
  await assert.rejects(
    runWindowsHandleHelper({ ...base, args: ["before_lock"], lockTimeoutMs: 20 }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "test.handle_helper"
      && error.code === "handle_lock_timeout",
  );

  let lockedPid = null;
  await assert.rejects(
    runWindowsHandleHelper({
      ...base,
      args: ["success"],
      callbackTimeoutMs: 20,
      whileLocked: ({ pid }) => {
        lockedPid = pid;
        return new Promise(() => {});
      },
    }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.code === "handle_callback_timeout",
  );
  assert.equal(Number.isInteger(lockedPid) && lockedPid > 0, true);

  await assert.rejects(
    runWindowsHandleHelper({
      ...base,
      args: ["completion"],
      completionTimeoutMs: 20,
    }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.code === "handle_completion_timeout",
  );

  const closeChild = new HandleHelperChild({ closeOnKill: true, closeAfterSuccess: false });
  await assert.rejects(
    runWindowsHandleHelper({
      ...base,
      args: ["success"],
      closeTimeoutMs: 20,
      spawnOperation: () => closeChild,
    }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.code === "handle_close_timeout",
  );
  assert.equal(closeChild.exitCode, 1);

  const drainChild = new HandleHelperChild({ closeOnKill: false, closeAfterSuccess: false });
  await assert.rejects(
    runWindowsHandleHelper({
      ...base,
      args: ["success"],
      closeTimeoutMs: 20,
      drainTimeoutMs: 20,
      spawnOperation: () => drainChild,
    }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.code === "handle_drain_timeout",
  );
  assert.equal(drainChild.exitCode, null);
});


test("harness exit wait closes the listener-registration race", async () => {
  /** 模拟注册退出监听器期间即刻退出的子进程，用于覆盖监听竞态边界。 */
  class ExitRaceChild extends EventEmitter {
    constructor() {
      super();
      this.exitCode = null;
    }

    once(event, listener) {
      const result = super.once(event, listener);
      if (event === "exit" && this.exitCode === null) this.exitCode = 7;
      return result;
    }
  }

  assert.equal(await waitForHarnessExit(new ExitRaceChild(), 20), 7);
});


test("harness close wait includes stdio EOF and remains bounded", async () => {
  let closeHarness;
  const closed = new Promise((resolve) => {
    closeHarness = resolve;
  });
  setTimeout(() => closeHarness(9), 5);
  assert.equal(await waitForHarnessClose({ closed }, 100), 9);
  await assert.rejects(
    waitForHarnessClose({ closed: new Promise(() => {}) }, 10),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "harness.close"
      && error.code === "timeout",
  );
});


test("private page session binds its target to an explicit off-the-record context", async () => {
  const calls = [];
  const browserClient = {
    async send(method, params) {
      calls.push([method, params]);
      if (method === "Target.createBrowserContext") {
        return { browserContextId: "private-context" };
      }
      if (method === "Target.disposeBrowserContext") return {};
      // 未列入合同的 CDP 指令必须让测试立即失败，避免错误调用被静默忽略。
      throw new Error("unexpected command");
    },
  };
  const session = await createPrivatePageSession(
    browserClient,
    19077,
    async (client, port, browserContextId) => {
      assert.equal(client, browserClient);
      assert.equal(port, 19077);
      assert.deepEqual(createPageTargetParameters(browserContextId), {
        url: "about:blank",
        browserContextId: "private-context",
      });
      return { browserContextId, targetId: "private-target", client: { close() {} } };
    },
  );
  assert.equal(session.browserContextId, "private-context");
  assert.deepEqual(calls, [[
    "Target.createBrowserContext",
    { disposeOnDetach: true },
  ]]);
  assert.throws(() => createPageTargetParameters(""));
});


test("private page disposal closes target before disposing its context", async () => {
  const calls = [];
  const browserClient = {
    async send(method, params) {
      calls.push([method, params]);
      if (method === "Target.closeTarget") return { success: true };
      if (method === "Target.disposeBrowserContext") return {};
      // 页面销毁测试只接受两个固定协议动作，其他指令代表边界发生漂移。
      throw new Error("unexpected command");
    },
  };
  const pageSession = {
    targetId: "private-target",
    browserContextId: "private-context",
    client: { close: () => calls.push(["Page.close", {}]) },
  };
  assert.equal(await disposePrivatePageSession(browserClient, pageSession), true);
  assert.deepEqual(calls, [
    ["Page.close", {}],
    ["Target.closeTarget", { targetId: "private-target" }],
    ["Target.disposeBrowserContext", { browserContextId: "private-context" }],
  ]);
});


test("graceful browser close only force-terminates after a bounded wait", async () => {
  const calls = [];
  const child = new EventEmitter();
  child.exitCode = null;
  child.stdout = {};
  child.stderr = {};
  child.unref = () => calls.push("unref");
  const browserClient = {
    async send(method) { calls.push(method); return {}; },
    close() { calls.push("client.close"); },
  };
  assert.deepEqual(
    await closeBrowserGracefully(browserClient, child, {
      waitOperation: async () => {
        child.exitCode = 0;
        queueMicrotask(() => child.emit("close", 0));
        return true;
      },
      terminateOperation: async () => calls.push("terminate"),
    }),
    { forced: false, treeDrained: true, stdioEof: true },
  );
  assert.deepEqual(calls, [
    "Browser.close",
    "client.close",
    "unref",
  ]);

  const forcedCalls = [];
  const forcedChild = new EventEmitter();
  forcedChild.exitCode = null;
  forcedChild.stdout = {};
  forcedChild.stderr = {};
  forcedChild.unref = () => forcedCalls.push("unref");
  assert.deepEqual(
    await closeBrowserGracefully(
      {
        async send() { forcedCalls.push("Browser.close"); return {}; },
        close() { forcedCalls.push("client.close"); },
      },
      forcedChild,
      {
        waitOperation: async () => false,
        terminateOperation: async () => {
          forcedCalls.push("terminate");
          forcedChild.exitCode = 1;
          forcedChild.emit("close", 1);
        },
      },
    ),
    { forced: true, treeDrained: true, stdioEof: true },
  );
  assert.deepEqual(forcedCalls, ["Browser.close", "client.close", "terminate"]);
});


test("browser exit wait closes the listener-registration race", async () => {
  /** 模拟浏览器在监听器注册期间退出，验证自然退出路径不会误判超时。 */
  class ExitRaceChild extends EventEmitter {
    constructor() {
      super();
      this.exitCode = null;
    }

    once(event, listener) {
      const result = super.once(event, listener);
      if (event === "exit" && this.exitCode === null) this.exitCode = 0;
      return result;
    }
  }

  assert.equal(await waitForBrowserExit(new ExitRaceChild(), 20), true);
});


test("browser parent exit without child close cannot prove profile-safe drain", async () => {
  const child = new EventEmitter();
  child.exitCode = null;
  child.stdout = {};
  child.stderr = {};
  child.unref = () => {};
  await assert.rejects(
    closeBrowserGracefully(
      { async send() { return {}; }, close() {} },
      child,
      {
        waitOperation: async () => true,
        closeWaitOperation: async () => {
          throw new BrowserAcceptanceFailure("cleanup.process_tree", "close_timeout");
        },
      },
    ),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "cleanup.process_tree"
      && error.code === "close_timeout",
  );
});


test("source navigation accepts any canonical response index", () => {
  assert.equal(canonicalSourceIndex("0"), "0");
  assert.equal(canonicalSourceIndex("4"), "4");
  assert.equal(canonicalSourceIndex("01"), null);
  assert.equal(canonicalSourceIndex("-1"), null);
});


test("run directory and page request guards reject traversal, external origins and private query keys", () => {
  assert.equal(safeRunId("prd077-browser-unit-01"), "prd077-browser-unit-01");
  assert.throws(() => safeRunId("../outside"), BrowserAcceptanceFailure);
  assert.equal(
    ownedRunDirectory(path.join(ROOT, ".artifacts", "prd077-browser"), "prd077-browser-unit-01"),
    path.join(ROOT, ".artifacts", "prd077-browser", "prd077-browser-unit-01"),
  );
  assert.equal(
    ownedStagingDirectory(
      path.join(ROOT, ".artifacts", "prd077-browser"),
      "prd077-browser-unit-01",
    ),
    path.join(ROOT, ".artifacts", "prd077-browser", ".prd077-browser-unit-01.staging"),
  );
  assert.equal(browserRequestAllowed("http://localhost:5173/"), true);
  assert.equal(browserRequestAllowed("http://localhost:8000/health"), true);
  assert.equal(browserRequestAllowed("http://localhost:8001/admin-assistant/query"), true);
  assert.equal(browserRequestAllowed("https://example.invalid/"), false);
  assert.equal(browserRequestAllowed("http://localhost:5173/?token=private"), false);
  assert.equal(classifyPageRequest("http://localhost:8001/admin-assistant/query"), "assistant");
  assert.equal(classifyPageRequest("http://localhost:8001/auth/me"), "auth_me");
  assert.equal(classifyPageRequest("http://localhost:8001/console/preferences"), "preferences");
  assert.equal(
    classifyPageRequest("http://localhost:8001/internal/config/summary"),
    "config_summary",
  );
  assert.equal(classifyPageRequest("http://localhost:8001/providers"), "providers");
  assert.equal(classifyPageRequest("http://localhost:8001/console/events"), "control_console");
  assert.equal(classifyPageRequest("http://localhost:8001/health"), "control_health_summary");
  assert.equal(classifyPageRequest("http://localhost:8001/health/live"), "control_health_live");
  assert.equal(classifyPageRequest("http://localhost:8000/health/ready"), "gateway_health_ready");
  assert.equal(classifyPageRequest("http://localhost:8000/version"), "gateway_health_version");
  assert.equal(classifyPageRequest("http://localhost:8001/user/preferences"), "control_user");
  assert.equal(
    classifyPageRequest("http://localhost:8001/providers/provider-private/keys"),
    "control_provider",
  );
  assert.equal(classifyPageRequest("https://example.invalid/"), "external");
});


test("private response header gate accepts only Authorization with optional CORS Origin", () => {
  const base = {
    "Cache-Control": "no-store, private",
    Pragma: "no-cache",
    Vary: "Authorization",
    "X-Request-ID": "req_prd077_unit",
    "X-Trace-ID": "trc_prd077_unit",
  };
  assert.equal(privateAssistantHeadersValid(base), true);
  assert.equal(privateAssistantHeadersValid({ ...base, Vary: "Origin, Authorization" }), true);
  assert.equal(privateAssistantHeadersValid({ ...base, Vary: "Origin" }), false);
  assert.equal(privateAssistantHeadersValid({ ...base, Vary: "Authorization, Accept" }), false);
  assert.equal(privateAssistantHeadersValid({ ...base, "X-Trace-ID": "" }), false);
  assert.equal(privateAssistantHeadersValid({ ...base, "Cache-Control": "no-store" }), false);
});


test("only the explicitly owned late assistant request tolerates Chromium ERR_FAILED", () => {
  const tracker = {
    requestOwners: new Map([
      ["late", { category: "assistant", method: "POST" }],
      ["other", { category: "control", method: "GET" }],
    ]),
    expectedCanceledRequestIds: new Set(),
    delayedRequestId: "",
    phase: "admin",
    unexpectedNetworkFailure: null,
    canceledAssistantRequestCount: 0,
    unexpectedNetworkFailureCount: 0,
  };
  registerDelayedAssistantRequest(tracker, {
    requestId: "fetch-late",
    networkId: "late",
  });
  assert.equal(tracker.delayedRequestId, "fetch-late");
  assert.deepEqual([...tracker.expectedCanceledRequestIds], ["late"]);
  recordNetworkLoadingFailure(tracker, {
    requestId: "late",
    canceled: false,
    errorText: "net::ERR_FAILED",
  });
  assert.equal(tracker.canceledAssistantRequestCount, 1);
  assert.equal(tracker.unexpectedNetworkFailureCount, 0);
  assert.equal(tracker.expectedCanceledRequestIds.size, 0);

  tracker.requestOwners.set("assistant-cancel", { category: "assistant", method: "POST" });
  recordNetworkLoadingFailure(tracker, {
    requestId: "assistant-cancel",
    canceled: true,
    errorText: "net::ERR_ABORTED",
  });
  assert.equal(tracker.canceledAssistantRequestCount, 1);
  assert.equal(tracker.unexpectedNetworkFailureCount, 1);

  recordNetworkLoadingFailure(tracker, {
    requestId: "other",
    canceled: false,
    errorText: "net::ERR_FAILED",
  });
  assert.equal(tracker.unexpectedNetworkFailureCount, 2);
  assert.deepEqual(tracker.unexpectedNetworkFailure, {
    phase: "admin",
    category: "assistant",
    reason: "canceled",
  });
});


test("expected assistant cancellation rejects connection, DNS and offline failures", () => {
  const failures = [
    ["net::ERR_CONNECTION_REFUSED", "connection_refused"],
    ["net::ERR_NAME_NOT_RESOLVED", "dns"],
    ["net::ERR_INTERNET_DISCONNECTED", "disconnected"],
  ];
  for (const [errorText, reason] of failures) {
    const tracker = {
      requestOwners: new Map([["late", { category: "assistant", method: "POST" }]]),
      expectedCanceledRequestIds: new Set(["late"]),
      phase: "admin",
      canceledAssistantRequestCount: 0,
      unexpectedNetworkFailureCount: 0,
      unexpectedNetworkFailure: null,
      networkActivityRevision: 0,
    };
    recordNetworkLoadingFailure(tracker, {
      requestId: "late",
      canceled: false,
      errorText,
    });
    assert.equal(tracker.canceledAssistantRequestCount, 0);
    assert.equal(tracker.unexpectedNetworkFailureCount, 1);
    assert.equal(tracker.unexpectedNetworkFailure.reason, reason);
  }
});


test("network summary requires exact assistant success, denial and cancellation counts", () => {
  const tracker = {
    assistantStatuses: [200, 200, 200, 200, 200, 200, 403, 403, 403],
    canceledAssistantRequestCount: 1,
    delayedLoadingFailureObserved: true,
    expectedCanceledRequestIds: new Set(),
    loginStatuses: [200, 200, 200, 200],
    logoutStatuses: [200, 200, 200, 200],
    browserTokens: new Set(["token-a", "token-b", "token-c", "token-d"]),
    answerLeafValues: new Set([
      "PRD077_USAGE_ANSWER_LEAF",
      "PRD077_ERRORS_ANSWER_LEAF",
      "PRD077_COST_ANSWER_LEAF",
      "PRD077_POLICY_ANSWER_LEAF",
    ]),
    answerLeavesByQuery: new Map([
      ["usage", new Set(["PRD077_USAGE_ANSWER_LEAF"])],
      ["errors", new Set(["PRD077_ERRORS_ANSWER_LEAF"])],
      ["cost", new Set(["PRD077_COST_ANSWER_LEAF"])],
      ["policy", new Set(["PRD077_POLICY_ANSWER_LEAF"])],
    ]),
    answerSerializations: new Set(
      Array.from({ length: 6 }, (_, index) => `PRD077_RESPONSE_${index}`),
    ),
    answerResponseCount: 6,
    answerResponsesByQuery: new Map([
      ["usage_statistics", 2],
      ["error_explanation", 2],
      ["cost_advice", 1],
      ["policy_explanation", 1],
    ]),
    pendingAnswerInspections: new Set(),
    answerInspectionFailureCount: 0,
    navigationStatuses: [200],
    healthProbeFailureCount: 0,
    healthProbeRecoveryCount: 0,
    pendingHealthRecoveries: new Map(),
    earlyHealthRecoveries: new Map(),
    externalRequestCount: 0,
    forbiddenQueryCount: 0,
    requestPrivacyViolationCount: 0,
    requestOwners: new Map(),
    assistantHeaderFailureCount: 0,
    unexpectedNetworkFailureCount: 0,
    unexpectedNetworkFailure: null,
    unexpectedConsoleErrorCount: 0,
    expectedConsoleErrorCount: 0,
    exceptionCount: 0,
    pendingConsoleInspections: new Set(),
    consoleInspectionFailureCount: 0,
    inspectionsSealed: true,
    lateInspectionFailureCount: 0,
    processPrivacyViolationCount: 0,
    edgeProcessErrorCount: 0,
  };
  assert.equal(assertTracker(tracker).assistant_http_200, 6);
  assert.equal(assertTracker(tracker).answer_query_types_scanned, 4);
  assert.throws(
    () => assertTracker({
      ...tracker,
      answerLeavesByQuery: new Map(
        [...tracker.answerLeavesByQuery].filter(([name]) => name !== "policy"),
      ),
    }),
    BrowserAcceptanceFailure,
  );
  assert.throws(
    () => assertTracker({
      ...tracker,
      assistantStatuses: [...tracker.assistantStatuses, 200],
    }),
    BrowserAcceptanceFailure,
  );
  assert.throws(
    () => assertTracker({ ...tracker, canceledAssistantRequestCount: 2 }),
    BrowserAcceptanceFailure,
  );
  assert.throws(
    () => assertTracker({
      ...tracker,
      requestOwners: new Map([["late-owner", { category: "control_budget", method: "GET" }]]),
    }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.code === "owners_remaining",
  );
  assert.throws(
    () => assertTracker({ ...tracker, answerResponseCount: 5 }),
    BrowserAcceptanceFailure,
  );
  assert.throws(
    () => assertTracker({
      ...tracker,
      answerResponsesByQuery: new Map([
        ["usage_statistics", 2],
        ["error_explanation", 2],
        ["cost_advice", 1],
      ]),
    }),
    BrowserAcceptanceFailure,
  );
  assert.throws(
    () => assertTracker({ ...tracker, answerInspectionFailureCount: 1 }),
    BrowserAcceptanceFailure,
  );
  assert.throws(
    () => assertTracker({ ...tracker, browserTokens: new Set(["token-a", "token-b", "token-c"]) }),
    BrowserAcceptanceFailure,
  );
  assert.equal(assertTracker({
    ...tracker,
    unexpectedConsoleErrorCount: 1,
    consoleNetworkFailureCount: 1,
    unexpectedConsoleFailure: {
      phase: "admin",
      source: "log",
      reason: "net_failed",
    },
  }).expected_console_errors, 1);
  assert.throws(
    () => assertTracker({
      ...tracker,
      unexpectedConsoleErrorCount: 2,
      consoleNetworkFailureCount: 2,
      unexpectedConsoleFailure: {
        phase: "admin",
        source: "log",
        reason: "net_failed",
      },
    }),
    BrowserAcceptanceFailure,
  );

  const recovered = {
    ...tracker,
    requestOwners: new Map([[
      "health-failed",
      {
        category: "control_health_live",
        method: "GET",
        phase: "security.login",
        healthAttempt: { logicalId: "control_live_probe_1", attempt: 1 },
      },
    ]]),
    healthProbeFailureCount: 0,
    healthProbeRecoveryCount: 0,
    pendingHealthRecoveries: new Map(),
    earlyHealthRecoveries: new Map(),
  };
  recordNetworkLoadingFailure(recovered, {
    requestId: "health-failed",
    canceled: false,
    errorText: "net::ERR_FAILED",
  });
  assert.throws(() => assertTracker(recovered), BrowserAcceptanceFailure);
  recovered.requestOwners.set(
    "health-retry",
    {
      category: "control_health_live",
      method: "GET",
      phase: "security.login",
      healthAttempt: { logicalId: "control_live_probe_1", attempt: 2 },
    },
  );
  recordNetworkResponse(recovered, {
    requestId: "health-retry",
    response: { status: 200, headers: {} },
  });
  recovered.requestOwners.delete("health-retry");
  assert.equal(assertTracker(recovered).recovered_health_probe_failures, 1);

  const secondFailure = {
    ...recovered,
    requestOwners: new Map([["health-failed-2", {
      category: "gateway_health_ready",
      method: "GET",
      phase: "final",
      healthAttempt: { logicalId: "gateway_ready_probe_2", attempt: 1 },
    }]]),
    pendingHealthRecoveries: new Map(),
  };
  recordNetworkLoadingFailure(secondFailure, {
    requestId: "health-failed-2",
    canceled: false,
    errorText: "net::ERR_FAILED",
  });
  secondFailure.requestOwners.set("health-retry-2", {
    category: "gateway_health_ready",
    method: "GET",
    phase: "final",
    healthAttempt: { logicalId: "gateway_ready_probe_2", attempt: 2 },
  });
  recordNetworkResponse(secondFailure, {
    requestId: "health-retry-2",
    response: { status: 200, headers: {} },
  });
  secondFailure.requestOwners.delete("health-retry-2");
  assert.equal(assertTracker(secondFailure).recovered_health_probe_failures, 2);

  const crossPhase = {
    ...recovered,
    requestOwners: new Map(),
    healthProbeFailureCount: 1,
    healthProbeRecoveryCount: 0,
    pendingHealthRecoveries: new Map([[
      "control_live_probe_1",
      { category: "control_health_live", phase: "security.login" },
    ]]),
  };
  crossPhase.requestOwners.set(
    "health-wrong-phase",
    {
      category: "control_health_live",
      method: "GET",
      phase: "final",
      healthAttempt: { logicalId: "control_live_probe_1", attempt: 2 },
    },
  );
  recordNetworkResponse(crossPhase, {
    requestId: "health-wrong-phase",
    response: { status: 200, headers: {} },
  });
  crossPhase.requestOwners.delete("health-wrong-phase");
  assert.equal(assertTracker(crossPhase).recovered_health_probe_failures, 1);

  const responseFirst = {
    ...tracker,
    requestOwners: new Map([["health-response-first", {
      category: "gateway_health_live",
      method: "GET",
      phase: "admin.allowed",
      healthAttempt: { logicalId: "gateway_live_probe_4", attempt: 2 },
    }]]),
    pendingHealthRecoveries: new Map(),
    earlyHealthRecoveries: new Map(),
  };
  recordNetworkResponse(responseFirst, {
    requestId: "health-response-first",
    response: { status: 200, headers: {} },
  });
  responseFirst.requestOwners.delete("health-response-first");
  assert.throws(() => assertTracker(responseFirst), BrowserAcceptanceFailure);
  responseFirst.requestOwners.set("health-failure-late", {
    category: "gateway_health_live",
    method: "GET",
    phase: "admin.cleared",
    healthAttempt: { logicalId: "gateway_live_probe_4", attempt: 1 },
  });
  recordNetworkLoadingFailure(responseFirst, {
    requestId: "health-failure-late",
    canceled: false,
    errorText: "net::ERR_FAILED",
  });
  assert.equal(assertTracker(responseFirst).recovered_health_probe_failures, 1);

  const wrongOwner = {
    ...tracker,
    requestOwners: new Map([["health-wrong-owner", {
      category: "control_health_ready",
      method: "GET",
      phase: "final",
      healthAttempt: { logicalId: "control_live_probe_5", attempt: 2 },
    }]]),
    healthProbeFailureCount: 1,
    healthProbeRecoveryCount: 0,
    pendingHealthRecoveries: new Map([[
      "control_live_probe_5",
      { category: "control_health_live", phase: "security.login" },
    ]]),
    earlyHealthRecoveries: new Map(),
  };
  recordNetworkResponse(wrongOwner, {
    requestId: "health-wrong-owner",
    response: { status: 200, headers: {} },
  });
  wrongOwner.requestOwners.delete("health-wrong-owner");
  assert.throws(() => assertTracker(wrongOwner), BrowserAcceptanceFailure);

  const failedRetry = {
    ...tracker,
    requestOwners: new Map([["health-retry-failed", {
      category: "control_health_live",
      method: "GET",
      phase: "security.login",
      healthAttempt: { logicalId: "control_live_probe_3", attempt: 2 },
    }]]),
    healthProbeFailureCount: 1,
    pendingHealthRecoveries: new Map([[
      "control_live_probe_3",
      { category: "control_health_live", phase: "security.login" },
    ]]),
  };
  recordNetworkLoadingFailure(failedRetry, {
    requestId: "health-retry-failed",
    canceled: false,
    errorText: "net::ERR_FAILED",
  });
  assert.equal(failedRetry.unexpectedNetworkFailure?.category, "control_health_live");
  assert.throws(
    () => assertTracker(failedRetry),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.network.security.login.control_health_live"
      && error.code === "loading_failed",
  );
});


test("health attempt request ids are strict and preserve a stable logical owner", () => {
  assert.deepEqual(parseHealthAttemptRequestId("control_live_probe_7_attempt_1"), {
    logicalId: "control_live_probe_7",
    attempt: 1,
  });
  assert.deepEqual(parseHealthAttemptRequestId("control_live_probe_7_attempt_2"), {
    logicalId: "control_live_probe_7",
    attempt: 2,
  });
  assert.equal(parseHealthAttemptRequestId("control_live_probe_7_attempt_3"), null);
  assert.equal(parseHealthAttemptRequestId("attempt_1"), null);
  assert.equal(parseHealthAttemptRequestId("private value attempt_1"), null);
});


test("browser storage facts require exact authenticated slots and empty logout state", () => {
  const active = {
    local_storage_count: 0,
    session_storage_count: 2,
    session_keys_exact: true,
    auth_token_known: true,
    auth_context_valid: true,
  };
  assert.equal(browserStorageFactsValid(active, true), true);
  assert.equal(browserStorageFactsValid({ ...active, session_storage_count: 3 }, true), false);
  assert.equal(browserStorageFactsValid({ ...active, auth_token_known: false }, true), false);
  assert.equal(browserStorageFactsValid({ ...active, auth_context_valid: false }, true), false);

  const logout = {
    local_storage_count: 0,
    session_storage_count: 0,
    session_keys_exact: false,
    auth_token_known: false,
    auth_context_valid: false,
  };
  assert.equal(browserStorageFactsValid(logout, false), true);
  assert.equal(browserStorageFactsValid({ ...logout, local_storage_count: 1 }, false), false);
  assert.equal(browserStorageFactsValid({ ...logout, session_keys_exact: true }, false), false);
});


test("process privacy scanner detects private values split across stream chunks", () => {
  const privateValue = `PRD077_CHUNK_${randomUUID()}`;
  const tracker = {
    privateValues: [privateValue],
    browserTokens: new Set(),
    processTails: new Map(),
    processPrivacyViolationCount: 0,
  };
  const split = Math.floor(privateValue.length / 2);
  inspectProcessChunk(privateValue.slice(0, split), tracker, "stdout");
  assert.equal(tracker.processPrivacyViolationCount, 0);
  inspectProcessChunk(privateValue.slice(split), tracker, "stdout");
  assert.equal(tracker.processPrivacyViolationCount, 1);
});


test("assistant response serialization is bounded, exact-schema and cross-runtime stable", () => {
  const payload = assistantResponsePayload();
  const raw = JSON.stringify(payload, null, 2);
  const facts = assistantResponseSerializations({ body: raw, base64Encoded: false });
  assert.equal(facts.queryType, "usage_statistics");
  assert.equal(facts.serializations.includes(raw), true);
  assert.equal(facts.serializations.some((value) => value.includes("\\u57fa\\u4e8e")), true);
  assert.equal(facts.serializations.some((value) => value.includes('"answer": {')), true);
  const tracker = createTracker([]);
  tracker.recentInspectionText.set("process.control_stdout", `prefix:${raw}:suffix`);
  registerAssistantResponseBody(tracker, { body: raw, base64Encoded: false });
  assert.equal(tracker.processPrivacyViolationCount, 1);

  assert.throws(
    () => assistantResponseSerializations({ body: raw, base64Encoded: true }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "encoding",
  );
  assert.throws(
    () => assistantResponseSerializations({ body: "not-json", base64Encoded: false }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "json",
  );
  assert.throws(
    () => assistantResponseSerializations({
      body: JSON.stringify({ ...payload, extra: "forbidden" }),
      base64Encoded: false,
    }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "schema",
  );
  assert.throws(
    () => assistantResponseSerializations({
      body: JSON.stringify({ ...payload, answer: { summary: "x".repeat(300 * 1024) } }),
      base64Encoded: false,
    }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "size",
  );
});


test("browser guards capture each successful assistant body and fail closed on read errors", async () => {
  /** 模拟 DevTools 客户端的事件订阅与响应正文读取，用于覆盖动态失败折叠。 */
  class FakeClient {
    /** 保存响应正文和读取失败开关，并初始化独立监听器与调用计数。 */
    constructor(responseBody, failRead = false) {
      this.responseBody = responseBody;
      this.failRead = failRead;
      this.listeners = new Map();
      this.bodyReads = 0;
    }

    on(method, listener) {
      const listeners = this.listeners.get(method) ?? [];
      listeners.push(listener);
      this.listeners.set(method, listeners);
    }

    emit(method, event) {
      for (const listener of this.listeners.get(method) ?? []) listener(event);
    }

    async send(method) {
      assert.equal(method, "Network.getResponseBody");
      this.bodyReads += 1;
      // 动态读取异常必须由浏览器守卫折叠，原始异常正文不得进入验收证据。
      if (this.failRead) throw new Error("private dynamic failure must be folded");
      return this.responseBody;
    }
  }

  const payload = assistantResponsePayload("usage_statistics");
  const body = { body: JSON.stringify(payload), base64Encoded: false };
  const client = new FakeClient(body);
  const tracker = createTracker([]);
  bindBrowserGuards(client, tracker);
  client.emit("Network.requestWillBeSent", {
    requestId: "assistant-success",
    request: {
      url: "http://localhost:8001/admin-assistant/query",
      method: "POST",
      headers: {},
    },
  });
  client.emit("Network.responseReceived", {
    requestId: "assistant-success",
    response: {
      status: 200,
      headers: {
        "Cache-Control": "no-store, private",
        Pragma: "no-cache",
        Vary: "Authorization",
        "X-Request-ID": "req-safe",
        "X-Trace-ID": "trc-safe",
      },
    },
  });
  client.emit("Network.loadingFinished", { requestId: "assistant-success" });
  await waitForConsoleInspections(tracker);
  assert.equal(client.bodyReads, 1);
  assert.equal(tracker.answerResponseCount, 1);
  assert.equal(tracker.answerResponsesByQuery.get("usage_statistics"), 1);
  assert.equal(tracker.answerSerializations.has(body.body), true);

  client.emit("Network.requestWillBeSent", {
    requestId: "assistant-denied",
    request: {
      url: "http://localhost:8001/admin-assistant/query",
      method: "POST",
      headers: {},
    },
  });
  client.emit("Network.responseReceived", {
    requestId: "assistant-denied",
    response: { status: 403, headers: {} },
  });
  client.emit("Network.loadingFinished", { requestId: "assistant-denied" });
  await waitForConsoleInspections(tracker);
  assert.equal(client.bodyReads, 1);

  const failingClient = new FakeClient(body, true);
  const failingTracker = createTracker([]);
  bindBrowserGuards(failingClient, failingTracker);
  failingClient.emit("Network.requestWillBeSent", {
    requestId: "assistant-failed-read",
    request: {
      url: "http://localhost:8001/admin-assistant/query",
      method: "POST",
      headers: {},
    },
  });
  failingClient.emit("Network.responseReceived", {
    requestId: "assistant-failed-read",
    response: { status: 200, headers: {} },
  });
  failingClient.emit("Network.loadingFinished", { requestId: "assistant-failed-read" });
  await assert.rejects(
    waitForConsoleInspections(failingTracker),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.answer_response"
      && error.code === "inspection_failed",
  );
});


test("four query answer leaves rescan output observed before DOM registration", () => {
  const leaves = Object.freeze({
    usage: `PRD077_USAGE_${randomUUID()}`,
    errors: `PRD077_ERRORS_${randomUUID()}`,
    cost: `PRD077_COST_${randomUUID()}`,
    policy: `PRD077_POLICY_${randomUUID()}`,
  });
  const tracker = {
    privateValues: [],
    browserTokens: new Set(),
    answerLeafValues: new Set(),
    answerLeavesByQuery: new Map(),
    processTails: new Map(),
    recentInspectionText: new Map(),
    processPrivacyViolationCount: 0,
  };
  inspectProcessChunk(`prefix:${leaves.errors}:suffix`, tracker, "control_stdout");
  tracker.recentInspectionText.set("browser.console", `prefix:${leaves.cost}:suffix`);
  assert.equal(tracker.processPrivacyViolationCount, 0);
  for (const [name, value] of Object.entries(leaves)) {
    assert.deepEqual(registerAnswerLeafValues(tracker, name, [value]), [value]);
  }
  assert.equal(tracker.processPrivacyViolationCount, 2);
  assert.deepEqual([...tracker.answerLeavesByQuery.keys()].sort(), [
    "cost",
    "errors",
    "policy",
    "usage",
  ]);
  inspectProcessChunk(leaves.policy, tracker, "control_stderr");
  assert.equal(tracker.processPrivacyViolationCount, 3);
  assert.throws(
    () => registerAnswerLeafValues(tracker, "errors", ["short"]),
    BrowserAcceptanceFailure,
  );
});


test("harness failure parser accepts only fixed exact coordinates", () => {
  const failure = parseHarnessFailureLine(
    '{"status":"failed","stage":"database.seed","code":"usage_cost"}',
  );
  assert.equal(failure instanceof BrowserAcceptanceFailure, true);
  assert.equal(failure.stage, "database.seed");
  assert.equal(failure.code, "usage_cost");
  assert.equal(
    parseHarnessFailureLine(
      '{"status":"failed","stage":"database.seed","code":"usage_cost","detail":"private"}',
    ),
    null,
  );
  assert.equal(
    parseHarnessFailureLine('{"status":"failed","stage":"C:/private","code":"unexpected"}'),
    null,
  );
  assert.equal(parseHarnessFailureLine("not-json"), null);
});


test("answer canary is privacy-scan safe and maps to exact usage and cost oracles", () => {
  for (let index = 0; index < 256; index += 1) {
    const canary = createPrivateConfiguration().answerCanary;
    assert.match(canary, /^[1-9][0-9]{4}\.\d{6}$/);
    assert.equal(canary.length, 12);
    assert.doesNotMatch(canary, /0$/);
  }
  assert.deepEqual(answerOracleForQuery("usage", "12345.678901"), {
    claimLabel: "用量成本",
    claimValue: "12345.678901 CNY",
    sourceLabel: "用量账本",
    sourceHref: "/#usage",
  });
  assert.deepEqual(answerOracleForQuery("cost", "12345.678901"), {
    claimLabel: "已入账借方成本",
    claimValue: "12345.678901 CNY",
    sourceLabel: "FinOps 账本",
    sourceHref: "/#finops-ledger",
  });
  assert.equal(answerOracleForQuery("errors", "12345.678901"), null);
});


test("harness environment allowlists host keys and owns every service secret", () => {
  const privateConfig = createPrivateConfiguration();
  const rejectedHostValue = `PRD077_HOST_${randomUUID()}`;
  const environment = harnessEnvironment(
    privateConfig,
    { nonce: "ab".repeat(32) },
    {
      Path: "C:\\safe-bin",
      SystemRoot: "C:\\Windows",
      ComSpec: "C:\\Windows\\System32\\cmd.exe",
      GITHUB_TOKEN: rejectedHostValue,
      NODE_OPTIONS: rejectedHostValue,
      USERPROFILE: rejectedHostValue,
    },
  );
  assert.equal(environment.PATH, "C:\\safe-bin");
  assert.equal(environment.SYSTEMROOT, "C:\\Windows");
  assert.equal(environment.COMSPEC, "C:\\Windows\\System32\\cmd.exe");
  assert.equal(Object.values(environment).includes(rejectedHostValue), false);
  assert.equal("GITHUB_TOKEN" in environment, false);
  assert.equal("NODE_OPTIONS" in environment, false);
  assert.equal("USERPROFILE" in environment, false);

  const serviceSecretMappings = [
    ["PRD077_BROWSER_FILE_ENCRYPTION_SECRET", "fileEncryptionSecret"],
    ["PRD077_BROWSER_BATCH_ENCRYPTION_SECRET", "batchEncryptionSecret"],
    ["PRD077_BROWSER_PROMPT_TEMPLATE_ENCRYPTION_SECRET", "promptTemplateEncryptionSecret"],
    ["PRD077_BROWSER_CACHE_AFFINITY_HMAC_SECRET", "cacheAffinityHmacSecret"],
  ];
  assert.equal(
    serviceSecretMappings.every(
      ([environmentName, fieldName]) => environment[environmentName] === privateConfig[fieldName],
    ),
    true,
  );
  for (const [, fieldName] of serviceSecretMappings) {
    const tracker = {
      privateValues: Object.values(privateConfig),
      browserTokens: new Set(),
      processTails: new Map(),
      processPrivacyViolationCount: 0,
    };
    inspectProcessChunk(privateConfig[fieldName], tracker, "harness_stdout");
    assert.equal(tracker.processPrivacyViolationCount, 1);
  }
});


test("console object inspection scans nested values and fails closed at both limits", async () => {
  const observed = [];
  const graph = new Map([
    ["root", {
      result: [{ name: "nested", value: { objectId: "child", description: "Object" } }],
    }],
    ["child", {
      result: [{ name: "private", value: { type: "string", value: "private-canary" } }],
    }],
  ]);
  const client = {
    send: async (_method, { objectId }) => graph.get(objectId) ?? { result: [] },
  };
  assert.deepEqual(
    await inspectConsoleObjectGraph(client, "root", (value) => observed.push(String(value))),
    { objects_inspected: 2 },
  );
  assert.equal(observed.includes("private-canary"), true);

  const deepClient = {
    send: async (_method, { objectId }) => ({
      result: [{
        name: "next",
        value: { objectId: `${objectId}-next`, description: "Object" },
      }],
    }),
  };
  await assert.rejects(
    inspectConsoleObjectGraph(deepClient, "root", () => {}, { depthLimit: 1 }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "depth_limit",
  );

  const wideClient = {
    send: async (_method, { objectId }) => objectId === "root"
      ? {
        result: ["a", "b", "c"].map((name) => ({
          name,
          value: { objectId: name, description: "Object" },
        })),
      }
      : { result: [] },
  };
  await assert.rejects(
    inspectConsoleObjectGraph(wideClient, "root", () => {}, { objectLimit: 2 }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "object_limit",
  );

  await assert.rejects(
    inspectConsoleObjectGraph(
      { send: async () => new Promise(() => {}) },
      "root",
      () => {},
      { deadline: Date.now() + 10 },
    ),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.console"
      && error.code === "timeout",
  );
});


test("pending inspections share one deadline and sealed trackers reject late tasks", async () => {
  const pendingTracker = createTracker([]);
  pendingTracker.pendingConsoleInspections.add(new Promise(() => {}));
  await assert.rejects(
    waitForConsoleInspections(pendingTracker, { timeoutMs: 15, stableMs: 5, pollMs: 2 }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.inspection"
      && error.code === "timeout",
  );

  /** 模拟可延后发出网络事件的 DevTools 客户端，验证 tracker 封口拒绝迟到任务。 */
  class FakeClient {
    /** 初始化监听器集合和发送计数，保证每个迟到事件都可被精确观察。 */
    constructor() {
      this.listeners = new Map();
      this.sendCount = 0;
    }

    on(name, listener) {
      const listeners = this.listeners.get(name) ?? [];
      listeners.push(listener);
      this.listeners.set(name, listeners);
    }

    emit(name, payload) {
      for (const listener of this.listeners.get(name) ?? []) listener(payload);
    }

    async send() {
      this.sendCount += 1;
      return { result: [] };
    }
  }
  const client = new FakeClient();
  const lateTracker = createTracker([]);
  bindBrowserGuards(client, lateTracker);
  const waiting = waitForConsoleInspections(
    lateTracker,
    { timeoutMs: 100, stableMs: 30, pollMs: 2 },
  );
  setTimeout(() => {
    client.emit("Runtime.consoleAPICalled", {
      type: "log",
      args: [{ objectId: "late-object", description: "Object" }],
    });
  }, 5);
  await assert.rejects(
    waiting,
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.inspection"
      && error.code === "late_task",
  );
  assert.equal(client.sendCount, 0);
});


test("final network owners must drain to a stable empty set", async () => {
  const tracker = createTracker([]);
  tracker.requestOwners.set("owned-request", { category: "control_budget", method: "GET" });
  setTimeout(() => tracker.requestOwners.delete("owned-request"), 10);
  await waitForRequestOwnersDrain(tracker, { timeoutMs: 200, stableMs: 10, pollMs: 2 });

  tracker.requestOwners.set("stuck-request", { category: "control_budget", method: "GET" });
  await assert.rejects(
    waitForRequestOwnersDrain(tracker, { timeoutMs: 15, stableMs: 5, pollMs: 2 }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "browser.network"
      && error.code === "owners_timeout",
  );

  const transient = createTracker([]);
  const started = Date.now();
  setTimeout(() => { transient.networkActivityRevision += 1; }, 8);
  await waitForRequestOwnersDrain(
    transient,
    { timeoutMs: 100, stableMs: 20, pollMs: 2 },
  );
  assert.equal(Date.now() - started >= 24, true);
});


test("harness quiesce request is bounded and exact", async () => {
  /** 模拟永不返回的控制请求，仅在 AbortSignal 触发时以固定异常结束。 */
  const blockedFetch = async (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
  });
  await assert.rejects(
    quiesceHarness({ fetchOperation: blockedFetch, timeoutMs: 10 }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "harness.quiesce"
      && error.code === "timeout",
  );
  await assert.rejects(
    quiesceHarness({
      fetchOperation: async () => ({ status: 200, json: async () => ({ status: "other" }) }),
      timeoutMs: 100,
    }),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "harness.quiesce"
      && error.code === "contract",
  );
  await quiesceHarness({
    fetchOperation: async () => ({ status: 200, json: async () => ({ status: "quiesced" }) }),
    timeoutMs: 100,
  });
});


test("harness stop preserves a reported safe cleanup coordinate", async () => {
  const reported = new BrowserAcceptanceFailure("cleanup.sqlite_sidecars", "remaining");
  const options = {
    requireSuccess: true,
    fetchOperation: async () => ({ status: 200 }),
    waitOperation: async () => 1,
    timeoutMs: 100,
  };
  await assert.rejects(
    stopHarness({ child: { exitCode: 1 }, failure: () => reported }, options),
    (error) => error === reported,
  );
  await assert.rejects(
    stopHarness({ child: { exitCode: 1 }, failure: () => null }, options),
    (error) => error instanceof BrowserAcceptanceFailure
      && error.stage === "harness.stop"
      && error.code === "exit",
  );
});


test("finance revoke request aborts on a bounded timeout", async () => {
  /** 模拟只响应中止信号的网络请求，验证撤权调用始终受超时上界约束。 */
  const blockedFetch = async (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
  });
  await assert.rejects(
    revokeFinancePermission({ fetchOperation: blockedFetch, timeoutMs: 10 }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "timeout",
  );
  await assert.rejects(
    revokeFinancePermission({
      fetchOperation: async (_url, { signal }) => ({
        status: 200,
        json: async () => new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
        }),
      }),
      timeoutMs: 10,
    }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "timeout",
  );
  await revokeFinancePermission({
    fetchOperation: async () => ({ status: 200, json: async () => ({ status: "revoked" }) }),
    timeoutMs: 100,
  });
});


test("Windows tree killer rejects taskkill non-zero exit", async () => {
  const killer = new EventEmitter();
  killer.exitCode = null;
  killer.kill = () => true;
  const attempt = terminateWindowsProcessTree(12345, {
    spawnOperation: () => {
      queueMicrotask(() => {
        killer.exitCode = 5;
        killer.emit("exit", 5);
      });
      return killer;
    },
    timeoutMs: 100,
  });
  await assert.rejects(
    attempt,
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "exit",
  );
});


test("isolated browser child cleanup is bounded and releases its process watcher", async () => {
  const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  await once(child, "spawn");
  await terminateBrowserTree(child);
  assert.equal(child.exitCode !== null || child.signalCode !== null, true);

  const harnessChild = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  await once(harnessChild, "spawn");
  await terminateHarnessTree(harnessChild);
  assert.equal(harnessChild.exitCode !== null || harnessChild.signalCode !== null, true);
});


test("Windows harness cleanup terminates an actual descendant process", {
  skip: process.platform !== "win32",
}, async () => {
  const parent = spawn(process.execPath, ["-e", `
    const { spawn } = require('node:child_process');
    const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], {
      stdio: 'ignore',
      windowsHide: true,
    });
    process.stdout.write(String(child.pid) + '\\n');
    setInterval(() => {}, 1000);
  `], {
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  await once(parent, "spawn");
  const [chunk] = await once(parent.stdout, "data");
  const descendantPid = Number(String(chunk).trim());
  assert.equal(Number.isInteger(descendantPid) && descendantPid > 0, true);
  try {
    await terminateHarnessTree(parent);
    assert.equal(parent.exitCode !== null || parent.signalCode !== null, true);
    assert.throws(() => process.kill(descendantPid, 0));
  } finally {
    try { process.kill(descendantPid); } catch {}
    try { if (parent.exitCode === null) parent.kill(); } catch {}
  }
});


test("layout and evidence contracts fail closed on overlap, private keys and private values", () => {
  const valid = {
    viewport_width: VIEWPORTS.desktop.width,
    viewport_height: VIEWPORTS.desktop.height,
    document_scroll_width: VIEWPORTS.desktop.width,
    body_scroll_width: VIEWPORTS.desktop.width,
    root_visible: true,
    root_inside_horizontal: true,
    controls_outside_horizontal: 0,
    text_overflow_count: 0,
    control_overlap_count: 0,
    shell_overlap_area: 0,
    sensitive_node_count: 0,
    question_length: 0,
  };
  assert.deepEqual(assertLayoutMetrics(valid, VIEWPORTS.desktop), valid);
  assert.throws(
    () => assertLayoutMetrics({ ...valid, control_overlap_count: 1 }, VIEWPORTS.desktop),
    BrowserAcceptanceFailure,
  );
  assert.equal(assertEvidenceKeys({ status: "passed", workflow: { count: 1 } }), true);
  assert.throws(() => assertEvidenceKeys({ question: "forbidden" }), BrowserAcceptanceFailure);
  assert.equal(assertSafeEvidence({ status: "passed" }, ["private-canary"]), true);
  assert.throws(
    () => assertSafeEvidence({ status: "private-canary" }, ["private-canary"]),
    BrowserAcceptanceFailure,
  );
  for (const queryName of ["usage", "errors", "cost", "policy"]) {
    const answerLeaf = `PRD077_${queryName.toUpperCase()}_ANSWER_${randomUUID()}`;
    assert.throws(
      () => assertSafeEvidence({ status: answerLeaf }, [answerLeaf]),
      BrowserAcceptanceFailure,
    );
  }
});


test("SHA-256 manifest is deterministic and keeps completion flag false", async () => {
  const directory = path.join(ROOT, ".tmp", `prd077-browser-unit-${randomUUID()}`);
  await mkdir(directory, { recursive: false });
  try {
    await writeFile(path.join(directory, "alpha.json"), "alpha\n", "utf8");
    await writeFile(path.join(directory, "beta.json"), "beta\n", "utf8");
    assert.deepEqual(SOURCE_PROVENANCE_NAMES, [
      "browser_driver",
      "browser_harness",
      "browser_helper_prd056",
      "browser_helper_prd066",
      "console_dev_server",
      "console_entry",
      "control_plane_source_tree",
      "frontend_source_tree",
      "gateway_source_tree",
      "migration_0070_down",
      "migration_0070_up",
      "migration_set",
      "runtime_acceptance",
      "runtime_helper_prd075",
      "shared_sdk_source_tree",
    ]);
    const provenance = Object.fromEntries(
      SOURCE_PROVENANCE_NAMES.map((name, index) => [
        name,
        index.toString(16).padStart(64, "0"),
      ]),
    );
    assert.equal(assertSourceProvenance(provenance), true);
    assert.equal(assertSourceProvenanceUnchanged(provenance, { ...provenance }), true);
    assert.throws(
      () => assertSourceProvenance({ ...provenance, unexpected: "0".repeat(64) }),
      BrowserAcceptanceFailure,
    );
    const manifest = await buildManifest(
      directory,
      ["beta.json", "alpha.json"],
      provenance,
    );
    assert.deepEqual(manifest.files.map((item) => item.path), ["alpha.json", "beta.json"]);
    assert.equal(
      manifest.files[0].sha256,
      createHash("sha256").update("alpha\n").digest("hex"),
    );
    assert.equal(manifest.all_108_prds_complete, false);
    assert.deepEqual(manifest.source_provenance, provenance);
    await writeFile(
      path.join(directory, "sha256-manifest.json"),
      `${JSON.stringify(manifest, null, 2)}\n`,
      "utf8",
    );
    assert.deepEqual(
      await verifyPublishedManifest(
        directory,
        ["beta.json", "alpha.json"],
        provenance,
      ),
      { files_verified: 3 },
    );
    const ownershipClaim = createOwnershipClaim();
    await writeFile(
      path.join(directory, OWNER_CLAIM_FILENAME),
      `${ownershipClaim.digest}\n`,
      "ascii",
    );
    assert.deepEqual(
      await verifyPublishedManifest(
        directory,
        ["beta.json", "alpha.json"],
        provenance,
        ownershipClaim,
      ),
      { files_verified: 4 },
    );
    await writeFile(path.join(directory, "alpha.json"), "tampered\n", "utf8");
    await assert.rejects(
      verifyPublishedManifest(
        directory,
        ["beta.json", "alpha.json"],
        provenance,
        ownershipClaim,
      ),
      BrowserAcceptanceFailure,
    );
  } finally {
    await rm(directory, { recursive: true, force: false });
  }
});


test("runtime summary validator locks nested database and privacy schema", () => {
  const scan = {
    files_scanned: 1,
    plaintext_matches: 0,
    sqlite_sidecars_remaining: 0,
  };
  const summary = {
    schema_version: "prd077-browser-harness-v2",
    status: "passed",
    gateway_http_200: true,
    control_http_200: true,
    console_http_200: true,
    finance_source_permission_revoked: true,
    base_permission_retained: true,
    backend_denied_after_source_revoke: true,
    privacy: { work: scan, staging_before_summary: scan },
    database: {
      migration_count: 70,
      latest_migration: "0070",
      integrity_check: "ok",
      foreign_key_violations: 0,
    },
    owned_processes_remaining: 0,
    all_108_prds_complete: false,
  };
  assert.equal(assertRuntimeSummary(summary), true);
  assert.throws(
    () => assertRuntimeSummary({
      ...summary,
      database: { ...summary.database, head: "0070" },
    }),
    BrowserAcceptanceFailure,
  );
});


test("atomic publish leaves final absent when rename fails", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-publish-${randomUUID()}`);
  const staging = path.join(parent, ".run.staging");
  const final = path.join(parent, "run");
  await mkdir(staging, { recursive: true });
  try {
    await assert.rejects(
      publishStagingAtomically(staging, final, {
        moveOperation: async () => {
          // 注入原子重命名失败，验证最终证据目录不会留下半发布状态。
          throw new Error("injected");
        },
      }),
      BrowserAcceptanceFailure,
    );
    assert.equal(existsSync(final), false);
    assert.equal(existsSync(staging), true);
    await publishStagingAtomically(staging, final);
    assert.equal(existsSync(final), true);
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("atomic publish removes its final when post-rename validation fails", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-validate-${randomUUID()}`);
  const staging = path.join(parent, ".run.staging");
  const final = path.join(parent, "run");
  await mkdir(staging, { recursive: true });
  await writeFile(path.join(staging, "evidence.json"), "{}\n", "utf8");
  try {
    await assert.rejects(
      publishStagingAtomically(staging, final, {
        validateFinal: async () => {
          // 注入发布后校验失败，验证驱动会移除已经重命名的不可信目录。
          throw new Error("injected");
        },
      }),
      (error) => error instanceof BrowserAcceptanceFailure && error.code === "validation",
    );
    assert.equal(existsSync(staging), false);
    assert.equal(existsSync(final), false);
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("atomic publish preserves a replacement even when it copies the owner claim", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-publish-replaced-${randomUUID()}`);
  const staging = path.join(parent, ".run.staging");
  const final = path.join(parent, "run");
  const ownershipClaim = createOwnershipClaim();
  await mkdir(staging, { recursive: true });
  await writeFile(
    path.join(staging, OWNER_CLAIM_FILENAME),
    `${ownershipClaim.digest}\n`,
    "ascii",
  );
  const identity = await captureDirectoryIdentity(staging, "test.publish_identity");
  try {
    await assert.rejects(
      publishStagingAtomically(staging, final, {
        ownershipClaim,
        expectedIdentity: identity,
        moveOperation: async () => {
          await rm(staging, { recursive: true, force: false });
          await mkdir(final, { recursive: false });
          await writeFile(
            path.join(final, OWNER_CLAIM_FILENAME),
            `${ownershipClaim.digest}\n`,
            "ascii",
          );
          await writeFile(path.join(final, "replacement.txt"), "replacement\n", "utf8");
        },
      }),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "evidence.publish"
        && error.code === "owner_claim",
    );
    assert.equal(await readFile(path.join(final, "replacement.txt"), "utf8"), "replacement\n");
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("verified publish move locks the exact source before moving it", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-publish-handle-${randomUUID()}`);
  const staging = path.join(parent, ".run.staging");
  const original = path.join(parent, "original-away");
  const final = path.join(parent, "run");
  const ownershipClaim = createOwnershipClaim();
  await mkdir(staging, { recursive: true });
  await Promise.all([
    writeFile(
      path.join(staging, OWNER_CLAIM_FILENAME),
      `${ownershipClaim.digest}\n`,
      "ascii",
    ),
    writeFile(path.join(staging, "original.txt"), "original\n", "utf8"),
  ]);
  const identity = await captureDirectoryIdentity(staging, "test.publish_handle_identity");
  try {
    await publishStagingAtomically(staging, final, {
      ownershipClaim,
      expectedIdentity: identity,
      afterMoveLocked: async () => {
        await assertWindowsSharingViolation(() => rename(staging, original));
        assert.equal(await readFile(path.join(staging, "original.txt"), "utf8"), "original\n");
      },
    });
    assert.equal(existsSync(staging), false);
    assert.equal(await readFile(path.join(final, "original.txt"), "utf8"), "original\n");
    assert.equal(existsSync(path.join(final, "replacement.txt")), false);
    assert.equal(existsSync(original), false);
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("verified publish move locks the exact destination parent", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-publish-parent-${randomUUID()}`);
  const parentAway = `${parent}-away`;
  const staging = path.join(parent, ".run.staging");
  const final = path.join(parent, "run");
  await mkdir(staging, { recursive: true });
  await writeFile(path.join(staging, "original.txt"), "original\n", "utf8");
  const identity = await captureDirectoryIdentity(staging, "test.publish_parent_identity");
  try {
    await publishStagingAtomically(staging, final, {
      expectedIdentity: identity,
      afterMoveLocked: async () => {
        await assertWindowsSharingViolation(() => rename(parent, parentAway));
        assert.equal(existsSync(parentAway), false);
      },
    });
    assert.equal(await readFile(path.join(final, "original.txt"), "utf8"), "original\n");
    assert.equal(existsSync(parentAway), false);
  } finally {
    await rm(parent, { recursive: true, force: false });
    await rm(parentAway, { recursive: true, force: true });
  }
});


test("verified publish move fails closed when the destination leaf is claimed after locking", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-publish-leaf-${randomUUID()}`);
  const staging = path.join(parent, ".run.staging");
  const final = path.join(parent, "run");
  await mkdir(staging, { recursive: true });
  await writeFile(path.join(staging, "original.txt"), "original\n", "utf8");
  const identity = await captureDirectoryIdentity(staging, "test.publish_leaf_identity");
  try {
    await assert.rejects(
      publishStagingAtomically(staging, final, {
        expectedIdentity: identity,
        afterMoveLocked: async () => {
          await mkdir(final, { recursive: false });
          await writeFile(path.join(final, "unknown.txt"), "unknown\n", "utf8");
        },
      }),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "evidence.publish"
        && error.code === "handle_operation",
    );
    assert.equal(await readFile(path.join(staging, "original.txt"), "utf8"), "original\n");
    assert.equal(await readFile(path.join(final, "unknown.txt"), "utf8"), "unknown\n");
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("failed-run cleanup preserves directories until this driver proves ownership", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-owner-${randomUUID()}`);
  const workRoot = path.join(parent, "work");
  const artifactRoot = path.join(parent, "artifacts");
  const work = path.join(workRoot, "same-run");
  const staging = path.join(artifactRoot, ".same-run.staging");
  const final = path.join(artifactRoot, "same-run");
  await Promise.all([
    mkdir(work, { recursive: true }),
    mkdir(staging, { recursive: true }),
    mkdir(final, { recursive: true }),
  ]);
  const ownershipClaim = createOwnershipClaim();
  await Promise.all([
    writeFile(path.join(work, OWNER_CLAIM_FILENAME), `${ownershipClaim.digest}\n`, "ascii"),
    writeFile(path.join(staging, OWNER_CLAIM_FILENAME), `${ownershipClaim.digest}\n`, "ascii"),
  ]);
  const workIdentity = await captureDirectoryIdentity(work, "test.work_identity");
  const stagingIdentity = await captureDirectoryIdentity(staging, "test.staging_identity");
  try {
    assert.equal(await assertOwnershipClaim(work, ownershipClaim, "test.owner"), true);
    assert.deepEqual(
      await cleanupFailedRunDirectories({
        workDirectory: work,
        workRoot,
        stagingDirectory: staging,
        artifactDirectory: final,
        artifactRoot,
        runDirectoriesOwned: false,
        artifactOwned: false,
        ownershipClaim,
        workIdentity,
        stagingIdentity,
        processTreeDrained: true,
      }),
      { directories_removed: 0 },
    );
    assert.equal(existsSync(work) && existsSync(staging) && existsSync(final), true);

    assert.deepEqual(
      await cleanupFailedRunDirectories({
        workDirectory: work,
        workRoot,
        stagingDirectory: staging,
        artifactDirectory: final,
        artifactRoot,
        runDirectoriesOwned: true,
        artifactOwned: false,
        ownershipClaim,
        workIdentity,
        stagingIdentity,
        processTreeDrained: true,
      }),
      { directories_removed: 2 },
    );
    assert.equal(existsSync(work) || existsSync(staging), false);
    assert.equal(existsSync(final), true);
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("failed-run cleanup refuses a same-name directory replaced after ownership", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-replaced-${randomUUID()}`);
  const workRoot = path.join(parent, "work");
  const artifactRoot = path.join(parent, "artifacts");
  const work = path.join(workRoot, "same-run");
  const staging = path.join(artifactRoot, ".same-run.staging");
  const final = path.join(artifactRoot, "same-run");
  const ownershipClaim = createOwnershipClaim();
  await Promise.all([
    mkdir(work, { recursive: true }),
    mkdir(staging, { recursive: true }),
  ]);
  await Promise.all([
    writeFile(path.join(work, OWNER_CLAIM_FILENAME), `${ownershipClaim.digest}\n`, "ascii"),
    writeFile(path.join(staging, OWNER_CLAIM_FILENAME), `${ownershipClaim.digest}\n`, "ascii"),
  ]);
  const workIdentity = await captureDirectoryIdentity(work, "test.work_identity");
  const stagingIdentity = await captureDirectoryIdentity(staging, "test.staging_identity");
  try {
    await rm(work, { recursive: true, force: false });
    await mkdir(work, { recursive: false });
    await writeFile(
      path.join(work, OWNER_CLAIM_FILENAME),
      `${ownershipClaim.digest}\n`,
      "ascii",
    );
    await writeFile(path.join(work, "replacement.txt"), "replacement\n", "utf8");
    await assert.rejects(
      cleanupFailedRunDirectories({
        workDirectory: work,
        workRoot,
        stagingDirectory: staging,
        artifactDirectory: final,
        artifactRoot,
        runDirectoriesOwned: true,
        artifactOwned: false,
        ownershipClaim,
        workIdentity,
        stagingIdentity,
        processTreeDrained: true,
      }),
      (error) => error instanceof BrowserAcceptanceFailure && error.code === "identity",
    );
    assert.equal(
      await readFile(path.join(work, "replacement.txt"), "utf8"),
      "replacement\n",
    );
    assert.equal(existsSync(staging), false);
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("owner validation lstat rejects an unresolved symlink or junction", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-link-${randomUUID()}`);
  const target = path.join(parent, "target");
  const linked = path.join(parent, "linked");
  await mkdir(target, { recursive: true });
  await symlink(target, linked, process.platform === "win32" ? "junction" : "dir");
  try {
    await assert.rejects(
      captureDirectoryIdentity(linked, "test.link"),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "test.link"
        && ["not_plain", "reparse_point"].includes(error.code),
    );
  } finally {
    await rm(linked, { recursive: true, force: false });
    await rm(parent, { recursive: true, force: false });
  }
});


test("recursive remove fails closed without retrying an unknown partially deleted tree", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-retry-${randomUUID()}`);
  const owned = path.join(parent, "owned");
  const ownershipClaim = createOwnershipClaim();
  await mkdir(owned, { recursive: true });
  await writeFile(
    path.join(owned, OWNER_CLAIM_FILENAME),
    `${ownershipClaim.digest}\n`,
    "ascii",
  );
  const identity = await captureDirectoryIdentity(owned, "test.retry_identity");
  let removeCalls = 0;
  try {
    await assert.rejects(
      removeOwnedDirectory(owned, parent, "test.retry", {
        expectedIdentity: identity,
        ownershipClaim,
        deleteOperation: async (target, expectedIdentity, stage) => {
          removeCalls += 1;
           assert.equal(typeof expectedIdentity.inode, "string");
           assert.equal(stage, "test.retry");
           assert.equal(existsSync(target), true);
           // 每次删除都注入失败，验证重试耗尽后只返回固定 remove_failed 坐标。
           throw new Error("injected retry");
        },
      }),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "test.retry"
        && error.code === "remove_failed",
    );
    assert.equal(removeCalls, 1);
    assert.equal(existsSync(owned), false);
    assert.equal(
      (await readdir(parent)).filter((name) => name.startsWith(".prd077-quarantine-")).length,
      1,
    );
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("quarantine deletion never touches a source replacement with a copied claim", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-source-race-${randomUUID()}`);
  const owned = path.join(parent, "owned");
  const ownershipClaim = createOwnershipClaim();
  await mkdir(owned, { recursive: true });
  await Promise.all([
    writeFile(
      path.join(owned, OWNER_CLAIM_FILENAME),
      `${ownershipClaim.digest}\n`,
      "ascii",
    ),
    writeFile(path.join(owned, "original.txt"), "original\n", "utf8"),
  ]);
  const identity = await captureDirectoryIdentity(owned, "test.race_identity");
  let firstDeleteTarget = "";
  try {
    await removeOwnedDirectory(owned, parent, "test.source_race", {
      expectedIdentity: identity,
      ownershipClaim,
      deleteOperation: async (target, expectedIdentity, stage) => {
        firstDeleteTarget ||= target;
        // 该回调发生在 source/claim 校验及原子 quarantine 接管之后、第一次递归删除之前。
        // 攻击者在原路径复制同一 claim；删除器只能继续操作不可预测的 quarantine leaf。
        assert.equal(existsSync(owned), false);
        assert.equal(await readFile(path.join(target, "original.txt"), "utf8"), "original\n");
        assert.deepEqual(await readdir(path.dirname(target)), [path.basename(target)]);
        await mkdir(owned, { recursive: false });
        await Promise.all([
          writeFile(
            path.join(owned, OWNER_CLAIM_FILENAME),
            `${ownershipClaim.digest}\n`,
            "ascii",
          ),
          writeFile(path.join(owned, "replacement.txt"), "replacement\n", "utf8"),
        ]);
        assert.equal(typeof expectedIdentity.inode, "string");
        assert.equal(stage, "test.source_race");
        await rm(target, { recursive: true, force: false });
      },
    });
    assert.notEqual(path.resolve(firstDeleteTarget), path.resolve(owned));
    assert.equal(path.dirname(firstDeleteTarget).startsWith(parent), true);
    assert.equal(await readFile(path.join(owned, "replacement.txt"), "utf8"), "replacement\n");
    assert.equal(existsSync(path.join(owned, "original.txt")), false);
    assert.equal(await assertOwnershipClaim(owned, ownershipClaim, "test.race_replacement"), true);
    await assert.rejects(
      assertDirectoryIdentity(owned, identity, "test.race_replacement_identity"),
      (error) => error instanceof BrowserAcceptanceFailure && error.code === "identity",
    );
    assert.deepEqual(
      (await readdir(parent)).filter((name) => name.startsWith(".prd077-quarantine-")),
      [],
    );
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("quarantine move locks the exact source before moving it", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-handle-race-${randomUUID()}`);
  const owned = path.join(parent, "owned");
  const original = path.join(parent, "original-away");
  const ownershipClaim = createOwnershipClaim();
  await mkdir(owned, { recursive: true });
  await Promise.all([
    writeFile(path.join(owned, OWNER_CLAIM_FILENAME), `${ownershipClaim.digest}\n`, "ascii"),
    writeFile(path.join(owned, "original.txt"), "original\n", "utf8"),
  ]);
  const identity = await captureDirectoryIdentity(owned, "test.handle_race_identity");
  try {
    await removeOwnedDirectory(owned, parent, "test.handle_race", {
      expectedIdentity: identity,
      ownershipClaim,
      afterMoveLocked: async () => {
        await assertWindowsSharingViolation(() => rename(owned, original));
        assert.equal(await readFile(path.join(owned, "original.txt"), "utf8"), "original\n");
      },
    });
    assert.equal(existsSync(owned), false);
    assert.equal(existsSync(original), false);
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("quarantine delete locks every leaf and removes only the exact tree", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-delete-lock-${randomUUID()}`);
  const owned = path.join(parent, "owned");
  const ownershipClaim = createOwnershipClaim();
  await mkdir(path.join(owned, "nested"), { recursive: true });
  await Promise.all([
    writeFile(path.join(owned, OWNER_CLAIM_FILENAME), `${ownershipClaim.digest}\n`, "ascii"),
    writeFile(path.join(owned, "nested", "original.txt"), "original\n", "utf8"),
  ]);
  const identity = await captureDirectoryIdentity(owned, "test.delete_lock_identity");
  let lockedLeaf = "";
  try {
    await removeOwnedDirectory(owned, parent, "test.delete_lock", {
      expectedIdentity: identity,
      ownershipClaim,
      afterDeleteLocked: async () => {
        const quarantine = (await readdir(parent))
          .find((name) => name.startsWith(".prd077-quarantine-"));
        assert.equal(typeof quarantine, "string");
        const [quarantineLeaf] = await readdir(path.join(parent, quarantine));
        lockedLeaf = path.join(parent, quarantine, quarantineLeaf);
        const target = path.join(lockedLeaf, "nested", "original.txt");
        await assertWindowsSharingViolation(() => rename(target, `${target}.away`));
        await assertWindowsSharingViolation(() => writeFile(target, "replacement\n", "utf8"));
        assert.equal(await readFile(target, "utf8"), "original\n");
      },
    });
    assert.equal(Boolean(lockedLeaf), true);
    assert.equal(existsSync(owned), false);
    assert.equal(existsSync(lockedLeaf), false);
    assert.deepEqual(
      (await readdir(parent)).filter((name) => name.startsWith(".prd077-quarantine-")),
      [],
    );
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("stable scan and bound delete support Chrome cache paths beyond MAX_PATH", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-long-profile-${randomUUID()}`);
  const owned = path.join(parent, "owned");
  const sibling = path.join(parent, "sibling");
  const segments = Array.from(
    { length: 4 },
    (_, index) => `chrome-cache-${index}-${"a".repeat(48)}`,
  );
  const nested = path.join(owned, ...segments);
  const cacheEntry = path.join(nested, "cache-entry.bin");
  await Promise.all([
    mkdir(nested, { recursive: true }),
    mkdir(sibling, { recursive: true }),
  ]);
  await Promise.all([
    writeFile(cacheEntry, "public-cache-entry\n", "utf8"),
    writeFile(path.join(sibling, "keep.txt"), "keep\n", "utf8"),
  ]);
  assert.equal(cacheEntry.length > 300, true);
  const identity = await captureDirectoryIdentity(owned, "test.long_profile_identity");
  try {
    assert.deepEqual(
      await scanPrivateTree(owned, [], "test.long_profile_scan"),
      { files_scanned: 1, plaintext_matches: 0 },
    );
    await deleteVerifiedDirectoryTreeAfterRelease(
      owned,
      identity,
      "test.long_profile_delete",
    );
    assert.equal(existsSync(owned), false);
    assert.equal(await readFile(path.join(sibling, "keep.txt"), "utf8"), "keep\n");
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("quarantine delete rejects child replacement and preserves an injected unknown entry", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-delete-child-${randomUUID()}`);
  const owned = path.join(parent, "owned");
  const ownershipClaim = createOwnershipClaim();
  await mkdir(path.join(owned, "nested"), { recursive: true });
  await Promise.all([
    writeFile(path.join(owned, OWNER_CLAIM_FILENAME), `${ownershipClaim.digest}\n`, "ascii"),
    writeFile(path.join(owned, "nested", "original.txt"), "original\n", "utf8"),
  ]);
  const identity = await captureDirectoryIdentity(owned, "test.delete_child_identity");
  let unknown = "";
  try {
    await assert.rejects(
      removeOwnedDirectory(owned, parent, "test.delete_child", {
        expectedIdentity: identity,
        ownershipClaim,
        afterDeleteLocked: async () => {
          const quarantine = (await readdir(parent))
            .find((name) => name.startsWith(".prd077-quarantine-"));
          const [leaf] = await readdir(path.join(parent, quarantine));
          const lockedRoot = path.join(parent, quarantine, leaf);
          const nested = path.join(lockedRoot, "nested");
          await assertWindowsSharingViolation(() => rename(nested, `${nested}.away`));
          unknown = path.join(lockedRoot, "unknown");
          await mkdir(unknown, { recursive: false });
          await writeFile(path.join(unknown, "sentinel.txt"), "unknown\n", "utf8");
          assert.equal(await readFile(path.join(nested, "original.txt"), "utf8"), "original\n");
        },
      }),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "test.delete_child"
        && error.code === "handle_operation",
    );
    assert.equal(existsSync(owned), false);
    assert.equal(await readFile(path.join(unknown, "sentinel.txt"), "utf8"), "unknown\n");
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("quarantine helper failure preserves source replacement and unknown quarantine entries", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-delete-failure-${randomUUID()}`);
  const owned = path.join(parent, "owned");
  const ownershipClaim = createOwnershipClaim();
  await mkdir(owned, { recursive: true });
  await Promise.all([
    writeFile(path.join(owned, OWNER_CLAIM_FILENAME), `${ownershipClaim.digest}\n`, "ascii"),
    writeFile(path.join(owned, "original.txt"), "original\n", "utf8"),
  ]);
  const identity = await captureDirectoryIdentity(owned, "test.delete_failure_identity");
  let unknown = "";
  try {
    await assert.rejects(
      removeOwnedDirectory(owned, parent, "test.delete_failure", {
        expectedIdentity: identity,
        ownershipClaim,
        deleteOperation: async (quarantineLeaf) => {
          await mkdir(owned, { recursive: false });
          await writeFile(path.join(owned, "replacement.txt"), "replacement\n", "utf8");
          unknown = path.join(path.dirname(quarantineLeaf), "unknown");
          await mkdir(unknown, { recursive: false });
          await writeFile(path.join(unknown, "sentinel.txt"), "unknown\n", "utf8");
          throw new BrowserAcceptanceFailure("test.delete_failure", "handle_operation");
        },
      }),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "test.delete_failure"
        && error.code === "handle_operation",
    );
    assert.equal(await readFile(path.join(owned, "replacement.txt"), "utf8"), "replacement\n");
    assert.equal(await readFile(path.join(unknown, "sentinel.txt"), "utf8"), "unknown\n");
    assert.equal(existsSync(path.join(owned, "original.txt")), false);
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("pre-ready failure takes over exact claimed directories only after harness drain", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-pre-ready-${randomUUID()}`);
  const workRoot = path.join(parent, "work");
  const artifactRoot = path.join(parent, "artifacts");
  const work = path.join(workRoot, "same-run");
  const staging = path.join(artifactRoot, ".same-run.staging");
  const final = path.join(artifactRoot, "same-run");
  const ownershipClaim = createOwnershipClaim();
  await Promise.all([mkdir(work, { recursive: true }), mkdir(staging, { recursive: true })]);
  await Promise.all([
    writeFile(path.join(work, OWNER_CLAIM_FILENAME), `${ownershipClaim.digest}\n`, "ascii"),
    writeFile(path.join(staging, OWNER_CLAIM_FILENAME), `${ownershipClaim.digest}\n`, "ascii"),
  ]);
  const options = {
    workDirectory: work,
    workRoot,
    stagingDirectory: staging,
    artifactDirectory: final,
    artifactRoot,
    runDirectoriesOwned: false,
    artifactOwned: false,
    ownershipClaim,
    takeoverAfterDrain: true,
  };
  try {
    await assert.rejects(
      cleanupFailedRunDirectories({ ...options, processTreeDrained: false }),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "cleanup.process_tree"
        && error.code === "not_drained",
    );
    assert.equal(existsSync(work) && existsSync(staging), true);
    assert.deepEqual(
      await cleanupFailedRunDirectories({ ...options, processTreeDrained: true }),
      { directories_removed: 2 },
    );
    assert.equal(existsSync(work) || existsSync(staging), false);
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("undrained browser preserves work while independent staging cleanup continues", async () => {
  const parent = path.join(ROOT, ".tmp", `prd077-browser-undrained-${randomUUID()}`);
  const workRoot = path.join(parent, "work");
  const artifactRoot = path.join(parent, "artifacts");
  const work = path.join(workRoot, "same-run");
  const staging = path.join(artifactRoot, ".same-run.staging");
  const final = path.join(artifactRoot, "same-run");
  const ownershipClaim = createOwnershipClaim();
  await Promise.all([mkdir(work, { recursive: true }), mkdir(staging, { recursive: true })]);
  await Promise.all([
    writeFile(path.join(work, OWNER_CLAIM_FILENAME), `${ownershipClaim.digest}\n`, "ascii"),
    writeFile(path.join(staging, OWNER_CLAIM_FILENAME), `${ownershipClaim.digest}\n`, "ascii"),
  ]);
  const workIdentity = await captureDirectoryIdentity(work, "test.work_identity");
  const stagingIdentity = await captureDirectoryIdentity(staging, "test.staging_identity");
  try {
    await assert.rejects(
      cleanupFailedRunDirectories({
        workDirectory: work,
        workRoot,
        stagingDirectory: staging,
        artifactDirectory: final,
        artifactRoot,
        runDirectoriesOwned: true,
        artifactOwned: false,
        ownershipClaim,
        workIdentity,
        stagingIdentity,
        processTreeDrained: true,
        browserTreeDrained: false,
      }),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "cleanup.failed_work"
        && error.code === "tree_not_drained",
    );
    assert.equal(existsSync(work), true);
    assert.equal(existsSync(staging), false);
  } finally {
    await rm(parent, { recursive: true, force: false });
  }
});


test("browser profile privacy scan rejects nested private representations", async () => {
  const directory = path.join(ROOT, ".tmp", `prd077-browser-profile-${randomUUID()}`);
  const nested = path.join(directory, "Default");
  const privateValues = ["usage", "errors", "cost", "policy"].map(
    (name) => `PRD077_${name.toUpperCase()}_PROFILE_${randomUUID()}`,
  );
  await mkdir(nested, { recursive: true });
  try {
    await writeFile(path.join(nested, "History"), "public-only\n", "utf8");
    assert.deepEqual(
      await scanPrivateTree(directory, privateValues, "test.profile"),
      { files_scanned: 1, plaintext_matches: 0 },
    );
    await Promise.all([
      writeFile(path.join(nested, "History"), privateValues[0], "utf8"),
      writeFile(path.join(nested, "Cookies"), Buffer.from(privateValues[1], "utf16le")),
      writeFile(
        path.join(nested, "Cache"),
        Buffer.from(privateValues[2], "utf8").toString("base64"),
        "ascii",
      ),
      writeFile(
        path.join(nested, "Code Cache"),
        Buffer.from(privateValues[3], "utf8").toString("hex"),
        "ascii",
      ),
    ]);
    await assert.rejects(
      scanPrivateTree(directory, privateValues, "test.profile"),
      BrowserAcceptanceFailure,
    );
  } finally {
    await rm(directory, { recursive: true, force: false });
  }
});


test("private scan tree lock rejects a child directory replacement", async () => {
  const directory = path.join(ROOT, ".tmp", `prd077-browser-scan-directory-${randomUUID()}`);
  const nested = path.join(directory, "Default");
  const original = path.join(directory, "Default-original");
  await mkdir(nested, { recursive: true });
  await writeFile(path.join(nested, "History"), "public-only\n", "utf8");
  let injected = false;
  try {
    assert.deepEqual(
      await scanPrivateTree(directory, [], "test.scan_directory", [], {
        scanHooks: {
          beforeDirectoryOpen: async ({ target }) => {
            if (injected || path.resolve(target) !== path.resolve(nested)) return;
            injected = true;
            await assertWindowsSharingViolation(() => rename(nested, original));
          },
        },
      }),
      { files_scanned: 1, plaintext_matches: 0 },
    );
    assert.equal(injected, true);
    assert.equal(await readFile(path.join(nested, "History"), "utf8"), "public-only\n");
    assert.equal(existsSync(original), false);
  } finally {
    await rm(directory, { recursive: true, force: false });
  }
});


test("private scan tree lock rejects file replacement and write races", async () => {
  for (const hookName of ["beforeFileOpen", "afterFileRead"]) {
    const directory = path.join(ROOT, ".tmp", `prd077-browser-scan-file-${randomUUID()}`);
    const target = path.join(directory, "History");
    const original = path.join(directory, "History-original");
    await mkdir(directory, { recursive: true });
    await writeFile(target, "public-only\n", "utf8");
    let injected = false;
    try {
      assert.deepEqual(
        await scanPrivateTree(directory, [], "test.scan_file", [], {
          scanHooks: {
            [hookName]: async ({ target: observed }) => {
              if (injected || path.resolve(observed) !== path.resolve(target)) return;
              injected = true;
              await assertWindowsSharingViolation(() => rename(target, original));
              await assertWindowsSharingViolation(() => writeFile(target, "replacement\n", "utf8"));
            },
          },
        }),
        { files_scanned: 1, plaintext_matches: 0 },
      );
      assert.equal(injected, true);
      assert.equal(await readFile(target, "utf8"), "public-only\n");
      assert.equal(existsSync(original), false);
    } finally {
      await rm(directory, { recursive: true, force: false });
    }
  }
});


test("artifact privacy scan tree lock rejects file replacement and write races", async () => {
  const directory = path.join(ROOT, ".tmp", `prd077-browser-artifact-race-${randomUUID()}`);
  const target = path.join(directory, "evidence.json");
  const original = `${target}.away`;
  await mkdir(directory, { recursive: false });
  await writeFile(target, "public-only\n", "utf8");
  let injected = false;
  try {
    assert.deepEqual(
      await scanArtifacts(directory, [], null, {
        scanHooks: {
          beforeFileRead: async ({ target: observed }) => {
            if (injected || path.resolve(observed) !== path.resolve(target)) return;
            injected = true;
            await assertWindowsSharingViolation(() => rename(target, original));
            await assertWindowsSharingViolation(() => writeFile(target, "replacement\n", "utf8"));
          },
        },
      }),
      { files_scanned: 1, plaintext_matches: 0 },
    );
    assert.equal(injected, true);
    assert.equal(await readFile(target, "utf8"), "public-only\n");
    assert.equal(existsSync(original), false);
  } finally {
    await rm(directory, { recursive: true, force: false });
  }
});


test("manifest verification tree lock rejects readback and digest replacement races", async () => {
  for (const hookName of ["beforeManifestRead", "beforeFileDigest"]) {
    const directory = path.join(ROOT, ".tmp", `prd077-browser-manifest-race-${randomUUID()}`);
    const artifact = path.join(directory, "alpha.json");
    const manifestPath = path.join(directory, "sha256-manifest.json");
    const provenance = sourceProvenanceFixture();
    await mkdir(directory, { recursive: false });
    await writeFile(artifact, "alpha\n", "utf8");
    const manifest = await buildManifest(directory, ["alpha.json"], provenance);
    await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
    let injected = false;
    try {
      assert.deepEqual(
        await verifyPublishedManifest(directory, ["alpha.json"], provenance, null, {
          scanHooks: {
            [hookName]: async (context) => {
              if (injected) return;
              const target = hookName === "beforeManifestRead" ? manifestPath : context.target;
              injected = true;
              await assertWindowsSharingViolation(() => rename(target, `${target}.away`));
              await assertWindowsSharingViolation(() => writeFile(target, "replacement\n", "utf8"));
            },
          },
        }),
        { files_verified: 2 },
      );
      assert.equal(injected, true);
      assert.equal(await readFile(artifact, "utf8"), "alpha\n");
      assert.equal(existsSync(`${artifact}.away`), false);
      assert.equal(existsSync(`${manifestPath}.away`), false);
    } finally {
      await rm(directory, { recursive: true, force: false });
    }
  }
});


test("live work privacy scopes source leaves to the authoritative database", async () => {
  const directory = path.join(ROOT, ".tmp", `prd077-browser-work-${randomUUID()}`);
  const logs = path.join(directory, "logs");
  const database = path.join(directory, "business.sqlite3");
  const wal = `${database}-wal`;
  const otherDatabase = path.join(directory, "other.sqlite3");
  const log = path.join(logs, "control.log");
  const sourceLeaf = `PRD077_SOURCE_LEAF_${randomUUID()}`;
  const privateConfig = createPrivateConfiguration();
  const tracker = createTracker(Object.values(privateConfig));
  tracker.browserTokens.add(`PRD077_BROWSER_TOKEN_${randomUUID()}`);
  registerAnswerLeafValues(tracker, "usage", [sourceLeaf]);
  const responseBody = {
    body: JSON.stringify(assistantResponsePayload("usage_statistics")),
    base64Encoded: false,
  };
  registerAssistantResponseBody(tracker, responseBody);

  await mkdir(logs, { recursive: true });
  try {
    await Promise.all([
      writeFile(database, sourceLeaf, "utf8"),
      writeFile(wal, sourceLeaf, "utf8"),
      writeFile(log, "public-only\n", "utf8"),
    ]);
    const scope = buildLiveWorkPrivacyScope(privateConfig, tracker, directory);
    /** 按当前 live-work 权限范围执行隐私扫描，便于复用同一权威来源集合。 */
    const scan = () => scanPrivateTree(
      directory,
      scope.privateValues,
      "test.work",
      scope.privateLabels,
      {
        authoritativeSourceFiles: scope.authoritativeSourceFiles,
        sourceScopedValues: scope.sourceScopedValues,
        sourceScopedLabels: scope.sourceScopedLabels,
      },
    );
    assert.deepEqual(await scan(), { files_scanned: 3, plaintext_matches: 0 });

    await writeFile(log, sourceLeaf, "utf8");
    await assert.rejects(
      scan(),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "test.work.answer_leaf"
        && error.code === "plaintext_found",
    );

    await writeFile(log, "public-only\n", "utf8");
    await writeFile(otherDatabase, sourceLeaf, "utf8");
    await assert.rejects(
      scan(),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "test.work.answer_leaf"
        && error.code === "plaintext_found",
    );
    await rm(otherDatabase, { force: false });

    await writeFile(database, `${sourceLeaf}\n${responseBody.body}`, "utf8");
    await assert.rejects(
      scan(),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "test.work.answer_response"
        && error.code === "plaintext_found",
    );

    await writeFile(database, `${sourceLeaf}\n${privateConfig.answerCanary}`, "utf8");
    await assert.rejects(
      scan(),
      (error) => error instanceof BrowserAcceptanceFailure
        && error.stage === "test.work.answer_canary"
        && error.code === "plaintext_found",
    );
  } finally {
    await rm(directory, { recursive: true, force: false });
  }
});
