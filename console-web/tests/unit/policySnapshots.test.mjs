// 本单测覆盖 PRD-045 策略快照控制台，验证双 CAS、稳定幂等键、admin 门禁、竞态、转义和响应式结构。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  createPolicySnapshotLoadLifecycle,
  createPolicySnapshotOperationMemory,
  isPolicySnapshotReasonValid,
  policySnapshotIdentityFromAuthContext,
  policySnapshotIdentitySnapshot,
  renderPolicySnapshotsPanel,
  shouldLoadPolicySnapshots,
} from "../../src/features/policy-snapshots/renderPolicySnapshotsPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";
import {
  createPolicySnapshotRequestId,
  getCurrentPolicySnapshot,
  listPolicySnapshots,
  publishPolicySnapshot,
  rollbackPolicySnapshot,
} from "../../src/lib/policySnapshots.js";

// 历史样本覆盖单调回滚、上一版限制、actor/reason 转义与候选漂移，不包含任何 DSL 或请求正文。
const v1 = {
  policy_snapshot_id: "psnap_v1",
  snapshot_version: 1,
  schema_version: "policy-snapshot-v1",
  operation: "publish",
  content_hash: "a".repeat(64),
  artifact_hash: "1".repeat(64),
  policy_count: 1,
  change_reason: "首次发布",
  approval_status: "not_required",
  previous_snapshot_version: null,
  rollback_source_snapshot_version: null,
  created_by: { user_id: "usr_1", email: "admin@example.com", display_name: "管理员" },
  created_at: "2026-07-17T08:00:00Z",
  published_at: "2026-07-17T08:00:00Z",
};
const v2 = {
  ...v1,
  policy_snapshot_id: "psnap_v2",
  snapshot_version: 2,
  content_hash: "b".repeat(64),
  artifact_hash: "2".repeat(64),
  policy_count: 2,
  change_reason: "切换候选",
  previous_snapshot_version: 1,
  created_at: "2026-07-17T09:00:00Z",
  published_at: "2026-07-17T09:00:00Z",
};
const v3 = {
  ...v1,
  policy_snapshot_id: "psnap_v3",
  snapshot_version: 3,
  operation: "rollback",
  artifact_hash: "3".repeat(64),
  change_reason: "回滚已验证内容",
  previous_snapshot_version: 2,
  rollback_source_snapshot_version: 1,
  created_at: "2026-07-17T10:00:00Z",
  published_at: "2026-07-17T10:00:00Z",
};

// SecurityAdmin 与 Admin 只要持有后端发布权限快照就可使用页面；角色名本身不参与授权。
const publisherContext = {
  user: { role: "security_admin" },
  permission_codes: [PERMISSIONS.POLICY_PUBLISH],
};

const currentPayload = {
  current_snapshot: v3,
  candidate_status: "valid",
  candidate_content_hash: "b".repeat(64),
  candidate_policy_count: 2,
  candidate_matches_current: false,
  request_id: "req_current",
  trace_id: "trc_current",
};

const historyPayload = {
  policy_snapshots: [v3, v2, v1],
  total: 3,
  limit: 20,
  offset: 0,
  request_id: "req_history",
  trace_id: "trc_history",
};

test("policy snapshot client preserves strong ETags and sends publish/rollback contracts", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    const requestUrl = String(url);
    const isCurrent = requestUrl.endsWith("/current");
    const isHistory = requestUrl.includes("/policy-snapshots?");
    const isRollback = requestUrl.endsWith("/psnap_v2/rollback");
    const payload = isCurrent
      ? currentPayload
      : isHistory
        ? historyPayload
        : {
          policy_snapshot: isRollback
            ? { ...v3, policy_snapshot_id: "psnap_v4", snapshot_version: 4, rollback_source_snapshot_version: 2 }
            : { ...v3, policy_snapshot_id: "psnap_v4", snapshot_version: 4, operation: "publish" },
          idempotent_replay: false,
          request_id: "req_write",
          trace_id: "trc_write",
        };
    return {
      ok: true,
      status: options.method === "POST" ? 201 : 200,
      headers: new Map([
        ["ETag", isCurrent ? '"3"' : '"4"'],
        ["X-Policy-Candidate-ETag", `"${"b".repeat(64)}"`],
      ]),
      async json() {
        return payload;
      },
    };
  };

  const controller = new AbortController();
  const current = await getCurrentPolicySnapshot({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    signal: controller.signal,
  });
  const history = await listPolicySnapshots({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    limit: 10,
    offset: 20,
    signal: controller.signal,
  });
  await publishPolicySnapshot({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    reason: "发布验证候选",
    headEtag: '"3"',
    candidateEtag: `"${"b".repeat(64)}"`,
    requestId: "req_publish_stable",
  });
  await rollbackPolicySnapshot({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    policySnapshotId: "psnap_v2",
    reason: "回滚上一版",
    headEtag: '"3"',
    requestId: "req_rollback_stable",
  });

  assert.equal(current.headEtag, '"3"');
  assert.equal(current.candidateEtag, `"${"b".repeat(64)}"`);
  assert.equal(current.current_snapshot.snapshot_version, 3);
  assert.equal(history.policy_snapshots.length, 3);
  assert.equal(calls[0].options.signal, controller.signal);
  assert.match(calls[1].url, /limit=10/);
  assert.match(calls[1].url, /offset=20/);
  assert.doesNotMatch(calls.map((call) => call.url).join(" "), /tenant/);

  const publishCall = calls[2];
  assert.equal(publishCall.options.headers["If-Match"], '"3"');
  assert.equal(publishCall.options.headers["If-Policy-Candidate-Match"], `"${"b".repeat(64)}"`);
  assert.equal(publishCall.options.headers["X-Request-ID"], "req_publish_stable");
  assert.deepEqual(JSON.parse(publishCall.options.body), { reason: "发布验证候选" });
  assert.equal(JSON.parse(publishCall.options.body).tenant_id, undefined);

  const rollbackCall = calls[3];
  assert.equal(rollbackCall.options.headers["If-Match"], '"3"');
  assert.equal(rollbackCall.options.headers["If-Policy-Candidate-Match"], undefined);
  assert.equal(rollbackCall.options.headers["X-Request-ID"], "req_rollback_stable");
  assert.match(rollbackCall.url, /psnap_v2\/rollback$/);
});

test("policy snapshot client maps unified HTTP errors and leaves network uncertainty intact", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 409,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "policy_snapshot.version_conflict",
        message: "线上版本已变化",
        request_id: "req_conflict",
        trace_id: "trc_conflict",
      };
    },
  });
  await assert.rejects(
    () => getCurrentPolicySnapshot({ controlApiUrl: "http://control.local", token: "jwt-token" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.status, 409);
      assert.equal(error.code, "policy_snapshot.version_conflict");
      assert.equal(error.requestId, "req_conflict");
      return true;
    },
  );

  const networkError = new TypeError("network offline");
  globalThis.fetch = async () => {
    // 网络异常必须原样穿透，页面才能把提交结果识别为未知并保留稳定 request_id。
    throw networkError;
  };
  await assert.rejects(
    () => getCurrentPolicySnapshot({ controlApiUrl: "http://control.local", token: "jwt-token" }),
    (error) => error === networkError,
  );
});

test("policy snapshot renderer shows drift and only permits the previous version rollback", () => {
  const unsafeState = {
    state: "ok",
    ...currentPayload,
    ...historyPayload,
    headEtag: '"3"',
    candidateEtag: `"${"b".repeat(64)}"`,
    publishReason: "<script>发布</script>",
    rollbackTargetId: "psnap_v2",
    rollbackReason: "</textarea><img src=x onerror=alert(1)>",
    operationStatus: "idle",
    policy_snapshots: [
      v3,
      {
        ...v2,
        change_reason: '<img src=x onerror="alert(1)">',
        created_by: { ...v2.created_by, display_name: "<svg onload=alert(1)>" },
      },
      v1,
    ],
  };
  const html = renderPolicySnapshotsPanel(unsafeState, publisherContext);

  assert.match(html, /当前 v3/);
  assert.match(html, /v3 &lt;- 回滚 v1/);
  assert.match(html, /回滚内容与候选不同/);
  assert.match(html, /下一次发布会重新上线当前候选内容/);
  assert.match(html, /data-policy-snapshot-rollback-open="psnap_v2"/);
  assert.doesNotMatch(html, /data-policy-snapshot-rollback-open="psnap_v1"/);
  assert.match(html, /data-policy-snapshot-rollback-form="psnap_v2"/);
  assert.match(html, /&lt;script&gt;发布&lt;\/script&gt;/);
  assert.match(html, /&lt;\/textarea&gt;&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /&lt;svg onload=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /<script>|<svg|<img/);
  assert.doesNotMatch(html, /tenant_id|condition_expr|canonical_expression|prompt|response/i);
});

test("policy snapshot renderer fails closed for invalid candidate and missing publish permission", () => {
  const invalid = renderPolicySnapshotsPanel(
    {
      state: "ok",
      ...historyPayload,
      current_snapshot: null,
      candidate_status: "invalid",
      candidate_content_hash: null,
      candidate_policy_count: null,
      candidate_matches_current: null,
      headEtag: '"0"',
      candidateEtag: null,
      publishReason: "尝试发布",
      operationStatus: "idle",
    },
    publisherContext,
  );
  assert.match(invalid, /候选无效/);
  assert.match(invalid, /data-policy-snapshot-publish disabled/);
  assert.match(invalid, /尚未发布/);
  assert.doesNotMatch(invalid, /<strong>v0<|>v0</);
  assert.equal(renderPolicySnapshotsPanel({ state: "ok" }, { user: { role: "developer" } }), "");
  assert.equal(renderPolicySnapshotsPanel({ state: "ok" }, { user: { role: "viewer" } }), "");
});

test("unknown write result locks the original operation and always exposes an abandon control", () => {
  const html = renderPolicySnapshotsPanel(
    {
      state: "ok",
      ...currentPayload,
      ...historyPayload,
      headEtag: '"3"',
      candidateEtag: '"candidate"',
      publishReason: "被后续输入覆盖的原因",
      rollbackTargetId: null,
      rollbackReason: "",
      operationStatus: "idle",
      pendingOperation: {
        kind: "publish",
        targetId: "",
        reason: "原始发布原因",
        headEtag: '"2"',
        candidateEtag: '"old-candidate"',
        requestId: "req_unknown_stable",
      },
    },
    publisherContext,
  );

  assert.match(html, /发布结果待确认/);
  assert.match(html, /req_unknown_stable/);
  assert.match(html, /readonly[^>]*>原始发布原因<\/textarea>/);
  assert.doesNotMatch(html, />被后续输入覆盖的原因<\/textarea>/);
  assert.match(html, /data-policy-snapshot-abandon/);
  assert.match(html, />重试发布<\/button>/);
});

test("operation memory reuses request id for exact retry and blocks changed fingerprints", () => {
  let counter = 0;
  const memory = createPolicySnapshotOperationMemory({
    requestIdFactory: () => `req_stable_${++counter}`,
  });
  const descriptor = {
    kind: "publish",
    targetId: "",
    reason: "上线候选",
    headEtag: '"3"',
    candidateEtag: '"candidate"',
  };
  const first = memory.acquire("tenant-a-user-a-admin", descriptor);
  memory.markUncertain(first.operation);
  const retry = memory.acquire("tenant-a-user-a-admin", { ...descriptor });
  const changedReason = memory.acquire("tenant-a-user-a-admin", { ...descriptor, reason: "另一次发布" });

  assert.equal(first.operation.requestId, "req_stable_1");
  assert.equal(retry.reused, true);
  assert.equal(retry.operation.requestId, first.operation.requestId);
  assert.equal(changedReason.state, "blocked");
  assert.equal(counter, 1);
  assert.equal(memory.markDefinitive(retry.operation), true);
  const next = memory.acquire("tenant-a-user-a-admin", { ...descriptor, reason: "另一次发布" });
  assert.equal(next.operation.requestId, "req_stable_2");
  assert.equal(memory.read("tenant-b-user-a-admin"), null);
});

test("load lifecycle aborts stale owners and loader predicate binds navigation generation and identity", () => {
  const lifecycle = createPolicySnapshotLoadLifecycle();
  const first = lifecycle.begin({ refreshId: 10, navigation: "policy-snapshots", identity: "tenant-a" });
  const second = lifecycle.begin({ refreshId: 11, navigation: "policy-snapshots", identity: "tenant-a" });
  assert.equal(first.signal.aborted, true);
  assert.equal(lifecycle.isCurrent(first, { refreshId: 10, navigation: "policy-snapshots", identity: "tenant-a" }), false);
  assert.equal(lifecycle.isCurrent(second, { refreshId: 11, navigation: "policy-snapshots", identity: "tenant-a" }), true);
  lifecycle.invalidate();
  assert.equal(second.signal.aborted, true);
  assert.equal(lifecycle.isBusy(), false);

  const valid = {
    canPublish: true,
    activeNavigationItemId: "policy-snapshots",
    hashNavigation: "policy-snapshots",
    refreshId: 12,
    currentRefreshId: 12,
    requestIdentity: "tenant-a-user-a-admin",
    currentIdentity: "tenant-a-user-a-admin",
  };
  assert.equal(shouldLoadPolicySnapshots(valid), true);
  assert.equal(shouldLoadPolicySnapshots({ ...valid, canPublish: false }), false);
  assert.equal(shouldLoadPolicySnapshots({ ...valid, activeNavigationItemId: "policies" }), false);
  assert.equal(shouldLoadPolicySnapshots({ ...valid, hashNavigation: "overview" }), false);
  assert.equal(shouldLoadPolicySnapshots({ ...valid, currentRefreshId: 13 }), false);
  assert.equal(shouldLoadPolicySnapshots({ ...valid, currentIdentity: "tenant-b" }), false);
});

test("identity reason and request id helpers enforce in-memory safety boundaries", () => {
  const identity = policySnapshotIdentityFromAuthContext({
    user: { tenant_id: "tenant_1", user_id: "user_1", role: "admin" },
    session: { session_id: "session_1" },
  });
  assert.equal(
    policySnapshotIdentitySnapshot(identity),
    JSON.stringify(["tenant_1", "user_1", "admin", "session_1"]),
  );
  assert.equal(isPolicySnapshotReasonValid("上线已验证候选"), true);
  assert.equal(isPolicySnapshotReasonValid("   "), false);
  assert.equal(isPolicySnapshotReasonValid(`非法\n原因`), false);
  assert.equal(isPolicySnapshotReasonValid("x".repeat(257)), false);
  assert.match(createPolicySnapshotRequestId(), /^req_console_policy_snapshot_[a-zA-Z0-9_]+$/);
});

test("developer start renders active snapshot page only with publish permission", () => {
  const shellState = {
    activeNavigationItemId: "policy-snapshots",
    navigation: {
      active_navigation_item_id: "policy-snapshots",
      navigation_items: [
        {
          id: "policy-snapshots",
          label: "策略发布",
          group: "安全与治理",
          target_section: "policy-snapshots",
          description: "策略发布",
          order: 88,
        },
      ],
    },
  };
  const adminContainer = { innerHTML: "" };
  const developerContainer = { innerHTML: "" };
  renderDeveloperStart(adminContainer, {
    authContext: {
      user: { role: "security_admin", email: "security@example.com" },
      tenant: { name: "Tenant" },
      permission_codes: [PERMISSIONS.POLICY_PUBLISH],
    },
    consoleShellState: shellState,
    policySnapshotsState: {
      state: "ok",
      ...currentPayload,
      ...historyPayload,
      headEtag: '"3"',
      candidateEtag: '"candidate"',
      operationStatus: "idle",
    },
  });
  renderDeveloperStart(developerContainer, {
    authContext: {
      user: { role: "developer", email: "dev@example.com" },
      tenant: { name: "Tenant" },
      permission_codes: [],
    },
    consoleShellState: shellState,
    policySnapshotsState: { state: "ok", ...currentPayload, ...historyPayload },
  });
  assert.match(adminContainer.innerHTML, /class="panel policy-snapshots-panel"/);
  assert.match(adminContainer.innerHTML, /当前 v3/);
  assert.doesNotMatch(developerContainer.innerHTML, /policy-snapshots-panel/);
  assert.doesNotMatch(developerContainer.innerHTML, /回滚已验证内容/);
});

test("main integration and CSS preserve active-only abort no-storage and responsive contracts", async () => {
  const [mainSource, featureSource, clientSource, css] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/policy-snapshots/renderPolicySnapshotsPanel.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/policySnapshots.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);

  assert.match(mainSource, /loadPolicySnapshotsState\(consoleShellState, refreshId\)/);
  assert.match(mainSource, /shouldLoadPolicySnapshots\(loadArguments\)/);
  assert.match(mainSource, /clearPolicySnapshotSessionState\(\)/);
  assert.match(mainSource, /policySnapshotOperationMemory\.markUncertain\(operation\)/);
  assert.match(mainSource, /operation\.requestId/);
  assert.match(clientSource, /If-Policy-Candidate-Match/);
  assert.doesNotMatch(featureSource, /(?:localStorage|sessionStorage)\s*\./);
  assert.doesNotMatch(clientSource, /tenant(?:Id|_id)\s*:/);
  assert.match(css, /\.policy-snapshot-row:not\(\.policy-snapshot-row-head\)[\s\S]*?content: attr\(data-label\)/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.policy-snapshot-summary-grid/);
  assert.match(css, /@media \(max-width: 1400px\)[\s\S]*?\.request-log-head/);
});
