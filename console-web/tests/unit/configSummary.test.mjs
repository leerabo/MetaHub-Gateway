// 本单测覆盖运行配置摘要读取，确保内部排障信息可见但敏感配置不会泄露到控制台。
import assert from "node:assert/strict";
import test from "node:test";

import { getConfigSummary } from "../../src/lib/configSummary.js";

test("getConfigSummary reads config summary without exposing secrets", async () => {
  globalThis.fetch = async () => ({
    ok: true,
    headers: new Map([["X-Request-ID", "req_config"]]),
    async json() {
      return {
        service_name: "control-plane-api",
        environment: "dev",
        items: [
          {
            key: "METAHUB_PROVIDER_DEEPSEEK_API_KEY",
            source: "env",
            status: "masked",
            value_preview: "***MASKED***",
          },
        ],
        warnings: [],
        request_id: "req_config",
      };
    },
  });

  const summary = await getConfigSummary({ controlApiUrl: "http://control.local" });

  assert.equal(summary.state, "ok");
  assert.equal(summary.items[0].value_preview, "***MASKED***");
  assert.equal(JSON.stringify(summary).includes("sk-secret"), false);
});

test("getConfigSummary degrades when endpoint is unavailable", async () => {
  globalThis.fetch = async () => {
    // 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
    throw new Error("network down");
  };

  const summary = await getConfigSummary({ controlApiUrl: "http://control.local" });

  assert.equal(summary.state, "error");
  assert.match(summary.message, /无法连接/);
});
