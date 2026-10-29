// PRD-101 Console 合同覆盖严格 DTO、Cookie/CSRF/CAS 写入和权限裁剪，测试值均为公开夹具。
import assert from "node:assert/strict";
import test from "node:test";

import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  getPlatformGatewayCells,
  updatePlatformGatewayPlacement,
} from "../../src/lib/platformGateway.js";

// 固定公开夹具只验证前端合同，不能被当作真实 Workspace 或控制面凭据复用。
const CONTROL_URL = "http://control.test";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;

test("Gateway client validates safe Cell projections and rejects sensitive extras", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => jsonResponse(cellPayload());
  try {
    assert.equal((await getPlatformGatewayCells({ controlApiUrl: CONTROL_URL })).cells.length, 2);
    globalThis.fetch = async () => jsonResponse({
      ...cellPayload(),
      cells: [{ ...cellPayload().cells[0], internal_address: "private" }],
    });
    await assert.rejects(
      getPlatformGatewayCells({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "platform_gateway.response_invalid",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("placement update binds cookies CSRF CAS and idempotency without bearer", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const calls = [];
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${"c".repeat(43)}` };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(placementPayload());
  };
  try {
    await updatePlatformGatewayPlacement({
      controlApiUrl: CONTROL_URL,
      workspaceId: WORKSPACE_ID,
      revision: 0,
      placement: {
        primary_region_code: "cn-north-1",
        primary_cell_code: "cn-north-1-a",
        failover_policy: "deny",
        failover_cell_code: null,
        residency_policy_version: "cn-residency-v1",
        reason_code: "initial_placement",
        confirmed: true,
      },
      idempotencyKey: "prd101-placement-client-0001",
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }
  assert.equal(calls[0].options.credentials, "include");
  assert.equal(calls[0].options.headers["If-Match"], '"0"');
  assert.equal(calls[0].options.headers["Idempotency-Key"], "prd101-placement-client-0001");
  assert.equal("Authorization" in calls[0].options.headers, false);
});

test("Gateway renderer prunes management and never renders signatures or providers", () => {
  const root = { innerHTML: "" };
  const state = platformState(["platform.gateway.read", "platform.gateway.manage"]);
  state.gatewayCells.cells[0].signature = "must-not-render";
  state.gatewayCells.cells[0].provider = "must-not-render-provider";
  renderPlatformWorkspace(root, state);
  assert.match(root.innerHTML, /data-platform-gateway-placement-form/u);
  assert.match(root.innerHTML, /name="residencyPolicyVersion" value=""/u);
  assert.doesNotMatch(
    root.innerHTML,
    /must-not-render|provider.test|If-Match \+ 幂等写入|不展示内部地址与 Provider/u,
  );

  const readonly = { innerHTML: "" };
  renderPlatformWorkspace(readonly, platformState(["platform.gateway.read"]));
  assert.match(readonly.innerHTML, /服务状态/u);
  assert.doesNotMatch(readonly.innerHTML, /data-platform-gateway-placement-form/u);
});

// 构造只含公开权限与安全投影的页面状态，禁止测试辅助对象引入签名或 Provider 字段。
function platformState(permissions) {
  return {
    session: { context: { permissions, operator: { display_name: "Ops", roles: ["platform_ops"] } } },
    navigation: [{ id: "gateway", label: "Gateway", href: "/platform.html#gateway" }],
    view: "gateway",
    gatewayCells: cellPayload(),
    gatewayPlacement: placementPayload(),
    loading: false,
  };
}

// 生成两个公开 Cell 摘要，链路标识仅用于验证严格响应字段集合。
function cellPayload() {
  return {
    cells: [cell("cn-north-1-a", "cn-north-1"), cell("cn-east-1-a", "cn-east-1")],
    request_id: "req-gateway-cells",
    trace_id: "trc-gateway-cells",
  };
}

// 单个 Cell 夹具保持枚举和数字水位合法，内部地址始终不进入渲染输入。
function cell(code, region) {
  return {
    cell_code: code,
    region_code: region,
    capacity_class: "standard",
    capacity_band: "available",
    status: "active",
    health_status: "healthy",
    release_watermark: 1,
    config_watermark: 2,
    revision: 1,
    updated_at: "2026-08-23T00:00:00Z",
  };
}

// Placement 响应夹具只保留 Workspace、区域、CAS 与 generation 的公开投影。
function placementPayload() {
  return {
    placement: {
      workspace_id: WORKSPACE_ID,
      primary_region_code: "cn-north-1",
      primary_cell_code: "cn-north-1-a",
      failover_policy: "deny",
      failover_cell_code: null,
      residency_policy_version: "cn-residency-v1",
      revision: 1,
      snapshot_generation: 1,
      updated_at: "2026-08-23T00:00:00Z",
    },
    idempotent_replay: false,
    request_id: "req-gateway-placement",
    trace_id: "trc-gateway-placement",
  };
}

// 模拟最小 Fetch JSON 响应，不附加 Cookie、Header 或其他认证材料。
function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return structuredClone(payload); },
  };
}
