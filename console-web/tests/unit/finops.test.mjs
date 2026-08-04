// 本单测覆盖 PRD-051 Console API 与 PAGE-074/075 纯渲染合同，重点验证租户边界、金额精度、统一错误和 DOM 脱敏。
import assert from "node:assert/strict";
import test from "node:test";

import { renderFinOpsLedger } from "../../src/features/finops-ledger/renderFinOpsLedger.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createFinOpsAdjustment,
  createFinOpsIdempotencyKey,
  listFinOpsLedger,
} from "../../src/lib/finops.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造正式账本的显式读写权限快照。 */
function finOpsContext(
  permissionCodes = [PERMISSIONS.BILLING_FINOPS_READ, PERMISSIONS.BILLING_FINOPS_WRITE],
  role = "finance_admin",
) {
  // 财务读写权限由用例参数精确控制，不能因 finance_admin 名称自动扩大授权。
  return withPermissionSnapshot({ user: { role } }, permissionCodes);
}

const ledgerEntry = {
  finops_ledger_entry_id: "fle_0123456789abcdef0123456789abcdef",
  event_id: "flevt_should_never_render",
  entry_type: "usage_charge",
  direction: "debit",
  amount: "12.345678",
  currency: "CNY",
  normalized_cny: "12.345678",
  cny_normalization_status: "normalized",
  quota_type: "paid",
  source_request_id: "req_finops_051_long_identifier_for_traceability",
  source_trace_id: "trc_finops_051",
  operation_request_id: "req_finops_051_long_identifier_for_traceability",
  operation_trace_id: "trc_finops_051",
  usage_ledger_id: "usg_finops_051",
  price_snapshot_id: "prs_finops_051",
  price_schema_version: "price-v7",
  price_snapshot_source: "model_price",
  price_evidence_status: "priced",
  input_tokens: 12,
  output_tokens: 8,
  total_tokens: 20,
  cache_tokens: 2,
  model_id: "model_finops",
  provider_id: "provider_finops",
  project_id: "project_finops",
  application_id: "application_finops",
  environment_id: "environment_finops",
  estimated: false,
  status: "posted",
  created_at: "2026-07-26T01:02:03Z",
};

const ledgerPayload = {
  ledger_entries: [ledgerEntry],
  totals: {
    by_currency: { CNY: { debit: "12.345678", credit: "0.000000", net: "12.345678" } },
    net_cost_cny: "12.345678",
    unnormalized_entry_count: 0,
    entry_count: 1,
  },
  total: 1,
  limit: 20,
  next_cursor: "opaque.cursor.must_not_enter.dom",
  has_more: true,
  request_id: "req_query_finops",
  trace_id: "trc_query_finops",
};

test("finops list client sends only whitelisted filters and opaque cursor", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      status: 200,
      headers: new Map(),
      async json() {
        return ledgerPayload;
      },
    };
  };

  const result = await listFinOpsLedger({
    controlApiUrl: "http://control.local",
    token: "jwt-secret",
    startDate: "2026-07-01",
    endDate: "2026-07-26",
    entryType: "usage_charge",
    direction: "debit",
    quotaType: "paid",
    requestId: "req_source",
    priceSnapshotId: "prs_source",
    relatedEntryId: "fle_source",
    limit: 20,
    cursor: "opaque_cursor",
    tenantId: "tenant_must_not_be_sent",
    actorUserId: "actor_must_not_be_sent",
  });

  const requestUrl = new URL(calls[0].url);
  assert.equal(result.state, "ok");
  assert.equal(requestUrl.pathname, "/finops/ledger");
  assert.equal(requestUrl.searchParams.get("start_date"), "2026-07-01");
  assert.equal(requestUrl.searchParams.get("entry_type"), "usage_charge");
  assert.equal(requestUrl.searchParams.get("request_id"), "req_source");
  assert.equal(requestUrl.searchParams.get("cursor"), "opaque_cursor");
  assert.equal(requestUrl.searchParams.has("tenant_id"), false);
  assert.equal(requestUrl.searchParams.has("actor_user_id"), false);
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-secret");
  assert.doesNotMatch(calls[0].url, /tenant_must_not_be_sent|actor_must_not_be_sent/);
});

test("finops adjustment keeps decimal string and omits authority fields", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      status: 201,
      headers: new Map(),
      async json() {
        return {
          adjustment: { finops_adjustment_id: "fadj_1" },
          ledger_entry: { ...ledgerEntry, entry_type: "manual_credit", direction: "credit" },
          idempotent_replay: false,
          request_id: "req_adjustment",
          trace_id: "trc_adjustment",
        };
      },
    };
  };

  const result = await createFinOpsAdjustment({
    controlApiUrl: "http://control.local",
    token: "jwt-secret",
    adjustmentType: "manual_credit",
    amount: "0.000001",
    relatedRequestId: "req_related",
    reason: "合同约定补偿",
    idempotencyKey: "finops_test_key_123456",
    tenantId: "tenant_must_not_be_sent",
    actorUserId: "actor_must_not_be_sent",
    currency: "USD",
    direction: "debit",
  });

  const body = JSON.parse(calls[0].options.body);
  assert.equal(result.idempotent_replay, false);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers["Idempotency-Key"], "finops_test_key_123456");
  assert.deepEqual(body, {
    adjustment_type: "manual_credit",
    amount: "0.000001",
    related_request_id: "req_related",
    reason: "合同约定补偿",
  });
  for (const forbidden of ["tenant_id", "actor_user_id", "currency", "direction", "quota_type"]) {
    assert.equal(Object.hasOwn(body, forbidden), false);
  }
});

test("finops reversal copies target contract without client currency", async () => {
  const calls = [];
  globalThis.fetch = async (_url, options) => {
    calls.push(options);
    return {
      ok: true,
      status: 201,
      headers: new Map(),
      async json() {
        return { adjustment: {}, ledger_entry: {}, idempotent_replay: false };
      },
    };
  };

  await createFinOpsAdjustment({
    controlApiUrl: "http://control.local",
    token: "jwt-secret",
    adjustmentType: "reversal",
    amount: "12.345678",
    reversalOfEntryId: ledgerEntry.finops_ledger_entry_id,
    reason: "供应商确认重复计费",
    idempotencyKey: "finops_reversal_key_123",
    currency: "USD",
  });
  assert.deepEqual(JSON.parse(calls[0].body), {
    adjustment_type: "reversal",
    amount: "12.345678",
    reversal_of_entry_id: ledgerEntry.finops_ledger_entry_id,
    reason: "供应商确认重复计费",
  });
});

test("finops client rejects float-like amounts and invalid field combinations before fetch", async () => {
  let called = false;
  globalThis.fetch = async () => {
    called = true;
    // 任一无效金额或字段组合若触达网络即说明前端门禁失效，固定哨兵错误必须使测试失败。
    throw new Error("must not call fetch");
  };
  const base = {
    controlApiUrl: "http://control.local",
    token: "jwt-secret",
    adjustmentType: "manual_debit",
    reason: "账务校正",
    idempotencyKey: "finops_invalid_test_key",
  };

  for (const amount of [1.2, "0", "-1", "+1", "1e-6", "0.0000001", "１００"]) {
    await assert.rejects(
      () => createFinOpsAdjustment({ ...base, amount }),
      (error) => error instanceof AuthApiError && error.code === "finops.amount_invalid",
    );
  }
  await assert.rejects(
    () => createFinOpsAdjustment({ ...base, amount: "1.000000", reversalOfEntryId: "fle_forbidden" }),
    (error) => error.code === "finops.adjustment_invalid",
  );
  assert.equal(called, false);
});

test("finops client preserves unified error code and request id", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: new Map([[
      "X-Request-ID",
      "req_header_fallback",
    ]]),
    async json() {
      return {
        code: "auth.permission_denied",
        message: "Permission denied.",
        request_id: "req_finops_denied",
        trace_id: "trc_finops_denied",
      };
    },
  });

  await assert.rejects(
    () => listFinOpsLedger({ controlApiUrl: "http://control.local", token: "jwt-secret" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "req_finops_denied");
      assert.equal(error.traceId, "trc_finops_denied");
      return true;
    },
  );
});

test("finops renderer shows exact amounts, detail evidence and escaped reason", () => {
  const unsafeReason = '<img src=x onerror="alert(1)">月度补偿';
  const adjusted = {
    ...ledgerEntry,
    finops_ledger_entry_id: "fle_adjustment",
    entry_type: "manual_credit",
    direction: "credit",
    reason: unsafeReason,
    actor: {
      user_id: "usr_finops_admin",
      email: "finance-admin@example.com",
      display_name: "财务管理员",
    },
    actor_display_name: "不应优先的旧操作者",
    price_evidence_status: "not_applicable",
  };
  const html = renderFinOpsLedger(
    {
      state: "ok",
      ...ledgerPayload,
      ledger_entries: [ledgerEntry, adjusted],
      selectedEntry: adjusted,
      filters: {},
      canPrevious: false,
      dialog: { open: false },
    },
    finOpsContext(),
  );

  assert.match(html, /FinOps 正式账本/);
  assert.match(html, /12\.345678/);
  assert.match(html, /PAGE-075/);
  assert.match(html, /fadj|人工贷记/);
  assert.match(html, /财务管理员/);
  assert.doesNotMatch(html, /不应优先的旧操作者/);
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;月度补偿/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.doesNotMatch(html, /opaque\.cursor\.must_not_enter\.dom/);
  assert.doesNotMatch(html, /flevt_should_never_render/);
});

test("finops renderer labels legacy evidence and never renders sensitive extra fields", () => {
  const legacy = {
    ...ledgerEntry,
    price_evidence_status: "legacy_amount_only",
    price_snapshot_source: "finops_legacy_backfill",
    raw_usage: "raw-secret-usage",
    prompt: "secret-prompt",
    response: "secret-response",
    provider_key: "sk-secret",
    endpoint_url: "https://secret-endpoint.invalid",
    normalized_usage: { hidden: "must-not-render" },
  };
  const html = renderFinOpsLedger(
    { state: "ok", ...ledgerPayload, ledger_entries: [legacy], selectedEntry: legacy, filters: {} },
    finOpsContext(),
  );

  assert.match(html, /仅历史金额证据/);
  assert.match(html, /不可按单价重算/);
  assert.match(html, /finops_legacy_backfill/);
  for (const secret of ["raw-secret-usage", "secret-prompt", "secret-response", "sk-secret", "secret-endpoint", "must-not-render"]) {
    assert.doesNotMatch(html, new RegExp(secret));
  }
});

test("finops renderer covers loading, error, empty, modal and backend permission boundary", () => {
  const loading = renderFinOpsLedger(null, finOpsContext());
  const error = renderFinOpsLedger(
    { state: "error", code: "finops.query_invalid", message: "筛选无效", requestId: "req_bad", filters: {} },
    finOpsContext(),
  );
  const empty = renderFinOpsLedger(
    {
      state: "ok",
      ledger_entries: [],
      total: 0,
      filters: {},
      dialog: { open: true, adjustmentType: "reversal", targetEntryId: ledgerEntry.finops_ledger_entry_id },
    },
    finOpsContext(),
  );
  const developer = renderFinOpsLedger(
    { state: "ok", ...ledgerPayload },
    finOpsContext([PERMISSIONS.CONSOLE_BASIC_READ], "developer"),
  );

  assert.match(loading, /正在加载账本/);
  assert.match(error, /finops\.query_invalid/);
  assert.match(error, /req_bad/);
  assert.match(error, /finops-filter-form/);
  assert.match(empty, /当前筛选下没有正式分录/);
  assert.match(empty, /<dialog[^>]*open/);
  assert.match(empty, new RegExp(ledgerEntry.finops_ledger_entry_id));
  assert.equal(developer, "");
});

test("ordinary adjustment cannot create a reversal without a target entry", () => {
  const html = renderFinOpsLedger(
    {
      state: "ok",
      ledger_entries: [],
      total: 0,
      filters: {},
      dialog: { open: true, adjustmentType: "manual_credit", targetEntryId: null },
    },
    finOpsContext(),
  );

  const adjustmentSelect = html.match(/<select name="adjustmentType"[\s\S]*?<\/select>/)?.[0] ?? "";
  assert.notEqual(adjustmentSelect, "");
  assert.doesNotMatch(adjustmentSelect, /<option value="reversal"/);
});

test("finops idempotency key uses visible ASCII and required length", () => {
  const key = createFinOpsIdempotencyKey();
  assert.match(key, /^finops_[!-~]{32}$/);
  assert.equal(key.length, 39);
});
