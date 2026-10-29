import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { renderTenantPanel } from "../../src/features/tenant/renderTenantPanel.js";
import { renderConsoleShell } from "../../src/features/console-shell/renderConsoleShell.js";

const main = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");

for (const accountSection of ["workspaces", "preferences", "billing", "invoices", "business", "enterprise", "scim", "access", "governance", "privacy", "compliance", "machines"]) {
  test(`account reads only the selected section: ${accountSection}`, async () => {
    const source = await readFile(new URL("../../src/features/identity/identityPageState.js", import.meta.url), "utf8");
    const block = source.match(/async function loadAccountSection\(ticket\) \{[\s\S]*?\n  \}/u)?.[0];
    assert.ok(block);
    let current = true;
    const calls = [];
    const loaders = { billing: "loadWorkspaceBilling", invoices: "loadWorkspaceInvoices",
      business: "loadBusinessVerification", enterprise: "loadEnterpriseIdentity", scim: "loadScimDirectory",
      access: "loadConditionalAccess", governance: "loadDataGovernance", privacy: "loadPrivacy",
      compliance: "loadComplianceReports", machines: "loadMachineIdentity" };
    const scope = { accountSection, isCurrent: () => current, enterAccountPreferences: () => true,
      accountPreferenceController: { load: async () => calls.push("preferences") } };
    for (const name of Object.values(loaders)) scope[name] = async () => calls.push(name);
    const run = runInNewContext(`${block}\nloadAccountSection;`, scope);
    await run({});
    assert.deepEqual(calls, accountSection === "workspaces" ? [] : [loaders[accountSection] ?? "preferences"]);
    calls.length = 0;
    current = false;
    assert.equal(await run({}), false);
    assert.deepEqual(calls, []);
  });
}

test("explicit console navigation pushes history; canonicalization replaces and same page is a no-op", () => {
  const start = main.indexOf("function replaceNavigationHash(");
  const end = main.indexOf("function currentNavigationPath(", start);
  const calls = [];
  const location = { pathname: "/console.html", search: "?from=test", hash: "" };
  const history = Object.fromEntries(["pushState", "replaceState"].map((method) => [method, (_state, _title, url) => {
    calls.push({ method, url });
    location.hash = new URL(url, "http://localhost").hash;
  }]));
  const navigate = runInNewContext(main.slice(start, end) + "\nreplaceNavigationHash;", { history, location });
  navigate("workspace.home.overview");
  navigate("workspace.settings.profile", { push: true });
  navigate("workspace.settings.profile", { push: true });
  assert.deepEqual(calls, [
    { method: "replaceState", url: "/console.html?from=test#workspace.home.overview" },
    { method: "pushState", url: "/console.html?from=test#workspace.settings.profile" },
  ]);
});

for (const file of ["../../src/app/main.js", "../../src/features/identity/main.js"]) {
  test(`${file}: cached page restoration revalidates through a fresh bootstrap`, async () => {
    const source = await readFile(new URL(file, import.meta.url), "utf8");
    const statement = source.match(/(?:globalThis\.)?window\.addEventListener\("pageshow", \(event\) => \{[\s\S]*?\n\s*\}\);/u)?.[0];
    assert.ok(statement);
    let handler;
    let reloads = 0;
    const window = { addEventListener: (event, listener) => { assert.equal(event, "pageshow"); handler = listener; }, location: { reload: () => reloads++ } };
    runInNewContext(statement, { window });
    handler({ persisted: false });
    assert.equal(reloads, 0);
    handler({ persisted: true });
    assert.equal(reloads, 1);
  });
}

test("organization profile only asks for a name; deployment facts are collapsed and never editable", () => {
  const html = renderTenantPanel({ workspace: { name: "<Organization>", kind: "organization", status: "active", region: "cn-north-1", service_mode: "internal_only", environment: { code: "dev" }, profile_state: "draft" } }, { permission_codes: ["tenant.settings.read"] }, { kind: "organization", role: "owner" });
  const form = html.match(/<form[\s\S]*?<\/form>/u)?.[0];
  assert.match(form, /name="workspaceName"/u);
  assert.match(form, /&lt;Organization&gt;/u);
  assert.doesNotMatch(form, /cn-north|dev|区域|资料状态|<select/u);
  assert.match(html, /<details class="workspace-system-details">/u);
  assert.doesNotMatch(html, /<details[^>]*\bopen\b|待确认/u);
});

test("authorized subpages have a predictable home link, without inventing an unavailable route", () => {
  const overview = { id: "overview", route_code: "workspace.home.overview", label: "Home", order: 1 };
  const profile = { id: "tenant", label: "Profile", order: 2 };
  const render = (items) => renderConsoleShell({ consoleShellState: { navigation: { navigation_items: items }, activeNavigationItemId: "tenant" }, contentHtml: "" });
  assert.match(render([overview, profile]), /aria-label="返回空间首页"/u);
  assert.match(render([overview, profile, { id: "quickstart", label: "Start", order: 3 }]), /href="#workspace.home.getting-started"/u);
  assert.doesNotMatch(render([profile]), /console-home-return/u);
});
