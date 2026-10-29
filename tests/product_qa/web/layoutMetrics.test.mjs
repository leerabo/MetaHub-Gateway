import assert from "node:assert/strict";
import test from "node:test";
import { createRequire } from "node:module";
import { measureInteractiveLayout } from "./layoutMetrics.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require("../../../.tools/product-qa-browser/node_modules/playwright");

test("layout audit ignores folded content and still detects visible overlap and clipping", async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const scale of [1, 1.25, 1.5]) {
      const page = await browser.newPage({ viewport: { width: 900, height: 600 }, deviceScaleFactor: scale });
      await page.setContent(`<style>
        details input, #action { position: absolute; left: 20px; top: 100px; width: 160px; height: 40px; }
        #clipped { position: absolute; top: 180px; width: 24px; white-space: nowrap; }
      </style><details><summary>Advanced</summary><input aria-label="Name"></details>
        <button id="action">Create</button>`);
      // Force a retained box, reproducing the closed-details Chromium behavior.
      await page.locator("input").evaluate(e => e.getBoundingClientRect());
      assert.equal((await page.evaluate(measureInteractiveLayout)).interactive_overlap_count, 0);
      await page.locator("summary").click();
      assert.equal((await page.evaluate(measureInteractiveLayout)).interactive_overlap_count, 1);
      await page.locator("summary").click();
      assert.equal((await page.evaluate(measureInteractiveLayout)).interactive_overlap_count, 0);
      await page.evaluate(() => {
        const button = document.createElement("button");
        button.id = "clipped"; button.textContent = "A long visible label";
        document.body.append(button);
      });
      assert.equal((await page.evaluate(measureInteractiveLayout)).clipped_control_count, 1);
      await page.close();
    }
  } finally { await browser.close(); }
});

test("layout audit measures painted scroll regions and retains real overlaps below the fold", async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 900, height: 600 } });
    for (const overflow of ["auto", "scroll", "hidden", "clip"]) {
      await page.setContent(`<style>
        #viewport { position:absolute; left:20px; top:20px; width:120px; height:40px; overflow:${overflow}; scrollbar-width:none; }
        #content { height:160px; }
        input { position:absolute; left:0; top:80px; width:80px; height:20px; padding:0; border:0; }
        button { position:absolute; left:20px; top:100px; width:80px; height:20px; padding:0; border:0; }
      </style><div id="viewport"><div id="content"><input aria-label="Event"></div></div><button>Save</button>`);
      assert.equal((await page.evaluate(measureInteractiveLayout)).interactive_overlap_count, 0, overflow);
      await page.locator("input").evaluate(element => { element.style.top = "35px"; });
      await page.locator("button").evaluate(element => { element.style.top = "65px"; });
      assert.equal((await page.evaluate(measureInteractiveLayout)).interactive_overlap_count, 0, `clipped portion ${overflow}`);
      await page.locator("button").evaluate(element => { element.style.top = "55px"; });
      assert.equal((await page.evaluate(measureInteractiveLayout)).interactive_overlap_count, 1, `painted portion ${overflow}`);
    }
    await page.setContent('<button style="position:absolute;top:2000px;left:20px;width:100px;height:40px">First</button><button style="position:absolute;top:2010px;left:20px;width:100px;height:40px">Second</button>');
    assert.equal((await page.evaluate(measureInteractiveLayout)).interactive_overlap_count, 1);
    await page.close();
  } finally { await browser.close(); }
});
