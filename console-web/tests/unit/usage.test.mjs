// 本单测覆盖用量与成本前端能力，确保查询参数、结构化错误、估算标记和双币种成本展示稳定。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { renderUsagePanel } from "../../src/features/usage/renderUsagePanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  pollUsageExportUntilReady,
  requestUsageCsvExport,
  safeUsageCsvFilename,
  triggerUsageCsvDownload,
  getUsageSummary,
  listUsageRecords,
} from "../../src/lib/usage.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

// 主应用事件绑定暂不导出测试接口，读取源码验证恢复分支始终先判断已有 job 和稳定幂等键。
const mainSource = readFileSync(new URL("../../src/app/main.js", import.meta.url), "utf8");

/**
 * 构造大小写不敏感响应头，保持与浏览器 Headers.get 行为一致。
 */
function responseHeaders(values = {}) {
  const normalized = new Map(Object.entries(values).map(([key, value]) => [key.toLowerCase(), value]));
  return { get(name) { return normalized.get(String(name).toLowerCase()) ?? null; } };
}

const summaryPayload = {
  totals: {
    request_count: 2,
    input_tokens: 120,
    output_tokens: 80,
    total_tokens: 200,
    cache_tokens: 20,
    cost_cny: 0.42,
    cost_usd: 0,
    estimated_records: 1,
    real_usage_records: 1,
  },
  by_day: [
    {
      day: "2026-07-09",
      request_count: 2,
      input_tokens: 120,
      output_tokens: 80,
      total_tokens: 200,
      cache_tokens: 20,
      cost_cny: 0.42,
      cost_usd: 0,
      estimated_records: 1,
    },
  ],
  by_model: [
    {
      model_id: "model_1",
      resolved_model: "deepseek-chat",
      provider_id: "provider_1",
      provider_code: "deepseek",
      request_count: 2,
      input_tokens: 120,
      output_tokens: 80,
      total_tokens: 200,
      cache_tokens: 20,
      cost_cny: 0.42,
      cost_usd: 0,
      estimated_records: 1,
    },
  ],
  by_project: [
    {
      project_id: "prj_1",
      project_name: "账务核心",
      request_count: 2,
      input_tokens: 120,
      output_tokens: 80,
      total_tokens: 200,
      cache_tokens: 20,
      cost_cny: 0.42,
      cost_usd: 0,
      estimated_records: 1,
    },
  ],
  by_application: [
    {
      application_id: "app_1",
      application_name: "账务 API",
      project_id: "prj_1",
      project_name: "账务核心",
      request_count: 2,
      input_tokens: 120,
      output_tokens: 80,
      total_tokens: 200,
      cache_tokens: 20,
      cost_cny: 0.42,
      cost_usd: 0,
      estimated_records: 1,
    },
  ],
  by_virtual_key: [
    {
      virtual_key_id: "vk_1",
      virtual_key_prefix: "mhv_dev_****",
      request_count: 2,
      input_tokens: 120,
      output_tokens: 80,
      total_tokens: 200,
      cache_tokens: 20,
      cost_cny: 0.42,
      cost_usd: 0,
      estimated_records: 1,
    },
  ],
  request_id: "req_usage_summary",
  trace_id: "trc_usage_summary",
};

const recordsPayload = {
  usage_records: [
    {
      usage_ledger_id: "usg_1",
      tenant_id: "tenant_1",
      request_id: "req_1",
      trace_id: "trc_1",
      virtual_key_id: "vk_1",
      virtual_key_prefix: "mhv_dev_****",
      project_id: "prj_1",
      project_name: "账务核心",
      application_id: "app_1",
      application_name: "账务 API",
      provider_id: "provider_1",
      provider_code: "deepseek",
      model_id: "model_1",
      request_model: "company-default",
      resolved_model: "deepseek-chat",
      endpoint: "/v1/chat/completions",
      input_tokens: 100,
      output_tokens: 60,
      total_tokens: 160,
      cache_tokens: 20,
      cost_amount: 0.42,
      cost_cny: 0.42,
      cost_usd: 0,
      currency: "CNY",
      price_schema_version: "manual-202607",
      price_snapshot_id: "prs_1",
      estimated: false,
      usage_source: "upstream",
      normalized_usage: {
        total_tokens: 160,
        provider_cost_amount: "0.4",
        provider_cost_currency: "CNY",
        normalization_warnings: [],
      },
      provider_raw_usage: { prompt_tokens: 100, completion_tokens: 60 },
      created_at: "2026-07-09T08:00:00Z",
      updated_at: "2026-07-09T08:00:00Z",
    },
    {
      usage_ledger_id: "usg_2",
      tenant_id: "tenant_1",
      request_id: "req_2",
      trace_id: "trc_2",
      virtual_key_id: "vk_1",
      virtual_key_prefix: "mhv_dev_****",
      project_id: "prj_1",
      project_name: "账务核心",
      application_id: "app_1",
      application_name: "账务 API",
      provider_id: "provider_1",
      provider_code: "deepseek",
      model_id: "model_1",
      request_model: "company-default",
      resolved_model: "deepseek-chat",
      endpoint: "/v1/chat/completions",
      input_tokens: 20,
      output_tokens: 20,
      total_tokens: 40,
      cache_tokens: 0,
      cost_amount: 0,
      cost_cny: 0,
      cost_usd: 0,
      currency: "CNY",
      price_schema_version: null,
      price_snapshot_id: null,
      estimated: true,
      usage_source: "estimated",
      normalized_usage: { estimated: true },
      provider_raw_usage: {},
      created_at: "2026-07-09T08:01:00Z",
      updated_at: "2026-07-09T08:01:00Z",
    },
  ],
  total: 2,
  limit: 20,
  offset: 0,
  request_id: "req_usage_records",
  trace_id: "trc_usage_records",
};

const budgetState = {
  state: "ok",
  status: {
    budget_date: "2026-07-09",
    statuses: [
      {
        budget_rule_id: "br_1",
        scope_type: "virtual_key",
        scope_id: "vk_1",
        scope_label: "生产 Key (mhv_prod_****)",
        spent_cny: "82",
        estimated_remaining_cny: "18",
        budget_cny: "100",
        usage_percent: 82,
        status: "threshold_reached",
        exhausted_action: "block",
        next_action: "warn",
      },
    ],
  },
};

test("usage client sends filters and keeps tenant out of query string", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_header"]]),
      async json() {
        // 用量和成本分支直接影响账本追踪，异常值要在这里归一化。
        return String(url).includes("/usage/summary") ? summaryPayload : recordsPayload;
      },
    };
  };

  const summary = await getUsageSummary({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    startDate: "2026-07-09",
    endDate: "2026-07-09",
    modelId: "model_1",
    virtualKeyId: "vk_1",
    projectId: "prj_1",
    applicationId: "app_1",
    providerId: "provider_1",
    estimated: "false",
    requestId: "req_1",
  });
  const records = await listUsageRecords({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    limit: 20,
    offset: 0,
  });

  assert.equal(summary.state, "ok");
  assert.equal(summary.totals.total_tokens, 200);
  assert.equal(records.usage_records.length, 2);
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.match(calls[0].url, /start_date=2026-07-09/);
  assert.match(calls[0].url, /estimated=false/);
  assert.match(calls[0].url, /project_id=prj_1/);
  assert.match(calls[0].url, /application_id=app_1/);
  assert.doesNotMatch(calls[0].url, /tenant_id=/);
});

test("usage client returns structured errors", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 422,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "usage.date_invalid",
        message: "Usage date range is invalid.",
        request_id: "req_bad_date",
        trace_id: "trc_bad_date",
      };
    },
  });

  await assert.rejects(
    () => getUsageSummary({ controlApiUrl: "http://control.local", token: "jwt-token" }),
    (error) => {
      assert.ok(error instanceof AuthApiError);
      assert.equal(error.code, "usage.date_invalid");
      assert.equal(error.requestId, "req_bad_date");
      return true;
    },
  );
});

test("usage csv client downloads 200 response with current filters and safe request headers", async () => {
  const calls = [];
  const csvBlob = new Blob(["\ufeffrequest_id\r\nreq_1\r\n"], { type: "text/csv" });
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      status: 200,
      headers: responseHeaders({
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": 'attachment; filename="metahub-usage-20260709-uex_1.csv"',
        "X-Request-ID": "req_export_sync",
        "X-Trace-ID": "trc_export_sync",
      }),
      async blob() { return csvBlob; },
    };
  };

  const result = await requestUsageCsvExport({
    controlApiUrl: "http://control.local",
    token: "admin-token",
    startDate: "2026-07-09",
    endDate: "2026-07-10",
    projectId: "prj_1",
    virtualKeyId: "vk_1",
    modelId: "model_1",
    environmentId: "env_1",
    applicationId: "app_1",
    providerId: "provider_1",
    estimated: "false",
    requestId: "req_filter",
    limit: 20,
    offset: 40,
    tenantId: "tenant_should_never_leave_browser",
    exportRequestId: "console_usage_export_fixed",
    exportTraceId: "trc_console_usage_export_fixed",
  });

  assert.equal(result.kind, "download");
  assert.equal(result.blob, csvBlob);
  assert.equal(result.filename, "metahub-usage-20260709-uex_1.csv");
  assert.equal(result.requestId, "req_export_sync");
  assert.equal(calls[0].options.headers.Authorization, "Bearer admin-token");
  assert.equal(calls[0].options.headers["X-Request-ID"], "console_usage_export_fixed");
  assert.equal(calls[0].options.headers["X-Trace-ID"], "trc_console_usage_export_fixed");
  assert.match(calls[0].url, /start_date=2026-07-09/);
  assert.match(calls[0].url, /project_id=prj_1/);
  assert.match(calls[0].url, /request_id=req_filter/);
  assert.doesNotMatch(calls[0].url, /tenant/i);
  assert.doesNotMatch(calls[0].url, /limit=|offset=/);
  assert.doesNotMatch(calls[0].url, /admin-token/);
});

test("usage csv create accepts stable request and trace ids across an ambiguous retry", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    if (calls.length === 1) {
      // 首次网络失败无法证明服务端是否创建任务，第二次必须复用完全相同的幂等键。
      throw new TypeError("network response lost");
    }
    return {
      ok: true,
      status: 202,
      headers: responseHeaders({}),
      async json() {
        return {
          export_job: { usage_export_job_id: "uex_retry_1", status: "pending" },
          request_id: "console_usage_export_retry_fixed",
          trace_id: "trc_console_usage_export_retry_fixed",
        };
      },
    };
  };
  const params = {
    controlApiUrl: "http://control.local",
    token: "admin-token",
    exportRequestId: "console_usage_export_retry_fixed",
    exportTraceId: "trc_console_usage_export_retry_fixed",
  };

  await assert.rejects(() => requestUsageCsvExport(params), /network response lost/);
  const retried = await requestUsageCsvExport(params);

  assert.equal(retried.usageExportJobId, "uex_retry_1");
  assert.equal(calls[0].options.headers["X-Request-ID"], calls[1].options.headers["X-Request-ID"]);
  assert.equal(calls[0].options.headers["X-Trace-ID"], calls[1].options.headers["X-Trace-ID"]);
});

test("usage csv client polls 202 job finitely and downloads completed artifact with bearer", async () => {
  const calls = [];
  let statusReads = 0;
  globalThis.fetch = async (url, options = {}) => {
    const normalizedUrl = String(url);
    calls.push({ url: normalizedUrl, options });
    // 首次固定导出入口模拟超过阈值，验证前端不会把 202 JSON 当成 Blob 保存。
    if (normalizedUrl.endsWith("/usage/export.csv")) {
      return {
        ok: true,
        status: 202,
        headers: responseHeaders({ "X-Request-ID": "req_create_header" }),
        async json() {
          return {
            export_job: { usage_export_job_id: "uex_async_1", status: "pending" },
            status_url: "/usage/exports/uex_async_1",
            download_url: "/usage/exports/uex_async_1/download",
            request_id: "req_create",
            trace_id: "trc_console_usage_export_async_fixed",
          };
        },
      };
    }
    // 状态接口先 processing 后 completed，覆盖有限轮询和完成后下载跳转。
    if (normalizedUrl.endsWith("/usage/exports/uex_async_1")) {
      statusReads += 1;
      const status = statusReads === 1 ? "processing" : "completed";
      return {
        ok: true,
        status: 200,
        headers: responseHeaders({ "X-Request-ID": `req_status_${statusReads}` }),
        async json() {
          return {
            export_job: { usage_export_job_id: "uex_async_1", status },
            request_id: `req_status_${statusReads}`,
            trace_id: `trc_status_${statusReads}`,
          };
        },
      };
    }
    assert.equal(normalizedUrl.endsWith("/usage/exports/uex_async_1/download"), true);
    return {
      ok: true,
      status: 200,
      headers: responseHeaders({
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": 'attachment; filename="metahub-usage-async-uex_async_1.csv"',
        "X-Request-ID": "req_download",
      }),
      async blob() { return new Blob(["csv"]); },
    };
  };

  const initial = await requestUsageCsvExport({
    controlApiUrl: "http://control.local",
    token: "admin-token",
    exportRequestId: "console_usage_export_async_fixed",
    exportTraceId: "trc_console_usage_export_async_fixed",
  });
  const observedStatuses = [];
  const result = await pollUsageExportUntilReady({
    controlApiUrl: "http://control.local",
    token: "admin-token",
    usageExportJobId: initial.usageExportJobId,
    traceId: initial.traceId,
    maxAttempts: 3,
    pollIntervalMs: 0,
    onStatus(statusResult) { observedStatuses.push(statusResult.exportJob.status); },
  });

  assert.equal(initial.kind, "pending");
  assert.deepEqual(observedStatuses, ["processing", "completed"]);
  assert.equal(result.filename, "metahub-usage-async-uex_async_1.csv");
  assert.equal(result.requestId, "req_download");
  assert.equal(calls.length, 4);
  for (const call of calls) {
    assert.equal(call.options.headers.Authorization, "Bearer admin-token");
    assert.equal(call.options.headers["X-Trace-ID"], "trc_console_usage_export_async_fixed");
    assert.doesNotMatch(call.url, /admin-token|tenant_id/);
  }
});

test("usage csv client aborts polling before another status request", async () => {
  const controller = new AbortController();
  controller.abort();
  let fetchCount = 0;
  globalThis.fetch = async () => {
    fetchCount += 1;
    // 已 Abort 的轮询不得触发此分支；抛错使误发网络请求立即暴露为测试失败。
    throw new Error("aborted polling must not fetch");
  };

  await assert.rejects(
    () => pollUsageExportUntilReady({
      controlApiUrl: "http://control.local",
      token: "admin-token",
      usageExportJobId: "uex_abort_1",
      signal: controller.signal,
      pollIntervalMs: 0,
    }),
    (error) => error?.name === "AbortError",
  );
  assert.equal(fetchCount, 0);
});

test("usage csv polling stops at the configured attempt limit with request id", async () => {
  let fetchCount = 0;
  globalThis.fetch = async () => {
    fetchCount += 1;
    return {
      ok: true,
      status: 200,
      headers: responseHeaders({ "X-Request-ID": `req_wait_${fetchCount}` }),
      async json() {
        return {
          export_job: { usage_export_job_id: "uex_wait_1", status: "processing" },
          request_id: `req_wait_${fetchCount}`,
        };
      },
    };
  };

  await assert.rejects(
    () => pollUsageExportUntilReady({
      controlApiUrl: "http://control.local",
      token: "admin-token",
      usageExportJobId: "uex_wait_1",
      maxAttempts: 2,
      pollIntervalMs: 0,
    }),
    (error) => {
      assert.equal(error.code, "usage.export_not_ready");
      assert.equal(error.requestId, "req_wait_2");
      return true;
    },
  );
  assert.equal(fetchCount, 2);
});

test("usage csv client rejects unsafe content type and preserves unified export errors", async () => {
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    headers: responseHeaders({
      "Content-Type": "application/json",
      "X-Request-ID": "req_wrong_type",
      "X-Trace-ID": "trc_wrong_type",
    }),
    async blob() { return new Blob(["should-not-download"]); },
  });
  await assert.rejects(
    () => requestUsageCsvExport({ controlApiUrl: "http://control.local", token: "admin-token" }),
    (error) => {
      assert.equal(error.code, "usage.export_unavailable");
      assert.equal(error.requestId, "req_wrong_type");
      return true;
    },
  );

  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: responseHeaders({ "X-Request-ID": "req_denied_header" }),
    async json() {
      return {
        code: "auth.permission_denied",
        message: "Permission denied.",
        request_id: "req_denied",
      };
    },
  });
  await assert.rejects(
    () => requestUsageCsvExport({ controlApiUrl: "http://control.local", token: "developer-token" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.status, 403);
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "req_denied");
      return true;
    },
  );
});

test("usage csv filename and object URL lifecycle reject unsafe names and always revoke", () => {
  assert.equal(safeUsageCsvFilename('attachment; filename="metahub-usage-1.csv"'), "metahub-usage-1.csv");
  assert.equal(safeUsageCsvFilename('attachment; filename="../../tenant.csv"'), "metahub-usage-export.csv");
  assert.equal(safeUsageCsvFilename('attachment; filename="report.xlsx"'), "metahub-usage-export.csv");
  assert.equal(safeUsageCsvFilename('attachment; filename="bad\r\nname.csv"'), "metahub-usage-export.csv");

  const events = [];
  const link = {
    hidden: false,
    click() { events.push("click"); },
    remove() { events.push("remove"); },
  };
  const environment = {
    URL: {
      createObjectURL(blob) { events.push(blob); return "blob:usage"; },
      revokeObjectURL(url) { events.push(`revoke:${url}`); },
    },
    document: {
      createElement(name) { assert.equal(name, "a"); return link; },
      body: { append(value) { assert.equal(value, link); events.push("append"); } },
    },
  };
  const blob = new Blob(["csv"]);
  triggerUsageCsvDownload({ blob, filename: "metahub-usage-1.csv" }, environment);

  assert.equal(link.href, "blob:usage");
  assert.equal(link.download, "metahub-usage-1.csv");
  assert.deepEqual(events, [blob, "append", "click", "remove", "revoke:blob:usage"]);

  const failureEvents = [];
  const failingLink = {
    click() { throw new Error("download blocked"); },
    remove() { failureEvents.push("remove"); },
  };
  const failingEnvironment = {
    URL: {
      createObjectURL() { return "blob:blocked"; },
      revokeObjectURL(url) { failureEvents.push(`revoke:${url}`); },
    },
    document: {
      createElement() { return failingLink; },
      body: { append() { failureEvents.push("append"); } },
    },
  };
  assert.throws(
    () => triggerUsageCsvDownload({ blob, filename: "metahub-usage-1.csv" }, failingEnvironment),
    /download blocked/,
  );
  assert.deepEqual(failureEvents, ["append", "remove", "revoke:blob:blocked"]);
});

test("usage panel renders totals records and estimated label for authorized roles", () => {
  const html = renderUsagePanel(
    {
      state: "ok",
      summary: summaryPayload,
      records: recordsPayload,
      filters: { estimated: "true" },
    },
    withPermissionSnapshot(
      { user: { role: "admin" } },
      [PERMISSIONS.BILLING_USAGE_EXPORT, PERMISSIONS.BILLING_USAGE_READ],
    ),
    budgetState,
  );

  assert.match(html, /Token 用量与成本/);
  assert.match(html, /200/);
  assert.match(html, /0.42 CNY/);
  assert.match(html, /deepseek-chat/);
  assert.match(html, /按 Project/);
  assert.match(html, /账务核心/);
  assert.match(html, /账务 API/);
  assert.match(html, /估算/);
  assert.match(html, /今日预算状态/);
  assert.match(html, /生产 Key/);
  assert.match(html, /82 \/ 100 CNY/);
  assert.match(html, /归一化详情/);
  assert.match(html, /Provider Raw Usage/);
  assert.match(html, /prompt_tokens/);
  assert.match(html, /id="usage-export-csv"/);
  for (const label of ["request_id", "模型", "Provider", "Key", "归属", "Token", "成本", "来源", "时间"]) {
    assert.match(html, new RegExp(`data-label="${label}"`));
  }
});

test("usage export command is admin only and feedback escapes job and request identifiers", () => {
  const baseState = {
    state: "ok",
    summary: summaryPayload,
    records: recordsPayload,
    filters: {},
  };
  const developerHtml = renderUsagePanel(
    baseState,
    withPermissionSnapshot(
      { user: { role: "developer" } },
      [PERMISSIONS.BILLING_USAGE_READ],
    ),
    budgetState,
  );
  assert.match(developerHtml, /Token 用量与成本/);
  assert.doesNotMatch(developerHtml, /usage-export-csv/);

  const adminHtml = renderUsagePanel({
    ...baseState,
    exportState: {
      status: "error",
      jobId: "uex_bad",
      operationRequestId: "console_usage_export_bad",
      message: '任务 <uex_bad> 失败 · request_id: "req_bad"',
    },
  }, withPermissionSnapshot(
    { user: { role: "admin" } },
    [PERMISSIONS.BILLING_USAGE_EXPORT, PERMISSIONS.BILLING_USAGE_READ],
  ), budgetState);
  assert.match(adminHtml, /data-usage-export-feedback/);
  assert.match(adminHtml, /role="alert"/);
  assert.match(adminHtml, />继续查询<\/button>/);
  assert.match(adminHtml, /&lt;uex_bad&gt;/);
  assert.doesNotMatch(adminHtml, /任务 <uex_bad>/);

  const retryHtml = renderUsagePanel({
    ...baseState,
    exportState: {
      status: "error",
      operationRequestId: "console_usage_export_uncertain",
      message: "网络结果不确定",
    },
  }, withPermissionSnapshot(
    { user: { role: "admin" } },
    [PERMISSIONS.BILLING_USAGE_EXPORT, PERMISSIONS.BILLING_USAGE_READ],
  ), budgetState);
  assert.match(retryHtml, />重试导出<\/button>/);
});

test("usage main state retries existing job before create and clears identifiers at session boundaries", () => {
  assert.match(mainSource, /const operationRequestId = usageExportState\.operationRequestId \?\? createUsageExportRequestId\(\)/);
  assert.match(mainSource, /let activeJobId = usageExportState\.jobId \?\? null/);
  assert.match(mainSource, /if \(!activeJobId\) \{[\s\S]*?requestUsageCsvExport\([\s\S]*?exportRequestId: operationRequestId/);
  assert.match(mainSource, /if \(activeJobId\) \{[\s\S]*?pollUsageExportUntilReady\([\s\S]*?usageExportJobId: activeJobId/);
  assert.match(mainSource, /jobId: activeJobId,[\s\S]*?operationRequestId: shouldRetainUsageExportOperation/);
  assert.match(mainSource, /function clearUsageExportSessionState\(\)[\s\S]*?usageExportController\?\.abort\(\)[\s\S]*?status: "idle"/);
});

test("usage responsive css restores labels after hiding the desktop header", () => {
  const css = readFileSync(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  assert.match(css, /\.console-main\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  assert.match(css, /@media \(max-width: 1100px\)[\s\S]*?\.usage-record-head\s*\{\s*display:\s*none;/);
  assert.match(css, /\.usage-record-row:not\(\.usage-record-head\) > \[data-label\]:not\(details\)::before\s*\{[\s\S]*?content:\s*attr\(data-label\)/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.usage-record-row:not\(\.usage-record-head\) > \[data-label\]:not\(details\)\s*\{\s*grid-template-columns:\s*88px minmax\(0, 1fr\)/);
  assert.match(css, /\.usage-record-table\s*\{[\s\S]*?overflow-x:\s*auto/);
  assert.match(css, /\.usage-record-row\s*\{[\s\S]*?min-width:\s*1225px/);
  assert.match(css, /\.usage-summary-grid\s*\{[\s\S]*?minmax\(min\(100%, 390px\), 1fr\)/);
  assert.match(css, /@media \(max-width: 1100px\) \{[\s\S]*?\.usage-filter-form\s*\{\s*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(css, /@media \(max-width: 720px\) \{\s*\.audit-log-filter-form,\s*\.usage-filter-form,\s*\.usage-record-row\s*\{\s*grid-template-columns:\s*1fr/);
  assert.match(css, /\.usage-export-feedback\s*\{[\s\S]*?overflow-wrap:\s*anywhere/);
});

test("usage panel fails closed when the backend read permission is absent", () => {
  assert.equal(renderUsagePanel(
    { state: "ok" },
    withPermissionSnapshot({ user: { role: "viewer" } }, []),
  ), "");
});
