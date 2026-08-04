// PRD-047 Browser 验收加固测试：只使用临时 SQLite 与纯 Buffer，不启动服务或 Chrome。
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { DatabaseSync } from "node:sqlite";
import { deflateSync } from "node:zlib";

import {
  assertChromeAvailable,
  assertSensitiveSurfaceSnapshot,
  cleanupState,
  inspectPngPixels,
  installAllowedOriginGuard,
  registerSession,
  registerTemporaryUser,
  registerUiToken,
  runOwnedStateLifecycle,
  validateRuntimeState,
} from "../../../scripts/acceptance/prd047-browser-acceptance.mjs";

// 使用标准 PNG 文件签名构造最小截图样本，确保像素验收走真实解码路径。
const PNG_SIGNATURE = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);

/** 建立只包含 Browser owner/cleanup 合同所需列的隔离数据库。 */
async function createFixture() {
  const root = await mkdtemp(path.join(tmpdir(), "metahub-prd047-browser-"));
  const databasePath = path.join(root, "acceptance.db");
  const database = new DatabaseSync(databasePath);
  database.exec(`
    CREATE TABLE tenants(
      tenant_id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
      status TEXT NOT NULL, updated_at TEXT
    );
    CREATE TABLE users(
      user_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, email TEXT NOT NULL,
      role TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT
    );
    CREATE TABLE sessions(
      session_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
      status TEXT NOT NULL, revoked_at TEXT, updated_at TEXT
    );
    CREATE TABLE environments(
      environment_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, code TEXT NOT NULL,
      status TEXT NOT NULL, updated_at TEXT
    );
    CREATE TABLE providers(
      provider_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, provider_code TEXT NOT NULL,
      name TEXT NOT NULL, base_url TEXT NOT NULL, status TEXT NOT NULL,
      disabled_at TEXT, updated_at TEXT
    );
    CREATE TABLE models(
      model_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, provider_id TEXT NOT NULL,
      model_alias TEXT NOT NULL, upstream_model_name TEXT NOT NULL, display_name TEXT NOT NULL,
      status TEXT NOT NULL, disabled_at TEXT, updated_at TEXT
    );
    CREATE TABLE model_aliases(
      model_alias_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, alias TEXT NOT NULL,
      environment TEXT NOT NULL, environment_id TEXT NOT NULL, default_model_id TEXT NOT NULL,
      fallback_model_id TEXT, description TEXT, status TEXT NOT NULL,
      disabled_at TEXT, updated_at TEXT
    );
    CREATE TABLE model_alias_routing_policies(
      routing_policy_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, model_alias_id TEXT NOT NULL,
      routing_mode TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
      updated_at TEXT
    );
    CREATE TABLE virtual_keys(
      virtual_key_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, environment_id TEXT NOT NULL,
      name TEXT NOT NULL, description TEXT, status TEXT NOT NULL,
      disabled_at TEXT, updated_at TEXT
    );
    CREATE TABLE budget_rules(
      budget_rule_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, environment_id TEXT NOT NULL,
      scope_type TEXT NOT NULL, scope_id TEXT NOT NULL, name TEXT NOT NULL,
      status TEXT NOT NULL, updated_at TEXT
    );
    CREATE TABLE webhook_endpoints(
      webhook_endpoint_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL,
      endpoint_url TEXT NOT NULL, event_types_json TEXT NOT NULL, status TEXT NOT NULL,
      updated_at TEXT
    );
    CREATE TRIGGER alias_policy_version
    AFTER UPDATE OF status ON model_aliases
    FOR EACH ROW WHEN NEW.status IS NOT OLD.status
    BEGIN
      UPDATE model_alias_routing_policies
      SET version=version+1, updated_at=NEW.updated_at
      WHERE tenant_id=NEW.tenant_id AND model_alias_id=NEW.model_alias_id;
    END;
  `);

  const suffix = "testrun01";
  database.prepare(
    "INSERT INTO tenants(tenant_id,slug,name,status) VALUES(?,?,?,?)",
  ).run("tenant_main", "default", "Default", "active");
  database.prepare(
    "INSERT INTO tenants(tenant_id,slug,name,status) VALUES(?,?,?,?)",
  ).run(`tenant_prd047_${suffix}`, `prd047-other-${suffix}`, `PRD047 other ${suffix}`, "active");
  for (const row of [
    ["user_admin", "tenant_main", "admin@metahub.local", "admin"],
    ["user_developer", "tenant_main", `prd047-developer-${suffix}@metahub.local`, "developer"],
    ["user_viewer", "tenant_main", `prd047-viewer-${suffix}@metahub.local`, "viewer"],
    [`user_prd047_other_${suffix}`, `tenant_prd047_${suffix}`, "admin@prd047-other.local", "admin"],
  ]) {
    database.prepare(
      "INSERT INTO users(user_id,tenant_id,email,role,status) VALUES(?,?,?,?, 'active')",
    ).run(...row);
  }
  for (const row of [
    ["session_admin", "tenant_main", "user_admin"],
    ["session_developer", "tenant_main", "user_developer"],
    ["session_viewer", "tenant_main", "user_viewer"],
    ["session_other_admin", `tenant_prd047_${suffix}`, `user_prd047_other_${suffix}`],
  ]) {
    database.prepare(
      "INSERT INTO sessions(session_id,tenant_id,user_id,status) VALUES(?,?,?, 'active')",
    ).run(...row);
  }
  database.prepare(
    "INSERT INTO environments(environment_id,tenant_id,code,status) VALUES(?,?,?, 'active')",
  ).run("env_main", "tenant_main", "dev");
  database.prepare(
    "INSERT INTO environments(environment_id,tenant_id,code,status) VALUES(?,?,?, 'active')",
  ).run("env_other", `tenant_prd047_${suffix}`, "dev");

  const providerRows = [
    ["provider_main_a", "tenant_main", `prd047_default_a_${suffix}`, "PRD047 default Provider A", "http://127.0.0.1:61234/upstream-a/v1"],
    ["provider_main_b", "tenant_main", `prd047_default_b_${suffix}`, "PRD047 default Provider B", "http://127.0.0.1:61234/upstream-b/v1"],
    ["provider_other_a", `tenant_prd047_${suffix}`, `prd047_other_a_${suffix}`, "PRD047 other Provider A", "http://127.0.0.1:61234/upstream-a/v1"],
    ["provider_other_b", `tenant_prd047_${suffix}`, `prd047_other_b_${suffix}`, "PRD047 other Provider B", "http://127.0.0.1:61234/upstream-b/v1"],
    ["provider_baseline", "tenant_main", "baseline", "Baseline", "https://example.invalid/v1"],
  ];
  for (const row of providerRows) {
    database.prepare(
      "INSERT INTO providers(provider_id,tenant_id,provider_code,name,base_url,status) "
        + "VALUES(?,?,?,?,?, 'active')",
    ).run(...row);
  }
  for (const row of [
    ["model_main_a", "tenant_main", "provider_main_a", `prd047-default-a-${suffix}`, "prd047-a", "PRD047 default Model A"],
    ["model_main_b", "tenant_main", "provider_main_b", `prd047-default-b-${suffix}`, "prd047-b", "PRD047 default Model B"],
    ["model_other_a", `tenant_prd047_${suffix}`, "provider_other_a", `prd047-other-a-${suffix}`, "prd047-a", "PRD047 other Model A"],
    ["model_other_b", `tenant_prd047_${suffix}`, "provider_other_b", `prd047-other-b-${suffix}`, "prd047-b", "PRD047 other Model B"],
  ]) {
    database.prepare(
      "INSERT INTO models(model_id,tenant_id,provider_id,model_alias,upstream_model_name,display_name,status) "
        + "VALUES(?,?,?,?,?,?, 'active')",
    ).run(...row);
  }
  database.prepare(
    "INSERT INTO model_aliases(model_alias_id,tenant_id,alias,environment,environment_id,"+
      "default_model_id,fallback_model_id,description,status) VALUES(?,?,?,?,?,?,?,?, 'active')",
  ).run(
    "alias_main", "tenant_main", `prd047-default-${suffix}`, "dev", "env_main",
    "model_main_a", "model_main_b", "PRD-047 runtime acceptance",
  );
  database.prepare(
    "INSERT INTO model_aliases(model_alias_id,tenant_id,alias,environment,environment_id,"+
      "default_model_id,fallback_model_id,description,status) VALUES(?,?,?,?,?,?,?,?, 'active')",
  ).run(
    "alias_other", `tenant_prd047_${suffix}`, `prd047-other-${suffix}`, "dev", "env_other",
    "model_other_a", "model_other_b", "PRD-047 runtime acceptance",
  );
  database.prepare(
    "INSERT INTO model_alias_routing_policies(routing_policy_id,tenant_id,model_alias_id,"+
      "routing_mode,status,version) VALUES(?,?,?,?, 'enabled',?)",
  ).run("rpol_main", "tenant_main", "alias_main", "availability_first", 3);
  database.prepare(
    "INSERT INTO model_alias_routing_policies(routing_policy_id,tenant_id,model_alias_id,"+
      "routing_mode,status,version) VALUES(?,?,?,?, 'enabled',?)",
  ).run("rpol_other", `tenant_prd047_${suffix}`, "alias_other", "fixed", 1);
  database.prepare(
    "INSERT INTO virtual_keys(virtual_key_id,tenant_id,environment_id,name,description,status) "
      + "VALUES(?,?,?,?,?, 'active')",
  ).run("virtual_key_main", "tenant_main", "env_main", `PRD047 runtime ${suffix}`, "PRD-047 isolated acceptance");
  for (const row of [
    ["budget_rule_main", "tenant_main", "env_main", "virtual_key", "virtual_key_main", `PRD047 runtime budget ${suffix}`, "disabled"],
    ["budget_rule_baseline", "tenant_main", "env_main", "tenant", "tenant_main", "Baseline budget", "active"],
    ["budget_rule_other", `tenant_prd047_${suffix}`, "env_other", "tenant", `tenant_prd047_${suffix}`, "Other budget", "disabled"],
  ]) {
    database.prepare(
      "INSERT INTO budget_rules(budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,status) "
        + "VALUES(?,?,?,?,?,?,?)",
    ).run(...row);
  }
  database.prepare(
    "INSERT INTO webhook_endpoints(webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,status) "
      + "VALUES(?,?,?,?,?, 'disabled')",
  ).run(
    "webhook_main", "tenant_main", `PRD047 runtime ${suffix}`,
    "http://127.0.0.1:61234/hook", '["route.policy_selected"]',
  );
  database.close();

  const runtime = {
    status: "passed",
    suffix,
    availability_first: { version: 3 },
    resource_ids: {
      admin_session_id: "session_admin",
      developer_session_id: "session_developer",
      viewer_session_id: "session_viewer",
      other_admin_session_id: "session_other_admin",
      alias_id: "alias_main",
      routing_policy_id: "rpol_main",
      model_a_id: "model_main_a",
      model_b_id: "model_main_b",
      provider_a_id: "provider_main_a",
      provider_b_id: "provider_main_b",
      virtual_key_id: "virtual_key_main",
      budget_rule_id: "budget_rule_main",
      webhook_endpoint_id: "webhook_main",
      developer_user_id: "user_developer",
      viewer_user_id: "user_viewer",
      other_tenant_id: `tenant_prd047_${suffix}`,
      other_admin_user_id: `user_prd047_other_${suffix}`,
      other_environment_id: "env_other",
      other_alias_id: "alias_other",
      other_routing_policy_id: "rpol_other",
      other_model_a_id: "model_other_a",
      other_model_b_id: "model_other_b",
      other_provider_a_id: "provider_other_a",
      other_provider_b_id: "provider_other_b",
    },
  };
  return { root, databasePath, runtime };
}

/** 删除由 mkdtemp 创建的唯一测试目录，避免 Windows SQLite 句柄残留。 */
async function removeFixture(fixture) {
  assert.ok(path.basename(fixture.root).startsWith("metahub-prd047-browser-"));
  await rm(fixture.root, { recursive: true, force: true });
}

/** 构造无需 CRC 校验的最小 PNG；被测解析器仍完整验证 chunk 和像素 filter。 */
function pngChunk(type, data) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  return Buffer.concat([length, Buffer.from(type, "ascii"), data, Buffer.alloc(4)]);
}

/** 按 RGB 行构造 8-bit 非交错 PNG，供像素非空断言的正反例使用。 */
function rgbPng(width, height, pixels) {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 2;
  const rows = [];
  for (let row = 0; row < height; row += 1) {
    rows.push(Buffer.from([0]));
    rows.push(Buffer.from(pixels.slice(row * width * 3, (row + 1) * width * 3)));
  }
  return Buffer.concat([
    PNG_SIGNATURE,
    pngChunk("IHDR", header),
    pngChunk("IDAT", deflateSync(Buffer.concat(rows))),
    pngChunk("IEND", Buffer.alloc(0)),
  ]);
}

test("runtime owner graph validates exact names and cleanup reaches tenant-qualified terminal state", async () => {
  const fixture = await createFixture();
  try {
    const state = validateRuntimeState(fixture.runtime, {
      databasePath: fixture.databasePath,
      tenantSlug: "default",
    });
    const cleanup = cleanupState(state, {
      databasePath: fixture.databasePath,
      tenantSlug: "default",
    });
    assert.deepEqual(
      {
        sessions: cleanup.revokedSessionCount,
        users: cleanup.disabledTemporaryUserCount,
        aliases: cleanup.disabledAliasCount,
        models: cleanup.disabledModelCount,
        providers: cleanup.disabledProviderCount,
        virtualKeys: cleanup.disabledVirtualKeyCount,
        webhooks: cleanup.disabledWebhookCount,
        environments: cleanup.disabledEnvironmentCount,
        tenants: cleanup.disabledSecondTenantCount,
      },
      { sessions: 4, users: 3, aliases: 2, models: 4, providers: 4, virtualKeys: 1, webhooks: 0, environments: 1, tenants: 1 },
    );
    assert.equal(cleanup.activeResidueCount, 0);
    assert.equal(cleanup.namedActiveResidueCount, 0);
    assert.equal(cleanup.retainedPolicyCount, 2);
    assert.equal(cleanup.callablePolicyCount, 0);
    assert.equal(cleanup.terminalStateVerified, true);

    const database = new DatabaseSync(fixture.databasePath, { readOnly: true });
    try {
      assert.equal(database.prepare("SELECT status FROM tenants WHERE tenant_id='tenant_main'").get().status, "active");
      assert.equal(database.prepare("SELECT status FROM users WHERE user_id='user_admin'").get().status, "active");
      assert.equal(database.prepare("SELECT status FROM providers WHERE provider_id='provider_baseline'").get().status, "active");
      assert.equal(database.prepare("SELECT status FROM budget_rules WHERE budget_rule_id='budget_rule_main'").get().status, "disabled");
      assert.equal(database.prepare("SELECT status FROM budget_rules WHERE budget_rule_id='budget_rule_baseline'").get().status, "active");
      assert.equal(
        database.prepare(
          "SELECT COUNT(*) AS count FROM model_alias_routing_policies rp JOIN model_aliases a "
            + "ON a.tenant_id=rp.tenant_id AND a.model_alias_id=rp.model_alias_id "
            + "WHERE rp.status='enabled' AND a.status='active'",
        ).get().count,
        0,
      );
    } finally {
      database.close();
    }
  } finally {
    await removeFixture(fixture);
  }
});

test("runtime owner graph rejects a missing Budget resource ID", async () => {
  const fixture = await createFixture();
  try {
    delete fixture.runtime.resource_ids.budget_rule_id;
    assert.throws(
      () => validateRuntimeState(fixture.runtime, {
        databasePath: fixture.databasePath,
        tenantSlug: "default",
      }),
      /Budget|budget_rule_id|标识符/,
    );
  } finally {
    await removeFixture(fixture);
  }
});

test("runtime owner graph rejects baseline, missing, and cross-tenant Budget IDs", async () => {
  for (const budgetRuleId of ["budget_rule_baseline", "budget_rule_missing", "budget_rule_other"]) {
    const fixture = await createFixture();
    try {
      fixture.runtime.resource_ids.budget_rule_id = budgetRuleId;
      assert.throws(
        () => validateRuntimeState(fixture.runtime, {
          databasePath: fixture.databasePath,
          tenantSlug: "default",
        }),
        /Budget|owner 图不唯一/,
      );
      const database = new DatabaseSync(fixture.databasePath, { readOnly: true });
      try {
        assert.equal(
          database.prepare("SELECT status FROM budget_rules WHERE budget_rule_id='budget_rule_baseline'").get().status,
          "active",
        );
      } finally {
        database.close();
      }
    } finally {
      await removeFixture(fixture);
    }
  }
});

test("runtime owner graph rejects an active Budget before Browser validation", async () => {
  const fixture = await createFixture();
  try {
    const database = new DatabaseSync(fixture.databasePath);
    database.prepare(
      "UPDATE budget_rules SET status='active' WHERE budget_rule_id='budget_rule_main'",
    ).run();
    database.close();
    assert.throws(
      () => validateRuntimeState(fixture.runtime, {
        databasePath: fixture.databasePath,
        tenantSlug: "default",
      }),
      /Budget|owner 图不唯一/,
    );
  } finally {
    await removeFixture(fixture);
  }
});

test("cleanup revalidates disabled Budget under write lock and preserves other resources on drift", async () => {
  const fixture = await createFixture();
  try {
    const state = validateRuntimeState(fixture.runtime, {
      databasePath: fixture.databasePath,
      tenantSlug: "default",
    });
    const database = new DatabaseSync(fixture.databasePath);
    database.prepare(
      "UPDATE budget_rules SET status='active' WHERE budget_rule_id='budget_rule_main'",
    ).run();
    database.close();

    assert.throws(
      () => cleanupState(state, { databasePath: fixture.databasePath, tenantSlug: "default" }),
      /Budget|owner 图不唯一/,
    );
    const after = new DatabaseSync(fixture.databasePath, { readOnly: true });
    try {
      assert.equal(after.prepare("SELECT status FROM model_aliases WHERE model_alias_id='alias_main'").get().status, "active");
      assert.equal(after.prepare("SELECT status FROM providers WHERE provider_id='provider_main_a'").get().status, "active");
      assert.equal(after.prepare("SELECT status FROM budget_rules WHERE budget_rule_id='budget_rule_main'").get().status, "active");
      assert.equal(after.prepare("SELECT status FROM budget_rules WHERE budget_rule_id='budget_rule_baseline'").get().status, "active");
    } finally {
      after.close();
    }
  } finally {
    await removeFixture(fixture);
  }
});

test("tampered cross-tenant runtime IDs fail before cleanup and preserve baseline resources", async () => {
  for (const mutation of [
    (runtime) => { runtime.resource_ids.other_tenant_id = "tenant_main"; },
    (runtime) => { runtime.resource_ids.other_provider_b_id = "provider_baseline"; },
  ]) {
    const fixture = await createFixture();
    try {
      mutation(fixture.runtime);
      assert.throws(
        () => validateRuntimeState(fixture.runtime, {
          databasePath: fixture.databasePath,
          tenantSlug: "default",
        }),
        /owner|命名|资源 ID/,
      );
      const database = new DatabaseSync(fixture.databasePath, { readOnly: true });
      try {
        assert.equal(database.prepare("SELECT status FROM tenants WHERE tenant_id='tenant_main'").get().status, "active");
        assert.equal(database.prepare("SELECT status FROM providers WHERE provider_id='provider_baseline'").get().status, "active");
      } finally {
        database.close();
      }
    } finally {
      await removeFixture(fixture);
    }
  }
});

test("cleanup revalidates graph under write lock and rolls back when state changes after validation", async () => {
  const fixture = await createFixture();
  try {
    const state = validateRuntimeState(fixture.runtime, {
      databasePath: fixture.databasePath,
      tenantSlug: "default",
    });
    state.runtime.resource_ids.other_provider_b_id = "provider_baseline";
    assert.throws(
      () => cleanupState(state, { databasePath: fixture.databasePath, tenantSlug: "default" }),
      /owner|命名/,
    );
    const database = new DatabaseSync(fixture.databasePath, { readOnly: true });
    try {
      assert.equal(database.prepare("SELECT status FROM model_aliases WHERE model_alias_id='alias_main'").get().status, "active");
      assert.equal(database.prepare("SELECT status FROM providers WHERE provider_id='provider_baseline'").get().status, "active");
    } finally {
      database.close();
    }
  } finally {
    await removeFixture(fixture);
  }
});

test("cleanup refuses missing owner facts instead of accepting zero-row updates", async () => {
  const fixture = await createFixture();
  try {
    const state = validateRuntimeState(fixture.runtime, {
      databasePath: fixture.databasePath,
      tenantSlug: "default",
    });
    const database = new DatabaseSync(fixture.databasePath);
    database.prepare("DELETE FROM providers WHERE provider_id='provider_other_b'").run();
    database.close();
    assert.throws(
      () => cleanupState(state, { databasePath: fixture.databasePath, tenantSlug: "default" }),
      /owner 图不唯一/,
    );
  } finally {
    await removeFixture(fixture);
  }
});

test("Chrome prerequisite failure after owner state creation still performs exact cleanup", async () => {
  const fixture = await createFixture();
  let cleanupCallCount = 0;
  try {
    await assert.rejects(
      () => runOwnedStateLifecycle({
        createStateAction: async () => validateRuntimeState(fixture.runtime, {
          databasePath: fixture.databasePath,
          tenantSlug: "default",
        }),
        executeAction: async (state) => {
          assert.equal(state.suffix, fixture.runtime.suffix, "Browser 与 Runtime 必须共用清理 suffix");
          assertChromeAvailable(path.join(fixture.root, "missing-chrome.exe"));
        },
        cleanupAction: (state) => {
          cleanupCallCount += 1;
          return cleanupState(state, {
            databasePath: fixture.databasePath,
            tenantSlug: "default",
          });
        },
      }),
      /Chrome 可执行文件不存在/,
    );
    assert.equal(cleanupCallCount, 1);
    const database = new DatabaseSync(fixture.databasePath, { readOnly: true });
    try {
      assert.equal(
        database.prepare("SELECT COUNT(*) AS count FROM sessions WHERE status='active'").get().count,
        0,
      );
      assert.equal(
        database.prepare(
          "SELECT COUNT(*) AS count FROM users WHERE status='active' AND user_id!='user_admin'",
        ).get().count,
        0,
      );
      assert.equal(
        database.prepare("SELECT COUNT(*) AS count FROM model_aliases WHERE status='active'").get().count,
        0,
      );
      assert.equal(
        database.prepare(
          "SELECT COUNT(*) AS count FROM providers WHERE status='active' AND provider_id!='provider_baseline'",
        ).get().count,
        0,
      );
      assert.equal(database.prepare("SELECT status FROM users WHERE user_id='user_admin'").get().status, "active");
      assert.equal(
        database.prepare("SELECT status FROM providers WHERE provider_id='provider_baseline'").get().status,
        "active",
      );
    } finally {
      database.close();
    }
  } finally {
    await removeFixture(fixture);
  }
});

test("browser network guard allows Control/Console and only Gateway status reads", async () => {
  let routeHandler = null;
  const context = {
    route: async (pattern, handler) => {
      assert.equal(pattern, "**/*");
      routeHandler = handler;
    },
  };
  const evidence = await installAllowedOriginGuard(context, [
    "http://127.0.0.1:8001",
    "http://127.0.0.1:5173",
  ], "http://127.0.0.1:8000");
  assert.equal(typeof routeHandler, "function");

  const decisions = [];
  /** 把固定 URL 交给已安装的 route handler，并记录继续或阻断决定。 */
  const dispatch = async (url, method = "GET", headers = {}) => routeHandler({
    request: () => ({ url: () => url, method: () => method, allHeaders: async () => headers }),
    continue: async () => decisions.push("continue"),
    abort: async (reason) => decisions.push(`abort:${reason}`),
  });
  await dispatch("http://127.0.0.1:8001/auth/login");
  await dispatch("http://127.0.0.1:5173/assets/app.js");
  for (const statusPath of ["/health", "/health/live", "/health/ready", "/version"]) {
    await dispatch(`http://127.0.0.1:8000${statusPath}`, "GET", {
      origin: "http://127.0.0.1:5173",
    });
  }
  await dispatch("http://127.0.0.1:8000/health/live", "OPTIONS", {
    origin: "http://127.0.0.1:5173",
    "access-control-request-method": "GET",
    "access-control-request-headers": "x-request-id",
  });
  await dispatch("http://127.0.0.1:8000/v1/models", "GET", {
    origin: "http://127.0.0.1:5173",
  });
  await dispatch("http://127.0.0.1:8000/health/ready", "POST", {
    origin: "http://127.0.0.1:5173",
  });
  await dispatch("http://127.0.0.1:8000/version?verbose=1", "GET", {
    origin: "http://127.0.0.1:5173",
  });
  await dispatch("http://127.0.0.1:8000/health/live", "OPTIONS", {
    origin: "http://127.0.0.1:5173",
    "access-control-request-method": "GET",
    "access-control-request-headers": "authorization,x-request-id",
  });
  await dispatch("http://127.0.0.1:8000/health", "GET", {
    origin: "http://127.0.0.1:5174",
  });
  for (const [name, value] of [
    ["authorization", "Bearer test-only-value"],
    ["authorization", ""],
    ["cookie", "session=test-only-value"],
    ["x-internal-token", "test-only-value"],
  ]) {
    await dispatch("http://127.0.0.1:8000/health", "GET", {
      origin: "http://127.0.0.1:5173",
      [name]: value,
    });
  }
  await dispatch("http://127.0.0.1:61235/collect");

  assert.deepEqual(decisions, [
    "continue", "continue",
    "continue", "continue", "continue", "continue", "continue",
    "abort:blockedbyclient", "abort:blockedbyclient", "abort:blockedbyclient",
    "abort:blockedbyclient", "abort:blockedbyclient", "abort:blockedbyclient",
    "abort:blockedbyclient", "abort:blockedbyclient", "abort:blockedbyclient",
    "abort:blockedbyclient",
  ]);
  assert.deepEqual(evidence, {
    requestCount: 17,
    blockedRequestCount: 10,
    violations: [
      "gateway_path",
      "gateway_method_or_headers",
      "gateway_query",
      "gateway_method_or_headers",
      "gateway_method_or_headers",
      "gateway_method_or_headers",
      "gateway_method_or_headers",
      "gateway_method_or_headers",
      "gateway_method_or_headers",
      "origin",
    ],
  });
});

test("User and Session IDs enter pending cleanup ledger before owner assertions", () => {
  const state = {
    userOwners: new Map(),
    userIds: new Set(),
    sessionOwners: new Map(),
    sessionIds: new Set(),
  };
  const expectedUser = {
    tenantId: "tenant_main",
    email: "prd047-browser-developer-testrun01@metahub.local",
    role: "developer",
  };
  assert.throws(
    () => registerTemporaryUser(state, {
      user_id: "user_pending",
      tenant_id: "tenant_other",
      email: expectedUser.email,
      role: expectedUser.role,
    }, expectedUser),
    /owner/,
  );
  assert.deepEqual(state.userOwners.get("user_pending"), expectedUser);
  assert.equal(state.userIds.has("user_pending"), true);

  const expectedSession = { ...expectedUser, userId: "user_pending" };
  assert.throws(
    () => registerSession(state, {
      session_id: "session_pending",
      user: {
        user_id: "user_other",
        tenant_id: expectedSession.tenantId,
        email: expectedSession.email,
        role: expectedSession.role,
      },
    }, expectedSession),
    /owner/,
  );
  assert.deepEqual(state.sessionOwners.get("session_pending"), expectedSession);
  assert.equal(state.sessionIds.has("session_pending"), true);
});

test("sensitive surface separates API and UI session tokens and rejects cross-role storage", () => {
  const apiToken = "token-api-session-very-secret";
  const uiToken = "token-ui-session-very-secret";
  const otherUiToken = "token-other-ui-session-secret";
  const password = "Prd047!unit-secret";
  const safe = {
    domText: "路由策略 mhv_abcd1234 sk_abcdefghi sk-abcdefghi whsec_abcdef",
    domHtml: (
      "<main>路由策略 <span>mhv_abcd1234</span> <span>sk_abcdefghi</span> "
      + "<span>sk-abcdefghi</span> <span>whsec_abcdef</span></main>"
    ),
    url: "http://localhost:5173/#routing-policies",
    localStorageEntries: [["theme", "dark"]],
    sessionStorageEntries: [
      ["metahub.auth_token", uiToken],
      ["metahub.auth_context", '{"role":"admin"}'],
    ],
  };
  assert.equal(
    assertSensitiveSurfaceSnapshot(safe, {
      exactSecrets: [apiToken, uiToken, otherUiToken, password],
      activeToken: uiToken,
    }).sessionStorageContractValid,
    true,
  );
  for (const snapshot of [
    { ...safe, domText: `路由策略 ${password}` },
    { ...safe, domText: "路由策略 mhv_abcd1234x" },
    { ...safe, domText: "路由策略 mhv_abcd1234secret" },
    { ...safe, domText: "路由策略 sk_abcdefghij" },
    { ...safe, domText: "路由策略 sk-abcdefghij" },
    { ...safe, domText: "路由策略 whsec_abcdefg" },
    { ...safe, localStorageEntries: [["debug", uiToken]] },
    { ...safe, sessionStorageEntries: [["metahub.auth_token", apiToken], ["metahub.auth_context", '{}']] },
    { ...safe, sessionStorageEntries: [["metahub.auth_token", otherUiToken], ["metahub.auth_context", '{}']] },
  ]) {
    let failure = null;
    try {
      assertSensitiveSurfaceSnapshot(snapshot, {
        exactSecrets: [apiToken, uiToken, otherUiToken, password],
        activeToken: uiToken,
      });
    } catch (error) {
      failure = error;
    }
    assert.ok(failure instanceof Error);
    assert.doesNotMatch(failure.message, new RegExp(password.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    for (const token of [apiToken, uiToken, otherUiToken]) {
      assert.doesNotMatch(failure.message, new RegExp(token));
    }
  }
  assert.throws(
    () => assertSensitiveSurfaceSnapshot(safe, {
      exactSecrets: [apiToken, uiToken, otherUiToken, password],
      activeToken: null,
    }),
    /注销后仍保留认证槽位/,
  );
});

test("UI token registry rejects API or cross-role reuse without leaking credentials", () => {
  const state = {
    adminToken: "token-api-admin-secret",
    roleTokens: { developer: "token-api-developer-secret" },
    uiTokens: new Map(),
  };
  const adminUiToken = "token-ui-admin-secret";
  assert.equal(registerUiToken(state, "admin", adminUiToken), adminUiToken);
  for (const [role, token] of [
    ["developer", state.adminToken],
    ["developer", adminUiToken],
    ["admin", "token-ui-admin-second-secret"],
  ]) {
    let failure = null;
    try {
      registerUiToken(state, role, token);
    } catch (error) {
      failure = error;
    }
    assert.ok(failure instanceof Error);
    for (const secret of [state.adminToken, state.roleTokens.developer, adminUiToken, token]) {
      assert.doesNotMatch(failure.message, new RegExp(secret));
    }
  }
});

test("owned lifecycle scrubs API and UI tokens after cleanup", async () => {
  const state = {
    accounts: { admin: { password: "Prd047!memory-only" } },
    roleTokens: { developer: "token-api-developer-secret" },
    adminToken: "token-api-admin-secret",
    uiTokens: new Map([["admin", "token-ui-admin-secret"]]),
  };
  const outcome = await runOwnedStateLifecycle({
    createStateAction: async () => state,
    executeAction: async () => ({ status: "passed" }),
    cleanupAction: async () => ({ terminalStateVerified: true }),
  });
  assert.deepEqual(outcome.state.accounts, {});
  assert.deepEqual(outcome.state.roleTokens, {});
  assert.equal(outcome.state.adminToken, null);
  assert.equal(outcome.state.uiTokens.size, 0);
});

test("owned lifecycle also scrubs UI tokens when browser execution fails", async () => {
  const state = {
    accounts: { admin: { password: "Prd047!failure-memory" } },
    roleTokens: { developer: "token-api-developer-failure" },
    adminToken: "token-api-admin-failure",
    uiTokens: new Map(),
  };
  let cleanupCount = 0;
  await assert.rejects(
    () => runOwnedStateLifecycle({
      createStateAction: async () => state,
      executeAction: async (ownedState) => {
        ownedState.uiTokens.set("admin", "token-ui-admin-failure");
        // 模拟真实 Chrome 在持有 UI Token 时失败，验证 finally 仍会 cleanup 并清空内存凭据。
        throw new Error("fixed_browser_failure");
      },
      cleanupAction: async () => {
        cleanupCount += 1;
        return { terminalStateVerified: true };
      },
    }),
    /fixed_browser_failure/,
  );
  assert.equal(cleanupCount, 1);
  assert.deepEqual(state.accounts, {});
  assert.deepEqual(state.roleTokens, {});
  assert.equal(state.adminToken, null);
  assert.equal(state.uiTokens.size, 0);
});

test("PNG inspector proves viewport size and rejects uniform placeholder pixels", () => {
  const colorful = rgbPng(2, 1, [255, 255, 255, 0, 0, 0]);
  assert.deepEqual(inspectPngPixels(colorful, { expectedWidth: 2, minHeight: 1 }), {
    width: 2,
    height: 1,
    byteLength: colorful.length,
    nonBlank: true,
  });
  const uniform = rgbPng(2, 1, [255, 255, 255, 255, 255, 255]);
  assert.throws(
    () => inspectPngPixels(uniform, { expectedWidth: 2, minHeight: 1 }),
    /像素为空或为单色占位图/,
  );
});
