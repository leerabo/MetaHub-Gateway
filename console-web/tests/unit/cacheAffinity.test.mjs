// 本单测覆盖 PRD-050 Console 亲和配置，验证 wire、CAS 生命周期、角色边界和 DOM 隐私。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  cacheAffinityDataClassValue,
  cacheAffinityMinimumPrefixValue,
  cacheAffinityTtlValue,
  createCacheAffinityPageMemory,
  isCacheAffinityDraftValid,
} from "../../src/features/routing-policies/cacheAffinityControls.js";
import {
  createRoutingPolicyPageMemory,
  renderRoutingPolicyPanel,
  routingPolicyIdentitySnapshot,
} from "../../src/features/routing-policies/renderRoutingPolicyPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createCacheAffinityRequestId,
  updateCacheAffinity,
} from "../../src/lib/modelAliases.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

// 固定身份和配置 ID 只用于证明 tenant/user/role/Session ticket 绑定，不包含真实凭据。
const TENANT_ID = "tenant_cache_affinity";
const MODEL_ALIAS_ID = "alias_cache_affinity";
const CACHE_AFFINITY_ID = `caf_${"a".repeat(32)}`;
const ADMIN_CONTEXT = withPermissionSnapshot({
  user: {
    tenant_id: TENANT_ID,
    user_id: "user_admin",
    email: "admin@example.com",
    role: "admin",
  },
  tenant: { tenant_id: TENANT_ID, name: "Cache Tenant" },
  session: { session_id: "session_cache", session_revision: 3 },
}, [PERMISSIONS.ROUTING_READ, PERMISSIONS.ROUTING_WRITE]);

/** 构造两类页面内存共用的安全 Alias DTO，并明确携带租户归属与权威配置版本。 */
function aliasFixture({ cacheAffinity = null, version = 1 } = {}) {
  return {
    model_alias_id: MODEL_ALIAS_ID,
    tenant_id: TENANT_ID,
    alias: "company-default",
    environment: "dev",
    environment_name: "开发环境",
    default_model_id: "model_1",
    default_internal_model_id: "deepseek-chat",
    default_provider_code: "deepseek",
    fallback_model_id: null,
    status: "active",
    updated_at: "2026-07-20T10:00:00Z",
    cache_affinity: cacheAffinity,
    routing_policy: {
      routing_policy_id: "rpol_cache",
      model_alias_id: MODEL_ALIAS_ID,
      routing_mode: "fixed",
      quality_cost_config: null,
      status: "enabled",
      version,
      candidates: [],
      warnings: [],
      created_at: "2026-07-20T10:00:00Z",
      updated_at: "2026-07-20T10:00:00Z",
    },
  };
}

/** 构造合法 CacheAffinity 权威摘要，可覆盖版本、TTL 和分类以测试边界。 */
function affinityFixture(overrides = {}) {
  return {
    cache_affinity_id: CACHE_AFFINITY_ID,
    tenant_id: TENANT_ID,
    model_alias_id: MODEL_ALIAS_ID,
    status: "enabled",
    data_class: "internal",
    minimum_prefix_chars: 1024,
    ttl_seconds: 777,
    version: 1,
    created_at: "2026-07-20T10:00:00Z",
    updated_at: "2026-07-20T10:00:00Z",
    ...overrides,
  };
}

/** 初始化两类独立页面内存，并返回合并渲染状态。 */
function initializedMemories(cacheAffinity = null, requestIds = []) {
  let requestIndex = 0;
  const identity = routingPolicyIdentitySnapshot(ADMIN_CONTEXT);
  const aliases = [aliasFixture({ cacheAffinity })];
  const routing = createRoutingPolicyPageMemory({ requestIdFactory: () => "req_routing" });
  const routingRefresh = routing.beginRefresh({ identity, navigation: "routing-policies" });
  assert.equal(routing.acceptRefresh(routingRefresh, aliases), true);
  const cache = createCacheAffinityPageMemory({
    requestIdFactory: () => requestIds[requestIndex++] ?? `req_cache_${requestIndex}`,
  });
  const cacheRefresh = cache.beginRefresh({ identity, navigation: "routing-policies" });
  assert.equal(cache.acceptRefresh(cacheRefresh, aliases), true);
  return { routing, cache, identity };
}

/** 把两个独立仓库压成生产渲染器接收的单一状态对象。 */
function combinedState(routing, cache) {
  return { ...routing.snapshot(), cacheAffinity: cache.snapshot() };
}

test("cache affinity API sends an exact bounded snapshot and stable request id", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      status: 200,
      headers: new Map(),
      async json() {
        return {
          cache_affinity: affinityFixture({ version: 2, ttl_seconds: 3600 }),
          changed: true,
          request_id: "req_cache_wire",
          trace_id: "trc_cache_wire",
        };
      },
    };
  };

  const result = await updateCacheAffinity({
    controlApiUrl: "http://control.local",
    token: "jwt-test-token",
    modelAliasId: MODEL_ALIAS_ID,
    status: "enabled",
    dataClass: "internal",
    minimumPrefixChars: 1024,
    ttlSeconds: 3600,
    expectedVersion: 1,
    requestId: "req_cache_wire",
    signal: new AbortController().signal,
  });
  assert.equal(result.cache_affinity.version, 2);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    `http://control.local/model-aliases/${MODEL_ALIAS_ID}/cache-affinity`,
  );
  assert.equal(calls[0].options.method, "PATCH");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-test-token");
  assert.equal(calls[0].options.headers["X-Request-ID"], "req_cache_wire");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    status: "enabled",
    data_class: "internal",
    minimum_prefix_chars: 1024,
    ttl_seconds: 3600,
    expected_version: 1,
  });
  assert.doesNotMatch(calls[0].options.body, /tenant_id|prompt|binding|credential/i);
});

test("cache affinity conflict error keeps only the safe current snapshot", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async () => ({
    ok: false,
    status: 409,
    headers: new Map(),
    async json() {
      return {
        code: "cache_affinity.version_conflict",
        message: "conflict",
        request_id: "req_cache_conflict",
        details: {
          current_cache_affinity: {
            ...affinityFixture({ version: 4 }),
            provider_credential_id: "credential_must_not_survive",
            cache_affinity_binding_id: "binding_must_not_survive",
            affinity_scope_digest: "digest_must_not_survive",
          },
        },
      };
    },
  });

  await assert.rejects(
    () => updateCacheAffinity({
      controlApiUrl: "http://control.local",
      token: "jwt-test-token",
      modelAliasId: MODEL_ALIAS_ID,
      status: "enabled",
      dataClass: "internal",
      minimumPrefixChars: 1024,
      ttlSeconds: 3600,
      expectedVersion: 3,
      requestId: "req_cache_conflict",
    }),
    (error) => {
      assert.ok(error instanceof AuthApiError);
      assert.equal(error.status, 409);
      assert.equal(error.currentCacheAffinity.version, 4);
      assert.doesNotMatch(
        JSON.stringify(error.currentCacheAffinity),
        /credential_must_not_survive|binding_must_not_survive|digest_must_not_survive/,
      );
      return true;
    },
  );
});

test("new configuration uses version zero and validates the exact response", () => {
  const { cache, identity } = initializedMemories(null, ["req_cache_create"]);
  assert.equal(cache.selectDraft(MODEL_ALIAS_ID, {
    status: "enabled",
    data_class: "internal",
    minimum_prefix_chars: 2048,
    ttl_seconds: 777,
  }), true);
  const acquired = cache.acquireSave({ identity, navigation: "routing-policies", modelAliasId: MODEL_ALIAS_ID });
  const duplicate = cache.acquireSave({ identity, navigation: "routing-policies", modelAliasId: MODEL_ALIAS_ID });
  assert.equal(acquired.state, "ok");
  assert.equal(acquired.ticket.baseVersion, 0);
  assert.equal(acquired.ticket.requestId, "req_cache_create");
  assert.equal(duplicate.reused, true);
  assert.equal(duplicate.ticket, acquired.ticket);
  assert.equal(cache.acceptSuccess(acquired.ticket, {
    cache_affinity: affinityFixture({
      version: 1,
      minimum_prefix_chars: 2048,
      ttl_seconds: 777,
    }),
    changed: true,
    request_id: "req_cache_create",
    trace_id: "trc_cache_create",
  }, { identity, navigation: "routing-policies" }), true);
  const saved = cache.snapshot().rows[0];
  assert.equal(saved.baseVersion, 1);
  assert.equal(saved.dirty, false);
  assert.equal(saved.busy, false);
  assert.equal(saved.draft.ttl_seconds, 777);
});

test("unknown result retries one intent id while draft changes allocate a new id", () => {
  const { cache, identity } = initializedMemories(
    affinityFixture(),
    ["req_cache_first", "req_cache_changed"],
  );
  cache.selectDraft(MODEL_ALIAS_ID, {
    status: "enabled",
    data_class: "internal",
    minimum_prefix_chars: 1024,
    ttl_seconds: 3600,
  });
  const first = cache.acquireSave({ identity, navigation: "routing-policies", modelAliasId: MODEL_ALIAS_ID }).ticket;
  assert.equal(cache.acceptFailure(first, {
    status: 503,
    code: "cache_affinity.update_failed",
    message: "unknown",
    requestId: first.requestId,
  }, { identity, navigation: "routing-policies" }), true);
  const retried = cache.acquireSave({ identity, navigation: "routing-policies", modelAliasId: MODEL_ALIAS_ID }).ticket;
  assert.equal(retried.requestId, first.requestId);
  assert.equal(cache.acceptFailure(retried, {
    status: 422,
    code: "cache_affinity.input_invalid",
    message: "invalid",
    requestId: retried.requestId,
  }, { identity, navigation: "routing-policies" }), true);
  cache.selectDraft(MODEL_ALIAS_ID, {
    status: "enabled",
    data_class: "internal",
    minimum_prefix_chars: 2048,
    ttl_seconds: 3600,
  });
  const changed = cache.acquireSave({ identity, navigation: "routing-policies", modelAliasId: MODEL_ALIAS_ID }).ticket;
  assert.equal(changed.requestId, "req_cache_changed");
});

test("409 and identity changes require explicit rebase and reject late callbacks", () => {
  const { cache, identity } = initializedMemories(affinityFixture(), ["req_cache_conflicted"]);
  cache.selectDraft(MODEL_ALIAS_ID, {
    status: "enabled",
    data_class: "internal",
    minimum_prefix_chars: 1024,
    ttl_seconds: 3600,
  });
  const ticket = cache.acquireSave({ identity, navigation: "routing-policies", modelAliasId: MODEL_ALIAS_ID }).ticket;
  assert.equal(cache.acceptFailure(ticket, {
    status: 409,
    code: "cache_affinity.version_conflict",
    message: "conflict",
    requestId: ticket.requestId,
  }, { identity, navigation: "routing-policies" }), true);
  assert.equal(cache.snapshot().rows[0].conflicted, true);
  assert.equal(cache.acquireSave({ identity, navigation: "routing-policies", modelAliasId: MODEL_ALIAS_ID }).state, "blocked");

  const refresh = cache.beginRefresh({ identity, navigation: "routing-policies" });
  cache.acceptRefresh(refresh, [aliasFixture({
    cacheAffinity: affinityFixture({ version: 2, ttl_seconds: 21600 }),
  })]);
  assert.equal(cache.rebase(MODEL_ALIAS_ID), true);
  assert.equal(cache.snapshot().rows[0].baseVersion, 2);
  assert.equal(cache.snapshot().rows[0].draft.ttl_seconds, 21600);

  cache.selectDraft(MODEL_ALIAS_ID, {
    status: "enabled",
    data_class: "internal",
    minimum_prefix_chars: 2048,
    ttl_seconds: 21600,
  });
  const late = cache.acquireSave({ identity, navigation: "routing-policies", modelAliasId: MODEL_ALIAS_ID }).ticket;
  cache.invalidate();
  assert.equal(late.signal.aborted, true);
  assert.equal(cache.acceptSuccess(late, {
    cache_affinity: affinityFixture({ version: 3, minimum_prefix_chars: 2048, ttl_seconds: 21600 }),
    changed: true,
    request_id: late.requestId,
  }, { identity, navigation: "routing-policies" }), false);
});

test("renderer gives admin editor developer readonly viewer no DOM and preserves custom TTL", () => {
  const authority = affinityFixture({
    ttl_seconds: 777,
    data_class: "internal.secure",
    provider_credential_id: "credential_secret",
    cache_affinity_binding_id: "binding_secret",
    affinity_scope_digest: "digest_secret",
    prompt_fingerprint: "prompt_secret",
  });
  const { routing, cache } = initializedMemories(authority);
  const state = combinedState(routing, cache);
  state.rows[0].alias.alias = "<script>unsafe alias</script>";
  const developer = withPermissionSnapshot(
    { ...ADMIN_CONTEXT, user: { ...ADMIN_CONTEXT.user, role: "developer" } },
    [PERMISSIONS.ROUTING_READ],
  );
  const viewer = withPermissionSnapshot(
    { ...ADMIN_CONTEXT, user: { ...ADMIN_CONTEXT.user, role: "viewer" } },
    [PERMISSIONS.CONSOLE_BASIC_READ],
  );

  const adminHtml = renderRoutingPolicyPanel(state, ADMIN_CONTEXT);
  const developerHtml = renderRoutingPolicyPanel(state, developer);
  const viewerHtml = renderRoutingPolicyPanel(state, viewer);
  assert.match(adminHtml, /data-cache-affinity-form/);
  assert.match(adminHtml, /role="switch"/);
  assert.match(adminHtml, /自定义 777 秒/);
  assert.match(adminHtml, /value="777" selected/);
  assert.match(adminHtml, /internal\.secure/);
  assert.match(adminHtml, /&lt;script&gt;unsafe alias&lt;\/script&gt;/);
  const patternMatch = adminHtml.match(
    /name="cacheAffinityDataClass"[^>]*pattern="([^"]+)"/,
  );
  // HTML pattern 由 Chrome 以 v 标志编译；这里锁定转义后的真实 DOM 值，防止提交时再产生 SyntaxError。
  assert.throws(
    () => new RegExp("[a-z0-9][a-z0-9._-]{0,63}", "v"),
    SyntaxError,
  );
  assert.equal(patternMatch?.[1], String.raw`[a-z0-9][a-z0-9._\-]{0,63}`);
  const dataClassPattern = new RegExp(`^(?:${patternMatch[1]})$`, "v");
  for (const value of [
    "internal", "internal.secure", "regulated-v2", "team_private", "a".repeat(64),
  ]) {
    assert.equal(dataClassPattern.test(value), true);
  }
  for (const value of ["-leading", "UPPER", "contains space", "a".repeat(65)]) {
    assert.equal(dataClassPattern.test(value), false);
  }
  assert.match(developerHtml, /Prompt Cache 亲和|internal\.secure|v1|777 秒/);
  assert.doesNotMatch(developerHtml, /data-cache-affinity-form|<form|<button/);
  assert.equal(viewerHtml, "");
  for (const html of [adminHtml, developerHtml]) {
    assert.doesNotMatch(
      html,
      /credential_secret|binding_secret|digest_secret|prompt_secret|provider_credential|affinity_scope_digest/i,
    );
  }
});

test("draft parsers reject ambiguous values and enabled requires a data class", () => {
  assert.equal(cacheAffinityDataClassValue(" internal.secure "), "internal.secure");
  assert.equal(cacheAffinityDataClassValue("   "), null);
  assert.equal(cacheAffinityDataClassValue(null), null);
  assert.equal(cacheAffinityMinimumPrefixValue("256"), 256);
  assert.equal(cacheAffinityMinimumPrefixValue("1e3"), null);
  assert.equal(cacheAffinityMinimumPrefixValue("1024.0"), null);
  assert.equal(cacheAffinityMinimumPrefixValue("16385"), null);
  assert.equal(cacheAffinityTtlValue("60"), 60);
  assert.equal(cacheAffinityTtlValue("86401"), null);
  assert.equal(isCacheAffinityDraftValid({
    status: "enabled",
    data_class: null,
    minimum_prefix_chars: 1024,
    ttl_seconds: 3600,
  }), false);
  assert.equal(isCacheAffinityDraftValid({
    status: "disabled",
    data_class: null,
    minimum_prefix_chars: 1024,
    ttl_seconds: 3600,
  }), true);
  assert.match(createCacheAffinityRequestId(), /^req_console_cache_affinity_/);
});

test("cache affinity CSS defines stable desktop tablet and mobile tracks", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  assert.match(css, /\.cache-affinity-config-row\s*\{[^}]*grid-template-columns:/s);
  assert.match(css, /\.cache-affinity-fields\s*\{[^}]*grid-template-columns:/s);
  assert.match(
    css,
    /\.cache-affinity-readonly-grid\s*\{[^}]*grid-template-columns:\s*repeat\(6, minmax\(0, 1fr\)\)/s,
  );
  assert.match(css, /@media \(max-width: 1100px\)[\s\S]*\.cache-affinity-readonly-grid/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*\.cache-affinity-config-row/);
  assert.match(css, /@media \(max-width: 390px\)[\s\S]*\.cache-affinity-form-footer/);
  assert.match(css, /overflow-wrap: anywhere/);
});

test("main wires shared refresh, admin PATCH ownership and lifecycle invalidation", async () => {
  const mainSource = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  assert.match(
    mainSource,
    /const cacheAffinityRefreshTicket = routingPolicyRefreshTicket[\s\S]*?loadModelAliasesState\(\)[\s\S]*?cacheAffinityPageMemory\.acceptRefresh\(/,
  );
  assert.match(
    mainSource,
    /function bindCacheAffinityInteractions[\s\S]*?!hasPermission\(authSession\.context, PERMISSIONS\.ROUTING_WRITE\)[\s\S]*?activeNavigationItemId !== "routing-policies"/,
  );
  assert.match(
    mainSource,
    /updateCacheAffinity\(\{[\s\S]{0,700}?expectedVersion: ticket\.baseVersion,[\s\S]{0,300}?requestId: ticket\.requestId,[\s\S]{0,200}?signal: ticket\.signal/,
  );
  assert.match(
    mainSource,
    /const syncDataClassDraft = \(\) => \{[\s\S]*?cacheAffinityDataClassValue\(dataClassInput\?\.value\)[\s\S]*?dataClassInput\.value = normalizedDataClass \?\? "";[\s\S]*?syncDraft\(\);/,
  );
  assert.match(
    mainSource,
    /function clearRoutingPolicySessionState\(\)[\s\S]*?routingPolicyPageMemory\.invalidate\(\)[\s\S]*?cacheAffinityPageMemory\.invalidate\(\)/,
  );
  assert.match(mainSource, /pagehide[\s\S]*?clearRoutingPolicySessionState\(\)/);
  assert.match(
    mainSource,
    /itemId !== "routing-policies"[\s\S]*?clearRoutingPolicySessionState\(\)/,
  );
  assert.doesNotMatch(
    mainSource,
    /updateCacheAffinity\(\{[\s\S]{0,700}?(?:tenantId|tenant_id|providerCredential|prompt|response)\s*:/,
  );
});
