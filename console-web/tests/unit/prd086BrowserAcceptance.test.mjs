/** PRD-086 浏览器合同冻结 PAGE-118 生命周期、视口、布局与证据安全接线。 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

// 验收视口与截图清单固定桌面、宽屏和移动端证据，防止只验证单一尺寸。
const VIEWPORTS = Object.freeze({
  desktop: Object.freeze({ width: 1280, height: 720 }),
  wide: Object.freeze({ width: 1440, height: 900 }),
  mobile: Object.freeze({ width: 390, height: 844 }),
});
const SCREENSHOTS = Object.freeze([
  "page-118-customer-health-healthy-desktop-1280x720.png",
  "page-118-customer-health-high-risk-wide-1440x900.png",
  "page-118-customer-health-no-samples-mobile-390x844.png",
  "page-118-customer-health-tenant-isolation-mobile-390x844.png",
]);

test("PAGE-118 freezes 390, 1280, and 1440 viewport evidence contracts", () => {
  assert.deepEqual(VIEWPORTS.mobile, { width: 390, height: 844 });
  assert.deepEqual(VIEWPORTS.desktop, { width: 1280, height: 720 });
  assert.deepEqual(VIEWPORTS.wide, { width: 1440, height: 900 });
  assert.equal(SCREENSHOTS.length, 4);
  assert.ok(SCREENSHOTS.every((name) => /^page-118-[a-z0-9-]+\.png$/.test(name)));
});

test("PRD-140 cutover removes legacy main wiring and gives Platform an independent owner", async () => {
  const [main, platformMain, platformState] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/platformPageState.js", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(main, /customerHealth|customer-health|\/customer-health/);
  assert.match(platformMain, /from "\.\.\/\.\.\/lib\/platformCustomerSuccess\.js"/);
  assert.match(platformMain, /customerAccounts: \(input\) => listPlatformCustomerAccounts/);
  assert.match(platformMain, /customerHealth: \(input\) => getPlatformCustomerHealth/);
  assert.match(platformState, /createPlatformCustomerSuccessController\(\{/);
  assert.match(platformState, /state\.view === "customer-success"[\s\S]{0,300}customerSuccessController\.enter/);
  assert.match(platformState, /function clearProtectedFacts\(\) \{[\s\S]{0,150}customerSuccessController\.leave\(\)/);
});

test("legacy and Platform owners both gate late results before protected state can change", async () => {
  const [pageState, platformStore, platformController] = await Promise.all([
    readFile(new URL("../../src/features/customer-health/customerHealthPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform-customer-success/platformCustomerSuccessStore.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform-customer-success/platformCustomerSuccessController.js", import.meta.url), "utf8"),
  ]);
  assert.match(pageState, /operation === ticket[\s\S]*ticket\.generation === generation[\s\S]*!ticket\.controller\.signal\.aborted/);
  assert.match(pageState, /try \{[\s\S]*clearDom\(\)[\s\S]*finally \{[\s\S]*pending\?\.controller\.abort\(\)/);
  assert.match(platformStore, /requests\.get\(ticket\.lane\) === ticket[\s\S]{0,250}ticket\.epoch === epoch[\s\S]{0,250}ticket\.owner === owner[\s\S]{0,250}!ticket\.controller\.signal\.aborted/);
  assert.match(platformStore, /function leave\(\) \{[\s\S]{0,250}clearRequests\(\)[\s\S]{0,250}owner = ""[\s\S]{0,250}state = initialState\(\)/);
  assert.match(platformController, /if \(!store\.reject\(ticket, error\)\) return false;[\s\S]{0,300}error\.status === 401[\s\S]{0,200}leave\(\)[\s\S]{0,200}onSessionInvalid\(error\)/);
  assert.match(platformController, /error\.status === 403[\s\S]{0,200}onPermissionRevoked\(error\)/);
});

test("strict client freezes the six-factor DTO and never composes source-domain APIs", async () => {
  const client = await readFile(new URL("../../src/lib/customerHealth.js", import.meta.url), "utf8");
  assert.match(client, /"usage_trend"[\s\S]*"reliability"[\s\S]*"cost_anomaly_health"[\s\S]*"budget_readiness"[\s\S]*"team_activity"[\s\S]*"ticket_health"/);
  assert.match(client, /const EFFECTIVE_WEIGHT_BPS = 9000/);
  assert.match(client, /BigInt\(signal\.score_bps\) \* BigInt\(signal\.weight_bps\)/);
  assert.match(client, /support_ticket_unconfigured/);
  assert.match(client, /fetch\(url, \{[\s\S]*cache: "no-store"[\s\S]*credentials: "omit"[\s\S]*redirect: "error"/);
  assert.match(client, /`\$\{controlApiUrl\}\/customer-health`/);
  assert.doesNotMatch(client, /\/(usage|budgets|users|cost-anomalies)(?:["'`/?])/);
});

test("renderer and CSS keep the six-column workbench safe at 1440/1280 and single-column at 390", async () => {
  const [renderer, shell, css, messages] = await Promise.all([
    readFile(new URL("../../src/features/customer-health/renderCustomerHealth.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/i18n/messages.js", import.meta.url), "utf8"),
  ]);
  assert.match(shell, /case "customer-health"/);
  assert.match(shell, /class="panel customer-health-panel"/);
  assert.match(renderer, /authorizedActionTarget\(action\.target_navigation_item_id, consoleShellState\)/);
  assert.match(renderer, /item\?\.target_section === normalized[\s\S]*item\?\.visible === true[\s\S]*item\?\.disabled === false/);
  assert.match(renderer, /escapeHtml\(error\?\.code[\s\S]*escapeHtml\(error\?\.requestId/);
  assert.doesNotMatch(renderer, /innerHTML\s*=|insertAdjacentHTML/);
  assert.match(css, /\.customer-health-row\s*\{[\s\S]*grid-template-columns:[\s\S]*minmax\(190px, 1\.7fr\)/);
  assert.match(css, /@media \(max-width: 620px\)[\s\S]*\.customer-health-row:not\(\.customer-health-table-head\)[\s\S]*grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(css, /\.customer-health-row > \*[\s\S]*overflow-wrap: anywhere/);
  assert.match(messages, /"nav\.customer-health"/);
  assert.match(messages, /"customer_health\.action\.connect_support_ticket_source"/);
});
