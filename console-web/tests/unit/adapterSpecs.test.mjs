// PRD-063 PAGE-090 专项覆盖客户端白名单、目录合同、权限、XSS、分类边界和迟到响应 owner。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  renderAdapterSpecsLoading,
  renderDeveloperStart,
} from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  adapterSpecApiFilters,
  adapterSpecFilterKey,
  adapterSpecIdentitySnapshot,
  canonicalAdapterSpecFilters,
  createAdapterSpecPageState,
} from "../../src/features/adapter-specs/adapterSpecPageState.js";
import { renderAdapterSpecs } from "../../src/features/adapter-specs/renderAdapterSpecs.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  listAdapterSpecs,
  sanitizeAdapterSpecPayload,
} from "../../src/lib/adapterSpecs.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

// 固定 hash 只用于构造完整目录身份，不代表任何真实租户或已发布 catalog。
const CATALOG_HASH = "a".repeat(64);

/** 构造带完整租户、Session、权限 revision 和指纹的后端授权快照。 */
function specAuth(permissionCodes = [PERMISSIONS.PROVIDER_HEALTH_READ], options = {}) {
  return withPermissionSnapshot({
    user: {
      tenant_id: options.tenantId ?? "tenant_spec_a",
      user_id: options.userId ?? "usr_spec_a",
      email: "developer@example.com",
      role: options.role ?? "developer",
    },
    session: {
      session_id: options.sessionId ?? "ses_spec_a",
      session_revision: options.sessionRevision ?? 1,
    },
  }, permissionCodes, {
    permissionRevision: options.permissionRevision ?? 9,
    authorizationFingerprint: options.authorizationFingerprint ?? "spec_fingerprint_9",
  });
}

/** 构造覆盖厂家、客户 Endpoint 和迁移平台的最小公开响应。 */
function specPayload(overrides = {}) {
  const base = {
    catalog: {
      version: "adapter-spec-v1",
      hash: CATALOG_HASH,
      activated_at: "2026-08-01T00:00:00Z",
    },
    specs: [
      makeSpec("zhipu", "智谱官方或云服务", "model_vendor_cloud"),
      makeSpec("dashscope", "通义千问官方或云服务", "model_vendor_cloud"),
      makeSpec("wenxin", "文心官方或云服务", "model_vendor_cloud"),
      makeSpec("volcengine", "火山引擎模型服务", "model_vendor_cloud", {
        model_id_strategy: "endpoint_or_model_id",
      }),
      makeSpec("hunyuan", "腾讯混元官方或云服务", "model_vendor_cloud"),
      makeSpec("deepseek", "DeepSeek 官方或云服务", "model_vendor_cloud"),
      makeSpec("openai_compatible", "客户 <本地> Endpoint", "local_compatible", {
        allowed_auth_types: ["none", "bearer"],
      }),
      makeSpec("custom", "客户自管 Endpoint", "customer_managed", {
        adapter_name: "generic_http",
        usage_profile: "generic_usage_v1",
        stream_profile: "generic_json_sse_v1",
      }),
      makeSpec("migration_compatible", "迁移兼容入口", "migration_platform", {
        adapter_name: "generic_http",
        status: "optional",
        platform_examples: ["OpenRouter", "硅基流动"],
      }),
    ],
    request_id: "req_spec_1",
    trace_id: "trc_spec_1",
  };
  return { ...base, ...overrides };
}

/** 构造单条 spec，并保持所有数组字段显式存在以验证严格成功合同。 */
function makeSpec(providerType, displayName, classification, overrides = {}) {
  return {
    spec_id: `adapter-spec-${providerType}-v1`,
    provider_type: providerType,
    display_name: displayName,
    classification,
    adapter_name: "openai_compatible",
    adapter_version: "1.0.0",
    protocol_family: "openai_chat_completions",
    allowed_auth_types: ["bearer"],
    model_id_strategy: "provider_model_id",
    usage_profile: "openai_compatible_usage_v1",
    error_profile: "provider_error_map_v1",
    stream_profile: "openai_sse_final_usage_v1",
    probe_profile: "openai_models",
    price_fields: ["input_price_per_1k", "output_price_per_1k", "currency", "price_schema_version"],
    platform_examples: [],
    status: "active",
    contract_case_id: `prd063_${providerType}_minimum_contract`,
    ...overrides,
  };
}

test("adapter spec client sends only classification/status and strips response extras", async () => {
  let captured;
  globalThis.fetch = async (url, options = {}) => {
    captured = { url: String(url), options };
    return {
      ok: true,
      headers: new Map(),
      async json() {
        const payload = specPayload();
        payload.tenant_id = "tenant_should_not_reach_ui";
        payload.secret = "response-secret-canary";
        payload.specs[0].base_url = "https://private.example";
        payload.specs[0].authorization = "Bearer secret";
        return payload;
      },
    };
  };

  const result = await listAdapterSpecs({
    controlApiUrl: "http://control.local",
    token: "session-spec-token",
    filters: {
      classification: "model_vendor_cloud",
      status: "active",
      tenant_id: "tenant_other",
      provider_type: "deepseek",
      base_url: "https://secret.example",
      api_key: "query-secret-canary",
    },
  });
  const parsed = new URL(captured.url);
  assert.equal(parsed.pathname, "/adapter-specs");
  assert.equal(parsed.searchParams.get("classification"), "model_vendor_cloud");
  assert.equal(parsed.searchParams.get("status"), "active");
  assert.equal([...parsed.searchParams.keys()].sort().join(","), "classification,status");
  assert.equal(captured.options.headers.Authorization, "Bearer session-spec-token");
  assert.doesNotMatch(captured.url, /tenant_other|secret\.example|query-secret-canary/);
  assert.doesNotMatch(JSON.stringify(result), /private\.example|Bearer secret|response-secret-canary|tenant_should/);
});

test("adapter spec client maps non-json errors and fails closed on damaged success payloads", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 503,
    headers: new Map([["X-Request-ID", "req_spec_proxy"]]),
    async json() { throw new Error("proxy-private-body"); },
  });
  await assert.rejects(
    () => listAdapterSpecs({ controlApiUrl: "http://control.local", token: "token" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.status, 503);
      assert.equal(error.requestId, "req_spec_proxy");
      assert.doesNotMatch(error.message, /proxy-private-body/);
      return true;
    },
  );

  assert.throws(
    () => sanitizeAdapterSpecPayload(specPayload({ catalog: { version: "v1", hash: "bad" } })),
    (error) => error instanceof AuthApiError && error.code === "adapter.spec_response_invalid",
  );
  assert.throws(
    () => sanitizeAdapterSpecPayload(specPayload({ specs: [{ provider_type: "deepseek" }] })),
    (error) => error instanceof AuthApiError && error.status === 502,
  );
  const truncatedContract = specPayload();
  delete truncatedContract.specs[0].usage_profile;
  assert.throws(
    () => sanitizeAdapterSpecPayload(truncatedContract),
    (error) => error instanceof AuthApiError && error.code === "adapter.spec_response_invalid",
  );
  const invalidActivation = specPayload();
  invalidActivation.catalog.activated_at = "not-a-timestamp";
  assert.throws(
    () => sanitizeAdapterSpecPayload(invalidActivation),
    (error) => error instanceof AuthApiError && error.code === "adapter.spec_response_invalid",
  );
  const duplicateAuth = specPayload();
  duplicateAuth.specs[0].allowed_auth_types = ["bearer", "bearer"];
  assert.throws(
    () => sanitizeAdapterSpecPayload(duplicateAuth),
    (error) => error instanceof AuthApiError && error.code === "adapter.spec_response_invalid",
  );
  const duplicated = specPayload();
  duplicated.specs.push({ ...duplicated.specs[0] });
  assert.throws(
    () => sanitizeAdapterSpecPayload(duplicated),
    (error) => error instanceof AuthApiError && error.code === "adapter.spec_response_invalid",
  );
  const conflated = specPayload();
  conflated.specs[0] = makeSpec("migration_compatible", "OpenRouter", "model_vendor_cloud");
  assert.throws(
    () => sanitizeAdapterSpecPayload(conflated),
    (error) => error instanceof AuthApiError && error.status === 502,
  );
});

test("adapter spec client rejects tampered enum values before network without echo", async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    // 执行到该分支即说明非法筛选越过了联网前白名单，测试必须立即失败且不记录篡改值。
    throw new Error("must not fetch");
  };
  await assert.rejects(
    () => listAdapterSpecs({
      controlApiUrl: "http://control.local",
      token: "token",
      filters: { classification: "vendor\nprivate-secret" },
    }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "adapter.spec_filter_invalid");
      assert.doesNotMatch(error.message, /private-secret/);
      return true;
    },
  );
  assert.equal(calls, 0);
});

test("PAGE-090 renders catalog dimensions, escapes data and keeps migration boundary", () => {
  const sanitized = sanitizeAdapterSpecPayload(specPayload());
  const defaultHtml = renderAdapterSpecs({
    ...sanitized,
    filters: { view: "default", status: "", provider_type: "" },
  }, specAuth());
  assert.match(defaultHtml, /国内模型 Endpoint 适配规范/);
  assert.match(defaultHtml, /adapter-spec-v1/);
  assert.match(defaultHtml, /DeepSeek 官方或云服务/);
  for (const providerType of ["zhipu", "dashscope", "wenxin", "volcengine", "hunyuan", "deepseek"]) {
    assert.match(defaultHtml, new RegExp(`data-provider-type="${providerType}"`));
  }
  assert.match(defaultHtml, /openai_compatible_usage_v1/);
  assert.match(defaultHtml, /provider_error_map_v1/);
  assert.match(defaultHtml, /openai_sse_final_usage_v1/);
  assert.match(defaultHtml, /input_price_per_1k/);
  assert.doesNotMatch(defaultHtml, /客户 &lt;本地&gt; Endpoint/);

  const customerHtml = renderAdapterSpecs({
    ...sanitized,
    filters: { view: "customer", status: "", provider_type: "openai_compatible" },
  }, specAuth());
  assert.match(customerHtml, /客户 &lt;本地&gt; Endpoint/);
  assert.doesNotMatch(customerHtml, /客户 <本地> Endpoint/);
  assert.doesNotMatch(customerHtml, /客户自管 Endpoint/);

  const migrationHtml = renderAdapterSpecs({
    ...sanitized,
    filters: { view: "migration", status: "optional", provider_type: "" },
  }, specAuth());
  assert.match(migrationHtml, /OpenRouter、硅基流动只用于对标或受控迁移兼容/);
  assert.match(migrationHtml, /不是模型厂家，也不是默认上游/);
  assert.match(migrationHtml, /prd063_migration_compatible_minimum_contract/);
});

test("PAGE-090 escapes every free-form catalog and contract field", () => {
  const payload = specPayload();
  const htmlCanary = `\"><img src=x onerror=alert(1)>`;
  payload.catalog.version = htmlCanary;
  payload.request_id = `req_${htmlCanary}`;
  Object.assign(payload.specs[0], {
    display_name: htmlCanary,
    adapter_version: htmlCanary,
    protocol_family: htmlCanary,
    model_id_strategy: htmlCanary,
    usage_profile: htmlCanary,
    error_profile: htmlCanary,
    stream_profile: htmlCanary,
    probe_profile: htmlCanary,
    contract_case_id: htmlCanary,
  });

  const html = renderAdapterSpecs({
    ...sanitizeAdapterSpecPayload(payload),
    filters: { view: "default", status: "", provider_type: "zhipu" },
  }, specAuth());

  assert.match(html, /&quot;&gt;&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /<img|<script|request_id: req_\"><img/);
});

test("PAGE-090 requires provider.health.read and exposes traceable loading/error states", () => {
  assert.equal(renderAdapterSpecs(specPayload(), specAuth([])), "");
  assert.match(renderAdapterSpecs(null, specAuth()), /正在读取 Adapter 规范/);
  const errorHtml = renderAdapterSpecs({
    state: "error",
    message: "目录 <不可用>",
    requestId: "req_<spec>",
  }, specAuth());
  assert.match(errorHtml, /目录 &lt;不可用&gt;/);
  assert.match(errorHtml, /request_id: req_&lt;spec&gt;/);
  assert.doesNotMatch(errorHtml, /目录 <不可用>|req_<spec>/);
});

test("adapter spec filters map product views without leaking provider or tenant fields", () => {
  assert.deepEqual(canonicalAdapterSpecFilters({
    view: " CUSTOMER ",
    status: " ACTIVE ",
    provider_type: " OPENAI_COMPATIBLE ",
    tenant_id: "tenant-secret",
  }), { view: "customer", status: "active", provider_type: "openai_compatible" });
  assert.deepEqual(adapterSpecApiFilters({ view: "default", status: "active", provider_type: "deepseek" }), {
    classification: "model_vendor_cloud",
    status: "active",
  });
  assert.deepEqual(adapterSpecApiFilters({ view: "customer", status: "", provider_type: "custom" }), {});
  assert.deepEqual(adapterSpecApiFilters({ view: "migration", status: "optional" }), {
    classification: "migration_platform",
    status: "optional",
  });
  assert.deepEqual(canonicalAdapterSpecFilters({ view: "tampered", provider_type: "openrouter" }), {
    view: "default",
    status: "",
    provider_type: "",
  });
});

test("adapter spec tickets reject late results across filters identity navigation and token", () => {
  const auth = specAuth();
  const identity = adapterSpecIdentitySnapshot(auth);
  const state = createAdapterSpecPageState();
  const first = state.begin({
    identity,
    navigation: "adapter-specs",
    filters: { view: "default", status: "active", provider_type: "deepseek" },
    token: "token-a",
  });
  assert.ok(first);
  assert.equal(state.owns(first, {
    identity,
    navigation: "adapter-specs",
    filterKey: adapterSpecFilterKey(first.filters),
    token: "token-a",
  }), true);

  const second = state.begin({
    identity,
    navigation: "adapter-specs",
    filters: { view: "migration", status: "optional", provider_type: "" },
    token: "token-a",
  });
  assert.equal(first.controller.signal.aborted, true);
  assert.equal(state.owns(first, {
    identity,
    navigation: "adapter-specs",
    filterKey: adapterSpecFilterKey(first.filters),
    token: "token-a",
  }), false);
  assert.equal(state.owns(second, {
    identity,
    navigation: "adapter-specs",
    filterKey: adapterSpecFilterKey(second.filters),
    token: "token-b",
  }), false);
  state.clear();
  assert.equal(second.controller.signal.aborted, true);
  assert.deepEqual(Object.keys(state.snapshot()).sort(), ["generation", "pending"]);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /token|tenant|deepseek|fingerprint/);
});

test("developer shell dispatch and loading projection require exact backend owner", () => {
  const authContext = specAuth();
  const shellState = {
    state: "ok",
    activeNavigationItemId: "adapter-specs",
    navigation: {
      active_navigation_item_id: "adapter-specs",
      navigation_items: [{
        id: "adapter-specs",
        label: "Adapter 规范",
        group: "Provider 治理",
        target_section: "adapter-specs",
        required_permissions: [PERMISSIONS.PROVIDER_HEALTH_READ],
        visible: true,
        disabled: false,
      }],
    },
  };
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext,
    consoleShellState: shellState,
    adapterSpecsState: { ...sanitizeAdapterSpecPayload(specPayload()), filters: { view: "default" } },
  });
  assert.match(container.innerHTML, /data-active-navigation="adapter-specs"/);
  assert.match(container.innerHTML, /data-adapter-spec-root/);

  const loadingContainer = { innerHTML: "existing" };
  const projected = renderAdapterSpecsLoading(loadingContainer, {
    authContext,
    consoleShellState: { ...shellState, activeNavigationItemId: "overview" },
  });
  assert.equal(projected.activeNavigationItemId, "adapter-specs");
  assert.match(loadingContainer.innerHTML, /正在读取 Adapter 规范/);

  const denied = { innerHTML: "preserved" };
  assert.equal(renderAdapterSpecsLoading(denied, {
    authContext: specAuth([]),
    consoleShellState: shellState,
  }), null);
  assert.equal(denied.innerHTML, "preserved");
});

test("responsive CSS keeps desktop tracks and converts the contract table on mobile", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  assert.match(css, /\.adapter-spec-page\s*\{[^}]*overflow-x:\s*hidden/s);
  assert.match(css, /\.adapter-spec-row\s*\{[^}]*grid-template-columns:/s);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.adapter-spec-table-wrap\s*\{[\s\S]*?overflow-x:\s*visible/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.adapter-spec-row:not\(\.adapter-spec-head\)\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(css, /@media \(max-width: 420px\)[\s\S]*?\.adapter-spec-catalog,[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\)/);
});
