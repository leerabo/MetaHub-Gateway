import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createIdentityPageController } from "../../src/features/identity/identityPageState.js";
import { renderComplianceCenter } from "../../src/features/identity/renderComplianceCenter.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  ComplianceApiError,
  listPlatformComplianceControls,
  listWorkspaceComplianceReports,
} from "../../src/lib/complianceCenter.js";

const CONTROL_URL = "http://control.local";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const CONTROL_ID = `ctl_${"2".repeat(32)}`;
const EVIDENCE_ID = `evd_${"3".repeat(32)}`;
const REMEDIATION_ID = `rem_${"4".repeat(32)}`;
const REPORT_ID = `crp_${"5".repeat(32)}`;
const SERVICE_ACCOUNT_ID = `svc_${"6".repeat(32)}`;
const ORIGINAL_FETCH = globalThis.fetch;

test.afterEach(() => { globalThis.fetch = ORIGINAL_FETCH; });

test("PRD-135 strict clients use no-store Cookie reads and freeze owner-qualified facts", async () => {
  const calls = installFetch((url) => new URL(String(url)).pathname.startsWith("/platform/")
    ? jsonResponse(platformControls()) : jsonResponse(reportList()));

  const controls = await listPlatformComplianceControls({
    controlApiUrl: CONTROL_URL,
    workspaceId: WORKSPACE_ID,
  });
  const reports = await listWorkspaceComplianceReports({
    controlApiUrl: CONTROL_URL,
    workspaceId: WORKSPACE_ID,
  });

  assert.deepEqual(calls.map(({ url }) => new URL(url).pathname), [
    "/platform/compliance/controls",
    `/workspaces/${WORKSPACE_ID}/compliance-reports`,
  ]);
  assert.equal(new URL(calls[0].url).searchParams.get("workspace_id"), WORKSPACE_ID);
  for (const call of calls) {
    assert.equal(call.options.method, "GET");
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.headers.Authorization, undefined);
  }
  assert.equal(Object.isFrozen(controls.controls[0].evidence), true);
  assert.equal(Object.isFrozen(reports.current.items), true);
});

test("PRD-135 clients reject false certification, response extensions, and owner drift", async () => {
  globalThis.fetch = async () => jsonResponse({
    ...platformControls(), certification_claimed: true,
  });
  await assert.rejects(
    listPlatformComplianceControls({ controlApiUrl: CONTROL_URL, workspaceId: WORKSPACE_ID }),
    responseInvalid,
  );

  globalThis.fetch = async () => jsonResponse({ ...reportList(), evidence_body: "forbidden" });
  await assert.rejects(
    listWorkspaceComplianceReports({ controlApiUrl: CONTROL_URL, workspaceId: WORKSPACE_ID }),
    responseInvalid,
  );

  globalThis.fetch = async () => jsonResponse(reportList({
    workspaceId: `wsp_${"9".repeat(32)}`,
  }));
  await assert.rejects(
    listWorkspaceComplianceReports({ controlApiUrl: CONTROL_URL, workspaceId: WORKSPACE_ID }),
    responseInvalid,
  );
});

test("PRD-135 renderers expose evidence freshness, gaps, and remediation without claims", () => {
  const platformRoot = createRoot();
  renderPlatformWorkspace(platformRoot, {
    loading: false,
    session: platformSession(),
    navigation: [{ id: "compliance", label: "Compliance" }],
    view: "compliance",
    complianceWorkspaceId: WORKSPACE_ID,
    complianceControls: platformControls(),
    error: null,
    feedback: null,
    pendingAction: null,
  });
  assert.match(platformRoot.innerHTML, /控制覆盖与恢复状态/u);
  assert.match(platformRoot.innerHTML, /collector-1\.2\.3/u);
  assert.match(platformRoot.innerHTML, /失败样本/u);
  assert.match(platformRoot.innerHTML, /整改/u);
  assert.match(platformRoot.innerHTML, /不代表外部认证结论/u);
  assert.doesNotMatch(platformRoot.innerHTML, /证据正文|evidence_body|artifact_url|已认证/u);

  const identityHtml = renderComplianceCenter({
    workspaceContext: { workspace_id: WORKSPACE_ID },
    complianceVisible: true,
    complianceBusy: false,
    complianceReports: reportList(),
  });
  assert.match(identityHtml, /控制事实报告/u);
  assert.match(identityHtml, /1 项差距/u);
  assert.match(identityHtml, new RegExp(REMEDIATION_ID.slice(0, 8), "u"));
  assert.match(identityHtml, /证据过期/u);
  assert.match(identityHtml, /不代表外部认证结论/u);
  assert.doesNotMatch(identityHtml, /evidence_artifact_sha256|已认证/u);
});

test("PRD-135 Identity controller loads and refreshes reports only with exact permission", async () => {
  const calls = [];
  const root = createRoot();
  const controller = createIdentityPageController({
    root,
    channelFactory: () => null,
    api: identityApi({
      complianceReports: async ({ workspaceId }) => {
        calls.push(workspaceId);
        return reportList();
      },
    }),
  });
  assert.equal(await controller.mount(), true);
  assert.deepEqual(calls, [WORKSPACE_ID]);
  assert.match(root.innerHTML, /控制事实报告/u);
  assert.equal(await controller.refreshComplianceReports(), true);
  assert.deepEqual(calls, [WORKSPACE_ID, WORKSPACE_ID]);
  controller.destroy();
  assert.equal(root.innerHTML, "");

  let deniedCalls = 0;
  const deniedRoot = createRoot();
  const denied = createIdentityPageController({
    root: deniedRoot,
    channelFactory: () => null,
    api: identityApi({
      permissionCodes: [],
      complianceReports: async () => { deniedCalls += 1; return reportList(); },
    }),
  });
  assert.equal(await denied.mount(), true);
  assert.equal(deniedCalls, 0);
  assert.doesNotMatch(deniedRoot.innerHTML, /identity-compliance-title/u);
  assert.equal(await denied.refreshComplianceReports(), false);
  denied.destroy();
});

test("PRD-135 browser source has no storage, evidence body, or certification override", async () => {
  const source = await readFile(new URL("../../src/lib/complianceCenter.js", import.meta.url),
    "utf8");
  assert.doesNotMatch(source, /localStorage|sessionStorage|artifact_url|evidence_body/u);
  assert.match(source, /value\.certification_claimed === false/u);
  assert.match(source, /credentials: "include"/u);
  assert.match(source, /cache: "no-store"/u);
});

function platformControls({ workspaceId = WORKSPACE_ID } = {}) {
  return {
    control_set_version: "2026.08-v1",
    workspace_id: workspaceId,
    certification_claimed: false,
    claim_template_code: "control_fact_only_v1",
    controls: [{
      control: control(),
      report_outcome: "evidence_stale",
      explanation_code: "evidence_expired",
      evidence: evidence({ workspaceId }),
      exception: null,
      remediation: remediation(),
    }],
    request_id: "req_compliance_controls",
    trace_id: "trc_compliance_controls",
  };
}

function control() {
  return {
    compliance_control_id: CONTROL_ID,
    control_code: "access.review",
    control_version: 1,
    control_set_version: "2026.08-v1",
    title: "Privileged access review",
    owner_code: "security",
    responsibility: "shared",
    evidence_kind: "access_review",
    collector_schema_version: 1,
    freshness_seconds: 86400,
    criticality: "high",
    evaluation_rule_code: "sample_failure_v1",
    evaluation_rule_sha256: "a".repeat(64),
    mappings: [{
      framework_code: "djcp_mlps",
      framework_version: "2.0",
      clause_code: "8.1.4",
      statement_template_code: "control_fact_only_v1",
    }],
  };
}

function evidence({ workspaceId = WORKSPACE_ID } = {}) {
  return {
    compliance_evidence_id: EVIDENCE_ID,
    workspace_id: workspaceId,
    compliance_control_id: CONTROL_ID,
    evidence_sequence: 3,
    evidence_generation: 2,
    collector_service_account_id: SERVICE_ACCOUNT_ID,
    collector_version: "collector-1.2.3",
    evidence_kind: "access_review",
    schema_version: 1,
    artifact_sha256: "b".repeat(64),
    classification: "confidential",
    scope_state: "present",
    observed_state: "ineffective",
    sample_count: 10,
    failed_count: 2,
    outcome: "failed",
    outcome_reason_code: "sample_failure_observed",
    immutable_receipt_sha256: "c".repeat(64),
    captured_at: "2026-08-28T00:00:00Z",
    valid_until: "2026-08-29T00:00:00Z",
    submitted_at: "2026-08-28T00:01:00Z",
    freshness: "expired",
  };
}

function remediation() {
  return {
    remediation_task_id: REMEDIATION_ID,
    compliance_control_id: CONTROL_ID,
    failed_evidence_generation: 2,
    status: "open",
    owner_code: "security",
    due_at: "2026-09-05T00:00:00Z",
    revision: 1,
  };
}

function reportList({ workspaceId = WORKSPACE_ID } = {}) {
  return {
    current: report({ workspaceId }),
    recent: [],
    certification_claimed: false,
    request_id: "req_compliance_reports",
    trace_id: "trc_compliance_reports",
  };
}

function report({ workspaceId = WORKSPACE_ID } = {}) {
  return {
    compliance_report_id: REPORT_ID,
    workspace_id: workspaceId,
    report_version: 3,
    control_set_version: "2026.08-v1",
    evidence_high_watermark: 7,
    generator_version: "prd135-v1",
    summary_sha256: "d".repeat(64),
    implemented_count: 0,
    gap_count: 1,
    stale_count: 1,
    stale: true,
    expires_at: "2026-08-31T00:00:00Z",
    generated_at: "2026-08-30T00:00:00Z",
    items: [{
      compliance_control_id: CONTROL_ID,
      control_code: "access.review",
      title: "Privileged access review",
      report_outcome: "evidence_stale",
      explanation_code: "evidence_expired",
      evidence_generation: 2,
      evidence_artifact_sha256: "b".repeat(64),
      evidence_classification: "confidential",
      evidence_valid_until: "2026-08-29T00:00:00Z",
      compliance_exception_id: null,
      remediation_task_id: REMEDIATION_ID,
    }],
  };
}

function identityApi({ permissionCodes = ["compliance.report.read"], ...overrides } = {}) {
  return {
    me: async () => ({ account: identityAccount(), current_session: identitySession() }),
    sessions: async () => ({ sessions: [identitySession()] }),
    identityWorkspaces: async () => ({ workspaces: [workspaceChoice()] }),
    workspaceSession: async () => ({ context: workspaceSessionContext() }),
    workspaceNavigation: async () => ({
      navigation_items: [],
      permission_codes: permissionCodes,
      permission_revision: 1,
      authorization_fingerprint: "compliance-authz-fingerprint",
    }),
    logout: async () => ({ logged_out: true }),
    ...overrides,
  };
}

function identityAccount() {
  return {
    email: "owner@example.test", display_name: "Owner", status: "active", mfa_configured: true,
  };
}

function identitySession() {
  return {
    session_id: `gis_${"7".repeat(32)}`,
    device_family: "chrome",
    created_at: "2026-08-30T09:00:00Z",
    last_seen_at: "2026-08-30T10:00:00Z",
    expires_at: "2026-08-31T10:00:00Z",
    is_current: true,
    assurance_level: "mfa",
    auth_method: "totp",
    trusted_device_id: null,
    revoke_generation: 1,
  };
}

function workspaceChoice() {
  return {
    workspace_id: WORKSPACE_ID, kind: "organization", name: "Acme", slug: "acme", role: "owner",
  };
}

function workspaceSessionContext() {
  return {
    ...workspaceChoice(), session_status: "active", expires_at: "2026-08-31T10:00:00Z",
  };
}

function platformSession() {
  return {
    context: {
      operator_id: `pto_${"8".repeat(32)}`,
      display_name: "Compliance operator",
      roles: ["platform_compliance"],
      permissions: ["platform.compliance.read"],
    },
  };
}

function responseInvalid(error) {
  return error instanceof ComplianceApiError
    && error.code === "compliance.response_invalid" && error.status === 502;
}

function createRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    contains() { return true; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    replaceChildren() { this.innerHTML = ""; },
  };
}

function installFetch(responder) {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return responder(url, options);
  };
  return calls;
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
