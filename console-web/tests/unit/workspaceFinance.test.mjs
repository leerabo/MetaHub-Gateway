import assert from "node:assert/strict";
import test from "node:test";
import { listBudgets, getBudgetStatus, createBudgetRule, updateBudgetRule, updateBudgetMode } from "../../src/lib/budgets.js";
import { listFinOpsLedger, createFinOpsAdjustment } from "../../src/lib/finops.js";

const base = { controlApiUrl: "https://control.example.test", authentication: "workspace" };
const adjustment = { adjustmentType: "manual_debit", amount: "0.000001", reason: "Test adjustment",
  idempotencyKey: "workspace-finance-test-0001", operationRequestId: "req_finance_test", operationTraceId: "trc_finance_test" };

function cookie(t, value = "c".repeat(43)) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", { configurable: true, value: {
    cookie: value ? `__Host-metahub-workspace-csrf=${value}` : "",
  } });
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, "document", previous);
    else delete globalThis.document;
  });
}

test("budget and ledger clients retain exact money, CSRF and Workspace-only credentials", async (t) => {
  cookie(t);
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url: new URL(url), options, headers: new Headers(options.headers) });
    return new Response("{}", { status: 200 });
  });
  const controller = new AbortController();
  const options = { ...base, signal: controller.signal };
  const rule = { ...options, scopeType: "tenant", dailyBudgetCny: "12.345678" };
  await listBudgets(options);
  await getBudgetStatus({ ...options, virtualKeyId: "vkey_test" });
  await listFinOpsLedger(options);
  await createBudgetRule(rule);
  await updateBudgetRule(rule);
  await updateBudgetMode({ ...options, budgetRuleId: "rule_test", mode: "alert_only" });
  await createFinOpsAdjustment({ ...options, ...adjustment, tenantId: "forbidden", currency: "USD" });
  for (const { url, options: input, headers } of calls) {
    assert.equal(input.credentials, "include");
    assert.equal(input.cache, "no-store");
    assert.equal(input.redirect, "error");
    assert.equal(input.signal, controller.signal);
    assert.equal(headers.has("Authorization"), false);
    assert.equal(url.searchParams.has("token"), false);
    assert.equal(headers.get("X-CSRF-Token"), input.method === "GET" ? null : "c".repeat(43));
  }
  assert.equal(JSON.parse(calls[3].options.body).daily_budget_cny, "12.345678");
  assert.equal(JSON.parse(calls[4].options.body).daily_budget_cny, "12.345678");
  assert.equal(calls[6].headers.get("Idempotency-Key"), adjustment.idempotencyKey);
  assert.equal(calls[6].headers.get("X-Trace-ID"), adjustment.operationTraceId);
  assert.deepEqual(JSON.parse(calls[6].options.body), {
    adjustment_type: "manual_debit", amount: "0.000001", reason: "Test adjustment",
  });
});

test("finance writes reject missing CSRF and mixed credentials before fetching", async (t) => {
  cookie(t, "");
  t.mock.method(globalThis, "fetch", () => { assert.fail("unauthorized fetch"); });
  for (const client of [createBudgetRule, updateBudgetRule, updateBudgetMode, createFinOpsAdjustment]) {
    await assert.rejects(client({ ...base, ...adjustment, budgetRuleId: "rule_test", mode: "block" }),
      (error) => error.code === "workspace_session.csrf_invalid");
  }
  for (const client of [listBudgets, getBudgetStatus, listFinOpsLedger]) {
    await assert.rejects(client({ ...base, token: "mixed-test-token" }),
      (error) => error.code === "workspace_session.credential_conflict");
  }
});
