import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { renderIdentitySecurityPage as renderAccountPage } from "../../src/features/identity/renderIdentityPage.js";
const renderIdentitySecurityPage = (root, state) => renderAccountPage(root, { accountSection: "invoices", ...state });
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  createPlatformInvoiceReversal,
  createPlatformReconciliation,
  createWorkspaceInvoice,
  downloadWorkspaceInvoiceArtifact,
  getWorkspaceInvoice,
  listPlatformReconciliationDifferences,
  listWorkspaceBillingStatements,
  reviewPlatformInvoiceReversal,
} from "../../src/lib/invoiceReconciliation.js";

const CONTROL_URL = "http://control.local";
const STATEMENT_ID = `bst_${"1".repeat(32)}`;
const CYCLE_ID = `pbc_${"2".repeat(32)}`;
const INVOICE_ID = `ivr_${"3".repeat(32)}`;
const ARTIFACT_ID = `ina_${"4".repeat(32)}`;
const RECONCILIATION_ID = `rec_${"5".repeat(32)}`;
const DIFFERENCE_ID = `rcd_${"6".repeat(32)}`;
const REVERSAL_ID = `irr_${"7".repeat(32)}`;
const DOWNLOAD_TOKEN = "download_token_value_must_remain_header_only_123";

test("workspace invoice clients use cookie domain, CSRF, idempotency, and server amounts", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=workspace%2Dcsrf" };
  const calls = installFetch((url, options) => {
    if (String(url).endsWith("/billing/statements")) return jsonResponse(statementsPayload());
    return jsonResponse(invoicePayload(), { status: options.method === "POST" ? 201 : 200 });
  });
  try {
    await listWorkspaceBillingStatements({ controlApiUrl: CONTROL_URL });
    await createWorkspaceInvoice({
      controlApiUrl: CONTROL_URL,
      billingStatementId: STATEMENT_ID,
      invoiceType: "electronic_general",
      idempotencyKey: "invoice-request-0001",
    });
    await getWorkspaceInvoice({ controlApiUrl: CONTROL_URL, invoiceId: INVOICE_ID });
  } finally {
    globalThis.document = originalDocument;
  }

  assert.deepEqual(calls.map(({ url, options }) => [url, options.method, options.credentials]), [
    ["http://control.local/billing/statements", "GET", "include"],
    ["http://control.local/invoices", "POST", "include"],
    [`http://control.local/invoices/${INVOICE_ID}`, "GET", "include"],
  ]);
  assert.equal("X-CSRF-Token" in calls[0].options.headers, false);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], "workspace-csrf");
  assert.equal(calls[1].options.headers["Idempotency-Key"], "invoice-request-0001");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    billing_statement_id: STATEMENT_ID,
    invoice_type: "electronic_general",
  });
  assert.doesNotMatch(calls[1].options.body, /amount|currency|workspace|provider|status/u);
  for (const call of calls) assert.equal("Authorization" in call.options.headers, false);
});

test("invoice artifact token is issued then consumed once in a dedicated header", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=workspace%2Dcsrf" };
  const content = new TextEncoder().encode("invoice-csv-content\r\n");
  const artifact = artifactPayload(content);
  const calls = installFetch((url) => {
    if (String(url).endsWith("/download-token")) {
      return jsonResponse({
        invoice_request_id: INVOICE_ID,
        invoice_artifact_id: ARTIFACT_ID,
        download_token: DOWNLOAD_TOKEN,
        expires_at: "2026-08-23T08:05:00Z",
        request_id: "req_invoice_download_grant",
        trace_id: "trc_invoice_download_grant",
      });
    }
    return new Response(content, {
      status: 200,
      headers: {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": `attachment; filename="invoice-${INVOICE_ID.slice(4, 12)}-${ARTIFACT_ID.slice(4, 12)}.csv"`,
        "Cache-Control": "no-store, private",
        "X-Content-Type-Options": "nosniff",
      },
    });
  });
  try {
    const result = await downloadWorkspaceInvoiceArtifact({
      controlApiUrl: CONTROL_URL,
      invoiceId: INVOICE_ID,
      artifact,
    });
    assert.equal(result.blob.size, content.byteLength);
  } finally {
    globalThis.document = originalDocument;
  }

  assert.equal(calls.length, 2);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers["X-CSRF-Token"], "workspace-csrf");
  assert.equal(calls[1].url, "http://control.local/invoice-downloads");
  assert.equal(calls[1].options.headers["X-MetaHub-Invoice-Download-Token"], DOWNLOAD_TOKEN);
  assert.equal(calls[1].url.includes(DOWNLOAD_TOKEN), false);
  assert.equal(calls[1].options.body, undefined);
});

test("platform reconciliation and reversal clients keep credentials domain-separated", async () => {
  const originalDocument = globalThis.document;
  const platformCsrf = "p".repeat(43);
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${platformCsrf}` };
  const calls = installFetch((url) => {
    const path = new URL(String(url)).pathname;
    if (path === "/platform/reconciliations") return jsonResponse(reconciliationPayload());
    if (path.endsWith("/differences")) return jsonResponse(differencesPayload());
    return jsonResponse(reversalPayload());
  });
  try {
    await createPlatformReconciliation({ controlApiUrl: CONTROL_URL, confirmed: true });
    await listPlatformReconciliationDifferences({
      controlApiUrl: CONTROL_URL,
      reconciliationId: RECONCILIATION_ID,
    });
    await createPlatformInvoiceReversal({
      controlApiUrl: CONTROL_URL,
      invoiceId: INVOICE_ID,
      reasonCode: "invoice_error",
      confirmed: true,
      idempotencyKey: "invoice-reversal-0001",
    });
    await reviewPlatformInvoiceReversal({
      controlApiUrl: CONTROL_URL,
      reversalId: REVERSAL_ID,
      decision: "approve",
      reasonCode: "facts_confirmed",
      confirmed: true,
      idempotencyKey: "invoice-approval-0001",
    });
  } finally {
    globalThis.document = originalDocument;
  }

  assert.deepEqual(calls.map(({ options }) => options.method), ["POST", "GET", "POST", "POST"]);
  assert.equal(calls[0].options.headers["X-CSRF-Token"], platformCsrf);
  assert.equal("X-CSRF-Token" in calls[1].options.headers, false);
  assert.equal(calls[2].options.headers["Idempotency-Key"], "invoice-reversal-0001");
  assert.equal(calls[3].options.headers["Idempotency-Key"], "invoice-approval-0001");
  assert.deepEqual(JSON.parse(calls[0].options.body), { scope: "all", confirmed: true });
  assert.deepEqual(JSON.parse(calls[3].options.body), {
    decision: "approve",
    reason_code: "facts_confirmed",
    confirmed: true,
  });
  for (const call of calls) {
    assert.equal(call.options.credentials, "include");
    assert.equal("Authorization" in call.options.headers, false);
  }
});

test("invoice client rejects unexpected response fields and noncanonical identifiers", async () => {
  installFetch(() => jsonResponse({ ...statementsPayload(), download_token: "must-not-pass" }));
  await assert.rejects(
    listWorkspaceBillingStatements({ controlApiUrl: CONTROL_URL }),
    (error) => error.code === "invoice.response_invalid" && error.status === 502,
  );
  await assert.rejects(
    async () => createWorkspaceInvoice({
      controlApiUrl: CONTROL_URL,
      billingStatementId: "statement-not-canonical",
      invoiceType: "electronic_general",
      idempotencyKey: "invoice-request-0002",
    }),
    (error) => error.code === "invoice.request_invalid",
  );
});

test("invoice and reconciliation renderers enforce finance and explicit platform roles", () => {
  const content = new TextEncoder().encode("invoice-csv-content\r\n");
  const identityContainer = { innerHTML: "" };
  const secondStatement = {
    ...statementsPayload().statements[0],
    billing_statement_id: `bst_${"a".repeat(32)}`,
    billing_cycle_id: `pbc_${"b".repeat(32)}`,
  };
  const identityState = {
    account: { display_name: "Finance", email: "finance@example.test", status: "active" },
    sessions: [],
    workspaceContext: { role: "finance_admin" },
    billingStatements: {
      ...statementsPayload(),
      statements: [...statementsPayload().statements, secondStatement],
    },
    currentInvoice: {
      ...invoicePayload(),
      invoice: { ...invoicePayload().invoice, artifacts: [artifactPayload(content)] },
    },
  };
  renderIdentitySecurityPage(identityContainer, identityState);
  assert.match(identityContainer.innerHTML, /data-invoice-request-form/u);
  assert.match(identityContainer.innerHTML, /data-identity-action="download-invoice-artifact"/u);
  assert.doesNotMatch(identityContainer.innerHTML, new RegExp(artifactPayload(content).content_sha256, "u"));
  renderIdentitySecurityPage(identityContainer, {
    ...identityState,
    workspaceContext: { role: "viewer" },
  });
  assert.doesNotMatch(identityContainer.innerHTML, /data-invoice-request-form|download-invoice-artifact/u);

  const platformContainer = { innerHTML: "" };
  const sourceId = `ord_${"8".repeat(32)}`;
  const platformState = platformBillingState("platform_billing");
  renderPlatformWorkspace(platformContainer, { ...platformState, reconciliation: null });
  assert.match(platformContainer.innerHTML, /data-platform-reconciliation-form/u);
  renderPlatformWorkspace(platformContainer, platformState);
  assert.match(platformContainer.innerHTML, /data-platform-action="reconciliation-refresh"/u);
  assert.match(platformContainer.innerHTML, /data-platform-reversal-review-form/u);
  assert.doesNotMatch(platformContainer.innerHTML, new RegExp(sourceId, "u"));
  renderPlatformWorkspace(platformContainer, platformBillingState("platform_owner"));
  assert.doesNotMatch(
    platformContainer.innerHTML,
    /data-platform-reconciliation-form|data-platform-reversal-review-form/u,
  );

  renderPlatformWorkspace(platformContainer, {
    ...platformBillingState("platform_compliance"),
    session: {
      context: {
        operator: { display_name: "Compliance", roles: ["platform_compliance"] },
        permissions: ["platform.compliance.read"],
      },
    },
    navigation: [{ id: "compliance", label: "Compliance" }],
    view: "compliance",
    billing: null,
    refunds: null,
    reconciliation: null,
  });
  assert.match(platformContainer.innerHTML, /<h1>合规复核<\/h1>/u);
  assert.match(platformContainer.innerHTML, /data-platform-reconciliation-form/u);
  assert.match(platformContainer.innerHTML, /data-platform-reversal-review-form/u);
  assert.doesNotMatch(platformContainer.innerHTML, /周期与差异状态|退款审核队列/u);
});

function statementsPayload() {
  return {
    statements: [{
      billing_statement_id: STATEMENT_ID,
      billing_cycle_id: CYCLE_ID,
      statement_kind: "standard",
      immutable_version: 1,
      currency: "CNY",
      subtotal_minor: 10_000,
      tax_rate_basis_points: 600,
      tax_minor: 600,
      total_minor: 10_600,
      status: "published",
      issued_at: "2026-08-23T08:00:00Z",
    }],
    request_id: "req_statements",
    trace_id: "trc_statements",
  };
}

function invoicePayload() {
  return {
    invoice: {
      invoice_request_id: INVOICE_ID,
      billing_statement_id: STATEMENT_ID,
      invoice_type: "electronic_general",
      status: "issued",
      currency: "CNY",
      total_minor: 10_600,
      billing_profile_revision: 2,
      state_revision: 3,
      artifacts: [],
      requested_at: "2026-08-23T08:01:00Z",
      updated_at: "2026-08-23T08:02:00Z",
    },
    idempotent_replay: false,
    request_id: "req_invoice",
    trace_id: "trc_invoice",
  };
}

function artifactPayload(content) {
  return {
    invoice_artifact_id: ARTIFACT_ID,
    artifact_kind: "invoice_csv",
    invoice_number_masked: "****1234",
    mime_type: "text/csv",
    size_bytes: content.byteLength,
    content_sha256: createHash("sha256").update(content).digest("hex"),
    issued_at: "2026-08-23T08:02:00Z",
    download_action: `/invoices/${INVOICE_ID}/artifacts/${ARTIFACT_ID}/download-token`,
  };
}

function reconciliationPayload() {
  return {
    reconciliation: reconciliationRun(),
    request_id: "req_reconciliation",
    trace_id: "trc_reconciliation",
  };
}

function differencesPayload() {
  return {
    reconciliation: reconciliationRun(),
    differences: [{
      reconciliation_difference_id: DIFFERENCE_ID,
      difference_type: "order_payment_amount_mismatch",
      left_source_type: "order",
      left_source_id: `ord_${"8".repeat(32)}`,
      right_source_type: "payment_intent",
      right_source_id: `pay_${"9".repeat(32)}`,
      currency: "CNY",
      amount_difference_minor: 100,
      status: "open",
      adjustment_reference: null,
      detected_at: "2026-08-23T08:04:00Z",
    }],
    next_cursor: null,
    request_id: "req_differences",
    trace_id: "trc_differences",
  };
}

function reconciliationRun() {
  return {
    reconciliation_run_id: RECONCILIATION_ID,
    scope: "all",
    order_watermark: 10,
    payment_watermark: 11,
    meter_watermark: 12,
    statement_watermark: 13,
    invoice_watermark: 14,
    status: "completed",
    generation: 1,
    created_at: "2026-08-23T08:03:00Z",
    completed_at: "2026-08-23T08:04:00Z",
  };
}

function reversalPayload() {
  return {
    reversal: {
      invoice_reversal_request_id: REVERSAL_ID,
      invoice_request_id: INVOICE_ID,
      reason_code: "invoice_error",
      status: "pending_approvals",
      approved_domains: [],
      state_revision: 1,
      requested_at: "2026-08-23T08:05:00Z",
      updated_at: "2026-08-23T08:05:00Z",
    },
    idempotent_replay: false,
    request_id: "req_reversal",
    trace_id: "trc_reversal",
  };
}

function platformBillingState(role) {
  return {
    session: {
      context: {
        operator: { display_name: "Operator", roles: [role] },
        permissions: ["platform.billing.read"],
      },
    },
    navigation: [{ id: "billing", label: "账单" }],
    view: "billing",
    billing: { cycles: [], next_cursor: null },
    refunds: { refunds: [] },
    reconciliation: differencesPayload(),
    reconciliationCursorHistory: [],
    reversal: reversalPayload(),
  };
}

function installFetch(resolver) {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return resolver(url, options);
  };
  return calls;
}

function jsonResponse(payload, { status = 200 } = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
