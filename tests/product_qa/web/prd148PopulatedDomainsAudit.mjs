/** Isolated real-login audit. API-created QA data is setup, never GA evidence. */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import path from "node:path";
import { mkdir } from "node:fs/promises";
import { measureInteractiveLayout } from "./layoutMetrics.mjs";
const { chromium } = createRequire(import.meta.url)(path.resolve(".tools/product-qa-browser/node_modules/playwright"));
const controlPort = Number(process.env.METAHUB_QA_CONTROL_PORT);
const consolePort = Number(process.env.METAHUB_QA_CONSOLE_PORT);
assert.ok(controlPort > 0 && consolePort > 0);
const origin = `http://127.0.0.1:${consolePort}`;
const root = path.resolve(process.env.METAHUB_QA_ARTIFACT_ROOT, "populated-screenshots");
await mkdir(root, { recursive: true });
const report = { schema: "metahub.prd148.populated-domains.v1", scope: "Disposable seeded accounts, real login and APIs; no live device or commercial acceptance", roles: [], findings: [], passed: false };
const browser = await chromium.launch({ headless: true });
let ticketId;
async function supportWorkflow(page, role) {
  const result = { role, actions: [], passed: false };
  const root = page.locator("[data-support-ticket-root]");
  const create = root.locator("[data-support-ticket-create-form]");
  if (role === "viewer") {
    assert.equal(await create.count(), 0, "viewer_must_not_create_tickets");
    const denied = await page.evaluate(async () => {
      const api = await import("/src/lib/customerSupport.js");
      try {
        await api.createSupportTicket({ controlApiUrl: "http://127.0.0.1:8001", category: "integration", severity: "p3", subject: "Unauthorized QA ticket", message: "Must be rejected", idempotencyKey: api.createSupportIdempotencyKey("denial") });
        return { status: 200 };
      } catch (error) { return { status: error.status, code: error.code }; }
    });
    assert.equal(denied.status, 403);
    result.actions.push({ name: "viewer_create_denied", ...denied });
    result.passed = true;
    return result;
  }
  const createDetails = root.locator("details.support-ticket-create");
  if (await createDetails.count() && !(await createDetails.getAttribute("open"))) {
    await createDetails.locator("summary").click();
  }
  async function submit(form, method, action) {
    const responsePromise = page.waitForResponse(r => new URL(r.url()).pathname.startsWith("/support/tickets") && r.request().method() === method, { timeout: 15000 });
    await form.locator("button[type=submit]").click();
    const response = await responsePromise;
    const payload = await response.json();
    result.actions.push({ name: action, status: response.status(), code: payload.code });
    assert.ok(response.ok(), `${action}:${response.status()}:${payload.code}`);
    await root.locator(".support-ticket-feedback.is-success").waitFor({ state: "visible", timeout: 10000 });
    return payload;
  }
  await create.locator("[name=subject]").fill(`QA ${role} browser-created ticket`);
  await create.locator("[name=message]").fill("Created through the rendered support form.");
  const created = await submit(create, "POST", "create");
  assert.ok(created.ticket?.ticket_id);
  await page.getByRole("heading", { name: `QA ${role} browser-created ticket`, exact: true }).waitFor({ state: "visible" });
  const reply = root.locator("[data-support-ticket-reply-form]");
  await reply.locator("[name=message]").fill("QA browser reply persists after refresh.");
  await submit(reply, "PATCH", "reply");
  await root.getByText("QA browser reply persists after refresh.", { exact: true }).waitFor({ state: "visible" });
  const escalation = root.locator("[data-support-ticket-escalation-form]");
  await escalation.locator("[name=confirmed]").check();
  await submit(escalation, "POST", "escalate");
  await submit(root.locator("[data-support-ticket-status-form]"), "PATCH", "resolve");
  const survey = root.locator("[data-support-ticket-survey-form]");
  await survey.locator("[name=survey_comment]").fill("QA survey from real rendered form.");
  await submit(survey, "PATCH", "survey");
  await submit(root.locator("[data-support-ticket-status-form]"), "PATCH", "reopen");
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.locator(`[data-support-ticket-select="${created.ticket.ticket_id}"]`).click();
  await page.locator("[data-support-ticket-detail]").getByText("QA browser reply persists after refresh.", { exact: true }).waitFor({ state: "visible", timeout: 10000 });
  result.actions.push({ name: "reload_persistence", passed: true });
  result.passed = true;
  return result;
}
try {
  for (const role of ["admin", "developer", "viewer"]) {
    const context = await browser.newContext({ viewport: role === "viewer" ? { width: 390, height: 844 } : { width: 1440, height: 900 }, serviceWorkers: "block" });
    const page = await context.newPage();
    const item = { role, api: [], pages: [], findings: [] };
    report.roles.push(item);
    for (const host of ["127.0.0.1", "localhost"]) {
      await page.route(`http://${host}:8001/**`, route => route.continue({ url: route.request().url().replace(`${host}:8001`, `127.0.0.1:${controlPort}`) }));
    }
    try {
      await page.goto(`${origin}/identity.html`, { waitUntil: "domcontentloaded" });
      const form = page.locator("[data-identity-login-form]");
      await form.waitFor({ state: "visible", timeout: 20000 });
      await form.locator("[name=email]").fill(process.env[`METAHUB_QA_${role.toUpperCase()}_EMAIL`]);
      await form.locator("[name=password]").fill(process.env[`METAHUB_QA_${role.toUpperCase()}_PASSWORD`]);
      await form.locator("button[type=submit]").click();
      await page.waitForURL(/console\.html(?:[?#]|$)/u, { timeout: 25000 });
      await page.locator("#console-sidebar").waitFor({ state: "visible", timeout: 20000 });
      await page.waitForLoadState("networkidle", { timeout: 10000 });
      if (role === "admin") {
        const setup = await page.evaluate(async () => {
          const api = await import("/src/lib/customerSupport.js");
          try {
            const result = await api.createSupportTicket({ controlApiUrl: "http://127.0.0.1:8001", category: "integration", severity: "p3", subject: "QA populated support workflow", message: "Disposable browser audit ticket. No customer information.", idempotencyKey: api.createSupportIdempotencyKey("populated-audit") });
            return { passed: true, ticketId: result.ticket.ticket_id };
          } catch (error) { return { passed: false, code: error.code, status: error.status }; }
        });
        item.fixture_setup = setup;
        ticketId = setup.ticketId;
      }
      for (const target of [
        { name: "support", route: "workspace.observability.support", selector: "[data-support-ticket-root]", api: "/support/tickets?limit=50" },
        { name: "device", route: "workspace.gateway.private-deployments", selector: "[data-private-gateway-root]", api: "/edge/deployments" },
      ]) {
        await page.goto(`${origin}/console.html#${target.route}`, { waitUntil: "domcontentloaded" });
        await page.locator(".console-shell").waitFor({ state: "visible", timeout: 15000 });
        await page.waitForLoadState("networkidle", { timeout: 10000 });
        const api = await page.evaluate(async ({ apiPath, ticketId }) => {
          const response = await fetch(`http://127.0.0.1:8001${apiPath}`, { credentials: "include" });
          const body = await response.json();
          const items = body.items ?? body.tickets ?? body.deployments ?? [];
          return { status: response.status, code: body.code, count: items.length, contains_setup_ticket: Boolean(ticketId && items.some(x => x.ticket_id === ticketId)) };
        }, { apiPath: target.api, ticketId });
        item.api.push({ domain: target.name, ...api });
        let visible = true;
        try { await page.locator(target.selector).waitFor({ state: "visible", timeout: 8000 }); } catch { visible = false; }
        const facts = await page.evaluate(selector => ({ active: document.querySelector(".console-shell")?.dataset.activeNavigation, root_present: Boolean(document.querySelector(selector)), forms: document.querySelectorAll(`${selector} form`).length, text: document.querySelector("main")?.innerText ?? "", overflow_px: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth) }), target.selector);
        const geometry = await page.evaluate(measureInteractiveLayout);
        const screenshot = path.join(root, `${role}-${target.name}.png`);
        await page.screenshot({ path: screenshot, fullPage: true });
        item.pages.push({ domain: target.name, visible, ...facts, geometry, screenshot: path.relative(process.cwd(), screenshot) });
        if (facts.overflow_px || geometry.clipped_control_count || geometry.interactive_overlap_count) item.findings.push({ code: "domain_layout_issue", domain: target.name, overflow_px: facts.overflow_px, ...geometry });
        if (api.status === 200 && !visible) item.findings.push({ code: "authorized_domain_controls_missing", domain: target.name, api_status: api.status, setup_ticket_visible_in_api: api.contains_setup_ticket });
        if (api.status === 403 && visible) item.findings.push({ code: "denied_domain_controls_visible", domain: target.name });
        if (target.name === "support" && visible) {
          try { item.support_workflow = await supportWorkflow(page, role); }
          catch (error) { item.support_workflow = { passed: false, failure: String(error.message).slice(0, 600) }; }
        }
      }
      // Explicitly leave the Workspace credential domain before opening Partner.
      // The Partner API intentionally rejects mixed Workspace + Global cookies.
      const handoff = await page.evaluate(async () => {
        const csrf = document.cookie.split(";").map(item => item.trim()).find(item => item.startsWith("__Host-metahub-global-csrf="))?.split("=").slice(1).join("=");
        const response = await fetch("http://127.0.0.1:8001/identity/partner-handoffs", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf ?? "", "Origin": location.origin },
          credentials: "include",
          body: "{}",
        });
        return { status: response.status, body: await response.json() };
      });
      item.partner_handoff = { status: handoff.status, code: handoff.body.code, handed_off: handoff.body.handed_off };
      if (handoff.status !== 200 || handoff.body.handed_off !== true) item.findings.push({ code: "partner_handoff_failed", status: handoff.status, error: handoff.body.code });
      // Normal user navigation after the explicit server-side handoff.
      const partnerResponse = page.waitForResponse(r => new URL(r.url()).pathname === "/partner/workspaces", { timeout: 15000 });
      await page.goto(`${origin}/partner.html`, { waitUntil: "domcontentloaded" });
      const response = await partnerResponse;
      const body = await response.json();
      item.partner_entry = { status: response.status(), code: body.code };
      if (body.code === "partner.credential_conflict") item.findings.push({ code: "normal_workspace_to_partner_entry_conflict", domain: "partner", status: response.status() });
    } catch (error) { item.failure = String(error.message).slice(0, 600); }
    finally { await context.close(); }
  }
  report.findings = report.roles.flatMap(x => x.findings.map(f => ({ role: x.role, ...f })));
  report.support_workflows_passed = report.roles.length === 3 && report.roles.every(x => x.support_workflow?.passed === true);
  report.passed = report.findings.length === 0 && report.support_workflows_passed && report.roles.every(x => !x.failure && (x.role !== "admin" || x.fixture_setup?.passed));
  process.stdout.write(JSON.stringify(report));
} finally { await browser.close(); }
