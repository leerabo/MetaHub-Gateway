import assert from "node:assert/strict";
import test from "node:test";
import { getPlatformMeteringState, recoverPlatformMeterFact } from "../../src/lib/platformBilling.js";
import { PlatformApiError } from "../../src/lib/platform.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";

const fact = {
  meter_outbox_id: `pmo_${"1".repeat(32)}`, source_sequence: 1, attempt_count: 1,
  source_sha256: "a".repeat(64), workspace_id: `wsp_${"2".repeat(32)}`,
  occurred_at: "2026-09-08T00:00:00Z", error_code: "platform_metering.owner_invalid",
  can_archive_internal_cost: true,
};
const metering = () => ({ state: "blocked", checkpoint: 0, pending_count: 2,
  blocked_fact: { ...fact }, recent_recoveries: [], request_id: "req_meter", trace_id: "trc_meter" });
const input = (action = "retry") => ({ meterOutboxId: fact.meter_outbox_id,
  sourceSequence: 1, attemptCount: 1, sourceSha256: fact.source_sha256, action, confirmed: true });
const wire = () => ({ meter_outbox_id: fact.meter_outbox_id, source_sequence: 1,
  expected_attempt_count: 1, expected_source_sha256: fact.source_sha256, action: "retry", confirmed: true });
const permissions = ["platform.billing.read", "platform.billing.adjust"];
const session = () => ({ context: { permissions: [...permissions], operator: {
  operator_id: `pto_${"3".repeat(32)}`, display_name: "Billing", roles: ["platform_billing"],
}, expires_at: "2026-09-10T00:00:00Z" } });
const root = () => ({ innerHTML: "", addEventListener() {}, querySelectorAll: () => [],
  replaceChildren() { this.innerHTML = ""; } });
const navigation = [{ id: "billing", label: "Billing", href: "/platform.html#billing" },
  { id: "audit", label: "Audit", href: "/platform.html#audit" }];
const tick = () => new Promise((resolve) => setImmediate(resolve));
async function controller(overrides = {}) {
  const page = root();
  const c = createPlatformPageController({ root: page, api: {
    session: async () => session(), navigation: async () => ({ items: navigation }),
    billingSummary: async () => ({ cycles: [], next_cursor: null }),
    workspaces: async () => ({ workspaces: [], next_cursor: null }),
    meteringState: async () => metering(), recoverMeterFact: async () => ({}), ...overrides,
  } });
  assert.equal(await c.mount(), true);
  return { c, page };
}

test("meter clients bind CSRF, idempotency and exact failed source without Bearer", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url, options });
    return new Response(JSON.stringify(String(url).endsWith("metering-state") ? metering() : {
      recovery: { recovery_id: `pmx_${"4".repeat(32)}`, source_sequence: 1,
        workspace_id: fact.workspace_id, action: "retry", created_at: fact.occurred_at },
      idempotent_replay: false, request_id: "req_recovery", trace_id: "trc_recovery",
    }));
  });
  const previous = globalThis.document;
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${"c".repeat(43)}` };
  try {
    await getPlatformMeteringState({ controlApiUrl: "http://control.test" });
    await recoverPlatformMeterFact({ controlApiUrl: "http://control.test", recovery: wire(),
      idempotencyKey: "meter-recovery-test-0001" });
    assert.equal(calls[1].options.headers["X-CSRF-Token"], "c".repeat(43));
    assert.equal(calls[1].options.headers["Idempotency-Key"], "meter-recovery-test-0001");
    assert.deepEqual(JSON.parse(calls[1].options.body), wire());
    for (const { options } of calls) {
      assert.equal(options.credentials, "include");
      assert.equal(options.cache, "no-store");
      assert.equal("Authorization" in options.headers, false);
    }
    for (const bad of [{ confirmed: 1 }, { source_sequence: "1" }, { prompt: "private" },
      { expected_attempt_count: 0 }, { action: "skip" }]) {
      assert.throws(() => recoverPlatformMeterFact({ recovery: { ...wire(), ...bad },
        idempotencyKey: "meter-recovery-test-0001" }), /./u);
    }
    assert.equal(calls.length, 2);
  } finally { globalThis.document = previous; }
});

test("meter client rejects extra fields, impossible states and unsafe source versions", async (t) => {
  let response = metering();
  t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify(response)));
  for (const changed of [{ prompt: "private" }, { state: "idle" }, { checkpoint: 8 },
    { pending_count: -1 }, { blocked_fact: { ...fact, source_sequence: Number.MAX_SAFE_INTEGER + 1 } },
    { blocked_fact: { ...fact, workspace_id: null } },
    { blocked_fact: { ...fact, error_code: "private detail" } }]) {
    response = { ...metering(), ...changed };
    await assert.rejects(getPlatformMeteringState({ controlApiUrl: "http://control.test" }),
      (error) => error.code === "platform_billing.response_invalid");
  }
});

test("meter renderer requires explicit action and confirmation, prunes archive and write permissions", () => {
  const page = root();
  const state = { session: session(), navigation, view: "billing", metering: metering() };
  renderPlatformWorkspace(page, state);
  assert.match(page.innerHTML, /data-platform-meter-recovery-form/u);
  assert.match(page.innerHTML, /<option value="">请选择处理方式/u);
  assert.match(page.innerHTML, /name="confirmed" type="checkbox" required/u);
  const recoveryForm = page.innerHTML.match(/<form[^>]*data-platform-meter-recovery-form[^>]*>[\s\S]*?<\/form>/u)[0];
  assert.doesNotMatch(recoveryForm, /<option[^>]*selected|name="confirmed"[^>]*checked/u);
  state.metering.blocked_fact.can_archive_internal_cost = false;
  renderPlatformWorkspace(page, state);
  assert.doesNotMatch(page.innerHTML, /value="internal_cost_only"/u);
  state.session.context.permissions = ["platform.billing.read"];
  renderPlatformWorkspace(page, state);
  assert.doesNotMatch(page.innerHTML, /data-platform-meter-recovery-form/u);
});

test("controller rejects stale forms and duplicate submissions and fences late completion on navigation", async () => {
  let finish;
  const calls = [];
  const { c } = await controller({ recoverMeterFact: (value) => {
    calls.push(value); return new Promise((resolve) => { finish = resolve; });
  } });
  try {
    for (const bad of [{ confirmed: false }, { attemptCount: 2 }, { sourceSequence: 2 },
      { sourceSha256: "b".repeat(64) }, { meterOutboxId: `pmo_${"9".repeat(32)}` }]) {
      assert.equal(await c.recoverMeterFact({ ...input(), ...bad }), false);
    }
    const pending = c.recoverMeterFact(input());
    assert.equal(calls.length, 1);
    assert.equal(await c.recoverMeterFact(input()), false);
    c.selectView("audit");
    assert.equal(calls[0].signal.aborted, true);
    finish({});
    assert.equal(await pending, false);
    assert.equal(c.snapshot().metering, null);
    assert.equal(c.snapshot().meterRecoveryIntent, null);
  } finally { c.destroy(); }
});

test("controller reuses uncertain retry only for same active identity and clears it on refresh", async () => {
  const calls = [];
  const { c } = await controller({ recoverMeterFact: async (value) => {
    calls.push(value); throw new PlatformApiError({ status: 503, code: "platform_metering.unavailable" });
  } });
  try {
    assert.equal(await c.recoverMeterFact(input()), false);
    assert.equal(await c.recoverMeterFact(input()), false);
    assert.equal(calls[0].idempotencyKey, calls[1].idempotencyKey);
    await c.refresh();
    assert.equal(c.snapshot().meterRecoveryIntent, null);
    assert.equal(await c.recoverMeterFact(input()), false);
    assert.notEqual(calls[1].idempotencyKey, calls[2].idempotencyKey);
  } finally { c.destroy(); }
});

test("meter reads are scoped to billing and late reads cannot restore facts after leaving", async () => {
  let readCount = 0;
  let finish;
  const { c } = await controller({ meteringState: () => {
    readCount += 1;
    return readCount === 1 ? Promise.resolve(metering()) : new Promise((resolve) => { finish = resolve; });
  } });
  try {
    c.selectView("audit");
    await c.refresh();
    assert.equal(readCount, 1);
    c.selectView("billing");
    await tick();
    assert.equal(readCount, 2);
    c.selectView("audit");
    finish(metering());
    await tick();
    assert.equal(c.snapshot().metering, null);
    assert.equal(c.snapshot().view, "audit");
  } finally { c.destroy(); }
});

test("a stale session refresh cannot restore an old operator or recovery authority", async () => {
  let count = 0;
  let finish;
  const restricted = session();
  restricted.context.operator.operator_id = `pto_${"8".repeat(32)}`;
  restricted.context.permissions = ["platform.billing.read"];
  const { c } = await controller({ session: () => {
    count += 1;
    if (count === 2) return new Promise((resolve) => { finish = resolve; });
    return Promise.resolve(count === 1 ? session() : restricted);
  } });
  try {
    const stale = c.refresh();
    await c.refresh();
    finish(session());
    assert.equal(await stale, false);
    assert.equal(c.snapshot().session.context.operator.operator_id, restricted.context.operator.operator_id);
    assert.equal(await c.recoverMeterFact(input()), false);
  } finally { c.destroy(); }
});
