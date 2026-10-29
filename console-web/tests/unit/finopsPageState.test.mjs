// 本单测覆盖 PRD-051 页面内存，验证 opaque cursor、详情选择、幂等键和调整原因不会跨身份或进入渲染快照。
import assert from "node:assert/strict";
import test from "node:test";

import { createFinOpsPageState } from "../../src/features/finops-ledger/finOpsPageState.js";

const firstEntry = {
  finops_ledger_entry_id: "fle_page_state_1",
  entry_type: "usage_charge",
  amount: "1.000000",
  currency: "CNY",
};

test("finops page state keeps opaque cursors out of render snapshots", () => {
  const state = createFinOpsPageState();
  const first = state.acceptPage({
    ledger_entries: [firstEntry],
    totals: {},
    total: 2,
    limit: 20,
    has_more: true,
    next_cursor: "signed_cursor_secret_1",
    request_id: "req_page_1",
  });

  assert.equal(first.canNext, true);
  assert.equal(Object.hasOwn(first, "next_cursor"), false);
  assert.equal(JSON.stringify(first).includes("signed_cursor_secret_1"), false);
  assert.equal(state.nextPage(), true);
  assert.equal(state.query().cursor, "signed_cursor_secret_1");
  assert.equal(state.snapshot().canPrevious, true);

  state.acceptPage({ ledger_entries: [], total: 2, has_more: false, next_cursor: null });
  assert.equal(state.previousPage(), true);
  assert.equal(state.query().cursor, null);
});

test("finops filter changes invalidate cursor stack and stale selection", () => {
  const state = createFinOpsPageState();
  state.acceptPage({ ledger_entries: [firstEntry], total: 1, has_more: true, next_cursor: "cursor_old" });
  assert.equal(state.selectEntry(firstEntry.finops_ledger_entry_id), true);
  state.nextPage();
  state.setFilters({ entryType: "manual_credit", tenantId: "tenant_forbidden", limit: 100 });

  const query = state.query();
  const snapshot = state.snapshot();
  assert.equal(query.cursor, null);
  assert.equal(query.entryType, "manual_credit");
  assert.equal(query.limit, 20);
  assert.equal(Object.hasOwn(query, "tenantId"), false);
  assert.equal(snapshot.canPrevious, false);
  assert.equal(snapshot.selectedEntry, null);
});

test("finops dialog hides idempotency and operation ids from render state", () => {
  const state = createFinOpsPageState();
  state.acceptPage({ ledger_entries: [firstEntry], total: 1, has_more: false });
  const opened = state.openDialog({
    targetEntryId: firstEntry.finops_ledger_entry_id,
    idempotencyKey: "finops_memory_secret_key",
    operationRequestId: "req_memory_secret",
    operationTraceId: "trc_memory_secret",
  });
  assert.equal(opened, true);

  const beforeSubmit = state.snapshot();
  assert.equal(beforeSubmit.dialog.targetEntryId, firstEntry.finops_ledger_entry_id);
  assert.equal(JSON.stringify(beforeSubmit).includes("finops_memory_secret_key"), false);
  assert.equal(JSON.stringify(beforeSubmit).includes("req_memory_secret"), false);

  const payload = state.beginSubmit({ amount: "0.500000", reason: "重复计费冲正" });
  assert.equal(payload.idempotencyKey, "finops_memory_secret_key");
  assert.equal(payload.reversalOfEntryId, firstEntry.finops_ledger_entry_id);
  assert.equal(payload.reason, "重复计费冲正");
  state.rejectAdjustment({ code: "finops.ledger_unavailable", message: "暂不可用", requestId: "req_failed" });
  const retry = state.beginSubmit({ amount: "0.500000", reason: "重复计费冲正" });
  assert.equal(retry.idempotencyKey, "finops_memory_secret_key");

  state.clear();
  assert.equal(state.snapshot().dialog.open, false);
  assert.equal(state.query().cursor, null);
  assert.equal(JSON.stringify(state.snapshot()).includes("重复计费冲正"), false);
});

test("finops page state rejects injected entry ids and marks stale selections", () => {
  const state = createFinOpsPageState();
  state.acceptPage({ ledger_entries: [firstEntry], total: 1, request_id: "req_first" });
  assert.equal(state.selectEntry("fle_cross_tenant_injected"), false);
  assert.equal(state.snapshot().actionError.code, "finops.entry_not_found");

  assert.equal(state.selectEntry(firstEntry.finops_ledger_entry_id), true);
  const refreshed = state.acceptPage({ ledger_entries: [], total: 1, request_id: "req_refresh" });
  assert.equal(refreshed.selectedEntry, null);
  assert.equal(refreshed.actionError.code, "finops.selection_stale");
  assert.equal(refreshed.actionError.requestId, "req_refresh");
});

test("finops adjustment success resets cursor and exposes only safe feedback", () => {
  const state = createFinOpsPageState();
  state.acceptPage({ ledger_entries: [firstEntry], total: 1, has_more: true, next_cursor: "cursor_before_write" });
  state.nextPage();
  state.openDialog({
    idempotencyKey: "finops_success_secret_key",
    operationRequestId: "req_success_operation",
    operationTraceId: "trc_success_operation",
  });
  state.beginSubmit({ adjustmentType: "manual_credit", amount: "2.000000", reason: "服务补偿" });
  state.acceptAdjustment({
    ledger_entry: { finops_ledger_entry_id: "fle_new_adjustment" },
    idempotent_replay: true,
    request_id: "req_success_public",
  });

  const snapshot = state.snapshot();
  assert.equal(state.query().cursor, null);
  assert.equal(snapshot.canPrevious, false);
  assert.equal(snapshot.feedback.idempotentReplay, true);
  assert.equal(snapshot.feedback.requestId, "req_success_public");
  assert.equal(JSON.stringify(snapshot).includes("服务补偿"), false);
  assert.equal(JSON.stringify(snapshot).includes("finops_success_secret_key"), false);
});
