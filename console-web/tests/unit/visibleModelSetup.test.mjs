import assert from "node:assert/strict";
import test from "node:test";
import { renderModelCatalogPanel } from "../../src/features/models/renderModelCatalogPanel.js";
import { renderModelAliasPanel } from "../../src/features/model-aliases/renderModelAliasPanel.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";
import { renderIdentityLoginPage, setIdentityPublicBranding } from "../../src/features/identity/renderIdentityPage.js";
import { renderProviderPanel } from "../../src/features/providers/renderProviderPanel.js";

const auth = { permission_codes: Object.values(PERMISSIONS) };
const provider = (id, status = "active") => ({ provider_id: id, name: id, status });
const model = (id, status = "active") => ({ model_id: id, internal_model_id: id, status });
const environment = (id, status = "active") => ({ environment_id: id, name: id, status });
const models = (providers, context = auth) => renderModelCatalogPanel({ state: "ok", models: [] },
  { state: "ok", providers }, context);
const routes = (entries, environments) => renderModelAliasPanel({ state: "ok", model_aliases: [] },
  { state: "ok", models: entries }, auth, { state: "ok", environments });

test("model creation never offers disabled or unknown provider states", () => {
  const html = models([provider("retired", "disabled"), provider("live"), provider("unknown", "unknown")]);
  assert.doesNotMatch(html, /value="retired"|value="unknown"/u);
  assert.match(html, /value="live" selected/u);
  assert.match(html, /name="providerId" required/u);
});

test("multiple services require a visible choice, zero services offers an authorized recovery", () => {
  const html = models([provider("one"), provider("two")]);
  assert.match(html, /<option value="">请选择模型服务/u);
  assert.doesNotMatch(html, /value="(?:one|two)" selected/u);
  const empty = models([provider("retired", "disabled")]);
  assert.match(empty, /暂无已启用的模型服务/u);
  assert.match(empty, /href="#workspace.gateway.providers"/u);
  assert.doesNotMatch(empty, /id="create-model-form"/u);
  assert.doesNotMatch(models([], { permission_codes: [PERMISSIONS.MODEL_READ, PERMISSIONS.MODEL_WRITE] }),
    /href="#workspace.gateway.providers"/u);
});

test("route names validate at the field and expose the naming constraint", () => {
  const html = routes([model("chat")], [environment("dev")]);
  const pattern = html.match(/name="alias"[^>]*pattern="([^"]+)"/u)[1];
  const constraint = new RegExp(`^(?:${pattern})$`, "v");
  for (const value of ["chat", "company-default", "model.v1_test", "1"]) assert.equal(constraint.test(value), true);
  for (const value of ["", "日常办公", "UPPER", "has space", "-prefix", "a".repeat(121)]) assert.equal(constraint.test(value), false);
  assert.match(html, /aria-describedby="model-alias-name-format"/u);
  assert.match(html, /id="model-alias-name-format">小写字母/u);
});

test("a single environment is automatic; multiple environments cannot hide a random default", () => {
  const single = routes([model("chat")], [environment("dev"), environment("off", "disabled")]);
  assert.match(single, /type="hidden" name="environmentId" value="dev"/u);
  assert.doesNotMatch(single, /<select name="environmentId"|value="off"/u);
  const multiple = routes([model("chat"), model("other")], [environment("test"), environment("prod")]);
  assert.match(multiple, /<select name="environmentId" required/u);
  assert.ok(multiple.indexOf('<select name="environmentId"') < multiple.indexOf('<details class="model-advanced"'));
  assert.match(multiple, /<option value="">请选择调用模型/u);
  assert.match(multiple, /<option value="">请选择运行环境/u);
  assert.doesNotMatch(multiple, /value="(?:test|prod|chat|other)" selected/u);
});

test("unchanged bootstrap branding cannot replace the modern login title or layout", () => {
  const original = { product_name: "MetaHub", primary_color: "#176B87", secondary_color: "#F59E0B",
    login_title: "企业 AI Gateway", login_message: "安全、可追踪的企业模型网关", brand_generation: 1,
    logo_path: null, favicon_path: null };
  try {
    const resolved = setIdentityPublicBranding(original, null);
    assert.equal(resolved.login_title, "登录账号");
    assert.equal(resolved.login_message, "");
    assert.equal(resolved.primary_color, "#08786F");
    const container = { innerHTML: "" };
    renderIdentityLoginPage(container, { busy: true });
    assert.match(container.innerHTML, /id="identity-login-title">登录账号/u);
    assert.doesNotMatch(container.innerHTML, /企业 AI Gateway/u);
    for (const changed of [{ brand_generation: 2 }, { product_name: "Company" }, { login_title: "Company Login" },
      { primary_color: "#123456" }, { logo_path: `/branding/public/assets/bas_${"a".repeat(32)}` }]) {
      const custom = { ...original, ...changed };
      assert.equal(setIdentityPublicBranding(custom, null).login_title, custom.login_title);
    }
  } finally { setIdentityPublicBranding(null, null); }
});

test("saved provider credentials lead to a visible connection test before model configuration", () => {
  const render = (health, status = "active", keys = [{ status: "active" }]) => renderProviderPanel({
    state: "ok", providers: [{ provider_id: "service", name: "Service", auth_type: "bearer", status, health_status: health }],
    providerKeysByProviderId: { service: { state: "ok", provider_keys: keys } },
  }, auth);
  for (const health of ["unknown", "unhealthy", "degraded"]) {
    const html = render(health);
    const action = html.indexOf('data-provider-test-form');
    assert.ok(action > 0 && action < html.indexOf('<details class="provider-maintenance"'));
    assert.match(html, /class="provider-setup-action" type="submit">测试连接/u);
    assert.equal((html.match(/data-provider-test-form/gu) ?? []).length, 1);
    assert.doesNotMatch(html, /下一步：配置可用模型/u);
  }
  assert.match(render("healthy"), /下一步：配置可用模型/u);
  assert.doesNotMatch(render("healthy", "disabled"), /下一步：配置可用模型/u);
  assert.doesNotMatch(render("healthy", "active", []), /下一步：配置可用模型/u);
  const restricted = renderProviderPanel({ state: "ok", providers: [
    { provider_id: "service", name: "Service", auth_type: "none", status: "active", health_status: "healthy" },
  ] }, { permission_codes: [PERMISSIONS.PROVIDER_READ, PERMISSIONS.PROVIDER_WRITE] });
  assert.doesNotMatch(restricted, /href="#workspace.gateway.models"/u);
});
