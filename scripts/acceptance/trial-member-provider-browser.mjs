import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";

/** Trial feedback, using actual API responses and a separate invitation recipient. */
export async function verifyTrialMemberProvider({ browser, page, origin, viewport, tag,
  report, layout, browserJson, setCurrentPage }) {
  const checks = { width: viewport.width, status: "running" };
  (report.trialMemberProvider ??= []).push(checks);
  await page.goto(origin + "/console.html");
  await page.locator(".console-shell").waitFor();
  await page.context().addInitScript(() => {
    window.__trialLoginPainted = false;
    new MutationObserver(() => {
      if (document.querySelector("[data-identity-login-form]")) window.__trialLoginPainted = true;
    }).observe(document, { subtree: true, childList: true });
  });
  await page.getByRole("link", { name: "账号设置", exact: true }).click();
  await page.locator('[data-account-section="security"]').waitFor();
  assert.equal(await page.evaluate(() => window.__trialLoginPainted), false);
  assert.ok(page.url().includes("view=account#security"));
  checks.accountSettingsNoLoginFlash = true;
  await layout(page, tag + "-account-security");
  await page.getByRole("link", { name: "返回空间", exact: true }).click();
  await page.locator(".console-shell").waitFor();

  const subscriptionResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/workspace/subscription");
  await page.goto(origin + "/console.html#workspace.finance.subscription");
  assert.equal((await subscriptionResponse).status(), 200);
  const subscription = page.locator('[data-workspace-commercial-root="workspace-subscription"]');
  await subscription.waitFor();
  await page.waitForFunction(() => {
    const panel = document.querySelector('[data-workspace-commercial-root="workspace-subscription"]');
    return panel && !/待加载|正在读取订阅|正在同步/u.test(panel.textContent);
  });
  assert.equal(await subscription.locator('[role="alert"]').count(), 0);
  await layout(page, tag + "-subscription-loaded");
  checks.subscriptionLoaded = true;

  await page.goto(origin + "/console.html#workspace.gateway.providers");
  await page.locator('[data-provider-tab="catalog"]').waitFor();
  assert.equal(await page.locator(".provider-custom-setup[open]").count(), 0);
  assert.equal(await page.locator(".provider-keypool[open]").count(), 0);
  await layout(page, tag + "-provider-list");
  await page.locator('[data-provider-tab="catalog"]').click();
  const manufacturer = page.locator("[data-provider-catalog-selection]");
  await manufacturer.waitFor();
  assert.equal(await page.locator("[data-provider-catalog-entry]:visible").count(), 0);
  await manufacturer.selectOption("deepseek");
  assert.equal(await page.locator("[data-provider-catalog-entry]:visible").count(), 1);
  const selected = page.locator('[data-provider-catalog-entry="deepseek"]');
  await selected.waitFor();
  await layout(page, tag + "-provider-selected");
  const catalogCreate = selected.locator("[data-provider-catalog-create-form]");
  await catalogCreate.waitFor();
  const providerCreatedResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/providers/from-catalog"
    && response.request().method() === "POST");
  await catalogCreate.locator('button[type="submit"]').click();
  assert.equal((await providerCreatedResponse).status(), 201);
  // The catalog instance is active immediately. A real Provider key remains a
  // separate customer-owned prerequisite and is intentionally not synthesized here.
  await page.locator("[data-provider-key-open]").waitFor();
  checks.providerCreatedFromCatalog = true;
  checks.providerProgressiveConfiguration = true;

  const ownerWorkspace = await browserJson(page, "/workspace/session");
  assert.equal(ownerWorkspace.status, 200);
  await page.goto(origin + "/identity.html?view=account#members");
  const inviteForm = page.locator("[data-membership-invitation-form]");
  await inviteForm.waitFor();
  const invitedEmail = "s1-invited-" + tag + "-" + randomUUID().slice(0, 8) + "@example.test";
  const invitedPassword = randomUUID() + "!Aa9";
  await inviteForm.locator('[name="email"]').fill(invitedEmail);
  await inviteForm.locator('[name="role"]').selectOption("developer");
  const createdResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname.endsWith("/membership-invitations")
    && response.request().method() === "POST");
  await inviteForm.locator('button[type="submit"]').click();
  const created = await createdResponse;
  assert.equal(created.status(), 201);
  const token = (await created.json()).accept_token;
  assert.ok(/^[A-Za-z0-9_-]{43}$/u.test(token));

  const invitedContext = await browser.newContext({ viewport, serviceWorkers: "block" });
  invitedContext.setDefaultTimeout(12_000);
  await invitedContext.route("**/*", (route) => {
    if (new URL(route.request().url()).origin === origin) return route.continue();
    report.blockedExternalRequests += 1;
    return route.abort();
  });
  const invited = await invitedContext.newPage();
  setCurrentPage(invited);
  invited.on("pageerror", () => { report.pageErrors += 1; });
  invited.on("response", (response) => {
    const url = new URL(response.url());
    if (response.status() >= 500) report.apiResponses.push({
      path: url.pathname.replace(/\/invitations\/[^/]+\/accept/gu, "/invitations/token/accept")
        .replace(/[0-9a-f]{32,}/gu, "id"), status: response.status(),
    });
  });
  try {
    await invited.goto(origin + "/#accept-invitation/" + token);
    const acceptanceForm = invited.locator("#invitation-accept-form");
    await acceptanceForm.waitFor();
    assert.equal(new URL(invited.url()).hash, "#accept-invitation");
    assert.match(await invited.locator(".invitation-recipient").textContent(), new RegExp(invitedEmail, "u"));
    assert.ok((await invited.locator(".invitation-recipient").textContent()).includes(ownerWorkspace.body.context.name));
    await layout(invited, tag + "-invitation-recipient");
    checks.invitationRecipientVisible = true;
    await acceptanceForm.locator('[name="displayName"]').fill("Invited Developer " + tag);
    await acceptanceForm.locator('[name="password"]').fill(invitedPassword);
    await acceptanceForm.locator('[name="confirmPassword"]').fill(invitedPassword);
    const acceptedResponse = invited.waitForResponse((response) =>
      new URL(response.url()).pathname.endsWith("/accept") && response.request().method() === "POST");
    const loginResponse = invited.waitForResponse((response) =>
      new URL(response.url()).pathname === "/identity/login");
    await acceptanceForm.locator('button[type="submit"]').click();
    assert.equal((await acceptedResponse).status(), 201);
    assert.equal((await loginResponse).status(), 200);
    await invited.waitForURL("**/console.html*");
    await invited.locator(".console-shell").waitFor();
    const me = await browserJson(invited, "/identity/me");
    const session = await browserJson(invited, "/workspace/session");
    assert.equal(me.status, 200);
    assert.equal(me.body.account.email, invitedEmail);
    assert.equal(session.status, 200);
    assert.equal(session.body.context.workspace_id, ownerWorkspace.body.context.workspace_id);
    assert.equal(session.body.context.role, "developer");
    assert.equal(await invited.locator('[data-identity-login-form],#login-form').count(), 0);
    await layout(invited, tag + "-invited-console");
    checks.invitedGlobalAndWorkspaceSession = true;
    await invited.locator("#logout-button").click();
    await invited.waitForURL(origin + "/");
    await invited.locator("#home-title").waitFor();
    const loggedOut = await browserJson(invited, "/identity/me");
    assert.equal(loggedOut.status, 401);
    await invited.goto(origin + "/console.html");
    // The guarded redirect carries a diagnostic reason/request_id query string;
    // validate the login surface without rejecting that security context.
    await invited.waitForURL((url) => (
      url.pathname === "/identity.html" && url.searchParams.get("view") === "login"
    ));
    await invited.locator("[data-identity-login-form]").waitFor();
    checks.logoutHomeAndModernLogin = true;
    checks.status = "passed";
  } finally {
    await invitedContext.close();
    setCurrentPage(page);
  }
}
