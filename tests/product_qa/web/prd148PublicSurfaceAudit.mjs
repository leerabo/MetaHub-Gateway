/** Read-only screenshot and interaction-surface audit for every public HTML entry. */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import fs from "node:fs/promises";
import path from "node:path";

const { chromium } = createRequire(import.meta.url)(
  path.resolve(".tools/product-qa-browser/node_modules/playwright"),
);
const origin = process.env.METAHUB_PUBLIC_QA_URL ?? "http://127.0.0.1:5173";
const artifactRoot = path.resolve(
  process.env.METAHUB_PUBLIC_SURFACE_ROOT ?? ".artifacts/product-qa/20260928-public-surface-r1",
);
const reportPath = path.resolve(
  process.env.METAHUB_PUBLIC_SURFACE_REPORT ?? "outputs/prd148-public-surface-20260928-r1.json",
);
const pages = ["index.html", "identity.html", "privacy.html", "status.html", "partner.html", "platform.html"];
const viewports = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
];

await fs.mkdir(artifactRoot, { recursive: true });
const browser = await chromium.launch({ headless: true });
const report = { schema: "metahub.prd148.public-surface-audit.v1", origin, pages: [], passed: false };

function named(element) {
  const labels = [...(element.labels ?? [])].map((label) => label.textContent?.trim()).filter(Boolean);
  return Boolean(element.textContent?.trim() || element.getAttribute("aria-label")?.trim()
    || element.getAttribute("title")?.trim() || labels.length
    || element.getAttribute("aria-labelledby")?.split(/\s+/u).some((id) => document.getElementById(id)?.textContent?.trim()));
}

try {
  for (const pageName of pages) {
    for (const viewport of viewports) {
      const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, serviceWorkers: "block" });
      const page = await context.newPage();
      const responseErrors = [];
      const requestFailures = [];
      const consoleErrors = [];
      page.on("console", (message) => {
        if (message.type() === "error" && !/status of 401|status of 403/iu.test(message.text())) consoleErrors.push(message.text());
      });
      page.on("response", (response) => {
        const pathname = new URL(response.url()).pathname;
        if (response.status() >= 500 || ((pathname.includes("/src/") || pathname.endsWith(".html")) && response.status() >= 400)) {
          responseErrors.push(`${response.request().method()} ${pathname}:${response.status()}`);
        }
      });
      page.on("requestfailed", (request) => requestFailures.push(`${request.method()} ${new URL(request.url()).pathname}`));
      let facts;
      try {
        await page.goto(`${origin}/${pageName}`, { waitUntil: "domcontentloaded", timeout: 20_000 });
        await page.waitForTimeout(500);
        await page.locator("body").waitFor({ state: "visible", timeout: 10_000 });
        facts = await page.evaluate(() => {
          const visible = (element) => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
          };
          const controls = [...document.querySelectorAll("button, input, select, textarea, a[href]")].filter(visible);
          const links = controls.filter((element) => element.tagName === "A");
          const counts = new Map();
          for (const link of links) counts.set(link.getAttribute("href"), (counts.get(link.getAttribute("href")) ?? 0) + 1);
          const duplicateLinks = [...counts.entries()].filter(([, count]) => count > 1).map(([href, count]) => ({ href, count }));
          const unnamedCommands = controls.filter((element) => (element.tagName === "BUTTON" || element.tagName === "A")
            && !element.textContent?.trim() && !element.getAttribute("aria-label")?.trim()
            && !element.getAttribute("title")?.trim() && !element.getAttribute("aria-labelledby")?.trim());
          const unlabeledInputs = controls.filter((element) => /^(INPUT|SELECT|TEXTAREA)$/u.test(element.tagName)
            && !element.getAttribute("aria-label")?.trim() && !element.getAttribute("aria-labelledby")?.trim()
            && !(element.labels?.length));
          return {
            width: innerWidth,
            height: innerHeight,
            documentWidth: document.documentElement.scrollWidth,
            viewportWidth: document.documentElement.clientWidth,
            controls: controls.length,
            links: links.length,
            buttons: controls.filter((element) => element.tagName === "BUTTON").length,
            unnamedCommands: unnamedCommands.length,
            unlabeledInputs: unlabeledInputs.length,
            duplicateLinks,
            clippedControls: controls.filter((element) => element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1).length,
            textSample: document.body.innerText.replace(/\s+/gu, " ").trim().slice(0, 260),
          };
        });
        const screenshot = path.join(artifactRoot, `${pageName.replace(".html", "")}-${viewport.name}.png`);
        await page.screenshot({ path: screenshot, fullPage: true });
        facts.screenshot = path.relative(process.cwd(), screenshot);
      } catch (error) {
        facts = { failure: String(error.message) };
      } finally {
        await context.close();
      }
      report.pages.push({ page: pageName, viewport: `${viewport.width}x${viewport.height}`, ...facts,
        responseErrors, requestFailures, consoleErrors });
    }
  }
} finally {
  await browser.close();
}

report.passed = report.pages.length === pages.length * viewports.length
  && report.pages.every((item) => !item.failure && item.documentWidth === item.viewportWidth
    && item.clippedControls === 0 && item.unnamedCommands === 0 && item.unlabeledInputs === 0
    && item.responseErrors.length === 0 && item.requestFailures.length === 0 && item.consoleErrors.length === 0);
await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
process.stdout.write(JSON.stringify({ pages: report.pages.length, passed: report.passed }));
