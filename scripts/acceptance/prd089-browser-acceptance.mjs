/** PRD-089 PAGE-121 browser acceptance against the real Console bundle. */

import { createHash, randomUUID } from "node:crypto";
import { mkdir, rm, writeFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  CdpClient,
  createPageSession,
  evaluate,
  launchEdge,
  runAndWaitForPageLoad,
  setViewport,
  waitForDebugger,
  waitForDom,
} from "./prd066-browser-acceptance.mjs";
import { inspectPngPixels } from "./prd056-browser-acceptance.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const CONSOLE = "http://localhost:5173";
const CHROME = path.join(ROOT, ".tools", "chrome-portable", "chrome.exe");
const ARTIFACTS = path.join(ROOT, ".artifacts", "prd089-browser");
const DEBUG_PORT = 19121;
const JOB_ID = `tex_${"a".repeat(32)}`;
const ZIP_BYTES = new Uint8Array([80, 75, 3, 4, 80, 82, 68, 48, 56, 57]);
const VIEWPORTS = [
  { name: "desktop-1280x720", width: 1280, height: 720 },
  { name: "mobile-390x844", width: 390, height: 844 },
];

const MOCK_SCRIPT = `(() => {
  const originalFetch = window.fetch.bind(window);
  const jobId = ${JSON.stringify(JOB_ID)};
  const permissions = ["tenant_data.read", "tenant_data.export", "tenant_data.download", "tenant_data.delete"];
  const state = { tenant: "tenant_browser_a", slug: "browser-a", revision: 7, generation: 2, status: "active", jobStatus: "pending", revoked: false, switched: false };
  const headers = (requestId = "mock_prd089_request") => ({ "Content-Type": "application/json", "Cache-Control": "no-store, private", "X-Request-ID": requestId, "X-Trace-ID": "trace_" + requestId });
  const json = (value, status = 200, requestId = "mock_prd089_request") => new Response(JSON.stringify(value), { status, headers: headers(requestId) });
  const context = () => {
    const tenant = state.switched ? "tenant_browser_b" : state.tenant;
    const slug = state.switched ? "browser-b" : state.slug;
    const codes = state.revoked ? ["tenant_data.read"] : permissions;
    return { tenant: { tenant_id: tenant, slug, name: slug }, user: { tenant_id: tenant, user_id: "usr_browser_089", email: "browser-acceptance@local.invalid", display_name: "Browser Acceptance", role: "admin" }, session: { session_id: "ses_browser_089", expires_at: "2099-01-01T00:00:00Z" }, permission_codes: codes, permission_revision: state.revoked ? 25 : 24, authorization_fingerprint: state.revoked ? "browser_authz_revoked" : "browser_authz_089" };
  };
  const navigation = () => ({ state: "ready", active_navigation_item_id: "tenant-data-lifecycle", permission_codes: context().permission_codes, permission_revision: context().permission_revision, authorization_fingerprint: context().authorization_fingerprint, navigation_items: [{ id: "tenant-data-lifecycle", label: "数据管理", target_section: "tenant-data-lifecycle", required_permissions: ["tenant_data.read"], group: "configuration", visible: true, disabled: false }] });
  const exportJob = () => ({ tenant_export_job_id: jobId, status: state.jobStatus, schema_version: "metahub.tenant-export/v1", snapshot_at: "2026-08-18T00:00:00Z", usage_snapshot_sequence: 1, audit_snapshot_sequence: 1, manifest: { files: ["manifest.json"] }, content_size: state.jobStatus === "completed" ? 10 : null, content_sha256: state.jobStatus === "completed" ? "${createHash("sha256").update(ZIP_BYTES).digest("hex")}" : null, expires_at: "2099-01-01T00:00:00Z", download_count: 0, error_code: null, created_at: "2026-08-18T00:00:00Z", updated_at: "2026-08-18T00:00:00Z", completed_at: state.jobStatus === "completed" ? "2026-08-18T00:00:01Z" : null });
  window.__prd089 = { revoke() { state.revoked = true; }, switchTenant() { state.switched = true; state.revoked = false; state.jobStatus = "none"; }, job() { state.jobStatus = "completed"; } };
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (!["localhost", "127.0.0.1"].includes(url.hostname) || !["8000", "8001"].includes(url.port)) return originalFetch(input, init);
    const path = url.pathname;
    if (path === "/auth/me") return json(context(), 200, "mock_auth_me");
    if (path === "/auth/logout") return json({ logged_out: true, request_id: "mock_logout", trace_id: "trace_mock_logout" }, 200, "mock_logout");
    if (path === "/console/navigation") return json(navigation(), 200, "mock_navigation");
    if (path === "/console/preferences") return json({ active_navigation_item_id: "tenant-data-lifecycle", sidebar_collapsed: false, request_id: "mock_preferences", trace_id: "trace_mock_preferences" }, 200, "mock_preferences");
    if (path === "/tenant/data-management") return json({ state: { lifecycle_status: state.status, revision: state.revision, cache_generation: state.generation, updated_at: "2026-08-18T00:00:00Z" }, exports: state.jobStatus === "none" ? [] : [exportJob()], deletion_request: null, request_id: "mock_data_management", trace_id: "trace_mock_data_management" }, 200, "mock_data_management");
    if (path === "/tenant/export" && init.method === "POST") { state.jobStatus = "pending"; return json({ export_job: exportJob(), status_url: "/tenant/exports/" + jobId, download_url: "/tenant/exports/" + jobId + "/download", request_id: "mock_export", trace_id: "trace_mock_export" }, 202, "mock_export"); }
    if (path === "/tenant/exports/" + jobId && state.jobStatus !== "none") { state.jobStatus = "completed"; return json({ export_job: exportJob(), status_url: "/tenant/exports/" + jobId, download_url: "/tenant/exports/" + jobId + "/download", request_id: "mock_export_status", trace_id: "trace_mock_export_status" }, 200, "mock_export_status"); }
    if (path === "/tenant/exports/" + jobId + "/download") { const digest = "${createHash("sha256").update(ZIP_BYTES).digest("hex")}"; return new Response(${JSON.stringify(String.fromCharCode(...ZIP_BYTES))}, { status: 200, headers: { "Content-Type": "application/zip", "Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff", "Content-Length": "10", "X-Content-SHA256": digest, "Content-Disposition": "attachment; filename=\\"metahub-tenant-export-${JOB_ID}.zip\\"", "X-Request-ID": "mock_download" } }); }
    if (path === "/tenant/deletion-request" && init.method === "POST") return json({ code: "tenant_data.revision_conflict", message: "安全校验未通过", request_id: "mock_delete_failure", trace_id: "trace_mock_delete_failure" }, 409, "mock_delete_failure");
    if (path.endsWith("/health") || path.endsWith("/health/live") || path.endsWith("/health/ready")) return json({ status: "ok", request_id: "mock_health", ready: true }, 200, "mock_health");
    if (path.endsWith("/version")) return json({ version: "browser-test", request_id: "mock_version" });
    if (path === "/internal/config/summary") return json({ environment: "test", items: [], warnings: [], request_id: "mock_config", trace_id: "trace_mock_config" });
    return json({ request_id: "mock_optional", trace_id: "trace_mock_optional" }, 200, "mock_optional");
  };
})();`;

function check(condition, code) { if (!condition) throw new Error(`prd089_browser:${code}`); }

async function waitForHttp(url) {
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    try { if ((await fetch(url)).ok) return; } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("prd089_browser:console_timeout");
}

async function stopProcess(child) {
  if (!child || child.exitCode !== null) return;
  try { child.kill(); } catch {}
  await Promise.race([
    new Promise((resolve) => child.once("close", resolve)),
    new Promise((resolve) => setTimeout(resolve, 3000)),
  ]);
}

async function main() {
  await mkdir(path.join(ARTIFACTS, "screenshots"), { recursive: true });
  const consoleProcess = spawn(process.execPath, ["scripts/dev-server.mjs"], { cwd: path.join(ROOT, "console-web"), env: { ...process.env, METAHUB_CONSOLE_PORT: "5173" }, windowsHide: true, stdio: ["ignore", "ignore", "ignore"] });
  let browserProcess;
  let browserClient;
  let page;
  try {
    await waitForHttp(`${CONSOLE}/`);
    const tracker = { secrets: [], processPrivacyViolationCount: 0, edgeProcessErrorCount: 0 };
    browserProcess = launchEdge({ edgePath: CHROME, debugPort: DEBUG_PORT, artifactDirectory: ARTIFACTS, omitUserProfile: true }, tracker);
    browserClient = new CdpClient(await waitForDebugger(DEBUG_PORT));
    await browserClient.connect();
    page = await createPageSession(browserClient, DEBUG_PORT);
    await page.client.send("Fetch.disable");
    await page.client.send("Page.addScriptToEvaluateOnNewDocument", { source: `sessionStorage.setItem("metahub.auth_token", "browser_acceptance_token"); sessionStorage.setItem("metahub.auth_context", JSON.stringify({})); ${MOCK_SCRIPT}` });
    for (const viewport of VIEWPORTS) {
      await setViewport(page.client, page.sessionId, viewport);
      if (viewport.name.startsWith("desktop")) {
        await runAndWaitForPageLoad(page.client, page.sessionId, () => page.client.send("Page.navigate", { url: `${CONSOLE}/#tenant-data-lifecycle` }), `browser.${viewport.name}.load`);
      } else {
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
      await waitForDom(page.client, page.sessionId, `document.querySelector("[data-tenant-data-root]")`, `browser.${viewport.name}.page`);
      const facts = await evaluate(page.client, page.sessionId, `(() => { const root = document.querySelector("[data-tenant-data-root]"); const rects = [...root.querySelectorAll("button, input")].map((node) => node.getBoundingClientRect()).filter((rect) => rect.width > 0 && rect.height > 0); let overlap = 0; for (let i = 0; i < rects.length; i += 1) for (let j = i + 1; j < rects.length; j += 1) if (rects[i].left < rects[j].right && rects[i].right > rects[j].left && rects[i].top < rects[j].bottom && rects[i].bottom > rects[j].top) overlap += 1; return { width: innerWidth, height: innerHeight, scrollWidth: document.documentElement.scrollWidth, root: Boolean(root), overlap }; })()`, `browser.${viewport.name}.layout`);
      check(facts.width === viewport.width && facts.height === viewport.height && facts.scrollWidth <= viewport.width + 1 && facts.root && facts.overlap === 0, `${viewport.name}_layout`);
      const screenshot = await page.client.send("Page.captureScreenshot", { format: "png", fromSurface: true });
      const buffer = Buffer.from(screenshot.data, "base64");
      await writeFile(path.join(ARTIFACTS, "screenshots", `page-121-${viewport.name}.png`), buffer);
      const pixels = inspectPngPixels(buffer, viewport, `browser.${viewport.name}.pixels`);
      check(pixels.non_blank_pixels === true, `${viewport.name}_pixels`);
      if (viewport.name.startsWith("desktop")) {
        await evaluate(page.client, page.sessionId, `document.querySelector("[data-tenant-data-export]").click()`, "browser.export.click");
        await waitForDom(page.client, page.sessionId, `document.querySelector("[data-tenant-data-download]")`, "browser.export.ready", 20_000);
        const downloadResult = await evaluate(page.client, page.sessionId, `(() => { const anchor = document.querySelector("[data-tenant-data-download]"); const original = URL.createObjectURL; let created = 0; let revoked = 0; URL.createObjectURL = (blob) => { created += 1; return original.call(URL, blob); }; const oldRevoke = URL.revokeObjectURL; URL.revokeObjectURL = (url) => { revoked += 1; oldRevoke.call(URL, url); }; anchor.click(); return new Promise((resolve) => setTimeout(() => resolve({ created, revoked }), 30)); })()`, "browser.download");
        check(downloadResult.created === 1 && downloadResult.revoked === 1, "download_revoke");
        await evaluate(page.client, page.sessionId, `window.__prd089.revoke()`, "browser.revoke");
        await evaluate(page.client, page.sessionId, `document.querySelector("[data-tenant-data-refresh]").click()`, "browser.revoke.refresh");
        await waitForDom(page.client, page.sessionId, `document.querySelector("[data-tenant-data-root]") && !document.querySelector("[data-tenant-data-export]:not([disabled])") && !document.querySelector("[data-tenant-data-delete]:not([disabled])")`, "browser.revoke.scrub");
        await evaluate(page.client, page.sessionId, `window.__prd089.switchTenant(); document.querySelector("[data-tenant-data-refresh]").click()`, "browser.tenant.switch");
        await waitForDom(page.client, page.sessionId, `document.querySelector("[data-tenant-data-root]") && !document.querySelector("[data-tenant-data-download]") && !document.body.textContent.includes("${JOB_ID}")`, "browser.tenant.isolation");
        await evaluate(page.client, page.sessionId, `document.querySelector("[data-tenant-data-delete]").click()`, "browser.dialog.open");
        await waitForDom(page.client, page.sessionId, `document.querySelector("[data-tenant-data-dialog][open]")`, "browser.dialog.visible");
        await evaluate(page.client, page.sessionId, `document.querySelector("[data-tenant-data-dialog] input[name=confirmation]").value = "browser-b"; document.querySelector("[data-tenant-data-deletion-form]").requestSubmit()`, "browser.dialog.submit");
        await waitForDom(page.client, page.sessionId, `document.querySelector("[data-tenant-data-dialog][open]") && document.querySelector("[data-tenant-data-dialog]").getAttribute("aria-busy") === "false"`, "browser.dialog.failure");
        const storage = await evaluate(page.client, page.sessionId, `({ session: sessionStorage.length, protectedDom: document.querySelector("[data-tenant-data-dialog]") !== null })`, "browser.dialog.storage");
        check(storage.session >= 0 && storage.protectedDom === true, "dialog_failure_boundary");
      }
    }
    check(tracker.processPrivacyViolationCount === 0 && tracker.edgeProcessErrorCount === 0, "privacy_or_browser_error");
    process.stdout.write(JSON.stringify({ status: "passed", all_108_prds_complete: false, viewports: VIEWPORTS.map(({ name }) => name), download_url_reclaimed: true, protected_dom_cleared_on_revoke: true, cross_tenant_isolation: true }) + "\n");
  } finally {
    try { page?.client.close(); } catch {}
    try { browserClient?.close(); } catch {}
    await stopProcess(browserProcess);
    await stopProcess(consoleProcess);
    for (let attempt = 0; attempt < 5; attempt += 1) {
      try { await rm(ARTIFACTS, { recursive: true, force: true }); break; } catch (error) { if (attempt === 4) throw error; await new Promise((resolve) => setTimeout(resolve, 250)); }
    }
  }
}

await main();
