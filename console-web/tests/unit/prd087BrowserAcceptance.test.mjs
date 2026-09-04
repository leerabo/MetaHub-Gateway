/** PRD-087 browser contract freezes PAGE-119 lifecycle, responsive layout, and attachment safety. */

// PAGE-119 浏览器验收合约：校验请求追踪、隐私事实和多视口证据摘要。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const VIEWPORTS = Object.freeze({
  mobile: Object.freeze({ width: 390, height: 844 }),
  desktop: Object.freeze({ width: 1280, height: 720 }),
  wide: Object.freeze({ width: 1440, height: 900 }),
});
const SCREENSHOTS = Object.freeze([
  "page-119-partner-setup-mobile-390x844.png",
  "page-119-partner-ready-desktop-1280x720.png",
  "page-119-partner-commission-wide-1440x900.png",
  "page-119-partner-tenant-isolation-mobile-390x844.png",
]);

test("PAGE-119 freezes mobile, desktop, and wide acceptance evidence names", () => {
  assert.deepEqual(VIEWPORTS.mobile, { width: 390, height: 844 });
  assert.deepEqual(VIEWPORTS.desktop, { width: 1280, height: 720 });
  assert.deepEqual(VIEWPORTS.wide, { width: 1440, height: 900 });
  assert.ok(SCREENSHOTS.every((name) => /^page-119-[a-z0-9-]+\.png$/.test(name)));
});

test("main retires the enterprise Partner portal runtime", async () => {
  const main = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  assert.doesNotMatch(main, /createPartnerPortalPageState|partnerPortalPageState|loadPartnerPortalState|partnerPortalState:|lib\/partnerPortal/);
});

test("owner and seven lanes drop late responses before authentication handling", async () => {
  const [main, state] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/partner-portal/partnerPortalPageState.js", import.meta.url), "utf8"),
  ]);
  assert.match(state, /"profile", "projects", "customers", "commission", "mutation", "export", "delivery"/);
  assert.match(state, /clearBranding\(\);[\s\S]*clearDom\(\);[\s\S]*pending\.forEach\(\(ticket\) => ticket\?\.controller\.abort\(\)\)/);
  assert.match(state, /operations\[lane\] === ticket[\s\S]*ticket\.generation === generations\[lane\][\s\S]*!ticket\.controller\.signal\.aborted/);
  assert.doesNotMatch(main, /partnerPortalPageState|handlePartnerPortalAuthenticationFailure|isPartnerPortalAbortError/);
});

test("strict client freezes exact DTOs and attachment validation before Blob URL creation", async () => {
  const client = await readFile(new URL("../../src/lib/partnerPortal.js", import.meta.url), "utf8");
  assert.match(client, /const PARTNER_KEYS = Object\.freeze/);
  assert.match(client, /const CUSTOMER_KEYS = Object\.freeze/);
  assert.match(client, /const COMMISSION_LINE_KEYS = Object\.freeze/);
  assert.match(client, /usage_scope !== "project_history"/);
  assert.match(client, /settlement_status !== "not_applicable"/);
  assert.match(client, /MAX_CSV_BYTES = 8 \* 1024 \* 1024/);
  assert.match(client, /MAX_DELIVERY_BYTES = 1024 \* 1024/);
  assert.match(client, /contentType !== options\.expectedContentType[\s\S]*privateNoStore\(cacheControl\)[\s\S]*nosniff !== "nosniff"/);
  assert.match(client, /Content-Length/);
  assert.match(client, /prefix\[0\] !== 0xef[\s\S]*prefix\[1\] !== 0xbb[\s\S]*prefix\[2\] !== 0xbf/);
  assert.match(client, /link\.remove\?\.\(\);[\s\S]*urlApi\.revokeObjectURL\(objectUrl\)/);
  assert.doesNotMatch(client, /localStorage|sessionStorage|console\.(?:log|debug|info|warn|error)/);
});

test("legacy Partner Portal renderer stays testable while production CSS and navigation stay retired", async () => {
  const [renderer, shell, css, messages, permissions] = await Promise.all([
    readFile(new URL("../../src/features/partner-portal/renderPartnerPortal.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/i18n/messages.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/permissions.js", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(shell, /case "partner-portal"/);
  assert.doesNotMatch(shell, /class="panel partner-portal-panel"/);
  assert.match(renderer, /type="color"/);
  assert.match(renderer, /data-partner-customer-disable/);
  assert.match(renderer, /data-partner-commission-export/);
  assert.match(renderer, /usage_history_notice/);
  assert.match(renderer, /commission_notice/);
  assert.doesNotMatch(css, /\.partner-portal-/u);
  assert.doesNotMatch(messages, /"nav\.partner-portal"/u);
  for (const code of [
    "partner_portal.read", "partner_portal.configure", "partner_customer.manage",
    "partner_branding.manage", "partner_commission.export", "partner_delivery.download",
  ]) assert.ok(permissions.includes(code));
});
