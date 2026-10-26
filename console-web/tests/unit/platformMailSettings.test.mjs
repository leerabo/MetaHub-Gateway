import assert from "node:assert/strict";
import test from "node:test";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { getPlatformMailSettings, updatePlatformMailSettings, testPlatformMailSettings,
  PlatformApiError } from "../../src/lib/platform.js";

const settings = () => ({ enabled: false, host: "smtp.example.invalid", port: 587,
  security: "starttls", username: "mailer", sender: "security@example.invalid", timeout_seconds: 10,
  password_set: true, revision: 1, source: "platform" });
const root = () => ({ innerHTML: "", addEventListener() {}, querySelectorAll: () => [],
  replaceChildren() { this.innerHTML = ""; } });
async function controller(overrides = {}) {
  const page = root();
  const c = createPlatformPageController({ root: page, api: {
    session: async () => ({ context: { permissions: ["platform.operator.manage"],
      operator: { display_name: "Mail admin", roles: ["platform_owner"] } } }),
    navigation: async () => ({ items: [{ id: "mail-settings", label: "邮件服务", href: "/platform.html#mail-settings" }] }),
    mailSettings: async () => settings(), updateMailSettings: async () => ({}),
    testMailSettings: async () => ({ test: { status: "passed", message: "连接、加密和账号认证成功。" } }),
    ...overrides,
  } });
  assert.equal(await c.mount(), true);
  return { c, page };
}

test("mail navigation loads the form, never restores a password and retains safe drafts after failed save", async () => {
  const { c, page } = await controller({ updateMailSettings: async () => {
    throw new PlatformApiError({ code: "platform.revision_conflict", message: "请刷新后重试。", status: 412 });
  } });
  assert.equal(c.snapshot().view, "mail-settings");
  assert.match(page.innerHTML, /data-platform-mail-settings-form/u);
  assert.match(page.innerHTML, /已保存；留空保留原密码/u);
  await c.updateMailSettings({ ...settings(), host: "new.example.invalid", password: "private-test-secret" });
  assert.equal(c.snapshot().mailDraft.host, "new.example.invalid");
  assert.doesNotMatch(JSON.stringify(c.snapshot()), /private-test-secret/u);
  assert.doesNotMatch(page.innerHTML, /private-test-secret/u);
  c.destroy();
});

test("save uses the observed revision and reloads; test feedback remains visible", async () => {
  let saved = settings();
  const { c, page } = await controller({ mailSettings: async () => saved,
    updateMailSettings: async ({ settings: input }) => {
      assert.equal(input.revision, 1);
      assert.equal(input.password, "private-test-secret");
      saved = { ...saved, revision: 2, enabled: true };
    } });
  assert.equal(await c.updateMailSettings({ ...settings(), revision: 999, password: "private-test-secret" }), true);
  assert.equal(c.snapshot().mailSettings.revision, 2);
  assert.equal(c.snapshot().mailDraft, null);
  assert.equal(await c.testMailSettings("connection"), true);
  assert.match(page.innerHTML, /测试成功/u);
  assert.doesNotMatch(JSON.stringify(c.snapshot()), /private-test-secret/u);
  c.destroy();
});

test("mail HTTP writes use platform CSRF and cookies without credentials in URLs", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url, options });
    return new Response(JSON.stringify(settings()));
  });
  const previous = globalThis.document;
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${"c".repeat(43)}` };
  try {
    await getPlatformMailSettings({ controlApiUrl: "https://control.test" });
    await updatePlatformMailSettings({ controlApiUrl: "https://control.test", settings: { ...settings(), password: "secret" } });
    await testPlatformMailSettings({ controlApiUrl: "https://control.test", test: { revision: 1, kind: "connection" } });
    for (const { url, options } of calls) {
      assert.equal(options.credentials, "include");
      assert.equal(options.cache, "no-store");
      assert.equal("Authorization" in options.headers, false);
      assert.doesNotMatch(url, /secret/u);
    }
    assert.equal(calls[1].options.method, "PUT");
    assert.equal(calls[2].options.method, "POST");
    assert.equal(calls[1].options.headers["X-CSRF-Token"], "c".repeat(43));
    assert.equal(calls[2].options.headers["X-CSRF-Token"], "c".repeat(43));
  } finally { globalThis.document = previous; }
});
