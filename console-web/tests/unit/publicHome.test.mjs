import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { legacyConsoleTarget } from "../../src/features/public-home/entryRoute.js";
import { icon } from "../../src/features/public-home/icons.js";
import { createGatewayScene } from "../../src/features/public-home/gatewayScene.js";
import { renderIdentityLoginPage, renderIdentitySecurityPage, setIdentityPublicBranding } from "../../src/features/identity/renderIdentityPage.js";

test("public anchors stay public and historical console links retain their complete location", () => {
  for (const hash of ["", "#main", "#capabilities", "#approach"]) {
    assert.equal(legacyConsoleTarget({ hash, search: "?from=public" }), null);
  }
  for (const hash of ["#virtual-keys", "#accept-invitation", "#logs", "#PAGE-080", "#unknown"]) {
    assert.equal(legacyConsoleTarget({ hash, search: "?test=one%20two" }), `console.html?test=one%20two${hash}`);
  }
});

test("homepage exposes eight factual capabilities and working entry links without executing JavaScript", async () => {
  const html = await readFile(new URL("../../index.html", import.meta.url), "utf8");
  assert.equal([...html.matchAll(/<article>/gu)].length, 8);
  assert.equal([...html.matchAll(/<h1\b/gu)].length, 1);
  assert.match(html, /<h1 id="home-title">MetaHub<\/h1>/u);
  for (const text of ["BYOK", "统一 KEY", "路由分发", "敏感数据防护", "精细化财务", "边缘 Agent", "团队与身份", "调用洞察"]) assert.ok(html.includes(text));
  assert.match(html, /href="identity.html"/u);
  assert.match(html, /href="status.html"/u);
  assert.match(html, /data-home-nav-toggle/iu);
  assert.match(html, /data-home-mobile-nav/iu);
  assert.doesNotMatch(html, /data-motion-toggle/iu);
  assert.doesNotMatch(html, /99\.9|SLA|72\s*小时|智能体编排|sk-[a-zA-Z0-9]/u);
});

test("public page is isolated from credentials and the console remains a separate entry", async () => {
  const main = await readFile(new URL("../../src/features/public-home/main.js", import.meta.url), "utf8");
  const html = await readFile(new URL("../../console.html", import.meta.url), "utf8");
  assert.doesNotMatch(main, /localStorage|sessionStorage|document\.cookie|fetch\(|getIdentity|loginIdentity/u);
  assert.doesNotMatch(main, /disposed|data-motion-toggle/u);
  assert.match(html, /id="app"/u);
  assert.match(html, /src="src\/app\/main.js"/u);
});

test("all static entry assets and links remain relative for reverse-proxy subpaths", async () => {
  for (const page of ["index.html", "identity.html", "console.html", "privacy.html", "partner.html", "platform.html", "status.html"]) {
    const html = await readFile(new URL("../../" + page, import.meta.url), "utf8");
    assert.doesNotMatch(html, /(?:href|src)="\//u, page);
  }
  assert.equal(new URL("src/styles/public-home.css", "https://example.test/metahub/index.html").pathname, "/metahub/src/styles/public-home.css");
  assert.equal(new URL("identity.html", "https://example.test/metahub/console.html").pathname, "/metahub/identity.html");
});

test("new public assets are declared for production, with no runtime CDN", async () => {
  const manifest = JSON.parse(await readFile(new URL("../../production-surface-manifest.json", import.meta.url), "utf8"));
  assert.ok(manifest.html_entrypoints.includes("console.html"));
  assert.ok(manifest.entrypoints.includes("src/features/public-home/main.js"));
  for (const style of ["identity-access", "public-home"]) assert.ok(manifest.style_entrypoints.includes(`src/styles/${style}.css`));
  const scene = await readFile(new URL("../../src/features/public-home/gatewayScene.js", import.meta.url), "utf8");
  assert.match(scene, /vendor\/three.js/u);
  assert.doesNotMatch(scene, /https?:\/\/|fetch\(/u);
  for (const lifecycle of ["prefers-reduced-motion", "visibilitychange", "webglcontextlost", "IntersectionObserver", "ResizeObserver", "dispose()", "cancelAnimationFrame"]) assert.ok(scene.includes(lifecycle));
});

test("login pending state disables duplicate submission without echoing a password", () => {
  setIdentityPublicBranding(null);
  const root = { innerHTML: "" };
  renderIdentityLoginPage(root, { email: "owner@example.com", busy: true, password: "not-rendered" });
  assert.match(root.innerHTML, /aria-busy="true"/u);
  assert.match(root.innerHTML, /type="submit" disabled/u);
  assert.match(root.innerHTML, /data-password-toggle/u);
  assert.match(root.innerHTML, /autocomplete="current-password"/u);
  assert.doesNotMatch(root.innerHTML, /not-rendered/u);
  renderIdentityLoginPage(root, { error: { message: "<script>unsafe</script>" } });
  assert.match(root.innerHTML, /aria-busy="false"/u);
  assert.match(root.innerHTML, /&lt;script&gt;/u);
  assert.doesNotMatch(root.innerHTML, /<script>/u);
});

test("account page only offers workspace console entry with a restored workspace", () => {
  const root = { innerHTML: "" };
  const state = { account: { email: "owner@example.com", status: "active" }, sessions: [] };
  renderIdentitySecurityPage(root, state);
  assert.doesNotMatch(root.innerHTML, /href="console.html"/u);
  renderIdentitySecurityPage(root, { ...state, workspaceContext: { name: "Demo", role: "owner" } });
  assert.match(root.innerHTML, /href="console.html"/u);
});

test("vendored Lucide icons are decorative and unknown names are rejected", () => {
  assert.match(icon("arrow-right"), /aria-hidden="true"/u);
  assert.match(icon("eye"), /<svg/u);
  assert.throws(() => icon("<script>"), TypeError);
});

test("browsers without WebGL retain the static homepage without a renderer error", (t) => {
  const previousDocument = globalThis.document;
  const previousMatchMedia = globalThis.matchMedia;
  t.after(() => { globalThis.document = previousDocument; globalThis.matchMedia = previousMatchMedia; });
  const contexts = [];
  globalThis.matchMedia = () => ({ matches: false });
  globalThis.document = { createElement: () => ({ getContext: (type) => { contexts.push(type); return null; } }) };
  const host = { dataset: {} };
  assert.equal(createGatewayScene(host), null);
  assert.deepEqual(contexts, ["webgl2", "webgl"]);
  assert.deepEqual(host.dataset, {});
});
