// 本单测覆盖 PRD-027 前端 i18n、用户偏好 API 客户端和语言设置页面渲染边界。
import assert from "node:assert/strict";
import test from "node:test";

import { createI18n, errorLabel, translate } from "../../src/i18n/messages.js";
import { renderLanguageSettingsPanel } from "../../src/features/language-settings/renderLanguageSettingsPanel.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { getUserPreferences, updateUserPreferences } from "../../src/lib/userPreferences.js";

const preferencePayload = {
  preference: {
    preference_id: "upr_prd027",
    tenant_id: "tenant_1",
    user_id: "user_1",
    locale: "zh-CN",
    available_locales: [
      { locale: "zh-CN", label: "Chinese (Simplified)", native_label: "简体中文" },
      { locale: "en-US", label: "English", native_label: "English" },
    ],
    created_at: "2026-07-10T00:00:00Z",
    updated_at: "2026-07-10T00:00:00Z",
  },
  request_id: "req_prd027",
  trace_id: "trc_prd027",
};

test("i18n translates known keys and falls back predictably", () => {
  const zh = createI18n("zh-CN");
  const en = createI18n("en-US");
  const unsupported = createI18n("fr-FR");

  assert.equal(zh.t("language.save"), "保存语言");
  assert.equal(en.t("language.save"), "Save language");
  assert.equal(unsupported.locale, "zh-CN");
  assert.equal(translate("en-US", "missing.key", {}, "Fallback text"), "Fallback text");
  assert.equal(translate("en-US", "missing.key"), "missing.key");
  assert.equal(errorLabel("auth.permission_denied", "zh-CN"), "权限不足");
  assert.equal(errorLabel("auth.permission_denied", "en-US"), "Permission denied");
});

test("user preference client reads and updates without tenant or user fields", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_header"]]),
      async json() {
        // 偏好 API 响应可以包含审计字段，但请求不能由前端提交 tenant_id/user_id。
        return preferencePayload;
      },
    };
  };

  const current = await getUserPreferences({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
  });
  const updated = await updateUserPreferences({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    locale: "en-US",
  });

  assert.equal(current.state, "ok");
  assert.equal(updated.preference.locale, "zh-CN");
  assert.equal(calls[0].url, "http://control.local/user/preferences");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.equal(calls[1].options.method, "PATCH");
  assert.deepEqual(JSON.parse(calls[1].options.body), { locale: "en-US" });
  assert.equal(JSON.stringify(calls).includes("tenant_id"), false);
  assert.equal(JSON.stringify(calls).includes("user_id"), false);
});

test("user preference client maps unified errors with request id", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 422,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "user_preference.locale_invalid",
        message: "User preference locale is invalid.",
        request_id: "req_bad_locale",
        trace_id: "trc_bad_locale",
        suggestion: "Use zh-CN or en-US.",
      };
    },
  });

  await assert.rejects(
    () =>
      updateUserPreferences({
        controlApiUrl: "http://control.local",
        token: "jwt-token",
        locale: "fr-FR",
      }),
    (error) => {
      assert.ok(error instanceof AuthApiError);
      assert.equal(error.code, "user_preference.locale_invalid");
      assert.equal(error.requestId, "req_bad_locale");
      return true;
    },
  );
});

test("language settings panel renders dictionary text and escaped locale data", () => {
  const html = renderLanguageSettingsPanel(
    {
      state: "ok",
      ...preferencePayload,
      feedback: "语言偏好已保存",
    },
    createI18n("zh-CN"),
  );

  assert.match(html, /语言设置/);
  assert.match(html, /保存语言/);
  assert.match(html, /name="locale"/);
  assert.match(html, /value="zh-CN" checked/);
  assert.match(html, /简体中文/);
  assert.match(html, /语言偏好已保存/);
});

test("developer shell renders active language settings with English i18n", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    i18n: createI18n("en-US"),
    authContext: {
      user: { display_name: "Dev", email: "dev@example.com", role: "developer", tenant_id: "tenant_1" },
      tenant: { name: "Tenant" },
      session: { expires_at: "2026-07-10T12:00:00Z" },
    },
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "language-settings",
        navigation_items: [
          {
            id: "language-settings",
            label: "语言设置",
            group: "系统",
            target_section: "language-settings",
            description: "切换语言",
            empty_state_hint: "切换语言",
            order: 132,
          },
        ],
      },
    },
    userPreferencesState: {
      state: "ok",
      ...preferencePayload,
      preference: { ...preferencePayload.preference, locale: "en-US" },
    },
  });

  assert.match(container.innerHTML, /MetaHub Console/);
  assert.match(container.innerHTML, /Governance/);
  assert.match(container.innerHTML, /Language/);
  assert.match(container.innerHTML, /Save language/);
  assert.match(container.innerHTML, /Session expires: 2026-07-10T12:00:00Z/);
  assert.doesNotMatch(container.innerHTML, /undefined/);
});
