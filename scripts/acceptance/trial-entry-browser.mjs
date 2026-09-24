import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { writeFile } from "node:fs/promises";
import path from "node:path";

const { PNG } = createRequire(import.meta.url)("pngjs");

function pixels(first, second) {
  const a = PNG.sync.read(first);
  const b = PNG.sync.read(second);
  const colors = new Set();
  let changed = 0;
  for (let i = 0; i < a.data.length; i += 4) {
    colors.add((a.data[i] << 16) | (a.data[i + 1] << 8) | a.data[i + 2]);
    if (Math.abs(a.data[i] - b.data[i]) + Math.abs(a.data[i + 1] - b.data[i + 1])
      + Math.abs(a.data[i + 2] - b.data[i + 2]) > 12) changed++;
  }
  return { colors: colors.size, changed };
}

export async function verifyTrialEntry(browser, origin, artifacts) {
  const report = { viewports: [], login: [] };
  const viewports = [
    { width: 2160, height: 1240 }, { width: 1920, height: 1080 }, { width: 1440, height: 900 },
    { width: 1280, height: 720 }, { width: 390, height: 844 },
    { width: 320, height: 568 },
  ];
  const context = await browser.newContext({ serviceWorkers: "block" });
  await context.route("**/*", (route) => new URL(route.request().url()).origin === origin
    ? route.continue() : route.abort());
  const page = await context.newPage();
  page.setDefaultTimeout(12000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.name));
  try {
    for (const viewport of viewports) {
      report.current = `home-${viewport.width}.load`;
      await page.setViewportSize(viewport);
      await page.goto(origin + "/");
      await page.locator('[data-gateway-scene][data-rendered="true"]').waitFor();
      const facts = await page.evaluate(() => {
        const bounds = (selector) => document.querySelector(selector).getBoundingClientRect();
        return { overflow: document.documentElement.scrollWidth > innerWidth,
          capabilityTop: bounds(".home-capabilities").top,
          introBottom: bounds(".home-capability-intro").bottom,
          headingTop: bounds("#capabilities-title").top,
          copyBottom: bounds(".home-hero-copy").bottom,
          sceneTop: bounds(".home-scene").top,
          sceneHeight: bounds(".home-scene").height,
          paragraphMinPx: Math.min(...[...document.querySelectorAll(".home-capability-grid p")]
            .map((element) => parseFloat(getComputedStyle(element).fontSize))) };
      });
      report.viewports.push({ ...viewport, ...facts });
      assert.equal(facts.overflow, false);
      assert.ok(Math.abs(facts.capabilityTop - (viewport.height - 28)) <= 1);
      assert.ok(Math.abs(facts.introBottom - viewport.height) <= 1);
      assert.ok(facts.headingTop > viewport.height);
      assert.ok(facts.copyBottom <= facts.sceneTop + 1);
      assert.ok(facts.sceneHeight > 100);
      assert.ok(facts.paragraphMinPx >= 15);
      const canvas = page.locator("[data-gateway-scene] canvas");
      report.current = `home-${viewport.width}.canvas`;
      const first = await canvas.screenshot();
      await page.waitForTimeout(500);
      const motion = pixels(first, await canvas.screenshot());
      Object.assign(report.viewports.at(-1), motion);
      assert.ok(motion.colors > 100 && motion.changed > 25, JSON.stringify(motion));
      report.current = `home-${viewport.width}.pause`;
      await page.locator("[data-motion-toggle]").click();
      assert.equal(await page.locator("[data-motion-toggle]").getAttribute("aria-pressed"), "true");
      await page.screenshot({ path: path.join(artifacts, `home-${viewport.width}.png`), fullPage: true });
    }
    await page.emulateMedia({ reducedMotion: "reduce" });
    report.current = "home.reduced-motion";
    await page.reload();
    await page.locator('[data-gateway-scene][data-rendered="true"]').waitFor();
    assert.equal(await page.locator("[data-motion-toggle]").getAttribute("aria-pressed"), "true");
    const still = await page.locator("[data-gateway-scene] canvas").screenshot();
    await page.waitForTimeout(250);
    assert.equal(pixels(still, await page.locator("[data-gateway-scene] canvas").screenshot()).changed, 0);
    report.reducedMotion = true;
    await page.emulateMedia({ reducedMotion: "no-preference" });
    for (const viewport of viewports.filter(({ width }) => [1440, 390].includes(width))) {
      report.current = `login-${viewport.width}`;
      await page.setViewportSize(viewport);
      let release;
      const gate = new Promise((resolve) => { release = resolve; });
      const delayed = async (route) => { await gate; await route.continue().catch(() => {}); };
      await page.route("**/branding/public", delayed);
      await page.route("**/identity/me", delayed);
      const start = Date.now();
      await page.goto(origin + "/identity.html", { waitUntil: "domcontentloaded" });
      const form = page.locator("[data-identity-login-form]");
      await form.waitFor({ timeout: 3000 });
      const visibleMs = Date.now() - start;
      await form.locator('[name="email"]').fill("typing@example.test");
      await form.locator('[name="password"]').fill("LocalTypingTest!2026");
      await page.waitForTimeout(750);
      release();
      await page.waitForTimeout(750);
      assert.equal(await form.locator('[name="email"]').inputValue(), "typing@example.test");
      assert.equal(await form.locator('[name="password"]').inputValue(), "LocalTypingTest!2026");
      assert.ok(await form.locator('button[type="submit"]').isEnabled());
      await form.locator('[name="email"]').clear();
      await form.locator('[name="password"]').clear();
      await page.screenshot({ path: path.join(artifacts, `login-${viewport.width}.png`) });
      await page.unroute("**/branding/public", delayed);
      await page.unroute("**/identity/me", delayed);
      report.login.push({ width: viewport.width, visibleMs, typingPreserved: true });
    }
    assert.deepEqual(errors, []);
    report.current = "passed";
    return report;
  } catch (error) {
    report.failureType = error.name;
    report.location = String(error.stack).match(/trial-entry-browser\.mjs:\d+:\d+/gu) ?? [];
    if (report.current.endsWith(".canvas")) {
      report.canvasFailure = error.message;
      report.canvasState = await page.evaluate(() => ({
        count: document.querySelectorAll("[data-gateway-scene] canvas").length,
        rendered: document.querySelector("[data-gateway-scene]")?.dataset.rendered,
        visibility: document.visibilityState,
        fonts: document.fonts.status,
      }));
      await page.screenshot({ path: path.join(artifacts, "home-failed.png"), timeout: 5000 }).catch(() => {});
    }
    throw error;
  } finally {
    await writeFile(path.join(artifacts, "trial-entry.json"), JSON.stringify(report, null, 2));
    await context.close();
  }
}
