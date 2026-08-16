/** PRD-082 MCP API client contract tests. */

import assert from "node:assert/strict";
import test from "node:test";

import {
  createMcpIdempotencyKey,
  listMcpInvocations,
  listMcpTools,
  normalizeMcpToolPolicy,
  registerMcpServer,
  updateMcpServerStatus,
} from "../../src/lib/mcpTools.js";

const SERVER_ID = `mcps_${"a".repeat(32)}`;

function response(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    async json() { return payload; },
  };
}

test("registration sends the closed body and no caller-controlled request identifiers", async () => {
  const previousFetch = globalThis.fetch;
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return response({
      server: {
        mcp_server_id: SERVER_ID,
        state_revision: 1,
      },
      request_id: "req_mcp_register",
      trace_id: "trace_mcp_register",
    }, 202);
  };
  try {
    const idempotencyKey = createMcpIdempotencyKey("register");
    await registerMcpServer({
      controlApiUrl: "http://control.test",
      token: "session-token-canary",
      name: "Operations",
      endpointUrl: "https://mcp.example.test/v1",
      authType: "bearer",
      bearerToken: "bearer-secret-canary",
      idempotencyKey,
    });
    assert.equal(captured.url, "http://control.test/mcp/servers");
    assert.equal(captured.options.method, "POST");
    assert.equal(captured.options.cache, "no-store");
    assert.equal(captured.options.credentials, "omit");
    assert.equal(captured.options.redirect, "error");
    assert.equal(captured.options.headers["Idempotency-Key"], idempotencyKey);
    assert.equal("X-Request-ID" in captured.options.headers, false);
    assert.deepEqual(JSON.parse(captured.options.body), {
      name: "Operations",
      endpoint_url: "https://mcp.example.test/v1",
      auth: { type: "bearer", bearer_token: "bearer-secret-canary" },
    });
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("Server status update sends only revision and status with the caller-owned Abort signal", async () => {
  const previousFetch = globalThis.fetch;
  let captured;
  const controller = new AbortController();
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return response({ server: { mcp_server_id: SERVER_ID, status: "disabled" } }, 202);
  };
  try {
    await updateMcpServerStatus({
      controlApiUrl: "http://control.test",
      token: "session-token-canary",
      serverId: SERVER_ID,
      expectedRevision: 7,
      status: "disabled",
      idempotencyKey: "mcp_disable_contract",
      signal: controller.signal,
    });
    assert.equal(captured.url, `http://control.test/mcp/servers/${SERVER_ID}`);
    assert.equal(captured.options.method, "PATCH");
    assert.equal(captured.options.cache, "no-store");
    assert.equal(captured.options.credentials, "omit");
    assert.equal(captured.options.redirect, "error");
    assert.equal(captured.options.signal, controller.signal);
    assert.equal(captured.options.headers["Idempotency-Key"], "mcp_disable_contract");
    assert.equal("X-Request-ID" in captured.options.headers, false);
    assert.deepEqual(JSON.parse(captured.options.body), {
      expected_revision: 7,
      status: "disabled",
    });
    await assert.rejects(updateMcpServerStatus({
      controlApiUrl: "http://control.test",
      token: "session-token-canary",
      serverId: SERVER_ID,
      expectedRevision: 7,
      status: "pending_sync",
      idempotencyKey: "mcp_invalid_status",
    }));
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("response body cancellation remains AbortError for the page owner", async () => {
  const previousFetch = globalThis.fetch;
  const abortError = new Error("response body cancelled");
  abortError.name = "AbortError";
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    headers: { get: () => null },
    async json() { throw abortError; },
  });
  try {
    await assert.rejects(
      listMcpTools({
        controlApiUrl: "http://control.test",
        token: "session-token-canary",
      }),
      (error) => error === abortError,
    );
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("invocation status filter uses only the backend-declared query shape", async () => {
  const previousFetch = globalThis.fetch;
  let capturedUrl = "";
  globalThis.fetch = async (url) => {
    capturedUrl = url;
    return response({ invocations: [], next_cursor: null, request_id: "req_inv", trace_id: "trace_inv" });
  };
  try {
    await listMcpInvocations({
      controlApiUrl: "http://control.test",
      token: "session-token-canary",
      status: "outcome_unknown",
    });
    assert.match(capturedUrl, /status=outcome_unknown/);
    assert.doesNotMatch(capturedUrl, /tenant|argument|result|token/i);
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("tool list query never accepts endpoint, token or arbitrary fields", async () => {
  const previousFetch = globalThis.fetch;
  let capturedUrl = "";
  globalThis.fetch = async (url) => {
    capturedUrl = url;
    return response({ tools: [], next_cursor: null, request_id: "req_list", trace_id: "trace_list" });
  };
  try {
    await listMcpTools({
      controlApiUrl: "http://control.test",
      token: "session-token-canary",
      serverId: SERVER_ID,
      status: "review_required",
      riskLevel: "high",
      approvalRequired: true,
      limit: 25,
    });
    assert.match(capturedUrl, /server_id=mcps_/);
    assert.match(capturedUrl, /status=review_required/);
    assert.match(capturedUrl, /risk=high/);
    assert.match(capturedUrl, /approval_required=true/);
    assert.doesNotMatch(capturedUrl, /token|endpoint|session-token-canary/i);
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("policy normalization freezes grants and enforces high-risk approval", () => {
  const policy = normalizeMcpToolPolicy({
    expected_policy_revision: 3,
    status: "active",
    risk_level: "high",
    approval_required: true,
    approval_expires_seconds: 900,
    required_permission_codes: ["project.read"],
    allowed_role_ids: ["role_ops"],
    allowed_user_ids: ["usr_operator"],
    result_redaction_pointers: ["/credentials/token"],
  });
  assert.deepEqual(policy, {
    expected_policy_revision: 3,
    status: "active",
    risk_level: "high",
    approval_required: true,
    approval_expires_seconds: 900,
    required_permission_codes: ["project.read"],
    allowed_role_ids: ["role_ops"],
    allowed_user_ids: ["usr_operator"],
    result_redaction_pointers: ["/credentials/token"],
  });
  assert.throws(() => normalizeMcpToolPolicy({
    ...policy,
    approval_required: false,
  }), /MCP 请求不合法/);
});
