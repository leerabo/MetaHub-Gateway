// PRD-052 Browser 验收纯合同测试：冻结批次路径、Runtime 投影、布局、PNG 像素和写一次证据边界。
// 测试不启动服务或 Chrome，不读取真实凭据；唯一文件写入限定在 D:\MetaHub\.tmp 的随机子目录。
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { deflateSync } from "node:zlib";

import {
  BrowserAcceptanceFailure,
  assertLayoutMetrics,
  inspectPngPixels,
  normalizeLoopbackOrigin,
  publishBrowserEvidence,
  resolveArtifactDirectory,
  validateBudgetApiProjection,
  validateRuntimeEvidence,
} from "../../../scripts/acceptance/prd052-browser-acceptance.mjs";


/** 构造冻结的脱敏 Runtime 证据，所有资源均为不透明 ID，不包含 Key 明文或认证材料。 */
function runtimeEvidenceFixture() {
  return {
    schema_version: 1,
    status: "passed",
    prd_id: "PRD-052",
    generated_resources: {
      tenant_id: "tenant_runtime",
      environment_id: "env_runtime",
      project_id: "prj_runtime",
      application_id: "app_runtime",
      virtual_key_id: "vk_runtime",
      budget_rule_ids: {
        tenant: "br_tenant_runtime",
        project: "br_project_runtime",
        virtual_key: "br_key_runtime",
      },
      usage_ledger_id: "ulg_runtime",
      finops_ledger_entry_id: "fle_runtime",
    },
    checks: {
      budget_status: {
        expected_split: {
          ledger_spent_cny: "1.000000",
          reserved_cny: "0.000000",
          effective_spend_cny: "1.000000",
        },
        expected_split_scope_types: ["tenant", "project", "virtual_key"],
        three_level_summary_verified: true,
        trusted_scope_chain_verified: true,
        project_exhausted_verified: true,
      },
      gateway: {
        http_status: 402,
        request_id: "req_runtime_gateway",
        trace_id: "trc_runtime_gateway",
        code: "gateway.budget_exhausted",
        project_rule_governed: true,
        provider_attempt_count_zero: true,
      },
    },
    privacy: {
      environment_only_credentials: true,
      safe_projection_verified: true,
    },
    cleanup: {
      session_revoked: true,
    },
  };
}


/** 构造带可信父链的 BudgetScope，模拟真实 /budget-status value object。 */
function scopeFixture(scopeType, scopeId, parentScopeType, parentScopeId, priority) {
  return {
    budget_scope_id: `bsc_${scopeType}_runtime`,
    tenant_id: "tenant_runtime",
    environment_id: "env_runtime",
    scope_type: scopeType,
    scope_id: scopeId,
    scope_label: `${scopeType} scope`,
    priority,
    parent_scope_type: parentScopeType,
    parent_scope_id: parentScopeId,
  };
}


/** 构造三层规则和四段兼容作用链，三层金额均来自同一条正式 Usage。 */
function budgetPayloadFixture() {
  const runtime = validateRuntimeEvidence(runtimeEvidenceFixture());
  const scopes = {
    tenant: scopeFixture("tenant", runtime.tenantId, null, null, 0),
    project: scopeFixture("project", runtime.projectId, "tenant", runtime.tenantId, 1),
    application: scopeFixture("application", runtime.applicationId, "project", runtime.projectId, 2),
    virtual_key: scopeFixture("virtual_key", runtime.virtualKeyId, "application", runtime.applicationId, 3),
  };
  const specifications = [
    ["tenant", runtime.tenantId, runtime.ruleIds.tenant],
    ["project", runtime.projectId, runtime.ruleIds.project],
    ["virtual_key", runtime.virtualKeyId, runtime.ruleIds.virtual_key],
  ];
  const statuses = specifications.map(([scopeType, scopeId, ruleId]) => ({
    budget_rule_id: ruleId,
    environment_id: runtime.environmentId,
    scope_type: scopeType,
    scope_id: scopeId,
    budget_scope: scopes[scopeType],
    ledger_spent_cny: runtime.expectedSplit.ledger_spent_cny,
    reserved_cny: runtime.expectedSplit.reserved_cny,
    effective_spend_cny: runtime.expectedSplit.effective_spend_cny,
    status: scopeType === "project" ? "exhausted" : "ok",
    next_action: scopeType === "project" ? "block" : "allow",
    block_status: scopeType === "project" ? "active" : null,
  }));
  return {
    runtime,
    rulesPayload: {
      budget_rules: specifications.map(([scopeType, scopeId, ruleId]) => ({
        budget_rule_id: ruleId,
        environment_id: runtime.environmentId,
        scope_type: scopeType,
        scope_id: scopeId,
      })),
    },
    statusPayload: {
      statuses,
      aggregate: {
        aggregate_status: "exhausted",
        effective_action: "block",
        scope_chain: [scopes.tenant, scopes.project, scopes.application, scopes.virtual_key],
      },
    },
  };
}


/** 构造最小 PNG chunk；CRC 不参与被测解析器合同，因此使用固定零值。 */
function pngChunk(type, data) {
  const chunk = Buffer.alloc(12 + data.length);
  chunk.writeUInt32BE(data.length, 0);
  chunk.write(type, 4, 4, "ascii");
  data.copy(chunk, 8);
  return chunk;
}


/** 构造 2x2 RGBA PNG，可切换为多色实像素或单色空白占位图。 */
function pngFixture(varied = true) {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(2, 0);
  header.writeUInt32BE(2, 4);
  header[8] = 8;
  header[9] = 6;
  const white = [255, 255, 255, 255];
  const rowOne = Buffer.from([0, ...white, ...(varied ? [255, 0, 0, 255] : white)]);
  const rowTwo = Buffer.from([0, ...(varied ? [0, 0, 255, 255] : white), ...(varied ? [0, 255, 0, 255] : white)]);
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    pngChunk("IHDR", header),
    pngChunk("IDAT", deflateSync(Buffer.concat([rowOne, rowTwo]))),
    pngChunk("IEND", Buffer.alloc(0)),
  ]);
}


test("PRD-052 artifact path stays in one direct batch and origins stay loopback", () => {
  const root = process.cwd();
  const batch = path.join(root, "artifacts", "prd052", "batch-contract-001");
  assert.equal(resolveArtifactDirectory(batch, root), batch);
  assert.throws(
    () => resolveArtifactDirectory(path.join(batch, "nested"), root),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "batch_depth",
  );
  assert.throws(
    () => resolveArtifactDirectory(path.join(root, "artifacts", "prd051", "batch-contract-001"), root),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "outside_root",
  );
  assert.equal(normalizeLoopbackOrigin("http://127.0.0.1:5173", "console"), "http://127.0.0.1:5173");
  assert.throws(
    () => normalizeLoopbackOrigin("https://example.com:5173", "console"),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "scheme_not_http",
  );
});


test("PRD-052 Runtime fields and three-level API projection fail closed", () => {
  const evidence = runtimeEvidenceFixture();
  const runtime = validateRuntimeEvidence(evidence);
  assert.deepEqual(runtime.expectedSplitScopeTypes, ["tenant", "project", "virtual_key"]);
  const payloads = budgetPayloadFixture();
  const projection = validateBudgetApiProjection(
    payloads.rulesPayload,
    payloads.statusPayload,
    payloads.runtime,
  );
  assert.equal(projection.evidence.three_level_rules_verified, true);
  assert.equal(projection.evidence.trusted_scope_chain_verified, true);
  assert.equal(projection.evidence.formal_ledger_reservation_split_verified, true);

  const unsafe = structuredClone(evidence);
  unsafe.generated_resources.provider_key = "forbidden";
  assert.throws(
    () => validateRuntimeEvidence(unsafe),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "forbidden_key",
  );
  const mismatched = budgetPayloadFixture();
  mismatched.statusPayload.statuses.find((item) => item.scope_type === "virtual_key").effective_spend_cny = "2.000000";
  assert.throws(
    () => validateBudgetApiProjection(mismatched.rulesPayload, mismatched.statusPayload, mismatched.runtime),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "split_value",
  );
});


test("PRD-052 layout contract rejects overflow clipping and overlap", () => {
  const viewport = { width: 1280, height: 720 };
  const metrics = {
    viewport_width: 1280,
    viewport_height: 720,
    document_scroll_width: 1280,
    body_scroll_width: 1280,
    root_left: 0,
    root_right: 1280,
    root_client_width: 1280,
    root_scroll_width: 1280,
    clipped_control_count: 0,
    clipped_content_count: 0,
    horizontal_container_overflow_count: 0,
    control_overlap_count: 0,
    region_overlap_count: 0,
  };
  assert.equal(assertLayoutMetrics(metrics, viewport).horizontal_overflow, false);
  assert.throws(
    () => assertLayoutMetrics({ ...metrics, clipped_content_count: 1 }, viewport),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "content_clipped",
  );
  assert.throws(
    () => assertLayoutMetrics({ ...metrics, region_overlap_count: 1 }, viewport),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "region_overlap",
  );
});


test("PRD-052 PNG contract verifies exact dimensions and nonblank pixels", () => {
  const inspected = inspectPngPixels(pngFixture(true), { width: 2, height: 2 });
  assert.equal(inspected.width, 2);
  assert.equal(inspected.height, 2);
  assert.equal(inspected.non_blank_pixels, true);
  assert.match(inspected.sha256, /^[a-f0-9]{64}$/);
  assert.throws(
    () => inspectPngPixels(pngFixture(true), { width: 3, height: 2 }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "png_dimensions",
  );
  assert.throws(
    () => inspectPngPixels(pngFixture(false), { width: 2, height: 2 }),
    (error) => error instanceof BrowserAcceptanceFailure && error.code === "png_blank",
  );
});


test("PRD-052 browser evidence is write-once inside workspace temp", async () => {
  const tempRoot = path.resolve(".tmp");
  await mkdir(tempRoot, { recursive: true });
  const batch = await mkdtemp(path.join(tempRoot, "prd052-browser-contract-"));
  const payload = {
    schema_version: 1,
    status: "passed",
    prd_id: "PRD-052",
    privacy: { safe_projection_verified: true },
  };
  try {
    const target = await publishBrowserEvidence(batch, payload);
    const written = JSON.parse(await readFile(target, "utf8"));
    assert.equal(written.status, "passed");
    await assert.rejects(
      () => publishBrowserEvidence(batch, payload),
      (error) => error instanceof BrowserAcceptanceFailure && error.code === "output_exists_or_unwritable",
    );
  } finally {
    await rm(batch, { recursive: true, force: true });
  }
});
