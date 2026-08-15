/** PRD-080 PAGE-112 request, owner, private-text, Dialog and responsive contracts. */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  billingDisputeCreateRequestFromFormData,
  billingDisputeFiltersFromFormData,
  billingDisputeTransitionRequestFromFormData,
  canonicalBillingDisputeAmount,
  hydrateBillingDisputePrivateText,
  installBillingDisputeBusyEscapeGuard,
  trapBillingDisputeDialogFocus,
} from "../../src/features/billing-disputes/bindBillingDisputes.js";
import {
  billingDisputePageOwner,
  createBillingDisputePageState,
} from "../../src/features/billing-disputes/billingDisputePageState.js";
import { renderBillingDisputes } from "../../src/features/billing-disputes/renderBillingDisputes.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createBillingDispute,
  listBillingDisputes,
  normalizeBillingDisputeDetailResponse,
  normalizeBillingDisputeListFilters,
  normalizeBillingDisputeListResponse,
  normalizeBillingDisputeMutationResponse,
  transitionBillingDispute,
} from "../../src/lib/billingDisputes.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const TENANT_ID = "tenant_dispute_a";
const DISPUTE_ID = `bdsp_${"a".repeat(32)}`;
const ITEM_ID = `bdsi_${"b".repeat(32)}`;
const HOLD_ID = `bhold_${"c".repeat(32)}`;
const TRANSITION_ID = `bdtr_${"d".repeat(32)}`;
const EVENT_ID = `bdevt_${"e".repeat(32)}`;
const LEDGER_ID = `fle_${"f".repeat(32)}`;
const USAGE_ID = "usg_prd080_browser_charge_1";
const FULL_PERMISSIONS = [
  PERMISSIONS.BILLING_DISPUTE_READ,
  PERMISSIONS.BILLING_DISPUTE_CREATE,
  PERMISSIONS.BILLING_DISPUTE_REVIEW,
  PERMISSIONS.BILLING_FINOPS_WRITE,
];

function authContext({
  permissions = FULL_PERMISSIONS,
  userId = "user_dispute_a",
  sessionId = "session_dispute_a",
  revision = 16,
  fingerprint = "billing_dispute_fingerprint_16",
} = {}) {
  return withPermissionSnapshot({
    tenant: { tenant_id: TENANT_ID, display_name: "Dispute tenant" },
    user: {
      tenant_id: TENANT_ID,
      user_id: userId,
      email: `${userId}@example.test`,
      role: "finance_admin",
    },
    session: { session_id: sessionId, session_revision: 2 },
  }, permissions, {
    permissionRevision: revision,
    authorizationFingerprint: fingerprint,
  });
}

function shellState(context = authContext()) {
  return {
    state: "ok",
    activeNavigationItemId: "billing-disputes",
    navigation: {
      active_navigation_item_id: "billing-disputes",
      permission_codes: context.permission_codes,
      permission_revision: context.permission_revision,
      authorization_fingerprint: context.authorization_fingerprint,
      navigation_items: [{
        id: "billing-disputes",
        target_section: "billing-disputes",
        label: "Billing disputes",
        group: "调用与成本",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.BILLING_DISPUTE_READ],
      }],
    },
  };
}

function actor(userId = "user_dispute_a") {
  return { user_id: userId, display_name: "Finance user", email: `${userId}@example.test` };
}

function summary(overrides = {}) {
  return {
    billing_dispute_id: DISPUTE_ID,
    tenant_id: TENANT_ID,
    scope_type: "request_ids",
    period_start: null,
    period_end: null,
    status: "submitted",
    state_revision: 1,
    item_count: 1,
    reason_sha256: "2".repeat(64),
    created_by: actor(),
    reviewer: null,
    disputed_currency_totals: { CNY: "2.000000" },
    approved_currency_totals: {},
    active_hold_count: 1,
    adjustment_count: 0,
    event_delivery_status: "pending",
    review_started_at: null,
    resolved_at: null,
    created_at: "2026-08-15T08:00:00Z",
    updated_at: "2026-08-15T08:00:00Z",
    ...overrides,
  };
}

function hold(overrides = {}) {
  return {
    billing_settlement_hold_id: HOLD_ID,
    billing_dispute_item_id: ITEM_ID,
    finops_ledger_entry_id: LEDGER_ID,
    status: "active",
    release_reason: null,
    state_revision: 1,
    created_transition_id: TRANSITION_ID,
    released_transition_id: null,
    created_at: "2026-08-15T08:00:00Z",
    released_at: null,
    updated_at: "2026-08-15T08:00:00Z",
    ...overrides,
  };
}

function transition(overrides = {}) {
  return {
    billing_dispute_transition_id: TRANSITION_ID,
    action: "create",
    from_status: null,
    to_status: "submitted",
    expected_revision: 0,
    new_revision: 1,
    actor_user_id: "user_dispute_a",
    decision_reason_sha256: null,
    approved_item_count: 0,
    approved_totals_sha256: null,
    operation_request_id: "request_dispute_create_a",
    operation_trace_id: "trace_dispute_create_a",
    created_at: "2026-08-15T08:00:00Z",
    ...overrides,
  };
}

function item(overrides = {}) {
  return {
    billing_dispute_item_id: ITEM_ID,
    finops_ledger_entry_id: LEDGER_ID,
    usage_ledger_id: USAGE_ID,
    source_request_id: "request_source_charge_a",
    source_trace_id: "trace_source_charge_a",
    ledger_sequence: 8,
    currency: "CNY",
    amount: "2.000000",
    disputable_amount: "2.000000",
    current_disputable_amount: "0.750000",
    price_snapshot_id: `psnap_${"3".repeat(32)}`,
    price_schema_version: "1",
    price_snapshot_source: "model_price",
    price_evidence_status: "<img>",
    created_at: "2026-08-15T08:00:00Z",
    hold: hold(),
    adjustment_link: null,
    ...overrides,
  };
}

function detail(overrides = {}) {
  return {
    summary: summary(),
    reason: "private_reason_<script>",
    decision_reason: null,
    create_request_id: "request_dispute_create_a",
    create_trace_id: "trace_dispute_create_a",
    items: [item()],
    holds: [hold()],
    adjustment_links: [],
    transitions: [transition()],
    event: {
      billing_dispute_event_id: EVENT_ID,
      event_type: "billing_dispute.created",
      delivery_status: "pending",
      attempt_count: 0,
      last_error_code: null,
      occurred_at: "2026-08-15T08:00:00Z",
      succeeded_at: null,
    },
    ...overrides,
  };
}

function listPayload(overrides = {}) {
  return {
    billing_disputes: [summary()],
    limit: 50,
    next_cursor: null,
    has_more: false,
    request_id: "request_dispute_list_a",
    trace_id: "trace_dispute_list_a",
    ...overrides,
  };
}

function detailPayload(overrides = {}) {
  return {
    dispute: detail(),
    request_id: "request_dispute_detail_a",
    trace_id: "trace_dispute_detail_a",
    ...overrides,
  };
}

function mutationPayload(overrides = {}) {
  return {
    dispute: detail(),
    transition: transition(),
    adjustment_links: [],
    idempotent_replay: false,
    request_id: "request_dispute_mutation_a",
    trace_id: "trace_dispute_mutation_a",
    ...overrides,
  };
}

function startReviewPayload() {
  const reviewTransition = transition({
    billing_dispute_transition_id: `bdtr_${"9".repeat(32)}`,
    action: "start_review",
    from_status: "submitted",
    to_status: "under_review",
    expected_revision: 1,
    new_revision: 2,
    actor_user_id: "user_reviewer_a",
    operation_request_id: "request_dispute_review_a",
    operation_trace_id: "trace_dispute_review_a",
    created_at: "2026-08-15T09:00:00Z",
  });
  const reviewedSummary = summary({
    status: "under_review",
    state_revision: 2,
    reviewer: actor("user_reviewer_a"),
    review_started_at: "2026-08-15T09:00:00Z",
    updated_at: "2026-08-15T09:00:00Z",
  });
  return mutationPayload({
    dispute: detail({ summary: reviewedSummary, transitions: [transition(), reviewTransition] }),
    transition: reviewTransition,
  });
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store, private",
      "X-Request-ID": payload.request_id,
      "X-Trace-ID": payload.trace_id,
    },
  });
}

async function withFetch(handler, callback) {
  const original = globalThis.fetch;
  globalThis.fetch = handler;
  try {
    return await callback();
  } finally {
    globalThis.fetch = original;
  }
}

test("PAGE-112 permissions are independent backend facts and approve adds finops.write", () => {
  assert.equal(PERMISSIONS.BILLING_DISPUTE_READ, "billing.dispute.read");
  assert.equal(PERMISSIONS.BILLING_DISPUTE_CREATE, "billing.dispute.create");
  assert.equal(PERMISSIONS.BILLING_DISPUTE_REVIEW, "billing.dispute.review");
  const full = authContext();
  assert.ok(billingDisputePageOwner(full, shellState(full), { mode: "approve" }));
  const reviewer = authContext({
    permissions: [PERMISSIONS.BILLING_DISPUTE_READ, PERMISSIONS.BILLING_DISPUTE_REVIEW],
  });
  assert.ok(billingDisputePageOwner(reviewer, shellState(reviewer), { mode: "review" }));
  assert.equal(billingDisputePageOwner(reviewer, shellState(reviewer), { mode: "approve" }), null);
  const readOnly = authContext({ permissions: [PERMISSIONS.BILLING_DISPUTE_READ] });
  assert.ok(billingDisputePageOwner(readOnly, shellState(readOnly)));
  assert.equal(billingDisputePageOwner(readOnly, shellState(readOnly), { mode: "create" }), null);
});

test("list client sends only declared filters and deeply validates the exact wire DTO", async () => {
  let captured;
  await withFetch(async (url, init) => {
    captured = { url, init };
    return jsonResponse(listPayload());
  }, async () => {
    const result = await listBillingDisputes({
      controlApiUrl: "http://control.test",
      token: "synthetic-token",
      tenantId: TENANT_ID,
      filters: { status: "submitted", scope_type: "request_ids", limit: 50 },
      requestId: "request_dispute_list_attempt_a",
    });
    assert.equal(result.billing_disputes[0].disputed_currency_totals.CNY, "2.000000");
  });
  const url = new URL(captured.url);
  assert.equal(url.pathname, "/billing-disputes");
  assert.equal(url.searchParams.get("status"), "submitted");
  assert.equal(url.searchParams.has("tenant_id"), false);
  assert.equal(url.searchParams.has("actor"), false);
  assert.equal(captured.init.cache, "no-store");
  assert.equal(captured.init.credentials, "omit");

  assert.equal(normalizeBillingDisputeListResponse({ ...listPayload(), secret: "forbidden" }, TENANT_ID, 50), null);
  assert.equal(normalizeBillingDisputeListResponse(listPayload({
    billing_disputes: [summary({ tenant_id: "tenant_other" })],
  }), TENANT_ID, 50), null);
  assert.ok(normalizeBillingDisputeListFilters({ cursor: "x".repeat(129) }));
  assert.throws(() => normalizeBillingDisputeListFilters({ cursor: "x".repeat(2049) }));
});

test("POST and PATCH send exact intent fields with one private idempotency key", async () => {
  const calls = [];
  await withFetch(async (url, init) => {
    calls.push({ url, init });
    return jsonResponse(init.method === "POST" ? mutationPayload() : startReviewPayload(), init.method === "POST" ? 201 : 200);
  }, async () => {
    await createBillingDispute({
      controlApiUrl: "http://control.test",
      token: "synthetic-token",
      tenantId: TENANT_ID,
      request: { scope_type: "request_ids", request_ids: ["request_source_charge_a"], reason: "private_reason" },
      idempotencyKey: "billing-dispute-key-a",
      requestId: "request_dispute_create_attempt_a",
    });
    await transitionBillingDispute({
      controlApiUrl: "http://control.test",
      token: "synthetic-token",
      tenantId: TENANT_ID,
      billingDisputeId: DISPUTE_ID,
      request: { action: "start_review", expected_revision: 1 },
      idempotencyKey: "billing-dispute-key-b",
      requestId: "request_dispute_patch_attempt_a",
    });
  });
  const createBody = JSON.parse(calls[0].init.body);
  assert.deepEqual(Object.keys(createBody).sort(), ["reason", "request_ids", "scope_type"]);
  assert.equal(createBody.tenant_id, undefined);
  assert.equal(createBody.actor, undefined);
  assert.equal(createBody.currency, undefined);
  assert.equal(createBody.status, undefined);
  assert.equal(calls[0].init.headers["Idempotency-Key"], "billing-dispute-key-a");
  assert.deepEqual(JSON.parse(calls[1].init.body), { action: "start_review", expected_revision: 1 });
  assert.equal(calls[1].url, `http://control.test/billing-disputes/${DISPUTE_ID}`);
});

test("detail and mutation normalizers match nested Control DTO and reject malformed children", () => {
  const normalized = normalizeBillingDisputeDetailResponse(detailPayload(), TENANT_ID, DISPUTE_ID);
  assert.equal(normalized.dispute.summary.created_by.user_id, "user_dispute_a");
  assert.equal(normalized.dispute.items[0].tenant_id, undefined);
  const zeroRemaining = normalizeBillingDisputeDetailResponse(detailPayload({
    dispute: detail({ items: [item({ current_disputable_amount: "0.000000" })] }),
  }), TENANT_ID, DISPUTE_ID);
  assert.equal(zeroRemaining.dispute.items[0].current_disputable_amount, "0.000000");
  assert.equal(normalizeBillingDisputeDetailResponse(detailPayload({
    dispute: detail({ items: [item({ tenant_id: TENANT_ID })] }),
  }), TENANT_ID, DISPUTE_ID), null);
  assert.equal(normalizeBillingDisputeMutationResponse({
    ...mutationPayload(),
    transition: { ...transition(), unknown: true },
  }, TENANT_ID), null);
});

test("state keeps reason, decision and idempotency keys out of snapshots and rejects late owners", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = billingDisputePageOwner(context, shell);
  const writer = billingDisputePageOwner(context, shell, { mode: "create" });
  const state = createBillingDisputePageState({
    createIdempotencyKey: () => "billing-dispute-private-key",
  });
  state.enter(owner);
  const list = state.beginList(owner);
  assert.equal(state.acceptList(list, normalizeBillingDisputeListResponse(listPayload(), TENANT_ID, 50), owner), true);
  state.finishList(list);
  state.selectDetail(owner, DISPUTE_ID);
  const detailOperation = state.beginDetail(owner);
  assert.equal(state.acceptDetail(
    detailOperation,
    normalizeBillingDisputeDetailResponse(detailPayload(), TENANT_ID, DISPUTE_ID),
    owner,
  ), true);
  state.finishDetail(detailOperation);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /private_reason|private-key|decision_reason/);
  assert.equal(state.revealText(owner, "reason"), true);
  assert.equal(state.revealedText(owner).reason, "private_reason_<script>");
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /private_reason/);

  assert.equal(state.openCreate(writer), true);
  const mutation = state.beginMutation(writer, {
    scope_type: "request_ids",
    request_ids: ["request_source_charge_a"],
    reason: "private_mutation_reason",
  });
  assert.equal(mutation.idempotencyKey, "billing-dispute-private-key");
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /private_mutation_reason|private-key/);
  state.rejectMutation(mutation, new AuthApiError({
    status: 503,
    code: "billing_dispute.unavailable",
    message: "ignored",
    requestId: "request_retry_a",
    traceId: "trace_retry_a",
  }), writer);
  state.finishMutation(mutation);
  const retry = state.beginMutation(writer, null, { retry: true });
  assert.equal(retry.idempotencyKey, mutation.idempotencyKey);

  const changed = authContext({ sessionId: "session_dispute_b", fingerprint: "billing_dispute_fingerprint_b" });
  assert.equal(state.enter(billingDisputePageOwner(changed, shellState(changed))), true);
  assert.equal(retry.controller.signal.aborted, true);
  assert.equal(state.acceptMutation(retry, normalizeBillingDisputeMutationResponse(mutationPayload(), TENANT_ID), writer), false);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /private_reason|private-key/);
});

test("rendering enforces self-review projection, escapes DTO text and never embeds private reason", () => {
  const context = authContext();
  const shell = shellState(context);
  const owner = billingDisputePageOwner(context, shell);
  const state = createBillingDisputePageState();
  state.enter(owner);
  const list = state.beginList(owner);
  state.acceptList(list, normalizeBillingDisputeListResponse(listPayload(), TENANT_ID, 50), owner);
  state.finishList(list);
  state.selectDetail(owner, DISPUTE_ID);
  const operation = state.beginDetail(owner);
  state.acceptDetail(operation, normalizeBillingDisputeDetailResponse(detailPayload(), TENANT_ID), owner);
  state.finishDetail(operation);
  state.revealText(owner, "reason");
  const html = renderBillingDisputes(state.snapshot(), context, shell);
  assert.match(html, /data-billing-dispute-open-action="cancel"/);
  assert.doesNotMatch(html, /data-billing-dispute-open-action="approve"|data-billing-dispute-open-action="reject"/);
  assert.match(html, /2\.000000 CNY/);
  assert.match(html, /&lt;img&gt;/);
  assert.doesNotMatch(html, /private_reason_<script>|billing-dispute-private-key/);
  assert.match(html, /data-billing-dispute-private-text="reason"/);

  const node = { textContent: "" };
  hydrateBillingDisputePrivateText({
    querySelector: (selector) => selector.includes('="reason"') ? node : null,
  }, state.revealedText(owner));
  assert.equal(node.textContent, "private_reason_<script>");
});

test("approval Dialog uses the backend current disputable limit", () => {
  const context = authContext({ userId: "user_dispute_reviewer" });
  const shell = shellState(context);
  const owner = billingDisputePageOwner(context, shell);
  const state = createBillingDisputePageState();
  state.enter(owner);
  const list = state.beginList(owner);
  const reviewSummary = summary({
    status: "under_review",
    state_revision: 2,
    created_by: actor("user_dispute_creator"),
    reviewer: actor("user_dispute_reviewer"),
    review_started_at: "2026-08-15T08:01:00Z",
    updated_at: "2026-08-15T08:01:00Z",
  });
  state.acceptList(
    list,
    normalizeBillingDisputeListResponse(
      listPayload({ billing_disputes: [reviewSummary] }),
      TENANT_ID,
      50,
    ),
    owner,
  );
  state.finishList(list);
  state.selectDetail(owner, DISPUTE_ID);
  const operation = state.beginDetail(owner);
  state.acceptDetail(
    operation,
    normalizeBillingDisputeDetailResponse(
      detailPayload({ dispute: detail({ summary: reviewSummary }) }),
      TENANT_ID,
      DISPUTE_ID,
    ),
    owner,
  );
  state.finishDetail(operation);
  assert.equal(state.openAction(billingDisputePageOwner(context, shell, { mode: "approve" }), "approve"), true);

  const html = renderBillingDisputes(state.snapshot(), context, shell);
  assert.match(html, /上限[^<]*0\.750000/);
  assert.doesNotMatch(html, /上限[^<]*2\.000000/);
});

test("form parsers canonicalize scopes and approval amounts without authority fields", () => {
  const createForm = new FormData();
  createForm.set("scope_type", "request_ids");
  createForm.set("request_ids", "request_b\nrequest_a");
  createForm.set("reason", "scope_reason");
  assert.deepEqual(billingDisputeCreateRequestFromFormData(createForm), {
    scope_type: "request_ids",
    request_ids: ["request_a", "request_b"],
    reason: "scope_reason",
  });
  assert.equal(canonicalBillingDisputeAmount("1.25"), "1.250000");
  assert.equal(canonicalBillingDisputeAmount("1e2"), "");

  const actionForm = new FormData();
  actionForm.set("action", "approve");
  actionForm.set("expected_revision", "2");
  actionForm.set("decision_reason", "decision_reason");
  actionForm.set(`adjustment:${ITEM_ID}`, "1.5");
  const action = billingDisputeTransitionRequestFromFormData(actionForm);
  assert.deepEqual(Object.keys(action).sort(), ["action", "adjustments", "decision_reason", "expected_revision"]);
  assert.deepEqual(action.adjustments, [{ billing_dispute_item_id: ITEM_ID, amount: "1.500000" }]);
  assert.equal(action.currency, undefined);

  const filters = new FormData();
  filters.set("status", "submitted");
  filters.set("scope_type", "request_ids");
  assert.equal(billingDisputeFiltersFromFormData(filters).cursor, null);
});

test("Dialog focus wraps and busy Escape survives node replacement", () => {
  const first = { offsetParent: {}, focused: false, focus() { this.focused = true; } };
  const last = { offsetParent: {}, focused: false, focus() { this.focused = true; } };
  const dialog = { querySelectorAll: () => [first, last] };
  let prevented = false;
  trapBillingDisputeDialogFocus(dialog, {
    shiftKey: false,
    preventDefault() { prevented = true; },
  }, { activeElement: last });
  assert.equal(prevented, true);
  assert.equal(first.focused, true);

  const listeners = [];
  let busyDialog = { open: true };
  const documentRef = {
    addEventListener(type, listener, options) { listeners.push({ type, listener, options }); },
    querySelector() { return busyDialog; },
  };
  assert.equal(installBillingDisputeBusyEscapeGuard(documentRef), true);
  assert.equal(installBillingDisputeBusyEscapeGuard(documentRef), false);
  listeners[0].listener({ key: "Escape", preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  busyDialog = null;
  prevented = false;
  listeners[0].listener({ key: "Escape", preventDefault() { prevented = true; } });
  assert.equal(prevented, false);
});

test("main lifecycle and CSS contain PAGE-112 cleanup plus 1280/mobile layout contracts", async () => {
  const [mainSource, stateSource, cssSource] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/billing-disputes/billingDisputePageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);
  assert.match(mainSource, /clearBillingDisputeStateOnIdentityChange/);
  assert.match(mainSource, /nextItemId !== "billing-disputes"/);
  assert.match(mainSource, /itemId !== "billing-disputes"/);
  assert.match(mainSource, /billingDisputePageState\.blocksShellDomReplacement\(\)/);
  assert.doesNotMatch(stateSource, /localStorage|sessionStorage|indexedDB/);
  assert.match(cssSource, /@media \(max-width: 1360px\)[\s\S]*\.billing-dispute-layout\.has-detail/);
  assert.match(cssSource, /@media \(max-width: 390px\)[\s\S]*\.billing-dispute-filters/);
  assert.match(cssSource, /max-width: 100%[\s\S]*overflow-x: auto/);
});
