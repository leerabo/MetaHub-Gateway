/**
 * Read-only static route smoke for the three frontend surfaces.
 *
 * This intentionally does not log in, seed data, or call mutating APIs. It
 * checks that every inventoried URL can load from the production-like local
 * frontend, that theme initialization is real, and that obvious viewport
 * geometry failures are recorded. A result from this tool never means that a
 * backend-authenticated page was visually or functionally accepted.
 */
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = path.resolve(fileURLToPath(new URL("../..", import.meta.url)));
const checklistPath = process.argv[2]
  ? path.resolve(process.argv[2])
  : path.join(root, "output/deployment-ubuntu-20261014/permissions-trial42-20261021/visual-checklist-trial42-inventory.json");
const reportPath = process.argv[3]
  ? path.resolve(process.argv[3])
  : path.join(root, "output/deployment-ubuntu-20261014/ui-static-all-routes-20261022.json");
const origin = "http://localhost:5173";
const viewport = { width: 1280, height: 720 };

const checklist = JSON.parse(await fs.readFile(checklistPath, "utf8"));
const rows = checklist.checklist.filter((row) =>
  ["workspace", "platform", "identity", "identity-auth"].includes(row.surface),
);
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport, locale: "zh-CN" });
const results = [];
const screenshotDir = path.join(path.dirname(reportPath), "ui-static-all-routes-20261022");
await fs.mkdir(screenshotDir, { recursive: true });

for (const row of rows) {
  for (const theme of ["dark", "light"]) {
    // Each pair gets a fresh page so saved-theme bootstrap scripts and stale
    // navigation state cannot leak from one route into the next.
    const page = await context.newPage();
    const consoleErrors = [];
    const pageErrors = [];
    const requestFailures = [];
    const onConsole = (message) => {
      if (message.type() === "error" && !message.text().includes("/api/")) consoleErrors.push(message.text().slice(0, 300));
    };
    const onPageError = (error) => pageErrors.push(String(error).slice(0, 300));
    const onRequestFailed = (request) => {
      if (!request.url().includes("/api/")) requestFailures.push(`${request.method()} ${request.url()}`.slice(0, 300));
    };
    page.on("console", onConsole);
    page.on("pageerror", onPageError);
    page.on("requestfailed", onRequestFailed);
    const started = Date.now();
    let navigation = "loaded";
    let navigationError = null;
    try {
      // Set the saved preference before the route's real initializer runs;
      // this avoids a second navigation for every theme pair.
      await page.addInitScript((value) => localStorage.setItem("metahub.appearance.theme", value), theme);
      await page.goto(`${origin}/${row.url}`, { waitUntil: "domcontentloaded", timeout: 1500 });
      await page.waitForTimeout(50);
    } catch (error) {
      navigation = "navigation_error";
      navigationError = String(error).slice(0, 400);
    }
    const facts = await page.evaluate(() => {
      const visible = (element) => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
      };
      const clippedButtons = [...document.querySelectorAll("button, [role='button']")]
        .filter(visible)
        .filter((element) => element.scrollWidth > element.clientWidth + 2 || element.scrollHeight > element.clientHeight + 2)
        .slice(0, 20)
        .map((element) => ({ text: element.textContent.trim().slice(0, 60), width: element.clientWidth, contentWidth: element.scrollWidth }));
      const dialogs = [...document.querySelectorAll("dialog[open], [role='dialog']")]
        .filter(visible)
        .map((element) => {
          const rect = element.getBoundingClientRect();
          return { top: rect.top, left: rect.left, right: rect.right, bottom: rect.bottom,
            fitsViewport: rect.top >= 0 && rect.left >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight };
        });
      return {
        title: document.title,
        theme: document.documentElement.dataset.theme ?? null,
        heading: document.querySelector("h1")?.textContent.trim() ?? null,
        bodyWidth: document.documentElement.scrollWidth,
        bodyHeight: document.documentElement.scrollHeight,
        horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1 || document.body.scrollWidth > innerWidth + 1,
        clippedButtons,
        dialogs,
        tableCount: document.querySelectorAll("table").length,
        loadingText: [...document.querySelectorAll("body *")].filter(visible)
          .map((element) => element.textContent.trim()).find((text) => /正在(读取|加载|获取|同步|检查)|无法连接|加载中/.test(text) && text.length < 100) ?? null,
      };
    });
    if ((row.surface === "workspace" || row.surface === "platform") && theme === "dark" && row === rows.find((candidate) => candidate.surface === row.surface)) {
      await page.screenshot({ path: path.join(screenshotDir, `${row.surface}-first-dark.png`), animations: "disabled" });
    }
    results.push({
      surface: row.surface,
      route: row.route,
      url: row.url,
      theme,
      navigation,
      navigationError,
      durationMs: Date.now() - started,
      facts,
      consoleErrors,
      pageErrors,
      requestFailures,
      status: navigation === "navigation_error" || pageErrors.length || facts.horizontalOverflow || facts.clippedButtons.length || facts.dialogs.some((dialog) => !dialog.fitsViewport)
        ? "requires_review"
        : facts.loadingText ? "static_loaded_backend_pending" : "static_loaded",
    });
    page.off("console", onConsole);
    page.off("pageerror", onPageError);
    page.off("requestfailed", onRequestFailed);
    await page.close();
  }
}
await browser.close();
const report = {
  generatedAt: new Date().toISOString(),
  release: checklist.release,
  origin,
  viewport,
  routes: rows.length,
  captures: results.length,
  scope: "Static frontend only. No authentication, business data, mutations, or backend page acceptance.",
  allPagesVerified: false,
  results,
};
await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
const summary = results.reduce((accumulator, item) => {
  accumulator[item.status] = (accumulator[item.status] ?? 0) + 1;
  return accumulator;
}, {});
console.log(JSON.stringify({ report: reportPath, routes: rows.length, captures: results.length, summary }));
