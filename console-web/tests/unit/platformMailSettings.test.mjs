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

const sidebarApi = {
  navigation: async () => ({ items: [
    { id: "operators", label: "操作员", href: "/platform.html#operators" },
    { id: "mail-settings", label: "邮件服务", href: "/platform.html#mail-settings" },
  ] }),
  operators: async () => ({ operators: [] }),
  registrationPolicy: async () => null,
};

test("entering mail settings from another sidebar page fetches and renders the form", async () => {
  let requests = 0;
  const { c, page } = await controller({ ...sidebarApi, mailSettings: async () => {
    requests += 1;
    return settings();
  } });
  assert.equal(c.snapshot().view, "operators");
  assert.equal(requests, 0);
  assert.equal(await c.selectView("mail-settings"), true);
  assert.equal(requests, 1);
  assert.equal(c.snapshot().loading, false);
  assert.match(page.innerHTML, /data-platform-mail-settings-form/u);
  assert.doesNotMatch(page.innerHTML, /正在加载邮件服务配置/u);
  c.destroy();
});

test("a failed sidebar load stops loading, offers retry, and can recover", async () => {
  let requests = 0;
  const { c, page } = await controller({ ...sidebarApi, mailSettings: async () => {
    requests += 1;
    if (requests === 1) throw new PlatformApiError({
      code: "platform.unavailable", message: "暂时无法读取配置。", status: 503,
    });
    return settings();
  } });
  assert.equal(await c.selectView("mail-settings"), false);
  assert.equal(c.snapshot().loading, false);
  assert.match(page.innerHTML, /暂时无法读取配置/u);
  assert.match(page.innerHTML, /重新加载/u);
  assert.doesNotMatch(page.innerHTML, /正在加载邮件服务配置/u);
  assert.equal(await c.refresh(), true);
  assert.equal(requests, 2);
  assert.match(page.innerHTML, /data-platform-mail-settings-form/u);
  c.destroy();
});

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

test("failed email tests retain the recipient during and after sending without success feedback", async () => {
  let finish;
  const { c, page } = await controller({ testMailSettings: ({ test: input }) => {
    assert.equal(input.recipient, "recipient@example.test");
    return new Promise((resolve) => { finish = resolve; });
  } });
  const pending = c.testMailSettings("email", " recipient@example.test ");
  assert.equal(c.snapshot().loading, true);
  assert.match(page.innerHTML, /value="recipient@example.test"/u);
  finish({ test: { status: "failed", message: "加密连接失败。" } });
  assert.equal(await pending, false);
  assert.equal(c.snapshot().loading, false);
  assert.equal(c.snapshot().feedback, null);
  assert.match(page.innerHTML, /value="recipient@example.test"/u);
  assert.match(page.innerHTML, /测试失败/u);
  assert.doesNotMatch(page.innerHTML, /邮件服务测试已完成/u);
  c.destroy();
  assert.equal(c.snapshot().mailRecipient, "");
});

test("network failures retain the test recipient and allow retry", async () => {
  let attempt = 0;
  const { c, page } = await controller({ testMailSettings: async () => {
    if (++attempt === 1) throw new PlatformApiError({ status: 503, message: "网络暂不可用。" });
    return { test: { status: "passed", message: "SMTP 服务已接受测试邮件。" } };
  } });
  assert.equal(await c.testMailSettings("email", "recipient@example.test"), false);
  assert.match(page.innerHTML, /value="recipient@example.test"/u);
  assert.match(page.innerHTML, /网络暂不可用/u);
  assert.equal(await c.testMailSettings("email", c.snapshot().mailRecipient), true);
  assert.match(page.innerHTML, /测试成功/u);
  assert.match(page.innerHTML, /value="recipient@example.test"/u);
  c.destroy();
});
