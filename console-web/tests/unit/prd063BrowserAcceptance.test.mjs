/**
 * PRD-063 PAGE-090 浏览器验收器纯合同测试。
 *
 * 测试不启动服务、浏览器或文件写入，只验证路径、loopback 网络、目录 DTO、布局、证据隐私和源码门禁。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  BrowserAcceptanceFailure,
  CATALOG_HASH,
  CATALOG_VERSION,
  VIEWPORTS,
  adapterSpecRequestView,
  assertLayoutMetrics,
  assertSafeEvidence,
  browserRequestAllowed,
  buildBrowserEnvironment,
  classifyBrowserConsoleMessage,
  normalizeLoopbackOrigin,
  pinBrowserWritableRoots,
  resolveArtifactDirectory,
  validateAdapterCatalogPayload,
} from "../../../scripts/acceptance/prd063-browser-acceptance.mjs";

/** 从测试文件位置计算仓库根，避免依赖执行命令的当前目录。 */
const REPOSITORY_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
/** 九类 Provider 到产品分类的测试映射。 */
const CLASSIFICATIONS = Object.freeze({
  zhipu: "model_vendor_cloud",
  dashscope: "model_vendor_cloud",
  wenxin: "model_vendor_cloud",
  volcengine: "model_vendor_cloud",
  hunyuan: "model_vendor_cloud",
  deepseek: "model_vendor_cloud",
  openai_compatible: "local_compatible",
  custom: "customer_managed",
  migration_compatible: "migration_platform",
});
/** 浏览器 API 无筛选读取应返回的完整九类集合。 */
const ALL_TYPES = Object.freeze(Object.keys(CLASSIFICATIONS));


/** 构造一条完整公开 Adapter 目录合同。 */
function makeSpec(providerType) {
  const migration = providerType === "migration_compatible";
  const generic = providerType === "custom" || migration;
  return {
    spec_id: `adapter-spec-${providerType}-v1`,
    tenant_id: "tenant_contract",
    provider_type: providerType,
    display_name: `公开目录 ${providerType}`,
    classification: CLASSIFICATIONS[providerType],
    adapter_name: generic ? "generic_http" : "openai_compatible",
    adapter_version: "1.0.0",
    protocol_family: generic ? "generic_json" : "openai_chat_completions",
    allowed_auth_types: providerType === "openai_compatible" ? ["none", "bearer"] : ["bearer"],
    model_id_strategy: "provider_model_id",
    usage_profile: generic ? "generic_usage_v1" : "openai_compatible_usage_v1",
    error_profile: "provider_error_map_v1",
    stream_profile: generic ? "generic_json_sse_v1" : "openai_sse_final_usage_v1",
    probe_profile: generic ? "generic_post_probe" : "openai_models",
    price_fields: ["input_price_per_1k", "output_price_per_1k", "currency", "price_schema_version"],
    platform_examples: migration ? ["OpenRouter", "硅基流动"] : [],
    status: migration ? "optional" : "active",
    contract_case_id: `prd063_${providerType}_minimum_contract`,
  };
}


/** 构造指定响应分类的真实 API 形状；客户视图对应无查询的完整九类目录。 */
function catalogPayload(view = "customer") {
  const responseTypes = view === "default"
    ? ALL_TYPES.filter((providerType) => CLASSIFICATIONS[providerType] === "model_vendor_cloud")
    : (view === "migration" ? ["migration_compatible"] : ALL_TYPES);
  return {
    catalog: {
      version: CATALOG_VERSION,
      hash: CATALOG_HASH,
      activated_at: "2026-08-01T00:00:00Z",
    },
    specs: responseTypes.map(makeSpec),
    request_id: "req_prd063_browser_contract",
    trace_id: "trc_prd063_browser_contract",
  };
}


/** 构造最小 Playwright Request 替身，Header 值只用于布尔合同判断。 */
function requestStub(url, overrides = {}) {
  return {
    url: () => url,
    method: () => overrides.method ?? "GET",
    headers: () => ({
      authorization: overrides.authorization ?? "Bearer session-contract-value",
      "x-request-id": overrides.requestId ?? "console_adapter_specs_1785500000000",
    }),
  };
}


/** 证据目录只接受固定根下的全新一级 run-id。 */
test("PRD-063 artifact path is fixed under the dedicated evidence root", () => {
  const target = path.join(REPOSITORY_ROOT, ".artifacts", "prd063-browser", "run-contract-001");
  assert.equal(resolveArtifactDirectory(target, REPOSITORY_ROOT), target);
  for (const rejected of [
    path.join(REPOSITORY_ROOT, ".artifacts", "prd063-browser"),
    path.join(REPOSITORY_ROOT, ".artifacts", "outside", "run-contract-001"),
    path.join(target, "nested"),
  ]) {
    assert.throws(
      () => resolveArtifactDirectory(rejected, REPOSITORY_ROOT),
      (error) => error instanceof BrowserAcceptanceFailure && error.code === "outside_or_nested",
    );
  }
});


/** 只放行三个 localhost Origin，三种 Adapter 请求必须精确匹配查询、Bearer 与 request_id。 */
test("PRD-063 loopback and adapter request contracts fail closed", () => {
  const consoleOrigin = normalizeLoopbackOrigin("http://localhost:5173", 5173);
  const controlOrigin = normalizeLoopbackOrigin("http://localhost:8001", 8001);
  assert.equal(consoleOrigin, "http://localhost:5173");
  for (const value of [
    "http://127.0.0.1:5173",
    "https://localhost:5173",
    "http://localhost:5173/path",
    "http://user:pass@localhost:5173",
  ]) assert.throws(() => normalizeLoopbackOrigin(value, 5173), BrowserAcceptanceFailure);

  const allowed = new Set([consoleOrigin, controlOrigin, "http://localhost:8000"]);
  assert.equal(browserRequestAllowed("http://localhost:8001/adapter-specs", allowed), true);
  assert.equal(browserRequestAllowed("https://example.com/adapter-specs", allowed), false);
  assert.equal(browserRequestAllowed("http://localhost:8001/adapter-specs?tenant_id=other", allowed), false);
  assert.equal(browserRequestAllowed("http://localhost:8001/adapter-specs?token=value", allowed), false);

  assert.equal(adapterSpecRequestView(
    requestStub("http://localhost:8001/adapter-specs?classification=model_vendor_cloud"),
    controlOrigin,
  ), "default");
  assert.equal(adapterSpecRequestView(
    requestStub("http://localhost:8001/adapter-specs"),
    controlOrigin,
  ), "customer");
  assert.equal(adapterSpecRequestView(
    requestStub("http://localhost:8001/adapter-specs?classification=migration_platform"),
    controlOrigin,
  ), "migration");
  assert.equal(adapterSpecRequestView(
    requestStub("http://localhost:8001/adapter-specs?classification=model_vendor_cloud&status=active"),
    controlOrigin,
  ), null);
  assert.equal(adapterSpecRequestView(
    requestStub("http://localhost:8001/adapter-specs", { authorization: "" }),
    controlOrigin,
  ), null);
  assert.equal(adapterSpecRequestView(
    requestStub("http://localhost:8001/adapter-specs", { requestId: "bad" }),
    controlOrigin,
  ), null);
});


/** 浏览器子进程不得继承宿主秘密，六个可写目录必须固定到当前批次。 */
test("PRD-063 browser environment strips secrets and pins writable roots", () => {
  const artifact = path.join(REPOSITORY_ROOT, ".artifacts", "prd063-browser", "run-env-contract");
  const environment = buildBrowserEnvironment({
    PATH: "runtime-path",
    METAHUB_ACCEPTANCE_ADMIN_PASSWORD: "must-not-pass",
    OPENAI_API_KEY: "must-not-pass",
    SESSION_TOKEN: "must-not-pass",
    AWS_ACCESS_KEY_ID: "must-not-pass",
    GITHUB_PAT: "must-not-pass",
    PUBLIC_SETTING: "must-not-pass",
  }, artifact);
  assert.equal(environment.PATH, "runtime-path");
  for (const key of [
    "METAHUB_ACCEPTANCE_ADMIN_PASSWORD",
    "OPENAI_API_KEY",
    "SESSION_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "GITHUB_PAT",
    "PUBLIC_SETTING",
  ]) assert.equal(Object.hasOwn(environment, key), false);
  assert.equal(environment.TEMP, artifact);
  assert.equal(environment.TMP, artifact);
  assert.equal(environment.HOME, path.join(artifact, "browser-home"));
  assert.equal(environment.USERPROFILE, path.join(artifact, "browser-home"));
  assert.equal(environment.LOCALAPPDATA, path.join(artifact, "browser-home", "local"));
  assert.equal(environment.APPDATA, path.join(artifact, "browser-home", "roaming"));
  const processEnvironment = { PATH: "runtime-path", TEMP: "outside", HOME: "outside" };
  assert.equal(pinBrowserWritableRoots(processEnvironment, artifact), processEnvironment);
  assert.equal(processEnvironment.PATH, "runtime-path");
  assert.equal(processEnvironment.TEMP, artifact);
  assert.equal(processEnvironment.TMP, artifact);
  assert.equal(processEnvironment.HOME, path.join(artifact, "browser-home"));
});


/** 冻结目录合同覆盖六类厂家、两类客户 Endpoint 和一个迁移入口。 */
test("PRD-063 catalog payload validates all three reads and nine type boundaries", () => {
  assert.deepEqual(validateAdapterCatalogPayload(catalogPayload("default"), "default"), {
    view: "default",
    entry_count: 6,
  });
  assert.deepEqual(validateAdapterCatalogPayload(catalogPayload("customer"), "customer"), {
    view: "customer",
    entry_count: 9,
  });
  assert.deepEqual(validateAdapterCatalogPayload(catalogPayload("migration"), "migration"), {
    view: "migration",
    entry_count: 1,
  });
});


/** 目录哈希、字段、分类、唯一性、迁移边界或隐私任一漂移都必须拒绝整批响应。 */
test("PRD-063 catalog payload rejects damaged or sensitive contracts", () => {
  const cases = [];
  const badHash = catalogPayload();
  badHash.catalog.hash = "a".repeat(64);
  cases.push(badHash);
  const missingField = catalogPayload();
  delete missingField.specs[0].usage_profile;
  cases.push(missingField);
  const duplicateType = catalogPayload();
  duplicateType.specs[1] = { ...duplicateType.specs[0] };
  cases.push(duplicateType);
  const wrongClassification = catalogPayload();
  wrongClassification.specs[0].classification = "migration_platform";
  cases.push(wrongClassification);
  const mixedTenant = catalogPayload();
  mixedTenant.specs[0].tenant_id = "tenant_other";
  cases.push(mixedTenant);
  const leakedAddress = catalogPayload();
  leakedAddress.specs[0].base_url = "https://private.invalid";
  cases.push(leakedAddress);
  const duplicateAuth = catalogPayload();
  duplicateAuth.specs[0].allowed_auth_types = ["bearer", "bearer"];
  cases.push(duplicateAuth);
  const conflatedPlatform = catalogPayload();
  conflatedPlatform.specs[0].platform_examples = ["OpenRouter", "硅基流动"];
  cases.push(conflatedPlatform);
  for (const payload of cases) {
    assert.throws(
      () => validateAdapterCatalogPayload(payload, "customer"),
      (error) => error instanceof BrowserAcceptanceFailure,
    );
  }
});


/** 布局合同拒绝页面横溢、侧栏遮挡、按钮裁切和移动合同表横溢。 */
test("PRD-063 layout contract covers desktop and mobile overflow", () => {
  const metrics = {
    viewport_width: 1280,
    viewport_height: 720,
    document_scroll_width: 1280,
    body_scroll_width: 1280,
    root_visible: true,
    root_horizontal_inside: true,
    root_scroll_width: 980,
    root_client_width: 980,
    table_scroll_width: 1080,
    table_client_width: 940,
    navigation_main_overlap_area: 0,
    overflowing_button_count: 0,
    empty_button_text_count: 0,
    view_button_count: 3,
    visible_row_count: 6,
  };
  assert.equal(assertLayoutMetrics(metrics, VIEWPORTS[0]), metrics);
  const mobile = {
    ...metrics,
    viewport_width: 390,
    viewport_height: 844,
    document_scroll_width: 390,
    body_scroll_width: 390,
    root_scroll_width: 358,
    root_client_width: 358,
    table_scroll_width: 358,
    table_client_width: 358,
    visible_row_count: 1,
  };
  assert.equal(assertLayoutMetrics(mobile, VIEWPORTS[1]), mobile);
  assert.throws(
    () => assertLayoutMetrics({ ...mobile, table_scroll_width: 400 }, VIEWPORTS[1]),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "mobile_table_overflow",
  );
  assert.throws(
    () => assertLayoutMetrics({ ...metrics, overflowing_button_count: 1 }, VIEWPORTS[0]),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "button_overflow",
  );
});


/** Console error 与证据对象都必须失败关闭，不能保留敏感键、原值或 Bearer 文本。 */
test("PRD-063 console and evidence privacy contracts fail closed", () => {
  assert.equal(classifyBrowserConsoleMessage({ type: "log" }), "ignored");
  assert.equal(classifyBrowserConsoleMessage({ type: "warning" }), "ignored");
  assert.equal(classifyBrowserConsoleMessage({ type: "error" }), "unexpected_error");
  const evidence = {
    prd_id: "PRD-063",
    status: "passed",
    checks: { catalog: { read_entry_counts: [6, 9, 1], all_nine_types_visible: true } },
  };
  assert.equal(assertSafeEvidence(evidence, ["not-present"]), evidence);
  for (const unsafe of [
    { tenant_id: "masked" },
    { endpoint_url: "masked" },
    { value: "Bearer session-contract-value" },
  ]) assert.throws(() => assertSafeEvidence(unsafe), BrowserAcceptanceFailure);
  assert.throws(() => assertSafeEvidence(evidence, ["PRD-063"]), BrowserAcceptanceFailure);
});


/** 源码门禁锁定证据根、链接校验、独占发布、双视口、三分类、隐私和注销流程。 */
test("PRD-063 acceptance source contains the complete safe browser workflow", async () => {
  const source = await readFile(
    new URL("../../../scripts/acceptance/prd063-browser-acceptance.mjs", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(source, /(?:[\\/])\.tmp(?:[\\/]|["'`])|C:\\\\tmp/i);
  assert.match(source, /\.artifacts["'],\s*["']prd063-browser/);
  assert.match(source, /realpath\(directory\)/);
  assert.match(source, /already_exists/);
  assert.match(source, /buildBrowserEnvironment\(process\.env, config\.artifactDirectory\)/);
  assert.match(source, /pinBrowserWritableRoots\(process\.env, config\.artifactDirectory\)/);
  assert.match(source, /flag:\s*"wx"/);
  assert.match(source, /\/adapter-specs/);
  assert.match(source, /classification:\s*"model_vendor_cloud"/);
  assert.match(source, /classification:\s*"migration_platform"/);
  assert.match(source, /data-adapter-spec-view="customer"/);
  assert.match(source, /data-adapter-spec-view="migration"/);
  assert.match(source, /OpenRouter、硅基流动只用于对标或受控迁移兼容/);
  assert.match(source, /desktop-1280x720/);
  assert.match(source, /mobile-390x844/);
  assert.match(source, /inspectPngPixels/);
  assert.match(source, /AUTH_STORAGE_KEYS/);
  assert.match(source, /Promise\.all\(\[\s*page\.waitForResponse[\s\S]*?#logout-button/);
});
