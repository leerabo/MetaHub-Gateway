import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  getPlatformConsoleOverview,
  listPlatformConsoleApprovals,
  normalizePlatformApprovals,
  normalizePlatformAssistant,
  normalizePlatformOverview,
  queryPlatformConsoleAssistant,
} from "../../src/lib/platformConsoleExperience.js";
import {
  getWorkspaceConsoleOverview,
  getWorkspaceDeploymentSummary,
  listWorkspaceConsoleApprovals,
  normalizeWorkspaceApprovals,
  normalizeWorkspaceAssistant,
  normalizeWorkspaceDeploymentSummary,
  normalizeWorkspaceOverview,
  queryWorkspaceConsoleAssistant,
} from "../../src/lib/workspaceConsoleExperience.js";
import {
  getAccountPreferences,
  normalizeAccountPreferenceCommand,
  normalizeAccountPreferences,
  updateAccountPreferences,
} from "../../src/lib/accountPreferences.js";
import { createPlatformConsoleExperienceStore } from "../../src/features/platform-console-experience/platformConsoleExperienceStore.js";
import { createWorkspaceConsoleExperienceStore } from "../../src/features/workspace-console-experience/workspaceConsoleExperienceStore.js";
import { createAccountPreferenceStore } from "../../src/features/account-preferences/accountPreferenceStore.js";

const ORIGINAL_FETCH = globalThis.fetch;
const ORIGINAL_DOCUMENT = globalThis.document;
const BASE_URL = "https://console.invalid";
const FINGERPRINT = "a".repeat(64);
const CSRF = "c".repeat(43);
const NOW = "2026-09-03T00:00:00Z";

test.afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  if (ORIGINAL_DOCUMENT === undefined) delete globalThis.document;
  else globalThis.document = ORIGINAL_DOCUMENT;
});

function overview(domain, revision = 1, epoch = 1) {
  return {
    domain,
    status: "ready",
    generation: 1,
    navigation_epoch: epoch,
    observed_at: NOW,
    sections: [{
      source_code: "operations",
      freshness: "fresh",
      source_revision: 1,
      source_watermark: NOW,
      metrics: [{
        metric_code: "requests",
        value: 3,
        state_code: "ready",
        action_code: null,
        reason_code: null,
      }],
    }],
    permission_revision: revision,
    permission_fingerprint: FINGERPRINT,
    request_id: "req_overview",
    trace_id: "trc_overview",
  };
}

function assistant(domain, revision = 1, epoch = 1) {
  return {
    query_id: "qry_summary",
    domain,
    query_type: "operations_summary",
    outcome: "answered",
    answer_code: "summary.ready",
    claims: [{ claim_code: "requests", value: 3, state_code: "ready" }],
    citations: [{
      source_type: "operations",
      label_code: "requests",
      navigation_id: "overview",
    }],
    sources: [{ source_type: "operations", status: "ready" }],
    navigation_epoch: epoch,
    permission_revision: revision,
    permission_fingerprint: FINGERPRINT,
    request_id: "req_assistant",
    trace_id: "trc_assistant",
  };
}

function approvals(domain, cursor = null, revision = 1, epoch = 1) {
  return {
    domain,
    items: [],
    next_cursor: cursor,
    navigation_epoch: epoch,
    permission_revision: revision,
    permission_fingerprint: FINGERPRINT,
    request_id: "req_approvals",
    trace_id: "trc_approvals",
  };
}

function deployment(revision = 1, epoch = 1) {
  return {
    scope: "operator",
    items: [],
    projection_generation: 1,
    navigation_epoch: epoch,
    permission_revision: revision,
    permission_fingerprint: FINGERPRINT,
    request_id: "req_deployment",
    trace_id: "trc_deployment",
  };
}

function preference(revision = 1, locale = "zh-CN", command = false) {
  return {
    preference_id: "pref_global",
    locale,
    available_locales: ["zh-CN", "en-US"],
    revision,
    confirmation_state: "confirmed",
    updated_at: NOW,
    request_id: "req_preference",
    trace_id: "trc_preference",
    ...(command ? { changed: true, idempotent_replay: false } : {}),
  };
}

function privateResponse(payload, { preferenceRevision = null } = {}) {
  const headers = {
    "content-type": "application/json",
    "cache-control": "private, no-store",
    pragma: "no-cache",
    vary: "Cookie, Origin",
  };
  if (payload.permission_fingerprint) {
    headers["x-permission-fingerprint"] = payload.permission_fingerprint;
  }
  if (preferenceRevision !== null) headers.etag = `"${preferenceRevision}"`;
  return new Response(JSON.stringify(payload), { status: 200, headers });
}

test("domain clients use cookie sessions, exact CSRF cookies, CAS and no bearer header", async () => {
  globalThis.document = {
    cookie: [
      `__Host-metahub-platform-csrf=${CSRF}`,
      `__Host-metahub-workspace-csrf=${CSRF}w`,
      `__Host-metahub-global-csrf=${CSRF}g`,
    ].join("; "),
  };
  const queue = [
    privateResponse(overview("platform")),
    privateResponse(assistant("platform")),
    privateResponse(approvals("platform")),
    privateResponse(overview("workspace")),
    privateResponse(assistant("workspace")),
    privateResponse(approvals("workspace")),
    privateResponse(deployment()),
    privateResponse(preference(1), { preferenceRevision: 1 }),
    privateResponse(preference(2, "en-US", true), { preferenceRevision: 2 }),
  ];
  const calls = [];
  globalThis.fetch = async (_url, options) => {
    calls.push(options);
    return queue.shift();
  };

  await getPlatformConsoleOverview({ controlApiUrl: BASE_URL });
  await queryPlatformConsoleAssistant({ controlApiUrl: BASE_URL, question: "Summarize operations" });
  await listPlatformConsoleApprovals({ controlApiUrl: BASE_URL });
  await getWorkspaceConsoleOverview({ controlApiUrl: BASE_URL });
  await queryWorkspaceConsoleAssistant({ controlApiUrl: BASE_URL, question: "Summarize operations" });
  await listWorkspaceConsoleApprovals({ controlApiUrl: BASE_URL });
  await getWorkspaceDeploymentSummary({ controlApiUrl: BASE_URL });
  await getAccountPreferences({ controlApiUrl: BASE_URL });
  await updateAccountPreferences({
    controlApiUrl: BASE_URL,
    locale: "en-US",
    revision: 1,
    idempotencyKey: "idem_account_preference_001",
  });

  assert.equal(calls.length, 9);
  for (const options of calls) {
    assert.equal(options.credentials, "include");
    assert.equal(options.cache, "no-store");
    assert.equal(new Headers(options.headers).has("authorization"), false);
  }
  assert.equal(new Headers(calls[1].headers).get("x-csrf-token"), CSRF);
  assert.equal(new Headers(calls[4].headers).get("x-csrf-token"), `${CSRF}w`);
  const preferenceHeaders = new Headers(calls[8].headers);
  assert.equal(preferenceHeaders.get("x-csrf-token"), `${CSRF}g`);
  assert.equal(preferenceHeaders.get("if-match"), '"1"');
  assert.equal(preferenceHeaders.get("idempotency-key"), "idem_account_preference_001");
});

test("all public DTO normalizers reject unknown fields", () => {
  const cases = [
    [normalizePlatformOverview, overview("platform")],
    [normalizePlatformAssistant, assistant("platform")],
    [normalizePlatformApprovals, approvals("platform")],
    [normalizeWorkspaceOverview, overview("workspace")],
    [normalizeWorkspaceAssistant, assistant("workspace")],
    [normalizeWorkspaceApprovals, approvals("workspace")],
    [normalizeWorkspaceDeploymentSummary, deployment()],
    [normalizeAccountPreferences, preference()],
    [normalizeAccountPreferenceCommand, preference(2, "en-US", true)],
  ];
  for (const [normalize, payload] of cases) {
    assert.ok(normalize(payload));
    assert.throws(
      () => normalize({ ...payload, unexpected_private_field: "blocked" }),
      (error) => error?.code?.endsWith(".response_invalid"),
    );
  }
});

test("platform and workspace stores isolate owners, cursors and late responses", () => {
  const platform = createPlatformConsoleExperienceStore();
  const workspace = createWorkspaceConsoleExperienceStore();
  const platformOwner = {
    operatorId: "operator_1",
    sessionRevision: 1,
    roles: ["platform_admin"],
    permissions: ["platform.console.overview.read"],
  };
  const workspaceOwner = {
    accountId: "account_1",
    tenantId: "tenant_1",
    sessionId: "session_1",
    permissionRevision: 1,
    permissionFingerprint: FINGERPRINT,
    permissions: ["workspace.console.overview.read"],
  };

  assert.equal(platform.enter(platformOwner, "overview"), true);
  assert.equal(workspace.enter(workspaceOwner, "overview"), true);
  const platformTicket = platform.begin("overview");
  const workspaceTicket = workspace.begin("overview");
  assert.equal(platform.accept(platformTicket, overview("platform"), platformOwner), true);
  assert.equal(workspace.accept(workspaceTicket, overview("workspace"), workspaceOwner), true);
  assert.equal(platform.snapshot().overview.domain, "platform");
  assert.equal(workspace.snapshot().overview.domain, "workspace");

  platform.enter(platformOwner, "approvals");
  const cursor = "x".repeat(43);
  const approvalTicket = platform.begin("approvals");
  assert.equal(platform.accept(approvalTicket, approvals("platform", cursor), platformOwner), true);
  assert.equal(platform.nextApprovals(platformOwner), true);
  const nextTicket = platform.begin("approvals");
  assert.equal(nextTicket.cursor, cursor);
  assert.doesNotMatch(JSON.stringify(platform.snapshot()), new RegExp(cursor));
  assert.equal(platform.accept(nextTicket, approvals("platform"), platformOwner), true);

  const late = workspace.begin("overview");
  assert.ok(late);
  workspace.leave();
  assert.equal(workspace.accept(late, overview("workspace", 2, 2), workspaceOwner), false);
  assert.equal(workspace.snapshot().overview, null);
  assert.equal(platform.snapshot().approvals.domain, "platform");
});

test("regressed console responses terminate busy state as a conflict", () => {
  const platform = createPlatformConsoleExperienceStore();
  const platformOwner = {
    operatorId: "operator_1",
    sessionRevision: 1,
    roles: ["platform_admin"],
    permissions: ["platform.console.overview.read"],
  };
  platform.enter(platformOwner, "overview");
  assert.equal(platform.accept(platform.begin("overview"), overview("platform", 2, 2), platformOwner), true);
  assert.equal(platform.accept(platform.begin("overview"), overview("platform", 1, 1), platformOwner), false);
  assert.equal(platform.snapshot().busy, false);
  assert.equal(platform.snapshot().status, "conflict");
  assert.equal(platform.snapshot().error.code, "console_experience.response_regressed");

  const workspace = createWorkspaceConsoleExperienceStore();
  const workspaceOwner = {
    accountId: "account_1",
    tenantId: "tenant_1",
    sessionId: "session_1",
    permissionRevision: 1,
    permissionFingerprint: FINGERPRINT,
    permissions: ["workspace.console.overview.read"],
  };
  workspace.enter(workspaceOwner, "overview");
  assert.equal(workspace.accept(workspace.begin("overview"), overview("workspace", 2, 2), workspaceOwner), true);
  assert.equal(workspace.accept(workspace.begin("overview"), overview("workspace", 1, 1), workspaceOwner), false);
  assert.equal(workspace.snapshot().busy, false);
  assert.equal(workspace.snapshot().status, "conflict");
});

test("global preference CAS reuses retry intent and rejects revision rollback", () => {
  let keySequence = 0;
  const store = createAccountPreferenceStore({
    createIdempotencyKey: () => `preference_intent_${++keySequence}`,
  });
  const owner = { accountId: "account_1", sessionId: "session_1" };
  assert.equal(store.enter(owner), true);
  assert.equal(store.accept(store.beginLoad(owner), preference(2), owner), true);

  const first = store.beginSave(owner, "en-US");
  assert.equal(first.revision, 2);
  assert.equal(store.reject(first, { status: 503, code: "identity.unavailable" }, owner), true);
  const retry = store.beginSave(owner, "en-US");
  assert.equal(retry.idempotencyKey, first.idempotencyKey);
  assert.equal(store.accept(retry, preference(3, "en-US", true), owner), true);

  const rollback = store.beginSave(owner, "zh-CN");
  assert.equal(store.accept(rollback, preference(2, "zh-CN", true), owner), false);
  assert.equal(store.snapshot().busy, false);
  assert.equal(store.snapshot().status, "conflict");
  assert.equal(store.snapshot().preference.locale, "en-US");
  assert.equal(store.enter(owner), true);
});

test("workspace active dispatcher never invokes legacy overview, assistant or approval loaders", async () => {
  const main = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const refreshStart = main.indexOf("async function performRefresh");
  const dispatchCall = main.indexOf("await loadAndRenderActiveNavigation", refreshStart);
  assert.ok(refreshStart >= 0 && dispatchCall > refreshStart);
  const activeRefreshPrefix = main.slice(refreshStart, dispatchCall);
  assert.doesNotMatch(
    activeRefreshPrefix,
    /loadRoleDashboardState|loadAdminAssistantState|loadApprovalsState/,
  );

  const dispatcherStart = main.indexOf("async function loadAndRenderActiveNavigation");
  const dispatcherEnd = main.indexOf("\nasync function ", dispatcherStart + 1);
  assert.ok(dispatcherStart >= 0 && dispatcherEnd > dispatcherStart);
  const dispatcher = main.slice(dispatcherStart, dispatcherEnd);
  assert.match(dispatcher, /case "overview":[\s\S]*loadWorkspaceConsoleExperienceState/);
  assert.match(dispatcher, /case "admin-assistant":[\s\S]*loadWorkspaceConsoleExperienceState/);
  assert.match(dispatcher, /case "approvals":[\s\S]*loadWorkspaceConsoleExperienceState/);
  assert.doesNotMatch(
    dispatcher,
    /loadRoleDashboardState|loadAdminAssistantState|loadApprovalsState/,
  );
});

test("workspace shell applies the current global account locale before rendering", async () => {
  const main = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const loaderStart = main.indexOf("async function loadConsoleShellState");
  const loaderEnd = main.indexOf("\nasync function ", loaderStart + 1);
  const loader = main.slice(loaderStart, loaderEnd);
  assert.doesNotMatch(loader, /getAccountPreferences/u);
  assert.match(loader, /locale: navigation\.account_locale/u);

  const refreshStart = main.indexOf("async function performRefresh");
  const ownerCheck = main.indexOf("if (!refreshOwnerCurrent())", refreshStart);
  const localeApply = main.indexOf(
    "currentI18n = createI18n(consoleShellState.locale);",
    ownerCheck,
  );
  const render = main.indexOf("await loadAndRenderActiveNavigation", localeApply);
  assert.ok(refreshStart >= 0 && ownerCheck > refreshStart);
  assert.ok(localeApply > ownerCheck && render > localeApply);
});
