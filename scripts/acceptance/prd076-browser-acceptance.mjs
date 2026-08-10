/**
 * PRD-076 PAGE-106 Response Cache 真实 Browser 验收器。
 *
 * 验收器从空 0069 SQLite 启动真实 Gateway、Control、Console 和回环 Provider，
 * 再用隔离 Chromium 完成双视口、真实策略写入、CAS 冲突、标签失效、调用日志与
 * 动态撤权验收。所有私有运行材料只存在于进程内存或仓库内 .tmp，最终只发布
 * exact 白名单 JSON、PNG 和 SHA-256 清单。
 */

import { spawn, spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import {
  lstat,
  mkdir,
  readdir,
  readFile,
  realpath,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "node:net";

import {
  CdpClient,
  createPageSession,
  evaluate,
  launchEdge,
  runAndWaitForPageLoad,
  setViewport,
  waitForChildExit,
  waitForDebugger,
  waitForDom,
  waitForTrackerCount,
} from "./prd066-browser-acceptance.mjs";
import { inspectPngPixels } from "./prd056-browser-acceptance.mjs";


// 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
const WORK_ROOT = path.join(REPOSITORY_ROOT, ".tmp", "prd076-browser");
const ARTIFACT_ROOT = path.join(REPOSITORY_ROOT, ".artifacts", "prd076-browser");
const PYTHON = path.join(REPOSITORY_ROOT, ".venv", "Scripts", "python.exe");
const CHROME = path.join(
  REPOSITORY_ROOT,
  ".tools",
  "chrome-for-testing",
  "chrome-win64",
  "chrome.exe",
);
// 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
const DEBUG_PORT = 19076;
const CONSOLE_ORIGIN = "http://localhost:5173";
const CONTROL_ORIGIN = "http://localhost:8001";
const GATEWAY_ORIGIN = "http://localhost:8000";
const ALLOWED_ORIGINS = new Set([CONSOLE_ORIGIN, CONTROL_ORIGIN, GATEWAY_ORIGIN]);
const VIEWPORTS = Object.freeze({
  desktop: Object.freeze({ name: "desktop-1280x720", width: 1280, height: 720 }),
  mobile: Object.freeze({ name: "mobile-390x844", width: 390, height: 844 }),
});
const RESPONSE_CACHE_PERMISSIONS = Object.freeze([
  "response_cache.read",
  "response_cache.write",
  "response_cache.invalidate",
]);
const PUBLIC_POLICY = Object.freeze({
  dataClass: "fixture-public",
  tag: "fixture-tag",
  initialTtl: 900,
  draftTtl: 901,
  concurrentTtl: 902,
});
// 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
const AUTH_STORAGE_KEYS = Object.freeze(["metahub.auth_token", "metahub.auth_context"]);
const SCREENSHOT_MASK_LABELS = Object.freeze(["request_id", "trace_id", "租户", "用户"]);
const SAFE_PERMISSION_CODE = /^[a-z][a-z0-9_.-]{1,80}$/;
const SAFE_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$/;
const SAFE_FAILURE_COORDINATE = /^[a-z][a-z0-9_.-]{1,80}$/;
const FORBIDDEN_QUERY_KEY = /tenant|password|token|secret|authorization|prompt|response|endpoint|provider_key|cache_key|data_class/i;
const FORBIDDEN_EVIDENCE_KEYS = new Set([
  "authorization",
  "token",
  "access_token",
  "password",
  "secret",
  "api_key",
  "provider_key",
  "provider_api_key",
  "virtual_key",
  "prompt",
  "prompt_text",
  "response",
  "response_body",
  "response_content",
  "response_payload",
  "content",
  "body",
  "raw_body",
  "payload",
  "endpoint",
  "endpoint_url",
  "base_url",
  "database_path",
  "database_url",
  "cache_key",
  "digest",
  "nonce",
  "ciphertext",
  "tenant_id",
  "user_id",
  "email",
]);
// 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
const MAX_PRIVATE_STREAM_BYTES = 16 * 1024 * 1024;
const MAX_PRIVATE_SCAN_FILE_BYTES = 128 * 1024 * 1024;


/** 失败只保留源码固定 stage/code，底层异常、URL、正文和 Header 均不得外泄。 */
class BrowserAcceptanceFailure extends Error {
  constructor(stage, code) {
    super(`${stage}:${code}`);
    this.name = "BrowserAcceptanceFailure";
    this.stage = stage;
    this.code = code;
  }
}


/** 把共享 Browser helper 的同名错误安全折叠到本模块，拒绝传播动态 message/stack。 */
function normalizeBrowserFailure(error, fallbackStage = "browser.acceptance") {
  if (error instanceof BrowserAcceptanceFailure) return error;
  if (error?.name === "BrowserAcceptanceFailure"
    && SAFE_FAILURE_COORDINATE.test(String(error.stage ?? ""))
    && SAFE_FAILURE_COORDINATE.test(String(error.code ?? ""))) {
    return new BrowserAcceptanceFailure(error.stage, error.code);
  }
  return new BrowserAcceptanceFailure(fallbackStage, "unexpected");
}


/** 统一失败关闭断言，调用方不得把运行值拼进 stage/code。 */
function requireCondition(condition, stage, code) {
  if (!condition) throw new BrowserAcceptanceFailure(stage, code);
}


/** run-id 只允许一个短 ASCII 路径段，禁止覆盖、绝对路径和父目录跳转。 */
function safeRunId(value) {
  const candidate = value || `run-${new Date().toISOString().replaceAll(/[-:.]/g, "").replace("Z", "Z")}`;
  requireCondition(SAFE_RUN_ID.test(candidate), "config.run_id", "invalid");
  requireCondition(!candidate.includes(".."), "config.run_id", "invalid");
  return candidate;
}


/** 把一个 run-id 绑定到给定根目录的直属子目录。 */
function resolveRunDirectory(root, runId) {
  const normalized = safeRunId(runId);
  const target = path.resolve(root, normalized);
  const relative = path.relative(path.resolve(root), target);
  requireCondition(
    relative === normalized && !path.isAbsolute(relative) && !relative.includes(path.sep),
    "config.run_directory",
    "outside_root",
  );
  return target;
}


/** 校验目录是精确普通目录而非符号链接或 Windows reparse point。 */
async function assertPlainDirectory(directory, stage) {
  let information;
  let canonical;
  try {
    information = await lstat(directory);
    canonical = await realpath(directory);
  } catch {
    throw new BrowserAcceptanceFailure(stage, "missing_or_unreadable");
  }
  requireCondition(information.isDirectory() && !information.isSymbolicLink(), stage, "not_plain");
  requireCondition(
    path.normalize(canonical).toLowerCase() === path.normalize(directory).toLowerCase(),
    stage,
    "reparse_point",
  );
  return directory;
}


/** 独占创建本次 .tmp 工作树；正式 artifact 目标必须尚不存在。 */
async function createRunDirectories(runId) {
  const temporaryRoot = path.dirname(WORK_ROOT);
  const artifactParent = path.dirname(ARTIFACT_ROOT);
  await mkdir(temporaryRoot, { recursive: true });
  await mkdir(artifactParent, { recursive: true });
  await assertPlainDirectory(temporaryRoot, "config.tmp_root");
  await assertPlainDirectory(artifactParent, "config.artifacts_root");
  if (!existsSync(WORK_ROOT)) await mkdir(WORK_ROOT, { recursive: false });
  if (!existsSync(ARTIFACT_ROOT)) await mkdir(ARTIFACT_ROOT, { recursive: false });
  await assertPlainDirectory(WORK_ROOT, "config.work_root");
  await assertPlainDirectory(ARTIFACT_ROOT, "config.artifact_root");
  const workDirectory = resolveRunDirectory(WORK_ROOT, runId);
  const artifactDirectory = resolveRunDirectory(ARTIFACT_ROOT, runId);
  const stagingDirectory = path.join(ARTIFACT_ROOT, `.${runId}.staging`);
  requireCondition(
    !existsSync(workDirectory) && !existsSync(artifactDirectory) && !existsSync(stagingDirectory),
    "config.run_directory",
    "already_exists",
  );
  await mkdir(workDirectory, { recursive: false });
  for (const name of ["browser", "screenshots", "runtime"]) {
    await mkdir(path.join(workDirectory, name), { recursive: false });
  }
  for (const name of ["logs", "temp", "usage-spool"]) {
    await mkdir(path.join(workDirectory, "runtime", name), { recursive: false });
  }
  return Object.freeze({ artifactDirectory, stagingDirectory, workDirectory });
}


/** 只删除本次独占目录；调用前再次验证其直属 owner，绝不枚举或清理其它批次。 */
async function removeOwnedDirectory(directory, root, stage) {
  if (!directory || !existsSync(directory)) return;
  const relative = path.relative(path.resolve(root), path.resolve(directory));
  requireCondition(
    SAFE_RUN_ID.test(relative.replace(/^\./, "x"))
      && !relative.includes(path.sep)
      && !path.isAbsolute(relative),
    stage,
    "outside_root",
  );
  await rm(directory, { recursive: true, force: false });
  requireCondition(!existsSync(directory), stage, "remove_failed");
}


/**
 * 工作流或清理任一失败时删除私有 work/profile 与未发布 staging；可注入根目录只用于纯合同测试。
 */
async function cleanupFailedRunDirectories(
  runDirectories,
  workflow,
  cleanupFailure,
  { workRoot = WORK_ROOT, artifactRoot = ARTIFACT_ROOT } = {},
) {
  if (workflow && !cleanupFailure) return cleanupFailure;
  let failure = cleanupFailure;
  try {
    await removeOwnedDirectory(runDirectories.workDirectory, workRoot, "cleanup.failed_work");
  } catch {
    failure ??= new BrowserAcceptanceFailure("cleanup.failed_work", "remove_failed");
  }
  try {
    await removeOwnedDirectory(runDirectories.stagingDirectory, artifactRoot, "cleanup.failed_staging");
  } catch {
    failure ??= new BrowserAcceptanceFailure("cleanup.failed_staging", "remove_failed");
  }
  return failure;
}


/** 只接受三个冻结本地 Origin，避免 CLI 把验收器变成任意网络客户端。 */
function normalizeLoopbackOrigin(value, expectedPort, stage = "config.origin") {
  let parsed;
  try {
    parsed = new URL(String(value));
  } catch {
    throw new BrowserAcceptanceFailure(stage, "invalid");
  }
  requireCondition(parsed.protocol === "http:", stage, "protocol");
  requireCondition(parsed.hostname === "localhost", stage, "host");
  requireCondition(parsed.port === String(expectedPort), stage, "port");
  requireCondition(!parsed.username && !parsed.password, stage, "credentials");
  requireCondition(parsed.pathname === "/" && !parsed.search && !parsed.hash, stage, "shape");
  return parsed.origin;
}


/** 纯函数复核浏览器请求只指向固定服务，且 query 不携带身份或缓存私有输入。 */
function browserRequestAllowed(value, allowedOrigins = ALLOWED_ORIGINS) {
  let target;
  try {
    target = new URL(String(value));
  } catch {
    return false;
  }
  if (["about:", "data:", "blob:"].includes(target.protocol)) return true;
  return allowedOrigins.has(target.origin)
    && !target.username
    && !target.password
    && ![...target.searchParams.keys()].some((key) => FORBIDDEN_QUERY_KEY.test(key));
}


/** 只有 PRD-076 的策略与失效端点声明 exact no-store/private 边界。 */
function requiresResponseCachePrivateBoundary(pathname) {
  return pathname === "/cache-policies" || pathname === "/cache/invalidate";
}


/** 解析唯一 --run-id；路径、端口、Origin 和账号都不允许由 CLI 覆盖。 */
function parseArguments(argv = process.argv.slice(2)) {
  let runId = "";
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value !== "--run-id" || runId || index + 1 >= argv.length) {
      throw new BrowserAcceptanceFailure("config.arguments", "invalid");
    }
    runId = argv[index + 1];
    index += 1;
  }
  return Object.freeze({ runId: safeRunId(runId) });
}


/**
 * 为一个私有值生成验收期间常见的落盘表示。扫描只保留字节 needle，永不把命中
 * 内容、文件名或私有值写入证据与错误输出。
 */
function sensitiveByteRepresentations(value) {
  const source = String(value ?? "");
  requireCondition(source.length >= 8, "privacy.value", "too_short");
  const utf8 = Buffer.from(source, "utf8");
  const utf16le = Buffer.from(source, "utf16le");
  const uriEncoded = encodeURIComponent(source);
  const formEncoded = new URLSearchParams({ value: source }).toString().slice("value=".length);
  const base64 = utf8.toString("base64");
  const base64url = base64.replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
  const hex = utf8.toString("hex");
  const candidates = [
    utf8,
    utf16le,
    Buffer.from(utf16le).swap16(),
    Buffer.from(uriEncoded, "ascii"),
    Buffer.from(uriEncoded.replace(/%[0-9A-F]{2}/g, (match) => match.toLowerCase()), "ascii"),
    Buffer.from(formEncoded, "ascii"),
    Buffer.from(base64, "ascii"),
    Buffer.from(base64url, "ascii"),
    Buffer.from(hex, "ascii"),
    Buffer.from(hex.toUpperCase(), "ascii"),
  ];
  const unique = new Map();
  for (const candidate of candidates) unique.set(candidate.toString("hex"), candidate);
  return Object.freeze([...unique.values()]);
}


/** 返回当前字节块中命中的不同私有 canary 数量，不返回命中位置或内容。 */
function countPrivateValueViolations(content, privateValues) {
  const bytes = Buffer.isBuffer(content) ? content : Buffer.from(content ?? "");
  const values = [...new Set((privateValues ?? []).filter((value) => typeof value === "string" && value.length > 0))];
  return values.reduce(
    (count, value) => count + (sensitiveByteRepresentations(value).some((needle) => bytes.includes(needle)) ? 1 : 0),
    0,
  );
}


/**
 * 停服后扫描 Runtime 的日志、spool 与临时文件。SQLite 正库由 Runtime 验收器的
 * 专用加密门禁负责；这里排除正库与 sidecar，避免把可信业务字段误判成进程日志泄漏。
 */
async function scanRuntimePrivateFiles(runtimeDirectory, privateValues) {
  await assertPlainDirectory(runtimeDirectory, "privacy.runtime_directory");
  const files = [];
  const visit = async (directory) => {
    const entries = await readdir(directory, { withFileTypes: true });
    entries.sort((left, right) => left.name.localeCompare(right.name, "en"));
    for (const entry of entries) {
      const target = path.join(directory, entry.name);
      const information = await lstat(target);
      requireCondition(!information.isSymbolicLink(), "privacy.runtime_files", "reparse_point");
      if (information.isDirectory()) {
        await visit(target);
      } else if (information.isFile()
        && !entry.name.toLowerCase().endsWith(".png")
        && !entry.name.toLowerCase().endsWith(".sqlite3")
        && !entry.name.toLowerCase().endsWith("-wal")
        && !entry.name.toLowerCase().endsWith("-shm")
        && !entry.name.toLowerCase().endsWith("-journal")) {
        requireCondition(
          information.size <= MAX_PRIVATE_SCAN_FILE_BYTES,
          "privacy.runtime_files",
          "file_too_large",
        );
        files.push(target);
      }
    }
  };
  try {
    await visit(runtimeDirectory);
    let violationCount = 0;
    for (const file of files) {
      violationCount += countPrivateValueViolations(await readFile(file), privateValues);
    }
    return Object.freeze({ filesScanned: files.length, violationCount });
  } catch (error) {
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure("privacy.runtime_files", "filesystem");
  }
}


/*
 * Python 子进程只负责本批次空库、真实服务和回环 Provider 的生命周期。它使用
 * PRD-076 Runtime 已审核 helper，向 Node 发送一次性内存配置与聚合调用计数；任何
 * stderr 或底层异常都不会进入终端和证据，但 stderr 会在内存中接受私有 canary 扫描。
 */
// 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
const RUNTIME_HARNESS = String.raw`
import dataclasses
import importlib.util
import json
import sys
from pathlib import Path

root = Path.cwd().resolve()
work = Path(sys.argv[1]).resolve()
module_path = root / "scripts" / "acceptance" / "prd076-runtime-acceptance.py"
spec = importlib.util.spec_from_file_location("_metahub_prd076_browser_runtime", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

def emit(state, **values):
    print(json.dumps({"channel": "prd076-browser", "state": state, **values}, separators=(",", ":")), flush=True)

processes = module.OwnedProcesses()
provider = None
failed = False
try:
    provenance = module.capture_source_provenance()
    snapshot = module.create_migration_snapshot(work, provenance)
    secrets = module.RuntimeSecrets.create()
    provider = module.ProviderStub(key=secrets.provider_key, answer=secrets.response_canary)
    provider_url = provider.start()
    graph = module.prepare_business_database(work, secrets, provider_url, snapshot)
    environment = module.service_environment(work, graph, secrets)
    gateway = processes.add(module.start_service("gateway", "metahub_gateway.app:app", module.GATEWAY_PORT, work=work, environment=environment))
    module.wait_health("http://127.0.0.1:8000/health", gateway, stage="browser.health.gateway")
    control = processes.add(module.start_service("control", "metahub_control.app:app", module.CONTROL_PORT, work=work, environment=environment))
    module.wait_health("http://127.0.0.1:8001/health", control, stage="browser.health.control")
    console = processes.add(module.start_console(work=work, environment=environment))
    module.wait_health("http://127.0.0.1:5173/", console, stage="browser.health.console")
    private_values = [
        str(getattr(secrets, field.name))
        for field in dataclasses.fields(secrets)
        # 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
        if isinstance(getattr(secrets, field.name), str)
    ]
    private_values.extend([str(graph.database), graph.database_url])
    emit(
        "ready",
        admin_password=secrets.admin_password,
        finance_password=secrets.finance_password,
        viewer_password=secrets.viewer_password,
        virtual_key=secrets.virtual_key,
        prompt_value=secrets.prompt_canary,
        response_value=secrets.response_canary,
        tenant_value=graph.tenant_id,
        model_alias=graph.model_alias,
        model_alias_id=graph.model_alias_id,
        private_values=private_values,
    )
    for line in sys.stdin:
        try:
            command = json.loads(line)
        except Exception:
            emit("invalid")
            continue
        operation = command.get("operation") if isinstance(command, dict) else None
        # 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
        if operation == "provider_stats":
            emit("provider_stats", **provider.snapshot())
        elif operation == "stop":
            emit("stopping")
            break
        else:
            emit("invalid")
except BaseException:
    failed = True
    emit("failed")
finally:
    try:
        module.stop_runtime_owners(processes, *([provider] if provider is not None else []))
    except BaseException:
        failed = True

raise SystemExit(1 if failed else 0)
`;


/** 按行消费子进程的私有内存协议；stderr 只做有界内存扫描，不记录原文。 */
class RuntimeHarnessClient {
  constructor(child) {
    this.child = child;
    this.buffer = "";
    this.messages = [];
    this.waiters = [];
    this.closed = false;
    this.stdioClosed = false;
    this.stdioCloseWaiters = [];
    this.privateNeedleGroups = [];
    this.detectedPrivateValues = new Set();
    this.stderrBuffer = [];
    this.stderrBufferedBytes = 0;
    this.stderrOverflow = false;
    this.stderrTail = Buffer.alloc(0);
    this.maximumNeedleLength = 0;
    child.stdout.on("data", (chunk) => this.#accept(String(chunk ?? "")));
    child.stderr.on("data", (chunk) => this.#acceptStderr(chunk));
    child.once("exit", () => {
      this.closed = true;
      while (this.waiters.length) {
        this.waiters.shift().reject(new BrowserAcceptanceFailure("runtime.protocol", "closed"));
      }
    });
    child.once("close", () => {
      this.stdioClosed = true;
      while (this.stdioCloseWaiters.length) this.stdioCloseWaiters.shift()(true);
    });
  }

  #accept(value) {
    this.buffer += value;
    requireCondition(this.buffer.length <= 1024 * 1024, "runtime.protocol", "oversize");
    while (this.buffer.includes("\n")) {
      const index = this.buffer.indexOf("\n");
      const line = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + 1);
      let parsed;
      try {
        parsed = JSON.parse(line);
      } catch {
        continue;
      }
      if (parsed?.channel !== "prd076-browser") continue;
      const waiter = this.waiters.shift();
      if (waiter) waiter.resolve(parsed);
      else this.messages.push(parsed);
    }
  }

  #acceptStderr(chunk) {
    const bytes = Buffer.isBuffer(chunk) ? Buffer.from(chunk) : Buffer.from(String(chunk ?? ""), "utf8");
    if (!this.privateNeedleGroups.length) {
      const remaining = MAX_PRIVATE_STREAM_BYTES - this.stderrBufferedBytes;
      if (remaining > 0) {
        const accepted = bytes.subarray(0, remaining);
        this.stderrBuffer.push(Buffer.from(accepted));
        this.stderrBufferedBytes += accepted.length;
      }
      if (bytes.length > remaining) this.stderrOverflow = true;
      return;
    }
    this.#scanStderr(bytes);
  }

  #scanStderr(bytes) {
    const combined = this.stderrTail.length
      ? Buffer.concat([this.stderrTail, bytes])
      : bytes;
    for (let index = 0; index < this.privateNeedleGroups.length; index += 1) {
      if (this.detectedPrivateValues.has(index)) continue;
      if (this.privateNeedleGroups[index].some((needle) => combined.includes(needle))) {
        this.detectedPrivateValues.add(index);
      }
    }
    const tailLength = Math.max(0, this.maximumNeedleLength - 1);
    this.stderrTail = tailLength > 0
      ? Buffer.from(combined.subarray(Math.max(0, combined.length - tailLength)))
      : Buffer.alloc(0);
  }

  setPrivateValues(privateValues) {
    requireCondition(this.privateNeedleGroups.length === 0, "runtime.privacy", "already_configured");
    const values = [...new Set(
      (privateValues ?? []).filter((value) => typeof value === "string" && value.length > 0),
    )];
    requireCondition(values.length > 0, "runtime.privacy", "values_missing");
    this.privateNeedleGroups = values.map(sensitiveByteRepresentations);
    this.maximumNeedleLength = Math.max(
      ...this.privateNeedleGroups.flatMap((group) => group.map((needle) => needle.length)),
    );
    const buffered = Buffer.concat(this.stderrBuffer, this.stderrBufferedBytes);
    this.stderrBuffer = [];
    this.stderrBufferedBytes = 0;
    this.#scanStderr(buffered);
  }

  get processPrivacyViolationCount() {
    return this.detectedPrivateValues.size + (this.stderrOverflow ? 1 : 0);
  }

  async waitForStdioClose(timeoutMs = 5000) {
    if (this.stdioClosed) return;
    const closed = await Promise.race([
      new Promise((resolve) => this.stdioCloseWaiters.push(resolve)),
      new Promise((resolve) => setTimeout(() => resolve(false), timeoutMs)),
    ]);
    requireCondition(closed === true || this.stdioClosed, "runtime.stdio", "close_timeout");
  }

  async next(stage, timeoutMs = 120000) {
    if (this.messages.length) return this.messages.shift();
    requireCondition(!this.closed, stage, "closed");
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const index = this.waiters.findIndex((item) => item.resolve === wrappedResolve);
        if (index >= 0) this.waiters.splice(index, 1);
        reject(new BrowserAcceptanceFailure(stage, "timeout"));
      }, timeoutMs);
      /** 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。 */
      const wrappedResolve = (value) => {
        clearTimeout(timer);
        resolve(value);
      };
      /** 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。 */
      const wrappedReject = (error) => {
        clearTimeout(timer);
        reject(error);
      };
      this.waiters.push({ resolve: wrappedResolve, reject: wrappedReject });
    });
  }

  async command(operation, expectedState) {
    requireCondition(!this.closed && this.child.stdin.writable, "runtime.protocol", "not_writable");
    this.child.stdin.write(`${JSON.stringify({ operation })}\n`);
    const message = await this.next(`runtime.${operation}`, 30000);
    requireCondition(message?.state === expectedState, `runtime.${operation}`, "response");
    return message;
  }

  stop() {
    if (!this.closed && this.child.stdin.writable) {
      this.child.stdin.write(`${JSON.stringify({ operation: "stop" })}\n`);
      this.child.stdin.end();
    }
  }
}


/** 启动唯一 Python Runtime owner，并校验 ready 消息所需字段但不输出其值。 */
async function startRuntime(workDirectory) {
  requireCondition(existsSync(PYTHON), "runtime.python", "missing");
  const child = spawn(PYTHON, ["-c", RUNTIME_HARNESS, path.join(workDirectory, "runtime")], {
    cwd: REPOSITORY_ROOT,
    env: {
      PATH: process.env.PATH ?? "",
      PATHEXT: process.env.PATHEXT ?? "",
      SYSTEMROOT: process.env.SYSTEMROOT ?? "",
      WINDIR: process.env.WINDIR ?? "",
      COMSPEC: process.env.COMSPEC ?? "",
      PYTHONDONTWRITEBYTECODE: "1",
    },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  const client = new RuntimeHarnessClient(child);
  try {
    const ready = await client.next("runtime.start", 180000);
    const privateValues = Array.isArray(ready?.private_values)
      ? ready.private_values.filter((item) => typeof item === "string" && item.length > 0)
      : [];
    requireCondition(
      ready?.state === "ready"
        && typeof ready.admin_password === "string" && ready.admin_password.length >= 12
        && typeof ready.finance_password === "string" && ready.finance_password.length >= 12
        && typeof ready.viewer_password === "string" && ready.viewer_password.length >= 12
        && typeof ready.virtual_key === "string" && ready.virtual_key.length >= 20
        && typeof ready.prompt_value === "string" && ready.prompt_value.length >= 20
        && typeof ready.response_value === "string" && ready.response_value.length >= 20
        && typeof ready.tenant_value === "string" && ready.tenant_value.length > 0
        && typeof ready.model_alias === "string" && ready.model_alias.length > 0
        && typeof ready.model_alias_id === "string" && ready.model_alias_id.length > 0
        && privateValues.length >= 10,
      "runtime.start",
      "contract",
    );
    client.setPrivateValues(privateValues);
    return Object.freeze({ child, client, ready: Object.freeze({ ...ready, privateValues }) });
  } catch (error) {
    client.stop();
    await waitForChildExit(child, 30000);
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (error instanceof BrowserAcceptanceFailure) throw error;
    throw new BrowserAcceptanceFailure("runtime.start", "failed");
  }
}


/** 构造浏览器追踪器，只保留状态码、计数和内存敏感集合。 */
function createTracker(runtime) {
  return {
    activeRequestIds: new Set(),
    authMeStatuses: [],
    browserTokens: new Set(),
    cacheGetStatuses: [],
    cachePatchStatuses: [],
    canceledNetworkFailureCodes: new Set(),
    consoleNetworkFailureCodes: [],
    consolePrivacyViolationCount: 0,
    edgeProcessErrorCount: 0,
    expectedHttpErrorCount: 0,
    externalRequestCount: 0,
    forbiddenQueryCount: 0,
    fetchCanceledResolutionCount: 0,
    fetchResolutionClassifiedCount: 0,
    fetchResolutionFailureCount: 0,
    fetchResolutionFailures: [],
    fetchResolutionPromises: new Set(),
    fetchStaleProtocolCount: 0,
    invalidationStatuses: [],
    loginStatuses: [],
    logoutStatuses: [],
    navigationStatuses: [],
    processPrivacyViolationCount: 0,
    get runtimeProcessPrivacyViolationCount() {
      return runtime.client.processPrivacyViolationCount;
    },
    requestLogDetailStatuses: [],
    requestLogListStatuses: [],
    requestCategories: new Map(),
    requestMethods: new Map(),
    networkFailedRequestIds: new Set(),
    requestPrivacyViolationCount: 0,
    runtimeExceptionCount: 0,
    secrets: [...runtime.ready.privateValues],
    unexpectedConsoleErrorCount: 0,
    unexpectedConsoleHttpStatuses: [],
    unexpectedConsoleOtherCount: 0,
    uncanceledNetworkFailureCategories: new Set(),
    uncanceledNetworkFailureCodes: new Set(),
  };
}


/** 大小写无关读取 CDP Header，不把返回值写入证据。 */
function readHeader(headers, name) {
  const expected = String(name).toLowerCase();
  for (const [key, value] of Object.entries(headers ?? {})) {
    if (key.toLowerCase() === expected) return String(value ?? "");
  }
  return "";
}


/** 把请求折叠为固定业务类别，诊断不得保留 URL、查询参数或资源标识。 */
function trackedRequestCategory(value) {
  let target;
  try { target = new URL(value); } catch { return "invalid"; }
  // CDP 也会报告 about/data/blob 生命周期；它们没有网络源，不能误标成实际外部请求。
  if (["about:", "data:", "blob:"].includes(target.protocol)) return "browser_internal";
  if (target.origin === CONSOLE_ORIGIN) return "console_asset";
  if (target.origin === GATEWAY_ORIGIN && target.pathname === "/v1/chat/completions") {
    return "gateway_chat";
  }
  if (target.origin !== CONTROL_ORIGIN) return "external";
  // 认证请求只映射为固定动作类别，绝不保留账号、令牌或请求正文。
  if (target.pathname === "/auth/login") return "auth_login";
  if (target.pathname === "/auth/logout") return "auth_logout";
  if (target.pathname === "/console/navigation") return "navigation";
  if (target.pathname === "/cache-policies") return "cache_policy";
  if (target.pathname === "/cache/invalidate") return "cache_invalidate";
  if (target.pathname === "/request-logs" || target.pathname.startsWith("/request-logs/")) {
    return "request_log";
  }
  const publicGroup = target.pathname.split("/").filter(Boolean)[0] ?? "";
  if ([
    "auth",
    "budgets",
    "config",
    "console",
    "dashboard",
    "error-codes",
    "health",
    "models",
    "providers",
    "tenants",
    "usage",
    "users",
  ].includes(publicGroup)) return `control_${publicGroup.replaceAll("-", "_")}`;
  return "control_other";
}


/**
 * Fetch 回写失败只有在同一 networkId 已收到 Network.loadingFailed 时才属于导航取消；
 * 其余失败保持阻断。异步分类 Promise 会在最终隐私门禁前全部收敛。
 */
function trackFetchResolution(operation, networkId, tracker) {
  const classification = operation.catch((error) => {
    tracker.fetchResolutionFailures.push(Object.freeze({
      networkId: typeof networkId === "string" ? networkId : "",
      staleProtocol: isStaleFetchProtocolFailure(error),
    }));
  });
  tracker.fetchResolutionPromises.add(classification);
  classification.finally(() => tracker.fetchResolutionPromises.delete(classification));
}


/** 只接受两个固定 Fetch 命令的 protocol 拒绝作为 interception 已失效证明。 */
function isStaleFetchProtocolFailure(error) {
  return error?.name === "BrowserAcceptanceFailure"
    && [
      "browser.cdp.Fetch.continueRequest",
      "browser.cdp.Fetch.failRequest",
    ].includes(String(error.stage ?? ""))
    && error.code === "protocol";
}


/** 在隐私断言前一次性分类尚未处理的 Fetch 回写失败，避免重复计数。 */
function settleFetchResolutionFailures(tracker) {
  const pending = tracker.fetchResolutionFailures.slice(tracker.fetchResolutionClassifiedCount);
  for (const failure of pending) {
    if (failure.staleProtocol) {
      tracker.fetchStaleProtocolCount += 1;
      tracker.fetchCanceledResolutionCount += 1;
    } else if (failure.networkId && tracker.networkFailedRequestIds.has(failure.networkId)) {
      tracker.fetchCanceledResolutionCount += 1;
    } else {
      tracker.fetchResolutionFailureCount += 1;
    }
  }
  tracker.fetchResolutionClassifiedCount = tracker.fetchResolutionFailures.length;
}


/** 把 Console/Log 错误折叠为固定类别；reason phrase 与正文均不参与证据。 */
function classifyBrowserConsoleError(text, level) {
  if (!["error", "assert"].includes(String(level))) return Object.freeze({ kind: "ignored" });
  const value = String(text ?? "");
  const httpMatch = value.match(
    /^Failed to load resource: the server responded with a status of ([1-5][0-9]{2})(?: \([^\r\n)]{0,80}\))?$/,
  );
  if (httpMatch) {
    const status = Number(httpMatch[1]);
    return Object.freeze({
      kind: [403, 409].includes(status) ? "expected_http" : "unexpected_http",
      status,
    });
  }
  const networkMatch = value.match(/^Failed to load resource: net::(ERR_[A-Z_]+)$/);
  if (networkMatch) {
    const known = Object.freeze({
      ERR_ABORTED: "aborted",
      ERR_BLOCKED_BY_CLIENT: "blocked",
      ERR_CONNECTION_REFUSED: "refused",
      ERR_FAILED: "failed",
    });
    return Object.freeze({ kind: "network", code: known[networkMatch[1]] ?? "other" });
  }
  return Object.freeze({ kind: "other" });
}


/** Console 网络失败只有被 CDP 明确标记为 canceled 且无同类未取消失败时才可豁免。 */
function consoleNetworkFailuresAreCanceled(
  consoleCodes,
  canceledCodes,
  uncanceledCodes,
  staleFetchProtocolCount = 0,
) {
  return consoleNetworkFailureReason(
    consoleCodes,
    canceledCodes,
    uncanceledCodes,
    staleFetchProtocolCount,
  ) === "";
}


/** 返回 Console 网络失败未能被取消证据覆盖的固定原因；空串表示全部已证明安全。 */
function consoleNetworkFailureReason(
  consoleCodes,
  canceledCodes,
  uncanceledCodes,
  staleFetchProtocolCount = 0,
) {
  const codes = consoleCodes ?? [];
  if (codes.some((code) => uncanceledCodes?.has(code))) return "uncanceled";
  if (codes.some((code) => code !== "failed" && !canceledCodes?.has(code))) return "non_failed_category";
  const uncorrelatedFailedCount = codes.filter(
    (code) => code === "failed" && !canceledCodes?.has(code),
  ).length;
  if (uncorrelatedFailedCount > 0 && staleFetchProtocolCount === 0) return "no_stale";
  if (uncorrelatedFailedCount > staleFetchProtocolCount * 2) return "excess";
  return "";
}


/** 绑定请求阻断、业务状态计数、动态 Bearer 边界和 Console 隐私门禁。 */
function bindBrowserTracker(client, pageSession, config, tracker) {
  client.on("Fetch.requestPaused", ({ requestId, networkId, request }, sessionId) => {
    if (sessionId !== pageSession.sessionId || !requestId || !request?.url) return;
    const suppliedPrivate = [...tracker.secrets, ...tracker.browserTokens]
      .some((value) => value && request.url.includes(value));
    if (browserRequestAllowed(request.url) && !suppliedPrivate) {
      trackFetchResolution(
        client.send("Fetch.continueRequest", { requestId }, sessionId),
        networkId,
        tracker,
      );
      return;
    }
    let target;
    try { target = new URL(request.url); } catch { target = null; }
    if (target && !["about:", "data:", "blob:"].includes(target.protocol)
      && !ALLOWED_ORIGINS.has(target.origin)) tracker.externalRequestCount += 1;
    if (target && [...target.searchParams.keys()].some((key) => FORBIDDEN_QUERY_KEY.test(key))) {
      tracker.forbiddenQueryCount += 1;
    }
    if (suppliedPrivate) {
      tracker.requestPrivacyViolationCount += 1;
    }
    trackFetchResolution(
      client.send("Fetch.failRequest", { requestId, errorReason: "BlockedByClient" }, sessionId),
      networkId,
      tracker,
    );
  });

  client.on("Network.requestWillBeSent", ({ requestId, request }, sessionId) => {
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (sessionId !== pageSession.sessionId || !request?.url) return;
    if (typeof requestId === "string") {
      tracker.activeRequestIds.add(requestId);
      tracker.requestMethods.set(requestId, request.method ?? "");
      tracker.requestCategories.set(requestId, trackedRequestCategory(request.url));
    }
    let target;
    try { target = new URL(request.url); } catch { target = null; }
    if (!target) {
      tracker.externalRequestCount += 1;
      return;
    }
    const authorization = readHeader(request.headers, "authorization");
    const bearer = authorization.match(/^Bearer ([^\s]+)$/)?.[1] ?? "";
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (bearer) tracker.browserTokens.add(bearer);
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (authorization && target.origin !== CONTROL_ORIGIN) {
      tracker.requestPrivacyViolationCount += 1;
    }
    const login = target.origin === CONTROL_ORIGIN
      && target.pathname === "/auth/login" && request.method === "POST";
    const body = String(request.postData ?? "");
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (!login && tracker.secrets.some((value) => value && body.includes(value))) {
      tracker.requestPrivacyViolationCount += 1;
    }
    for (const token of tracker.browserTokens) {
      // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
      if (token && (request.url.includes(token) || body.includes(token))) {
        tracker.requestPrivacyViolationCount += 1;
      }
    }
  });

  client.on("Network.responseReceived", ({ requestId, response }, sessionId) => {
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (sessionId !== pageSession.sessionId || !response?.url) return;
    const method = tracker.requestMethods.get(requestId) ?? "";
    tracker.requestMethods.delete(requestId);
    tracker.requestCategories.delete(requestId);
    let target;
    try { target = new URL(response.url); } catch { return; }
    if (target.origin !== CONTROL_ORIGIN) return;
    const status = Number(response.status);
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (target.pathname === "/auth/login" && method === "POST") tracker.loginStatuses.push(status);
    if (target.pathname === "/auth/me" && method === "GET") tracker.authMeStatuses.push(status);
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (target.pathname === "/auth/logout" && method === "POST") tracker.logoutStatuses.push(status);
    if (target.pathname === "/console/navigation" && method === "GET") tracker.navigationStatuses.push(status);
    if (target.pathname === "/cache-policies" && method === "GET") tracker.cacheGetStatuses.push(status);
    if (target.pathname === "/cache-policies" && method === "PATCH") tracker.cachePatchStatuses.push(status);
    if (target.pathname === "/cache/invalidate" && method === "POST") tracker.invalidationStatuses.push(status);
    if (target.pathname === "/request-logs" && method === "GET") tracker.requestLogListStatuses.push(status);
    if (/^\/request-logs\/[A-Za-z0-9_.:-]+$/.test(target.pathname) && method === "GET") {
      tracker.requestLogDetailStatuses.push(status);
    }
  });

  client.on("Network.loadingFailed", ({ requestId, canceled, blockedReason, errorText }, sessionId) => {
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (sessionId !== pageSession.sessionId) return;
    tracker.activeRequestIds.delete(requestId);
    tracker.requestMethods.delete(requestId);
    const requestCategory = tracker.requestCategories.get(requestId) ?? "unknown";
    tracker.requestCategories.delete(requestId);
    if (typeof requestId === "string") {
      tracker.networkFailedRequestIds.add(requestId);
      if (tracker.networkFailedRequestIds.size > 512) {
        tracker.networkFailedRequestIds.delete(tracker.networkFailedRequestIds.values().next().value);
      }
    }
    const classification = classifyBrowserConsoleError(
      `Failed to load resource: ${String(errorText ?? "")}`,
      "error",
    );
    if (classification.kind === "network") {
      if (canceled === true && !blockedReason) tracker.canceledNetworkFailureCodes.add(classification.code);
      else {
        tracker.uncanceledNetworkFailureCodes.add(classification.code);
        tracker.uncanceledNetworkFailureCategories.add(requestCategory);
      }
    }
  });

  client.on("Network.loadingFinished", ({ requestId }, sessionId) => {
    // responseReceived 只代表响应头到达；必须等 loadingFinished 才能证明 reload 不会取消旧请求。
    if (sessionId === pageSession.sessionId) tracker.activeRequestIds.delete(requestId);
  });

  /** 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。 */
  const inspectConsole = (text, level) => {
    const value = String(text ?? "");
    if (tracker.secrets.some((secret) => secret && value.includes(secret))) {
      tracker.consolePrivacyViolationCount += 1;
    }
    const classification = classifyBrowserConsoleError(value, level);
    if (classification.kind === "ignored") return;
    if (classification.kind === "expected_http") {
      tracker.expectedHttpErrorCount += 1;
      return;
    }
    tracker.unexpectedConsoleErrorCount += 1;
    if (classification.kind === "unexpected_http") tracker.unexpectedConsoleHttpStatuses.push(classification.status);
    else if (classification.kind === "network") tracker.consoleNetworkFailureCodes.push(classification.code);
    else tracker.unexpectedConsoleOtherCount += 1;
  };
  client.on("Runtime.consoleAPICalled", ({ type, args }, sessionId) => {
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (sessionId !== pageSession.sessionId) return;
    inspectConsole((args ?? []).map((item) => item.value ?? item.description ?? "").join(" "), type);
  });
  client.on("Runtime.exceptionThrown", (_, sessionId) => {
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (sessionId === pageSession.sessionId) tracker.runtimeExceptionCount += 1;
  });
  client.on("Log.entryAdded", ({ entry }, sessionId) => {
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (sessionId === pageSession.sessionId) inspectConsole(entry?.text, entry?.level);
  });
}


/** 使用固定请求 ID 形态调用 Control，并核验 no-store、request/trace 镜像和错误码。 */
async function controlJson(
  pathname,
  { method = "GET", token = "", body = null, idempotencyKey = "", expectedStatus = 200, expectedCode = "" },
  stage,
) {
  const requestId = `prd076_browser_${stage.replaceAll(/[^a-z0-9]/gi, "_")}_${randomUUID().replaceAll("-", "")}`;
  const headers = { Accept: "application/json", "X-Request-ID": requestId };
  // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== null) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  let response;
  try {
    response = await fetch(`${CONTROL_ORIGIN}${pathname}`, {
      method,
      headers,
      body: body === null ? undefined : JSON.stringify(body),
      redirect: "error",
      signal: AbortSignal.timeout(20000),
    });
  } catch {
    throw new BrowserAcceptanceFailure(stage, "network");
  }
  let payload;
  try { payload = await response.json(); } catch {
    throw new BrowserAcceptanceFailure(stage, "json");
  }
  requireCondition(response.status === expectedStatus, stage, "status");
  if (requiresResponseCachePrivateBoundary(pathname)) {
    requireCondition(response.headers.get("cache-control") === "no-store, private", stage, "cache_control");
  }
  requireCondition(
    payload?.request_id === requestId
      && response.headers.get("x-request-id") === requestId
      && typeof payload?.trace_id === "string" && payload.trace_id.length > 0
      && response.headers.get("x-trace-id") === payload.trace_id,
    stage,
    "request_chain",
  );
  // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
  if (expectedCode) requireCondition(payload?.code === expectedCode, stage, "error_code");
  return payload;
}


/** 建立一个仅驻 Node 内存的 Control Session。 */
/** 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。 */
async function loginControl(email, password, stage) {
  const payload = await controlJson("/auth/login", {
    method: "POST",
    body: { tenant_slug: "default", email, password },
  }, stage);
  requireCondition(typeof payload?.access_token === "string" && payload.access_token.length > 40, stage, "token");
  // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
  return payload.access_token;
}


/** 注销独立 Control Session；重复或失败不得静默遗留高权限 owner。 */
async function logoutControl(token, stage) {
  if (!token) return;
  const payload = await controlJson("/auth/logout", { method: "POST", token }, stage);
  requireCondition(payload?.logged_out === true, stage, "result");
}


/** 权限集合只接受目录规范 code 并返回稳定排序副本。 */
function normalizePermissionCodes(value, stage = "permission.codes") {
  requireCondition(Array.isArray(value) && value.length > 0, stage, "shape");
  requireCondition(
    value.every((item) => typeof item === "string" && SAFE_PERMISSION_CODE.test(item))
      && new Set(value).size === value.length,
    stage,
    "value",
  );
  return Object.freeze([...value].sort());
}


/** 比较两个 canonical 权限快照。 */
/** 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。 */
function samePermissionCodes(left, right) {
  return left.length === right.length && left.every((code, index) => code === right[index]);
}


/** 读取 finance_admin 的实时 role/revision/permission owner。 */
async function readFinanceRole(token) {
  const payload = await controlJson("/permissions", { token }, "permissions.read");
  const matches = Array.isArray(payload?.roles)
    ? payload.roles.filter((item) => item?.code === "finance_admin")
    : [];
  requireCondition(matches.length === 1, "permissions.read", "finance_role");
  const role = matches[0];
  requireCondition(
    typeof role.role_id === "string" && role.role_id.length > 0
      && Number.isSafeInteger(role.permission_revision) && role.permission_revision >= 1,
    "permissions.read",
    "owner",
  );
  return Object.freeze({
    roleId: role.role_id,
    revision: role.permission_revision,
    permissionCodes: normalizePermissionCodes(role.permission_codes, "permissions.read"),
  });
}


/** 使用服务端 revision CAS 完整替换 finance_admin 权限并核对写后投影。 */
async function replaceFinanceRole(token, snapshot, permissionCodes, operation) {
  const expected = normalizePermissionCodes(permissionCodes, `permissions.${operation}`);
  const payload = await controlJson(
    `/roles/${encodeURIComponent(snapshot.roleId)}/permissions`,
    {
      method: "PATCH",
      token,
      body: { expected_revision: snapshot.revision, permission_codes: expected },
    },
    `permissions.${operation}`,
  );
  requireCondition(payload?.changed === true && payload?.role?.code === "finance_admin", `permissions.${operation}`, "changed");
  const actual = normalizePermissionCodes(payload.role.permission_codes, `permissions.${operation}`);
  requireCondition(samePermissionCodes(actual, expected), `permissions.${operation}`, "projection");
  requireCondition(payload.role.permission_revision === snapshot.revision + 1, `permissions.${operation}`, "revision");
  // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
  return Object.freeze({ roleId: snapshot.roleId, revision: payload.role.permission_revision, permissionCodes: actual });
}


/** 构造 Browser 与并发 Control 共用的公开合成策略，不包含任何真实分级或标签。 */
function policyBody(modelAliasId, expectedVersion, ttlSeconds) {
  return {
    model_alias_id: modelAliasId,
    status: "enabled",
    data_class: PUBLIC_POLICY.dataClass,
    single_data_class_confirmed: true,
    ttl_seconds: ttlSeconds,
    charge_cache_hits: false,
    tags: [PUBLIC_POLICY.tag],
    expected_version: expectedVersion,
  };
}


/** 通过真实 Gateway 生成 miss/hit；正文只做当前调用栈等值比较。 */
async function invokeGateway(runtime, requestId) {
  let response;
  try {
    response = await fetch(`${GATEWAY_ORIGIN}/v1/chat/completions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${runtime.ready.virtual_key}`,
        "Content-Type": "application/json",
        "X-Request-ID": requestId,
        "X-Trace-ID": `trace-${requestId}`,
        "X-MetaHub-Contract-Tenant-ID": runtime.ready.tenant_value,
      },
      body: JSON.stringify({
        model: runtime.ready.model_alias,
        messages: [{ role: "user", content: runtime.ready.prompt_value }],
        temperature: 0.2,
      }),
      redirect: "error",
      signal: AbortSignal.timeout(30000),
    });
  } catch {
    throw new BrowserAcceptanceFailure("gateway.invoke", "network");
  }
  requireCondition(response.status === 200, "gateway.invoke", "status");
  let payload;
  try { payload = await response.json(); } catch {
    throw new BrowserAcceptanceFailure("gateway.invoke", "json");
  }
  const value = payload?.choices?.[0]?.message?.content;
  requireCondition(value === runtime.ready.response_value, "gateway.invoke", "body_mismatch");
  payload = null;
  return true;
}


/** 导航到真实 Console 首页并等待登录表单，而不是注入 Session Storage。 */
async function openConsole(client, sessionId) {
  await runAndWaitForPageLoad(
    client,
    sessionId,
    () => client.send("Page.navigate", { url: `${CONSOLE_ORIGIN}/` }, sessionId),
    "browser.console.load",
    30000,
  );
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", "browser.console.login_form");
}


/** 通过真实表单登录指定合成角色，口令只进入该次登录 POST。 */
async function loginThroughConsole(client, sessionId, tracker, account, expectedRole) {
  const expectedLoginCount = tracker.loginStatuses.length + 1;
  const expectedNavigationCount = tracker.navigationStatuses.length + 1;
  const values = JSON.stringify({ email: account.email, password: account.password });
  const submitted = await evaluate(client, sessionId, `(() => {
    const values = ${values};
    const form = document.querySelector("#login-form");
    if (!form) return false;
    const setValue = (name, value) => {
      const input = form.querySelector('[name="' + name + '"]');
      if (!input) return false;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
      setter.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    };
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (!setValue("tenantSlug", "default") || !setValue("email", values.email)
      || !setValue("password", values.password)) return false;
    form.requestSubmit();
    return true;
  })()`, `browser.login.${expectedRole}`);
  requireCondition(submitted === true, `browser.login.${expectedRole}`, "submit");
  await waitForDom(
    client,
    sessionId,
    "document.querySelector('#logout-button') && document.querySelector('.console-shell')",
    `browser.login.${expectedRole}.shell`,
  );
  await waitForTrackerCount(
    () => tracker.loginStatuses.length,
    expectedLoginCount,
    `browser.login.${expectedRole}.network`,
  );
  await waitForTrackerCount(
    () => tracker.navigationStatuses.length,
    expectedNavigationCount,
    `browser.login.${expectedRole}.navigation`,
  );
  requireCondition(
    tracker.loginStatuses.at(-1) === 200 && tracker.navigationStatuses.at(-1) === 200,
    `browser.login.${expectedRole}`,
    "status",
  );
}


/** 真实点击注销并证明 Session Storage 与 PAGE-106/RequestLog DOM 同步清零。 */
async function logoutThroughConsole(client, sessionId, tracker, stage) {
  const expectedLogoutCount = tracker.logoutStatuses.length + 1;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector("#logout-button");
    if (!button) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForDom(client, sessionId, "document.querySelector('#login-form')", `${stage}.form`);
  await waitForTrackerCount(() => tracker.logoutStatuses.length, expectedLogoutCount, `${stage}.network`);
  requireCondition(tracker.logoutStatuses.at(-1) === 200, stage, "status");
  const cleared = await evaluate(client, sessionId, `(() => ({
    auth_slots: ${JSON.stringify(AUTH_STORAGE_KEYS)}.filter((key) => sessionStorage.getItem(key) !== null).length,
    local_storage: localStorage.length,
    protected_dom: document.querySelectorAll("[data-response-cache-root],.request-log-detail,.response-cache-evidence").length,
  }))()`, `${stage}.privacy`);
  requireCondition(
    cleared.auth_slots === 0 && cleared.local_storage === 0 && cleared.protected_dom === 0,
    `${stage}.privacy`,
    "not_cleared",
  );
}


/** 点击后端授权的 PAGE-106 导航并等待真实 GET 200 和完整编辑器。 */
async function openResponseCache(client, sessionId, tracker, stage = "browser.cache.open") {
  const expectedGetCount = tracker.cacheGetStatuses.length + 1;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-console-nav="response-cache"]');
    if (!button) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "navigation");
  await waitForDom(
    client,
    sessionId,
    `document.querySelector("[data-response-cache-root]")
      && document.querySelector("[data-response-cache-policy-form]")`,
    `${stage}.dom`,
  );
  await waitForTrackerCount(() => tracker.cacheGetStatuses.length, expectedGetCount, `${stage}.network`);
  requireCondition(tracker.cacheGetStatuses.at(-1) === 200, stage, "status");
  const locationSafe = await evaluate(client, sessionId, `location.origin === ${JSON.stringify(CONSOLE_ORIGIN)}
    && location.pathname === "/" && location.search === "" && location.hash === "#response-cache"`, `${stage}.location`);
  requireCondition(locationSafe === true, `${stage}.location`, "unsafe");
}


/** 修改一个表单控件并等待两帧，让状态机重绘后的新 DOM 成为后续 owner。 */
async function changePolicyControl(client, sessionId, name, value, stage) {
  const changed = await evaluate(client, sessionId, `(() => {
    const input = document.querySelector('[data-response-cache-policy-form] [name=${JSON.stringify(name)}]');
    if (!input || input.disabled) return false;
    const next = ${JSON.stringify(value)};
    if (input.type === "checkbox") {
      if (input.checked !== Boolean(next)) input.click();
    } else {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
      setter.call(input, String(next));
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return true;
  })()`, stage);
  requireCondition(changed === true, stage, "control");
  await evaluate(
    client,
    sessionId,
    "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))",
    `${stage}.settle`,
  );
}


/** 从未配置基线通过真实控件建立一个公开合成草稿。 */
async function draftInitialPolicy(client, sessionId) {
  await changePolicyControl(client, sessionId, "data_class", PUBLIC_POLICY.dataClass, "browser.policy.data_class");
  await changePolicyControl(client, sessionId, "tags", PUBLIC_POLICY.tag, "browser.policy.tags");
  await changePolicyControl(client, sessionId, "ttl_seconds", PUBLIC_POLICY.initialTtl, "browser.policy.ttl");
  await changePolicyControl(client, sessionId, "enabled", true, "browser.policy.enabled");
  await changePolicyControl(
    client,
    sessionId,
    "single_data_class_confirmed",
    true,
    "browser.policy.confirmation",
  );
  await waitForDom(
    client,
    sessionId,
    `document.querySelector('[data-response-cache-policy-form] button.primary:not([disabled])')
      && document.querySelector('.response-cache-editor-live')?.textContent.includes("有未保存更改")`,
    "browser.policy.dirty",
  );
}


/** 点击策略保存并等待一条新的真实 PATCH 状态。 */
async function submitPolicy(client, sessionId, tracker, expectedStatus, stage) {
  const expectedCount = tracker.cachePatchStatuses.length + 1;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-response-cache-policy-form] button[type="submit"]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForTrackerCount(() => tracker.cachePatchStatuses.length, expectedCount, `${stage}.network`);
  const actualStatus = tracker.cachePatchStatuses.at(-1);
  if (actualStatus !== expectedStatus) {
    // 失败诊断只暴露固定 HTTP 状态白名单，不保留响应体、链路标识或请求配置。
    const safeStatus = [400, 401, 403, 404, 409, 422, 500, 502, 503].includes(actualStatus)
      ? `status_${actualStatus}`
      : "status_unexpected";
    throw new BrowserAcceptanceFailure(stage, safeStatus);
  }
  if (expectedStatus === 200) {
    await waitForDom(
      client,
      sessionId,
      `document.querySelector('.response-cache-feedback')?.textContent.includes("策略")
        && !document.querySelector('[data-response-cache-policy-form] button.primary:not([disabled])')`,
      `${stage}.success`,
    );
  } else if (expectedStatus === 409) {
    await waitForDom(client, sessionId, "document.querySelector('[data-response-cache-rebase]')", `${stage}.conflict`);
  }
}


/** 读取不含 Alias、data class、tag 或 request_id 的 PAGE-106 状态事实。 */
async function readPolicyFacts(client, sessionId) {
  // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
  return evaluate(client, sessionId, `(() => {
    const root = document.querySelector("[data-response-cache-root]");
    const versionText = root?.querySelector(".response-cache-editor-heading > b")?.textContent || "";
    const readyText = root?.querySelector(".response-cache-row.is-selected [data-label='可用条目']")?.textContent || "";
    const ttlText = root?.querySelector('[data-response-cache-policy-form] [name="ttl_seconds"]')?.value || "";
    return {
      root_visible: Boolean(root),
      policy_row_count: root?.querySelectorAll(".response-cache-row:not(.is-head)").length ?? 0,
      version: /^v[0-9]+$/.test(versionText.trim()) ? Number(versionText.trim().slice(1)) : -1,
      ready_count: /^[0-9]+$/.test(readyText.trim()) ? Number(readyText.trim()) : -1,
      ttl_seconds: /^[0-9]+$/.test(ttlText.trim()) ? Number(ttlText.trim()) : -1,
      dirty: Boolean(root?.querySelector('[data-response-cache-policy-form] button.primary:not([disabled])')),
      conflict: Boolean(root?.querySelector('[data-response-cache-rebase]')),
      readonly: Boolean(root?.querySelector('.response-cache-readonly')),
      save_visible: Boolean(root?.querySelector('[data-response-cache-policy-form] button.primary')),
      invalidate_visible: Boolean(root?.querySelector('[data-response-cache-invalidate-open]')),
      dialog_visible: Boolean(root?.querySelector('[role="dialog"]')),
    };
  })()`, "browser.policy.facts");
}


/** 冲突态必须同时保留 stale 版本和本地 TTL 草稿，并禁用直接保存。 */
function assertStaleConflictFacts(facts) {
  requireCondition(
    facts?.conflict === true
      && facts.version === 1
      && facts.ttl_seconds === PUBLIC_POLICY.draftTtl
      && facts.dirty === false,
    "browser.conflict",
    "stale_state",
  );
  return facts;
}


/** 真实并发管理员 Session 推进版本，随后 Browser stale PATCH 必须进入显式 rebase。 */
async function exerciseConflict(client, sessionId, tracker, adminToken, runtime, onConflict = null) {
  await changePolicyControl(client, sessionId, "ttl_seconds", PUBLIC_POLICY.draftTtl, "browser.conflict.draft");
  const concurrent = await controlJson("/cache-policies", {
    method: "PATCH",
    token: adminToken,
    body: policyBody(runtime.ready.model_alias_id, 1, PUBLIC_POLICY.concurrentTtl),
    idempotencyKey: `response-cache-${randomUUID()}`,
  }, "conflict.concurrent");
  requireCondition(concurrent?.cache_policy?.version === 2, "conflict.concurrent", "version");
  await submitPolicy(client, sessionId, tracker, 409, "browser.conflict.stale");
  const staleFacts = await readPolicyFacts(client, sessionId);
  assertStaleConflictFacts(staleFacts);
  if (typeof onConflict === "function") await onConflict();
  const expectedGetCount = tracker.cacheGetStatuses.length + 1;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-response-cache-rebase]');
    if (!button) return false;
    button.click();
    return true;
  })()`, "browser.conflict.rebase");
  requireCondition(clicked === true, "browser.conflict.rebase", "button");
  await waitForTrackerCount(() => tracker.cacheGetStatuses.length, expectedGetCount, "browser.conflict.rebase.network");
  await waitForDom(
    client,
    sessionId,
    `!document.querySelector('[data-response-cache-rebase]')
      && document.querySelector('[name="ttl_seconds"]')?.value === ${JSON.stringify(String(PUBLIC_POLICY.draftTtl))}
      && document.querySelector('.response-cache-editor-heading > b')?.textContent.trim() === "v2"`,
    "browser.conflict.rebase.dom",
  );
  await submitPolicy(client, sessionId, tracker, 200, "browser.conflict.final_save");
  const finalFacts = await readPolicyFacts(client, sessionId);
  requireCondition(finalFacts.version === 3 && !finalFacts.dirty && !finalFacts.conflict, "browser.conflict", "final_state");
  return Object.freeze({ stale_conflict_rendered: true, explicit_rebase: true, final_save_succeeded: true });
}


/** 使用真实刷新重读 ready entry 数，证明页面不是仅显示旧策略快照。 */
async function refreshPolicyAndRequireEntry(client, sessionId, tracker) {
  const expectedGetCount = tracker.cacheGetStatuses.length + 1;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-response-cache-refresh]');
    if (!button) return false;
    button.click();
    return true;
  })()`, "browser.cache.refresh");
  requireCondition(clicked === true, "browser.cache.refresh", "button");
  await waitForTrackerCount(() => tracker.cacheGetStatuses.length, expectedGetCount, "browser.cache.refresh.network");
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const facts = await readPolicyFacts(client, sessionId);
    if (facts.ready_count >= 1) return facts.ready_count;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new BrowserAcceptanceFailure("browser.cache.refresh", "entry_missing");
}


/** 发送一个真实键盘按键，覆盖 Dialog Escape 与 Tab 焦点圈。 */
async function pressKey(client, sessionId, key, { shift = false } = {}) {
  const code = key === "Escape" ? "Escape" : key;
  const modifiers = shift ? 8 : 0;
  await client.send("Input.dispatchKeyEvent", { type: "rawKeyDown", key, code, modifiers }, sessionId);
  await client.send("Input.dispatchKeyEvent", { type: "keyUp", key, code, modifiers }, sessionId);
}


/** 打开失效 Dialog，验证焦点首尾循环、Escape 关闭及关闭后焦点恢复。 */
async function exerciseDialogKeyboard(client, sessionId) {
  const opened = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-response-cache-invalidate-open]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, "browser.dialog.open");
  requireCondition(opened === true, "browser.dialog.open", "button");
  await waitForDom(client, sessionId, "document.querySelector('[role=\"dialog\"]')", "browser.dialog.dom");
  const initialFocus = await evaluate(
    client,
    sessionId,
    "document.activeElement?.hasAttribute('data-response-cache-invalidate-close') === true",
    "browser.dialog.focus.initial",
  );
  requireCondition(initialFocus === true, "browser.dialog.focus", "initial");
  await pressKey(client, sessionId, "Tab", { shift: true });
  const wrappedLast = await evaluate(
    client,
    sessionId,
    "document.activeElement?.matches('[data-response-cache-invalidate-form] button[type=\"submit\"]') === true",
    "browser.dialog.focus.last",
  );
  requireCondition(wrappedLast === true, "browser.dialog.focus", "shift_tab");
  await pressKey(client, sessionId, "Tab");
  const wrappedFirst = await evaluate(
    client,
    sessionId,
    "document.activeElement?.hasAttribute('data-response-cache-invalidate-close') === true",
    "browser.dialog.focus.first",
  );
  requireCondition(wrappedFirst === true, "browser.dialog.focus", "tab");
  await pressKey(client, sessionId, "Escape");
  await waitForDom(client, sessionId, "!document.querySelector('[role=\"dialog\"]')", "browser.dialog.escape");
  const restored = await evaluate(
    client,
    sessionId,
    "document.activeElement?.hasAttribute('data-response-cache-invalidate-open') === true",
    "browser.dialog.focus.restored",
  );
  requireCondition(restored === true, "browser.dialog.focus", "restore");
  return Object.freeze({ initial_focus: true, focus_trap: true, escape_close: true, focus_restored: true });
}


/** 重新打开 Dialog 并选择公开 fixture tag，供截图和真实失效 POST 使用。 */
async function prepareTagInvalidation(client, sessionId) {
  const prepared = await evaluate(client, sessionId, `(() => {
    const open = document.querySelector('[data-response-cache-invalidate-open]');
    if (!open || open.disabled) return false;
    open.click();
    return true;
  })()`, "browser.invalidation.open");
  requireCondition(prepared === true, "browser.invalidation.open", "button");
  await waitForDom(client, sessionId, "document.querySelector('[role=\"dialog\"]')", "browser.invalidation.dialog");
  const selected = await evaluate(client, sessionId, `(() => {
    const scope = document.querySelector('[data-response-cache-invalidate-form] [name="scope"][value="tags"]');
    if (!scope || scope.disabled) return false;
    scope.click();
    const selector = document.querySelector('[data-response-cache-selector-list] [value=${JSON.stringify(`tag:${PUBLIC_POLICY.tag}`)}]');
    if (!selector || selector.disabled) return false;
    selector.click();
    return scope.checked && selector.checked;
  })()`, "browser.invalidation.selector");
  requireCondition(selected === true, "browser.invalidation.selector", "missing");
}


/** 提交当前 Dialog，并要求真实 POST 200、关闭和成功反馈。 */
async function submitInvalidation(client, sessionId, tracker, expectedStatus, stage) {
  const expectedCount = tracker.invalidationStatuses.length + 1;
  const clicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-response-cache-invalidate-form] button[type="submit"]');
    if (!button || button.disabled) return false;
    button.click();
    return true;
  })()`, stage);
  requireCondition(clicked === true, stage, "button");
  await waitForTrackerCount(() => tracker.invalidationStatuses.length, expectedCount, `${stage}.network`);
  requireCondition(tracker.invalidationStatuses.at(-1) === expectedStatus, stage, "status");
  if (expectedStatus === 200) {
    await waitForDom(
      client,
      sessionId,
      `!document.querySelector('[role="dialog"]')
        && document.querySelector('.response-cache-feedback')?.textContent.includes("失效")`,
      `${stage}.success`,
    );
    const feedback = await evaluate(client, sessionId, `(() => {
      const text = document.querySelector('.response-cache-feedback')?.textContent || "";
      const matched = text.match(/匹配\\s+([0-9]+)/);
      const deleted = text.match(/删除\\s+([0-9]+)/);
      return {
        matched_entry_count: matched ? Number(matched[1]) : -1,
        deleted_entry_count: deleted ? Number(deleted[1]) : -1,
      };
    })()`, `${stage}.feedback`);
    let readyCountAfter = -1;
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      readyCountAfter = (await readPolicyFacts(client, sessionId)).ready_count;
      if (readyCountAfter === 0) break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return assertTagInvalidationFacts({
      ...feedback,
      ready_count_after: readyCountAfter,
    });
  }
  return null;
}


/** 标签失效必须真实命中并删除至少一个 ready entry，fresh GET 后计数归零。 */
function assertTagInvalidationFacts(facts) {
  requireCondition(
    Number.isSafeInteger(facts?.matched_entry_count) && facts.matched_entry_count >= 1
      && Number.isSafeInteger(facts?.deleted_entry_count) && facts.deleted_entry_count >= 1
      && facts.deleted_entry_count <= facts.matched_entry_count
      && facts.ready_count_after === 0,
    "browser.invalidation",
    "effect_missing",
  );
  return Object.freeze({
    matched_entry_count: facts.matched_entry_count,
    deleted_entry_count: facts.deleted_entry_count,
    ready_count_after: 0,
  });
}


/** 构造只返回 Boolean 的浏览器 revision 镜像断言，不读取 Token 或权限正文。 */
function permissionRevisionExpression(expectedRevision) {
  requireCondition(
    Number.isSafeInteger(expectedRevision) && expectedRevision >= 1,
    "browser.permissions.revision",
    "invalid_expected",
  );
  return `(() => {
    try {
      const context = JSON.parse(sessionStorage.getItem("metahub.auth_context") ?? "null");
      // 顶层与 user 镜像必须同时命中新 revision，残缺或过期上下文不能提前放行 DOM 验收。
      return Number.isSafeInteger(context?.permission_revision)
        && context.permission_revision === ${expectedRevision}
        && Number.isSafeInteger(context?.user?.permission_revision)
        && context.user.permission_revision === ${expectedRevision};
    } catch {
      return false;
    }
  })()`;
}


/** 等待页面请求持续空闲，避免权限 reload 与 15 秒后台刷新互相取消。 */
async function waitForTrackedNetworkIdle(
  tracker,
  stage,
  timeoutMs = 60000,
  stableMs = 500,
) {
  requireCondition(
    tracker?.activeRequestIds instanceof Set
      && Number.isSafeInteger(timeoutMs) && timeoutMs >= 1
      && Number.isSafeInteger(stableMs) && stableMs >= 1 && stableMs <= timeoutMs,
    stage,
    "contract",
  );
  const deadline = Date.now() + timeoutMs;
  let idleSince = null;
  while (Date.now() < deadline) {
    if (tracker.activeRequestIds.size === 0) {
      idleSince ??= Date.now();
      if (Date.now() - idleSince >= stableMs) return true;
    } else {
      idleSince = null;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new BrowserAcceptanceFailure(stage, "timeout");
}


/** 重新加载当前 Session，并按 /auth/me、revision、navigation、DOM 的顺序确认新 owner。 */
async function reloadForPermissions(
  client,
  sessionId,
  tracker,
  expectedRevision,
  expression,
  stage,
) {
  await waitForTrackedNetworkIdle(tracker, `${stage}.pre_reload`);
  const expectedAuthMeCount = tracker.authMeStatuses.length + 1;
  const expectedNavigationCount = tracker.navigationStatuses.length + 1;
  await runAndWaitForPageLoad(
    client,
    sessionId,
    () => client.send("Page.reload", { ignoreCache: true }, sessionId),
    `${stage}.reload`,
  );
  await waitForTrackerCount(
    () => tracker.authMeStatuses.length,
    expectedAuthMeCount,
    `${stage}.auth_me`,
    60000,
  );
  requireCondition(
    tracker.authMeStatuses.slice(expectedAuthMeCount - 1).includes(200),
    stage,
    "auth_me_status",
  );
  await waitForDom(
    client,
    sessionId,
    permissionRevisionExpression(expectedRevision),
    `${stage}.revision`,
    60000,
  );
  await waitForTrackerCount(() => tracker.navigationStatuses.length, expectedNavigationCount, `${stage}.navigation`);
  requireCondition(
    tracker.navigationStatuses.slice(expectedNavigationCount - 1).includes(200),
    stage,
    "navigation_status",
  );
  // Console 完整刷新会顺序读取多个模块；只在新授权 owner 的 PAGE-106 DOM 落地后继续。
  await waitForDom(client, sessionId, expression, `${stage}.dom`, 90000);
  await waitForTrackedNetworkIdle(tracker, `${stage}.post_dom`);
}


/** 验证 write/invalidate/read 三个权限独立、后端 403 和 owner 清理。 */
async function exercisePermissionTiers(
  client,
  sessionId,
  tracker,
  adminToken,
  financeToken,
  originalRole,
  runtime,
) {
  let current = originalRole;
  const withoutWrite = current.permissionCodes.filter((code) => code !== "response_cache.write");
  current = await replaceFinanceRole(adminToken, current, withoutWrite, "revoke_write");
  await reloadForPermissions(
    client,
    sessionId,
    tracker,
    current.revision,
    `document.querySelector('[data-response-cache-root]')
      && document.querySelector('.response-cache-readonly')
      && document.querySelector('[data-response-cache-invalidate-open]')`,
    "browser.permissions.write_revoked",
  );
  const writeFacts = await readPolicyFacts(client, sessionId);
  requireCondition(writeFacts.readonly && !writeFacts.save_visible && writeFacts.invalidate_visible, "browser.permissions.write_revoked", "dom");
  await controlJson("/cache-policies", {
    method: "PATCH",
    token: financeToken,
    body: policyBody(runtime.ready.model_alias_id, 3, PUBLIC_POLICY.draftTtl),
    idempotencyKey: `response-cache-${randomUUID()}`,
    expectedStatus: 403,
    expectedCode: "auth.permission_denied",
  }, "permissions.write_denied");

  current = await replaceFinanceRole(adminToken, current, originalRole.permissionCodes, "restore_write");
  await reloadForPermissions(
    client,
    sessionId,
    tracker,
    current.revision,
    `document.querySelector('[data-response-cache-root]')
      && document.querySelector('[data-response-cache-policy-form] button.primary')
      && document.querySelector('[data-response-cache-invalidate-open]')`,
    "browser.permissions.write_restored",
  );

  await prepareTagInvalidation(client, sessionId);
  const withoutInvalidate = current.permissionCodes.filter((code) => code !== "response_cache.invalidate");
  current = await replaceFinanceRole(adminToken, current, withoutInvalidate, "revoke_invalidate");
  await submitInvalidation(client, sessionId, tracker, 403, "browser.permissions.invalidate_inflight");
  await waitForDom(
    client,
    sessionId,
    `document.querySelector('[data-response-cache-root]')
      && !document.querySelector('[role="dialog"]')
      && !document.querySelector('[data-response-cache-invalidate-open]')`,
    "browser.permissions.invalidate_cleared",
  );
  await controlJson("/cache/invalidate", {
    method: "POST",
    token: financeToken,
    body: { scope: "all", match_mode: "all", tags: [], reason_code: "manual_finops" },
    idempotencyKey: `response-cache-${randomUUID()}`,
    expectedStatus: 403,
    expectedCode: "auth.permission_denied",
  }, "permissions.invalidate_denied");

  current = await replaceFinanceRole(adminToken, current, originalRole.permissionCodes, "restore_invalidate");
  await reloadForPermissions(
    client,
    sessionId,
    tracker,
    current.revision,
    `document.querySelector('[data-response-cache-root]')
      && document.querySelector('[data-response-cache-invalidate-open]')`,
    "browser.permissions.invalidate_restored",
  );
  await changePolicyControl(client, sessionId, "ttl_seconds", 903, "browser.permissions.read_dirty");
  const withoutRead = current.permissionCodes.filter((code) => code !== "response_cache.read");
  current = await replaceFinanceRole(adminToken, current, withoutRead, "revoke_read");
  await reloadForPermissions(
    client,
    sessionId,
    tracker,
    current.revision,
    `document.querySelector('#logout-button')
      && !document.querySelector('[data-console-nav="response-cache"]')
      && !document.querySelector('[data-response-cache-root]')
      && location.hash !== "#response-cache"`,
    "browser.permissions.read_revoked",
  );
  await controlJson("/cache-policies", {
    token: financeToken,
    expectedStatus: 403,
    expectedCode: "auth.permission_denied",
  }, "permissions.read_denied");

  current = await replaceFinanceRole(adminToken, current, originalRole.permissionCodes, "restore_read");
  await reloadForPermissions(
    client,
    sessionId,
    tracker,
    current.revision,
    `document.querySelector('#logout-button')
      && document.querySelector('[data-console-nav="response-cache"]')`,
    "browser.permissions.read_restored",
  );
  await openResponseCache(client, sessionId, tracker, "browser.permissions.reopen");
  const restoredFacts = await readPolicyFacts(client, sessionId);
  requireCondition(
    restoredFacts.version === 3 && !restoredFacts.dirty && !restoredFacts.conflict,
    "browser.permissions.restored",
    "draft_resurrected",
  );
  return Object.freeze({
    read_revocation_cleared_dom: true,
    write_backend_status: 403,
    write_readonly_projection: true,
    invalidate_backend_status: 403,
    invalidate_dialog_cleared: true,
    permissions_restored: true,
  });
}


/** 打开命中 RequestLog，并只读取 Response Cache 枚举、状态和金额证据位。 */
async function openHitRequestLog(client, sessionId, tracker, requestId) {
  const expectedInitialList = tracker.requestLogListStatuses.length + 1;
  const opened = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-console-nav="request-logs"]');
    if (!button) return false;
    button.click();
    return true;
  })()`, "browser.request_log.open");
  requireCondition(opened === true, "browser.request_log.open", "navigation");
  await waitForDom(client, sessionId, "document.querySelector('#request-log-filter-form')", "browser.request_log.form");
  await waitForTrackerCount(() => tracker.requestLogListStatuses.length, expectedInitialList, "browser.request_log.initial");
  const expectedFilteredList = tracker.requestLogListStatuses.length + 1;
  const submitted = await evaluate(client, sessionId, `(() => {
    const form = document.querySelector("#request-log-filter-form");
    const input = form?.querySelector('[name="requestId"]');
    if (!form || !input) return false;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(input, ${JSON.stringify(requestId)});
    input.dispatchEvent(new Event("input", { bubbles: true }));
    form.requestSubmit();
    return true;
  })()`, "browser.request_log.filter");
  requireCondition(submitted === true, "browser.request_log.filter", "submit");
  await waitForTrackerCount(() => tracker.requestLogListStatuses.length, expectedFilteredList, "browser.request_log.filter.network");
  requireCondition(tracker.requestLogListStatuses.at(-1) === 200, "browser.request_log.filter", "status");
  await waitForDom(
    client,
    sessionId,
    `document.querySelector('[data-request-log-detail=${JSON.stringify(requestId)}]')`,
    "browser.request_log.row",
  );
  const expectedDetailCount = tracker.requestLogDetailStatuses.length + 1;
  const detailClicked = await evaluate(client, sessionId, `(() => {
    const button = document.querySelector('[data-request-log-detail=${JSON.stringify(requestId)}]');
    if (!button) return false;
    button.click();
    return true;
  })()`, "browser.request_log.detail");
  requireCondition(detailClicked === true, "browser.request_log.detail", "button");
  await waitForTrackerCount(() => tracker.requestLogDetailStatuses.length, expectedDetailCount, "browser.request_log.detail.network");
  requireCondition(tracker.requestLogDetailStatuses.at(-1) === 200, "browser.request_log.detail", "status");
  await waitForDom(
    client,
    sessionId,
    `document.querySelector('.response-cache-evidence[data-response-cache-status="hit"]')`,
    "browser.request_log.cache_fact",
  );
  const facts = await evaluate(client, sessionId, `(() => {
    const root = document.querySelector('.response-cache-evidence[data-response-cache-status="hit"]');
    const labels = [...(root?.querySelectorAll('dt') || [])].map((item) => item.textContent.trim());
    const text = root?.textContent || "";
    return {
      visible: Boolean(root),
      hit_label: root?.querySelector('.response-cache-hit-state')?.textContent.trim() === "命中",
      cost_pair_count: ["源请求生成成本", "当前参考成本", "本次节省", "本次计费"]
        .filter((label) => labels.includes(label)).length,
      invalid_projection: Boolean(root?.classList.contains('is-invalid')),
      raw_content_terms: ["Prompt", "Provider Response", "Provider Key", "Authorization"]
        .filter((value) => text.includes(value)).length,
    };
  })()`, "browser.request_log.facts");
  requireCondition(
    facts.visible && facts.hit_label && facts.cost_pair_count === 4
      && !facts.invalid_projection && facts.raw_content_terms === 0,
    "browser.request_log",
    "cache_projection",
  );
  // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
  return Object.freeze({ hit_visible: true, cost_pair_count: 4, raw_private_terms: 0 });
}


/** 计算可见元素矩形的非法重叠；祖先包含子元素属于正常布局，不计为重叠。 */
function countLayoutOverlaps(candidates) {
  requireCondition(Array.isArray(candidates) && candidates.length > 0, "browser.layout", "candidates");
  const normalized = candidates.map((candidate, index) => {
    const rectangle = {
      left: Number(candidate?.left),
      top: Number(candidate?.top),
      right: Number(candidate?.right),
      bottom: Number(candidate?.bottom),
    };
    requireCondition(
      Object.values(rectangle).every(Number.isFinite)
        && rectangle.right > rectangle.left && rectangle.bottom > rectangle.top,
      "browser.layout",
      "rectangle",
    );
    const ancestors = Array.isArray(candidate?.ancestor_indexes) ? candidate.ancestor_indexes : [];
    requireCondition(
      ancestors.every((value) => Number.isInteger(value)
        && value >= 0 && value < candidates.length && value !== index),
      "browser.layout",
      "ancestors",
    );
    return Object.freeze({ ...rectangle, ancestors: new Set(ancestors) });
  });
  let overlapCount = 0;
  for (let left = 0; left < normalized.length; left += 1) {
    for (let right = left + 1; right < normalized.length; right += 1) {
      const a = normalized[left];
      const b = normalized[right];
      if (a.ancestors.has(right) || b.ancestors.has(left)) continue;
      if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1
        && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1) overlapCount += 1;
    }
  }
  return overlapCount;
}


/** 读取目标页面的双视口几何事实；只返回尺寸、矩形和计数，不返回任何 DOM 文本。 */
async function readLayoutMetrics(client, sessionId, selector) {
  const raw = await evaluate(client, sessionId, `(() => {
    const target = document.querySelector(${JSON.stringify(selector)});
    const visible = (element) => {
      if (!element || !(element instanceof HTMLElement)) return false;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden"
        && rect.width > 0 && rect.height > 0;
    };
    const rect = target?.getBoundingClientRect();
    const textNodes = [...(target?.querySelectorAll(
      "h1,h2,h3,h4,h5,h6,p,button,label,span,strong,small,code,dt,dd,input,select,textarea"
    ) || [])]
      .filter(visible);
    /** 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。 */
    const textOverflows = (element) => {
      const style = getComputedStyle(element);
      if (style.display !== "inline") return element.scrollWidth > element.clientWidth + 1;
      let container = element.parentElement;
      while (container && container !== target && getComputedStyle(container).display === "inline") {
        container = container.parentElement;
      }
      if (!container) return false;
      const boundary = container.getBoundingClientRect();
      const range = document.createRange();
      range.selectNodeContents(element);
      return [...range.getClientRects()].some((textRect) => textRect.width > 0
        && (textRect.left < boundary.left - 1 || textRect.right > boundary.right + 1));
    };
    /** 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。 */
    const textOverflowCategory = (element) => {
      if (element.matches(".response-cache-toolbar, .response-cache-toolbar *")) return "toolbar";
      if (element.matches(".response-cache-row, .response-cache-row *")) {
        const row = element.closest(".response-cache-row");
        const cell = element.closest(".response-cache-row > span");
        const index = row && cell ? [...row.children].indexOf(cell) + 1 : 0;
        if (index >= 1 && index <= 6) {
          return "row_" + (row.classList.contains("is-head") ? "head" : "value") + "_" + index;
        }
        return "row";
      }
      if (element.matches(".response-cache-editor, .response-cache-editor *")) return "editor";
      if (element.matches(".response-cache-dialog, .response-cache-dialog *")) return "dialog";
      if (element.matches(".response-cache-evidence, .response-cache-evidence *")) return "evidence";
      if (element.matches("button,input,select,textarea")) return "control";
      if (element.matches("h1,h2,h3,h4,h5,h6")) return "heading";
      return "other";
    };
    const overflowingTextNodes = textNodes.filter(textOverflows);
    const controls = [...(target?.querySelectorAll(
      "button,input,select,textarea,a[href],[tabindex]"
    ) || [])].filter(visible);
    const overlapNodes = target
      ? [target, ...target.querySelectorAll("*")].filter(visible)
      : [];
    const overlapIndexes = new Map(overlapNodes.map((element, index) => [element, index]));
    const overlapCandidates = overlapNodes.map((element) => {
      const item = element.getBoundingClientRect();
      const ancestorIndexes = [];
      for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
        const ancestorIndex = overlapIndexes.get(ancestor);
        if (Number.isInteger(ancestorIndex)) ancestorIndexes.push(ancestorIndex);
        if (ancestor === target) break;
      }
      return {
        left: item.left,
        top: item.top,
        right: item.right,
        bottom: item.bottom,
        ancestor_indexes: ancestorIndexes,
      };
    });
    const dialog = document.querySelector(".response-cache-dialog");
    const dialogActions = dialog?.querySelector(".response-cache-dialog-actions")?.getBoundingClientRect();
    return {
      viewport_width: innerWidth,
      viewport_height: innerHeight,
      document_scroll_width: document.documentElement.scrollWidth,
      page_scroll_width: document.body.scrollWidth,
      target_visible: Boolean(rect) && rect.width > 0 && rect.height > 0,
      target_inside_horizontal: Boolean(rect) && rect.left >= -1 && rect.right <= innerWidth + 1,
      target_horizontal_overflow: target ? Math.max(0, target.scrollWidth - target.clientWidth) : -1,
      text_overflow_count: overflowingTextNodes.length,
      text_overflow_categories: [...new Set(overflowingTextNodes.map(textOverflowCategory))],
      control_outside_horizontal_count: controls.filter((element) => {
        const item = element.getBoundingClientRect();
        return item.left < -1 || item.right > innerWidth + 1;
      }).length,
      overlap_candidates: overlapCandidates,
      dialog_count: dialog && visible(dialog) ? 1 : 0,
      dialog_actions_reachable: !dialogActions
        || (dialogActions.top >= -1 && dialogActions.bottom <= innerHeight + 1),
    };
  })()`, "browser.layout.read");
  const { overlap_candidates: overlapCandidates, ...metrics } = raw ?? {};
  return Object.freeze({
    ...metrics,
    overlap_candidate_count: Array.isArray(overlapCandidates) ? overlapCandidates.length : 0,
    overlap_count: countLayoutOverlaps(overlapCandidates),
  });
}


/** 断言桌面和移动端均无横向溢出、文字溢出、控件越界或操作重叠。 */
function assertLayoutMetrics(metrics, viewport, { expectDialog = false } = {}) {
  const stage = `browser.layout.${viewport.name}`;
  requireCondition(
    metrics?.viewport_width === viewport.width && metrics?.viewport_height === viewport.height,
    stage,
    "viewport",
  );
  requireCondition(
    metrics.document_scroll_width <= viewport.width + 1
      && metrics.page_scroll_width <= viewport.width + 1,
    stage,
    "document_overflow",
  );
  requireCondition(
    metrics.target_visible && metrics.target_inside_horizontal
      && metrics.target_horizontal_overflow <= 1,
    stage,
    "target",
  );
  if (metrics.text_overflow_count !== 0) {
    const codeByCategory = Object.freeze({
      toolbar: "text_overflow_toolbar",
      row: "text_overflow_row",
      editor: "text_overflow_editor",
      dialog: "text_overflow_dialog",
      evidence: "text_overflow_evidence",
      control: "text_overflow_control",
      heading: "text_overflow_heading",
      other: "text_overflow_other",
    });
    const category = Array.isArray(metrics.text_overflow_categories)
      ? metrics.text_overflow_categories[0]
      : "";
    const rowCode = /^row_(?:head|value)_[1-6]$/.test(category)
      ? `text_overflow_${category}`
      : "";
    throw new BrowserAcceptanceFailure(stage, (codeByCategory[category] ?? rowCode) || "text_overflow");
  }
  requireCondition(metrics.control_outside_horizontal_count === 0, stage, "control_outside");
  requireCondition(metrics.overlap_candidate_count > 0, stage, "overlap_coverage");
  requireCondition(metrics.overlap_count === 0, stage, "overlap");
  requireCondition(metrics.dialog_count === (expectDialog ? 1 : 0), stage, "dialog_count");
  requireCondition(metrics.dialog_actions_reachable === true, stage, "dialog_actions");
  return metrics;
}


/** 截图名只接受固定状态标识与冻结视口，禁止路径分隔符或动态 owner。 */
function screenshotFilename(state, viewportName) {
  requireCondition(/^[a-z][a-z0-9-]{2,50}$/.test(String(state)), "screenshot.name", "state");
  requireCondition(
    Object.values(VIEWPORTS).some((viewport) => viewport.name === viewportName),
    "screenshot.name",
    "viewport",
  );
  return `${state}-${viewportName}.png`;
}


/** 截图前只隐藏身份、tenant 和链路 ID，固定公开 fixture 配置仍保持可见。 */
async function prepareScreenshotDom(client, sessionId, selector, { dialogActions = false } = {}) {
  const prepared = await evaluate(client, sessionId, `(() => {
    const target = document.querySelector(${JSON.stringify(selector)});
    const maskLabels = ${JSON.stringify(SCREENSHOT_MASK_LABELS)};
    if (!target) return false;
    document.querySelector('[data-prd076-browser-mask-style]')?.remove();
    const style = document.createElement("style");
    style.setAttribute("data-prd076-browser-mask-style", "true");
    style.textContent = [
      ".user-menu > div",
      ".response-cache-toolbar > div:first-child > span",
      "[data-response-cache-chain-id]"
    ].join(",") + "{visibility:hidden!important}";
    document.head.appendChild(style);
    for (const labelNode of document.querySelectorAll("dt")) {
      const label = labelNode.textContent?.trim() || "";
      if (maskLabels.includes(label.toLowerCase())) {
        const valueNode = labelNode.nextElementSibling;
        if (!valueNode || valueNode.tagName !== "DD") return false;
        for (const element of [labelNode, valueNode]) {
          element.setAttribute("data-prd076-browser-mask", "true");
          element.style.visibility = "hidden";
        }
      }
    }
    if (${dialogActions ? "true" : "false"}) {
      const dialog = document.querySelector('.response-cache-dialog');
      if (dialog) dialog.scrollTop = dialog.scrollHeight;
    }
    target.scrollIntoView({ block: "start", inline: "nearest" });
    return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))));
  })()`, "browser.screenshot.prepare");
  requireCondition(prepared === true, "browser.screenshot.prepare", "target");
}


/** 截图后恢复本验收器添加的临时 mask，不触碰业务状态或表单内容。 */
async function restoreScreenshotDom(client, sessionId) {
  await evaluate(client, sessionId, `(() => {
    document.querySelector('[data-prd076-browser-mask-style]')?.remove();
    for (const element of document.querySelectorAll('[data-prd076-browser-mask]')) {
      element.style.visibility = "";
      element.removeAttribute('data-prd076-browser-mask');
    }
    return true;
  })()`, "browser.screenshot.restore");
}


/** 截图前证明运行期私有 canary 和动态 Bearer 均未进入 URL 或可见 DOM。 */
async function assertScreenshotDomSafe(client, sessionId, tracker) {
  const needles = JSON.stringify([...tracker.secrets, ...tracker.browserTokens].filter(Boolean));
  const facts = await evaluate(client, sessionId, `(() => {
    const needles = ${needles};
    const values = [location.href, document.documentElement.outerHTML];
    const chainText = [...document.body.querySelectorAll("*")]
      .filter((element) => element.children.length === 0 && /(?:request|trace)_id\b/i.test(element.textContent || ""));
    return {
      private_hit_count: needles.filter((needle) => values.some((value) => value.includes(needle))).length,
      mask_style_count: document.querySelectorAll('[data-prd076-browser-mask-style]').length,
      visible_chain_text_count: chainText.filter((element) => {
        const style = getComputedStyle(element);
        return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0;
      }).length,
    };
  })()`, "browser.screenshot.privacy");
  requireCondition(
    facts.private_hit_count === 0
      && facts.mask_style_count === 1
      && facts.visible_chain_text_count === 0,
    "browser.screenshot.privacy",
    "private_dom",
  );
}


/** 捕获一个真实视口、验证 PNG 像素并只返回安全元数据。 */
async function captureState(
  client,
  sessionId,
  tracker,
  workDirectory,
  state,
  viewport,
  selector,
  options = {},
) {
  await setViewport(client, sessionId, viewport);
  await prepareScreenshotDom(client, sessionId, selector, options);
  try {
    await assertScreenshotDomSafe(client, sessionId, tracker);
    const metrics = assertLayoutMetrics(
      await readLayoutMetrics(client, sessionId, selector),
      viewport,
      { expectDialog: options.expectDialog === true },
    );
    const screenshot = await client.send("Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: false,
    }, sessionId, 30000);
    const buffer = Buffer.from(screenshot.data, "base64");
    const pixels = inspectPngPixels(buffer, viewport, `browser.screenshot.${state}`);
    const filename = screenshotFilename(state, viewport.name);
    await writeFile(path.join(workDirectory, "screenshots", filename), buffer, { flag: "wx" });
    return Object.freeze({
      state,
      viewport: viewport.name,
      filename,
      byte_length: buffer.length,
      sha256: createHash("sha256").update(buffer).digest("hex"),
      ...pixels,
      layout: metrics,
    });
  } finally {
    await restoreScreenshotDom(client, sessionId);
  }
}


/** 验证活跃 Session 的 Token 只存在于两个约定槽位，不进入 URL、DOM 或 localStorage。 */
async function assertBrowserPrivacy(client, sessionId, tracker) {
  await Promise.all([...tracker.fetchResolutionPromises]);
  settleFetchResolutionFailures(tracker);
  const secretNeedles = JSON.stringify(tracker.secrets.filter(Boolean));
  const tokenNeedles = JSON.stringify([...tracker.browserTokens].filter(Boolean));
  const facts = await evaluate(client, sessionId, `(() => {
    const secrets = ${secretNeedles};
    const tokens = ${tokenNeedles};
    const dom = document.documentElement.outerHTML;
    const local = JSON.stringify(Object.entries(localStorage));
    const sessionEntries = Object.entries(sessionStorage);
    const nonAuthSession = JSON.stringify(sessionEntries.filter(([key]) => !${JSON.stringify(AUTH_STORAGE_KEYS)}.includes(key)));
    return {
      local_storage_count: localStorage.length,
      auth_slot_count: ${JSON.stringify(AUTH_STORAGE_KEYS)}.filter((key) => sessionStorage.getItem(key) !== null).length,
      secret_url_dom_storage_hits: secrets.filter((needle) => [location.href, dom, local, JSON.stringify(sessionEntries)]
        .some((value) => value.includes(needle))).length,
      token_outside_auth_hits: tokens.filter((needle) => [location.href, dom, local, nonAuthSession]
        .some((value) => value.includes(needle))).length,
    };
  })()`, "browser.privacy.dom");
  requireCondition(facts.local_storage_count === 0, "browser.privacy", "local_storage");
  requireCondition(facts.auth_slot_count === 2, "browser.privacy", "auth_slots");
  requireCondition(facts.secret_url_dom_storage_hits === 0, "browser.privacy", "secret_hit");
  requireCondition(facts.token_outside_auth_hits === 0, "browser.privacy", "token_hit");
  requireCondition(tracker.externalRequestCount === 0, "browser.privacy", "external_request");
  requireCondition(tracker.forbiddenQueryCount === 0, "browser.privacy", "forbidden_query");
  requireCondition(tracker.requestPrivacyViolationCount === 0, "browser.privacy", "request_private");
  requireCondition(tracker.consolePrivacyViolationCount === 0, "browser.privacy", "console_private");
  requireCondition(tracker.processPrivacyViolationCount === 0, "browser.privacy", "process_private");
  requireCondition(
    tracker.runtimeProcessPrivacyViolationCount === 0,
    "browser.privacy",
    "runtime_process_private",
  );
  requireCondition(tracker.runtimeExceptionCount === 0, "browser.privacy", "runtime_exception");
  const canceledConsoleNetwork = consoleNetworkFailuresAreCanceled(
    tracker.consoleNetworkFailureCodes,
    tracker.canceledNetworkFailureCodes,
    tracker.uncanceledNetworkFailureCodes,
    tracker.fetchStaleProtocolCount,
  );
  const consoleNetworkReason = consoleNetworkFailureReason(
    tracker.consoleNetworkFailureCodes,
    tracker.canceledNetworkFailureCodes,
    tracker.uncanceledNetworkFailureCodes,
    tracker.fetchStaleProtocolCount,
  );
  const unresolvedConsoleErrorCount = tracker.unexpectedConsoleHttpStatuses.length
    + tracker.unexpectedConsoleOtherCount
    + (canceledConsoleNetwork ? 0 : tracker.consoleNetworkFailureCodes.length);
  // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
  if (unresolvedConsoleErrorCount !== 0) {
    const status = tracker.unexpectedConsoleHttpStatuses[0];
    const network = tracker.consoleNetworkFailureCodes[0];
    const networkCategory = tracker.uncanceledNetworkFailureCategories.values().next().value ?? "unknown";
    const code = status === 401
      ? "console_http_unauthorized"
      : status === 404
        ? "console_http_not_found"
        : Number.isInteger(status) && status >= 500
          ? "console_http_server"
          : Number.isInteger(status)
            ? "console_http_unexpected"
            : network
              ? `console_network_${consoleNetworkReason || network}_${networkCategory}`
              : "console_error";
    throw new BrowserAcceptanceFailure("browser.privacy", code);
  }
  requireCondition(tracker.edgeProcessErrorCount === 0, "browser.privacy", "browser_process");
  requireCondition(
    tracker.fetchResolutionFailureCount === 0,
    "browser.privacy",
    "fetch_resolution",
  );
  return Object.freeze({
    canceled_fetch_resolution_count: tracker.fetchCanceledResolutionCount,
    stale_fetch_protocol_count: tracker.fetchStaleProtocolCount,
    canceled_console_network_error_count: canceledConsoleNetwork
      ? tracker.consoleNetworkFailureCodes.length
      : 0,
    external_request_count: 0,
    forbidden_query_count: 0,
    private_dom_storage_count: 0,
    private_network_count: 0,
    unexpected_console_error_count: 0,
  });
}


/** 读取并冻结验收相关源码哈希，运行结束必须完全相等。 */
async function captureSourceProvenance() {
  const sources = Object.freeze({
    browser_acceptance: fileURLToPath(import.meta.url),
    console_main: path.join(REPOSITORY_ROOT, "console-web", "src", "app", "main.js"),
    page_renderer: path.join(REPOSITORY_ROOT, "console-web", "src", "features", "cache-policies", "renderResponseCache.js"),
    request_log_renderer: path.join(REPOSITORY_ROOT, "console-web", "src", "features", "request-logs", "renderRequestLogPanel.js"),
    runtime_acceptance: path.join(REPOSITORY_ROOT, "scripts", "acceptance", "prd076-runtime-acceptance.py"),
  });
  const result = {};
  for (const [name, filename] of Object.entries(sources)) {
    const content = await readFile(filename);
    result[name] = createHash("sha256").update(content).digest("hex");
  }
  return Object.freeze(result);
}


/** 规范化证据字段名，避免 camelCase、连字符或重复分隔符绕过隐私门禁。 */
function normalizeEvidenceKey(key) {
  return String(key)
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
}


/** 判断字段名是否提供了可写入凭据、正文或私有端点的槽位。 */
function forbiddenEvidenceKeyCategory(key) {
  const normalized = normalizeEvidenceKey(key);
  if (FORBIDDEN_EVIDENCE_KEYS.has(normalized)) return "denylist";
  // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
  if (/(?:^|_)(?:authorization|password|token|secret|credential)(?:_|$)/.test(normalized)) return "auth_material";
  // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
  if (/(?:^|_)(?:provider_(?:api_)?key|virtual_key|api_key|raw_key|key_(?:plaintext|secret|value))(?:_|$)/.test(normalized)) return "key_material";
  if (/(?:^|_)prompt(?:_|$)/.test(normalized)) return "prompt_data";
  if (normalized === "response"
    || /(?:^|_)provider_response(?:_|$)/.test(normalized)
    || /(?:^|_)response_(?:body|content|payload|text)(?:_|$)/.test(normalized)) return "response_data";
  if (/(?:^|_)(?:raw_)?(?:body|content|payload)(?:_|$)/.test(normalized)) return "message_data";
  if (/(?:^|_)(?:endpoint|base_url|database_(?:path|url))(?:_|$)/.test(normalized)) return "location_data";
  return "";
}


/** 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。 */
function isForbiddenEvidenceKey(key) {
  return forbiddenEvidenceKeyCategory(key) !== "";
}


/** 递归拒绝证据中的敏感键、路径/URL 形态和全部一次性私有值。 */
function assertSafeEvidence(value, privateValues = []) {
  const visit = (item) => {
    if (Array.isArray(item)) {
      item.forEach(visit);
      return;
    }
    if (item && typeof item === "object") {
      for (const [key, child] of Object.entries(item)) {
        const forbiddenCategory = forbiddenEvidenceKeyCategory(key);
        requireCondition(
          !isForbiddenEvidenceKey(key),
          "evidence.privacy",
          forbiddenCategory ? `forbidden_key_${forbiddenCategory}` : "forbidden_key",
        );
        visit(child);
      }
      return;
    }
    requireCondition(
      item === null || typeof item === "string" || typeof item === "boolean"
        || (typeof item === "number" && Number.isFinite(item)),
      "evidence.privacy",
      "non_json_value",
    );
  };
  visit(value);
  let serialized;
  try { serialized = JSON.stringify(value); } catch {
    throw new BrowserAcceptanceFailure("evidence.privacy", "not_json");
  }
  const forbiddenPatterns = [
    /Bearer\s+/i,
    /https?:\/\//i,
    /sqlite:\/\//i,
    /[A-Za-z]:[\\/]/,
    /(?:^|[^a-z])sk-[A-Za-z0-9]/i,
  ];
  requireCondition(!forbiddenPatterns.some((pattern) => pattern.test(serialized)), "evidence.privacy", "forbidden_value");
  requireCondition(
    privateValues.every((privateValue) => !privateValue || !serialized.includes(privateValue)),
    "evidence.privacy",
    "private_value",
  );
  return Object.freeze({ exact_safe_evidence: true });
}


/** 根据待发布文件字节生成 canonical SHA256SUMS，不包含清单自身。 */
function buildManifest(files) {
  const entries = [...files.entries()].sort(([left], [right]) => left.localeCompare(right, "en"));
  requireCondition(entries.length >= 2, "evidence.manifest", "empty");
  for (const [name, content] of entries) {
    requireCondition(/^[a-z0-9][a-z0-9.-]{2,100}$/.test(name), "evidence.manifest", "filename");
    requireCondition(Buffer.isBuffer(content) && content.length > 0, "evidence.manifest", "content");
  }
  return entries
    .map(([name, content]) => `${createHash("sha256").update(content).digest("hex")}  ${name}\n`)
    .join("");
}


/** 把已验证截图收进内存，随后可先删除整个私有工作树再发布。 */
async function collectScreenshotFiles(runDirectories, evidence, privateValues) {
  const files = new Map();
  for (const screenshot of evidence.screenshots) {
    const content = await readFile(path.join(runDirectories.workDirectory, "screenshots", screenshot.filename));
    requireCondition(
      createHash("sha256").update(content).digest("hex") === screenshot.sha256,
      "evidence.screenshot",
      "hash_mismatch",
    );
    for (const privateValue of privateValues) {
      if (!privateValue) continue;
      const representations = [
        Buffer.from(privateValue, "utf8"),
        Buffer.from(Buffer.from(privateValue, "utf8").toString("base64"), "ascii"),
        Buffer.from(Buffer.from(privateValue, "utf8").toString("hex"), "ascii"),
      ];
      requireCondition(
        representations.every((needle) => content.indexOf(needle) === -1),
        "evidence.screenshot",
        "private_value",
      );
    }
    files.set(screenshot.filename, content);
  }
  return files;
}


/** 原子发布 exact JSON/PNG/SHA 白名单；任何历史目标或 staging 都使运行失败。 */
async function publishEvidence(runDirectories, evidence, privateValues, screenshotFiles) {
  assertSafeEvidence(evidence, privateValues);
  const files = new Map(screenshotFiles);
  const json = Buffer.from(`${JSON.stringify(evidence, null, 2)}\n`, "utf8");
  files.set("browser-acceptance.json", json);
  const manifest = Buffer.from(buildManifest(files), "ascii");
  await mkdir(runDirectories.stagingDirectory, { recursive: false });
  for (const [name, content] of files) {
    await writeFile(path.join(runDirectories.stagingDirectory, name), content, { flag: "wx" });
  }
  await writeFile(path.join(runDirectories.stagingDirectory, "SHA256SUMS.txt"), manifest, { flag: "wx" });
  const persistedManifest = await readFile(path.join(runDirectories.stagingDirectory, "SHA256SUMS.txt"), "ascii");
  requireCondition(persistedManifest === manifest.toString("ascii"), "evidence.manifest", "persisted_mismatch");
  await rename(runDirectories.stagingDirectory, runDirectories.artifactDirectory);
  return path.join(runDirectories.artifactDirectory, "browser-acceptance.json");
}


/** 等待精确子进程退出；超时后只终止本验收器创建的 owner。 */
async function stopRuntime(runtime) {
  if (!runtime) return;
  if (runtime.child.exitCode !== null) {
    await runtime.client.waitForStdioClose();
    requireCondition(runtime.child.exitCode === 0, "runtime.stop", "exit");
    return;
  }
  runtime.client.stop();
  const exited = await Promise.race([
    new Promise((resolve) => runtime.child.once("exit", () => resolve(true))),
    new Promise((resolve) => setTimeout(() => resolve(false), 30000)),
  ]);
  if (!exited && runtime.child.exitCode === null) runtime.child.kill();
  if (!exited) {
    await Promise.race([
      new Promise((resolve) => runtime.child.once("exit", () => resolve(true))),
      new Promise((resolve) => setTimeout(() => resolve(false), 10000)),
    ]);
  }
  await runtime.client.waitForStdioClose();
  requireCondition(runtime.child.exitCode === 0, "runtime.stop", "exit");
}


/** 停服后统一核验进程 stderr 与 Runtime 非数据库文件，结果只保留聚合计数。 */
async function verifyRuntimePrivacy(workDirectory, runtime, privateValues) {
  const files = await scanRuntimePrivateFiles(path.join(workDirectory, "runtime"), privateValues);
  const processViolationCount = runtime.client.processPrivacyViolationCount;
  requireCondition(processViolationCount === 0, "privacy.runtime_process", "plaintext_found");
  requireCondition(files.violationCount === 0, "privacy.runtime_files", "plaintext_found");
  return Object.freeze({
    process_stream_plaintext_matches: 0,
    runtime_file_plaintext_matches: 0,
    runtime_files_scanned: files.filesScanned,
  });
}


/** 有界等待并只按本验收器持有的精确浏览器 PID 收拢整棵子进程树。 */
async function waitForOwnedBrowserExit(child, timeoutMs = 10000) {
  if (!child) return;
  if (child.exitCode === null) {
    const closed = await Promise.race([
      new Promise((resolve) => child.once("close", () => resolve(true))),
      new Promise((resolve) => setTimeout(() => resolve(false), timeoutMs)),
    ]);
    if (!closed && child.exitCode === null) {
      requireCondition(
        Number.isInteger(child.pid) && child.pid > 0
          && path.resolve(child.spawnfile).toLowerCase() === path.resolve(CHROME).toLowerCase(),
        "cleanup.browser",
        "owner_mismatch",
      );
      if (process.platform === "win32") {
        const stopped = spawnSync(
          "taskkill.exe",
          ["/PID", String(child.pid), "/T", "/F"],
          { stdio: "ignore", windowsHide: true, timeout: 10000 },
        );
        requireCondition(stopped.status === 0 || child.exitCode !== null, "cleanup.browser", "stop_failed");
      } else {
        child.kill("SIGKILL");
      }
      await Promise.race([
        new Promise((resolve) => child.once("close", resolve)),
        new Promise((resolve) => setTimeout(resolve, 10000)),
      ]);
    }
  }
  requireCondition(child.exitCode !== null, "cleanup.browser", "exit_timeout");
}


/** 读取一个固定本地健康端点，只保存状态码。 */
async function readHealthStatus(url, stage) {
  let response;
  try {
    response = await fetch(url, { redirect: "error", signal: AbortSignal.timeout(5000) });
  } catch {
    throw new BrowserAcceptanceFailure(stage, "network");
  }
  requireCondition(response.status === 200, stage, "status");
  await response.body?.cancel().catch(() => undefined);
  return 200;
}


/** 尝试独占绑定一个本地端口并立即释放，不枚举、终止或接管未知进程。 */
async function fixedPortAvailable(port) {
  return new Promise((resolve) => {
    const server = createServer();
    server.once("error", () => resolve(false));
    server.listen({ host: "127.0.0.1", port, exclusive: true }, () => {
      server.close(() => resolve(true));
    });
  });
}


/** 有界等待本验收器全部固定端口释放。 */
async function waitForFixedPortsReleased(timeoutMs = 20000) {
  const ports = [8000, 8001, 5173, 18855, 18856, DEBUG_PORT];
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const available = await Promise.all(ports.map((port) => fixedPortAvailable(port)));
    if (available.every(Boolean)) return true;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new BrowserAcceptanceFailure("cleanup.ports", "not_released");
}


/** 执行 PAGE-106 与 RequestLog 的真实浏览器工作流，finally 只关闭自有 Chrome。 */
async function executeBrowserWorkflow(
  runDirectories,
  runtime,
  adminToken,
  financeToken,
  viewerToken,
  originalRole,
) {
  requireCondition(existsSync(CHROME), "browser.chrome", "missing");
  const tracker = createTracker(runtime);
  const config = Object.freeze({
    artifactDirectory: path.join(runDirectories.workDirectory, "browser"),
    debugPort: DEBUG_PORT,
    edgePath: CHROME,
  });
  let browserChild = null;
  let browserClient = null;
  let pageClient = null;
  let pageSession = null;
  const screenshots = [];
  let financeLoggedIn = false;
  let viewerLoggedIn = false;
  let adminLoggedIn = false;
  try {
    for (const directory of [
      path.join(config.artifactDirectory, "profile"),
      path.join(config.artifactDirectory, "browser-cache"),
      path.join(config.artifactDirectory, "crash-dumps"),
      path.join(config.artifactDirectory, "browser-home", "local"),
      path.join(config.artifactDirectory, "browser-home", "roaming"),
    ]) await mkdir(directory, { recursive: true });
    browserChild = launchEdge(config, tracker);
    const webSocketUrl = await waitForDebugger(DEBUG_PORT, 60000);
    browserClient = new CdpClient(webSocketUrl);
    await browserClient.connect();
    pageSession = await createPageSession(browserClient, DEBUG_PORT);
    pageClient = pageSession.client;
    bindBrowserTracker(pageClient, pageSession, config, tracker);
    await openConsole(pageClient, pageSession.sessionId);

    await loginThroughConsole(
      pageClient,
      pageSession.sessionId,
      tracker,
      { email: "finance-prd076@local.invalid", password: runtime.ready.finance_password },
      "finance_admin",
    );
    financeLoggedIn = true;
    await openResponseCache(pageClient, pageSession.sessionId, tracker);
    const emptyFacts = await readPolicyFacts(pageClient, pageSession.sessionId);
    requireCondition(
      emptyFacts.root_visible && emptyFacts.policy_row_count >= 2
        && emptyFacts.version === 0 && !emptyFacts.dirty,
      "browser.policy.empty",
      "state",
    );
    screenshots.push(await captureState(
      pageClient,
      pageSession.sessionId,
      tracker,
      runDirectories.workDirectory,
      "empty-policy",
      VIEWPORTS.desktop,
      "[data-response-cache-root]",
    ));

    await draftInitialPolicy(pageClient, pageSession.sessionId);
    screenshots.push(await captureState(
      pageClient,
      pageSession.sessionId,
      tracker,
      runDirectories.workDirectory,
      "editing-policy",
      VIEWPORTS.mobile,
      "[data-response-cache-root]",
    ));
    await setViewport(pageClient, pageSession.sessionId, VIEWPORTS.desktop);
    await submitPolicy(pageClient, pageSession.sessionId, tracker, 200, "browser.policy.create");
    const createdFacts = await readPolicyFacts(pageClient, pageSession.sessionId);
    requireCondition(createdFacts.version === 1 && !createdFacts.dirty, "browser.policy.create", "version");
    screenshots.push(await captureState(
      pageClient,
      pageSession.sessionId,
      tracker,
      runDirectories.workDirectory,
      "configured-policy",
      VIEWPORTS.desktop,
      "[data-response-cache-root]",
    ));

    const conflict = await exerciseConflict(
      pageClient,
      pageSession.sessionId,
      tracker,
      adminToken,
      runtime,
      async () => {
        screenshots.push(await captureState(
          pageClient,
          pageSession.sessionId,
          tracker,
          runDirectories.workDirectory,
          "version-conflict",
          VIEWPORTS.desktop,
          "[data-response-cache-root]",
        ));
      },
    );

    const providerBefore = await runtime.client.command("provider_stats", "provider_stats");
    requireCondition(providerBefore.calls === 0, "browser.cache_flow", "provider_initial");
    const missRequestId = "prd076-browser-fixture-miss";
    const hitRequestId = "prd076-browser-fixture-hit";
    await invokeGateway(runtime, missRequestId);
    const providerAfterMiss = await runtime.client.command("provider_stats", "provider_stats");
    requireCondition(providerAfterMiss.calls === 1, "browser.cache_flow", "provider_miss");
    await invokeGateway(runtime, hitRequestId);
    const providerAfterHit = await runtime.client.command("provider_stats", "provider_stats");
    requireCondition(providerAfterHit.calls === 1, "browser.cache_flow", "provider_hit");
    const readyEntryCount = await refreshPolicyAndRequireEntry(pageClient, pageSession.sessionId, tracker);

    const keyboard = await exerciseDialogKeyboard(pageClient, pageSession.sessionId);
    await prepareTagInvalidation(pageClient, pageSession.sessionId);
    screenshots.push(await captureState(
      pageClient,
      pageSession.sessionId,
      tracker,
      runDirectories.workDirectory,
      "invalidate-dialog",
      VIEWPORTS.desktop,
      ".response-cache-dialog",
      { expectDialog: true },
    ));
    screenshots.push(await captureState(
      pageClient,
      pageSession.sessionId,
      tracker,
      runDirectories.workDirectory,
      "invalidate-actions",
      VIEWPORTS.mobile,
      ".response-cache-dialog",
      { expectDialog: true, dialogActions: true },
    ));
    const tagInvalidation = await submitInvalidation(
      pageClient,
      pageSession.sessionId,
      tracker,
      200,
      "browser.invalidation.submit",
    );
    await setViewport(pageClient, pageSession.sessionId, VIEWPORTS.desktop);

    const permissionTiers = await exercisePermissionTiers(
      pageClient,
      pageSession.sessionId,
      tracker,
      adminToken,
      financeToken,
      originalRole,
      runtime,
    );
    const financePrivacy = await assertBrowserPrivacy(pageClient, pageSession.sessionId, tracker);
    await logoutThroughConsole(pageClient, pageSession.sessionId, tracker, "browser.logout.finance");
    financeLoggedIn = false;

    await loginThroughConsole(
      pageClient,
      pageSession.sessionId,
      tracker,
      { email: "viewer-prd076@local.invalid", password: runtime.ready.viewer_password },
      "viewer",
    );
    viewerLoggedIn = true;
    const viewerDom = await evaluate(pageClient, pageSession.sessionId, `(() => ({
      navigation_absent: !document.querySelector('[data-console-nav="response-cache"]'),
      panel_absent: !document.querySelector('[data-response-cache-root]'),
    }))()`, "browser.viewer.dom");
    requireCondition(viewerDom.navigation_absent && viewerDom.panel_absent, "browser.viewer", "protected_dom");
    await controlJson("/cache-policies", {
      token: viewerToken,
      expectedStatus: 403,
      expectedCode: "auth.permission_denied",
    }, "viewer.read_denied");
    screenshots.push(await captureState(
      pageClient,
      pageSession.sessionId,
      tracker,
      runDirectories.workDirectory,
      "viewer-denied",
      VIEWPORTS.desktop,
      ".console-main",
    ));
    const viewerPrivacy = await assertBrowserPrivacy(pageClient, pageSession.sessionId, tracker);
    await logoutThroughConsole(pageClient, pageSession.sessionId, tracker, "browser.logout.viewer");
    viewerLoggedIn = false;

    await loginThroughConsole(
      pageClient,
      pageSession.sessionId,
      tracker,
      { email: "admin@metahub.local", password: runtime.ready.admin_password },
      "admin",
    );
    adminLoggedIn = true;
    const requestLog = await openHitRequestLog(pageClient, pageSession.sessionId, tracker, hitRequestId);
    screenshots.push(await captureState(
      pageClient,
      pageSession.sessionId,
      tracker,
      runDirectories.workDirectory,
      "request-log-hit",
      VIEWPORTS.desktop,
      ".response-cache-evidence",
    ));
    screenshots.push(await captureState(
      pageClient,
      pageSession.sessionId,
      tracker,
      runDirectories.workDirectory,
      "request-log-hit",
      VIEWPORTS.mobile,
      ".response-cache-evidence",
    ));
    const adminPrivacy = await assertBrowserPrivacy(pageClient, pageSession.sessionId, tracker);
    await logoutThroughConsole(pageClient, pageSession.sessionId, tracker, "browser.logout.admin");
    adminLoggedIn = false;

    requireCondition(screenshots.length === 9, "browser.screenshots", "count");
    requireCondition(screenshots.every((item) => item.non_blank_pixels === true), "browser.screenshots", "blank");
    requireCondition(tracker.expectedHttpErrorCount >= 2, "browser.console", "expected_errors_missing");
    return Object.freeze({
      screenshots: Object.freeze(screenshots),
      checks: Object.freeze({
        services: Object.freeze({ gateway: 200, control: 200, console: 200 }),
        policy_workflow: Object.freeze({
          empty_version: 0,
          created_version: 1,
          final_version: 3,
          configured_entry_count_positive: readyEntryCount >= 1,
          ui_create_succeeded: true,
          ui_update_succeeded: true,
          ...conflict,
        }),
        cache_flow: Object.freeze({
          miss_status: 200,
          hit_status: 200,
          calls_before: 0,
          calls_after_miss: 1,
          calls_after_hit: 1,
          tag_invalidation_status: 200,
          ...tagInvalidation,
        }),
        request_log: requestLog,
        permissions: Object.freeze({
          ...permissionTiers,
          viewer_backend_status: 403,
          viewer_navigation_absent: true,
        }),
        interaction: keyboard,
        privacy: Object.freeze({
          finance: financePrivacy,
          viewer: viewerPrivacy,
          admin: adminPrivacy,
          screenshot_public_fixture_only: true,
        }),
        browser_errors: Object.freeze({
          expected_http_error_count: tracker.expectedHttpErrorCount,
          unexpected_console_error_count: 0,
          runtime_exception_count: 0,
        }),
      }),
    });
  } finally {
    if (financeLoggedIn || viewerLoggedIn || adminLoggedIn) {
      try { await logoutThroughConsole(pageClient, pageSession?.sessionId, tracker, "browser.logout.failure"); } catch { /* 临时数据库最终仍被删除。 */ }
    }
    if (browserClient) {
      try { await browserClient.send("Browser.close"); } catch { /* 关闭竞态由精确子进程等待收口。 */ }
      browserClient.close();
    }
    pageClient?.close();
    if (browserChild) await waitForOwnedBrowserExit(browserChild, 10000);
  }
}


/** 执行真实 Browser、恢复共享角色、清理私有工作树，再原子发布最终证据。 */
async function executeAcceptance(runId) {
  const sourceProvenance = await captureSourceProvenance();
  const runDirectories = await createRunDirectories(runId);
  let runtime = null;
  let adminToken = "";
  let financeToken = "";
  let viewerToken = "";
  let originalRole = null;
  let workflow = null;
  let privateValues = [];
  let runtimePrivacy = null;
  let cleanupFailure = null;
  try {
    runtime = await startRuntime(runDirectories.workDirectory);
    privateValues = [...runtime.ready.privateValues];
    const health = await Promise.all([
      readHealthStatus(`${GATEWAY_ORIGIN}/health`, "health.gateway"),
      readHealthStatus(`${CONTROL_ORIGIN}/health`, "health.control"),
      readHealthStatus(`${CONSOLE_ORIGIN}/`, "health.console"),
    ]);
    requireCondition(health.every((status) => status === 200), "health", "contract");
    adminToken = await loginControl("admin@metahub.local", runtime.ready.admin_password, "session.admin");
    financeToken = await loginControl("finance-prd076@local.invalid", runtime.ready.finance_password, "session.finance");
    viewerToken = await loginControl("viewer-prd076@local.invalid", runtime.ready.viewer_password, "session.viewer");
    originalRole = await readFinanceRole(adminToken);
    requireCondition(
      RESPONSE_CACHE_PERMISSIONS.every((code) => originalRole.permissionCodes.includes(code)),
      "permissions.baseline",
      "missing",
    );
    workflow = await executeBrowserWorkflow(
      runDirectories,
      runtime,
      adminToken,
      financeToken,
      viewerToken,
      originalRole,
    );
  } finally {
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    if (adminToken && originalRole) {
      try {
        const current = await readFinanceRole(adminToken);
        // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
        if (!samePermissionCodes(current.permissionCodes, originalRole.permissionCodes)) {
          await replaceFinanceRole(adminToken, current, originalRole.permissionCodes, "final_restore");
        }
      } catch {
        cleanupFailure ??= new BrowserAcceptanceFailure("cleanup.permissions", "restore_failed");
      }
    }
    for (const [token, stage] of [
      [viewerToken, "cleanup.session.viewer"],
      [financeToken, "cleanup.session.finance"],
      [adminToken, "cleanup.session.admin"],
    ]) {
      // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
      if (token) {
        try { await logoutControl(token, stage); } catch {
          cleanupFailure ??= new BrowserAcceptanceFailure(stage, "logout_failed");
        }
      }
    }
    if (runtime) {
      try { await stopRuntime(runtime); } catch {
        cleanupFailure ??= new BrowserAcceptanceFailure("cleanup.runtime", "stop_failed");
      }
      try {
        runtimePrivacy = await verifyRuntimePrivacy(
          runDirectories.workDirectory,
          runtime,
          privateValues,
        );
      } catch (error) {
        cleanupFailure ??= error instanceof BrowserAcceptanceFailure
          ? error
          : new BrowserAcceptanceFailure("privacy.runtime", "unexpected");
      }
    }
    cleanupFailure = await cleanupFailedRunDirectories(
      runDirectories,
      workflow,
      cleanupFailure,
    );
    if (cleanupFailure) throw cleanupFailure;
  }
  requireCondition(workflow !== null, "browser.workflow", "missing");
  requireCondition(runtimePrivacy !== null, "privacy.runtime", "missing");
  await waitForFixedPortsReleased();
  requireCondition(
    JSON.stringify(await captureSourceProvenance()) === JSON.stringify(sourceProvenance),
    "evidence.provenance",
    "source_changed",
  );
  const evidence = Object.freeze({
    schema_version: 1,
    prd: "PRD-076",
    page: "PAGE-106",
    status: "passed",
    all_108_prds_complete: false,
    source_provenance: Object.freeze({ algorithm: "sha256", ...sourceProvenance }),
    checks: Object.freeze({
      ...workflow.checks,
      privacy: Object.freeze({
        ...workflow.checks.privacy,
        runtime: runtimePrivacy,
      }),
      cleanup: Object.freeze({
        owned_browser_stopped: true,
        owned_services_stopped: true,
        fixed_ports_released: true,
        role_permissions_restored: true,
        private_runtime_removed: true,
        browser_profile_removed: true,
      }),
    }),
    screenshots: workflow.screenshots,
  });
  try {
    const screenshotFiles = await collectScreenshotFiles(runDirectories, evidence, privateValues);
    await removeOwnedDirectory(runDirectories.workDirectory, WORK_ROOT, "cleanup.work");
    const evidencePath = await publishEvidence(runDirectories, evidence, privateValues, screenshotFiles);
    return Object.freeze({ evidence, evidencePath });
  } catch (error) {
    try { await removeOwnedDirectory(runDirectories.workDirectory, WORK_ROOT, "cleanup.post_work"); } catch { /* 原失败优先。 */ }
    try { await removeOwnedDirectory(runDirectories.stagingDirectory, ARTIFACT_ROOT, "cleanup.post_staging"); } catch { /* 原失败优先。 */ }
    // 浏览器验收只保留脱敏状态和布局计数，身份变化必须清理旧页面所有者。
    throw error;
  }
}


/** CLI 成功只输出固定摘要；失败只输出安全 stage/code。 */
async function main(argv = process.argv.slice(2)) {
  try {
    const arguments_ = parseArguments(argv);
    const result = await executeAcceptance(arguments_.runId);
    process.stdout.write(`${JSON.stringify({
      prd: "PRD-076",
      page: "PAGE-106",
      status: "passed",
      screenshot_count: result.evidence.screenshots.length,
      evidence: "browser-acceptance.json",
    })}\n`);
    return 0;
  } catch (error) {
    const failure = normalizeBrowserFailure(error);
    process.stderr.write(`${JSON.stringify({ status: "failed", stage: failure.stage, code: failure.code })}\n`);
    return 1;
  }
}


export {
  ARTIFACT_ROOT,
  BrowserAcceptanceFailure,
  PUBLIC_POLICY,
  RESPONSE_CACHE_PERMISSIONS,
  RuntimeHarnessClient,
  SCREENSHOT_MASK_LABELS,
  VIEWPORTS,
  WORK_ROOT,
  assertLayoutMetrics,
  assertStaleConflictFacts,
  assertTagInvalidationFacts,
  assertSafeEvidence,
  browserRequestAllowed,
  buildManifest,
  classifyBrowserConsoleError,
  consoleNetworkFailuresAreCanceled,
  consoleNetworkFailureReason,
  cleanupFailedRunDirectories,
  countLayoutOverlaps,
  countPrivateValueViolations,
  isStaleFetchProtocolFailure,
  normalizeBrowserFailure,
  normalizeLoopbackOrigin,
  normalizePermissionCodes,
  parseArguments,
  permissionRevisionExpression,
  policyBody,
  resolveRunDirectory,
  requiresResponseCachePrivateBoundary,
  safeRunId,
  scanRuntimePrivateFiles,
  samePermissionCodes,
  screenshotFilename,
  trackedRequestCategory,
  waitForTrackedNetworkIdle,
};


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main();
}
