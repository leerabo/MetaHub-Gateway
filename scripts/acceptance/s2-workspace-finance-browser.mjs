import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";

export async function verifyWorkspaceFinance({ page, origin, tag, report, step, layout, browserJson }) {
  const fixture = await page.evaluate(async (token) => {
    const session = await (await fetch("/workspace/session", { credentials: "include" })).json();
    const response = await fetch("/__acceptance/usage", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Acceptance-Fixture": token },
      body: JSON.stringify({ workspace_id: session.context.workspace_id }),
    });
    if (!response.ok) throw new Error("Isolated usage fixture failed");
    return response.json();
  }, process.env.S1_FIXTURE_TOKEN);
  assert.equal(fixture.source, "isolated_usage_fixture");
  report.finance ??= [];
  const record = { width: Number(tag), usageSource: fixture.source, status: "started" };
  report.finance.push(record);

  const action = async (method, pathname, expected, perform) => {
    const pending = page.waitForResponse((response) => response.request().method() === method
      && new URL(response.url()).pathname === pathname);
    await perform();
    const response = await pending;
    assert.equal(response.status(), expected, method + " " + pathname);
    return response.json();
  };
  await step(tag + ".finance-budget", async () => {
    await page.goto(origin + "/console.html#budgets");
    const form = page.locator("#budget-rule-form");
    await form.waitFor();
    await form.locator('[name="scopeType"]').selectOption("tenant");
    await form.locator('[name="name"]').fill("Browser budget " + tag);
    await form.locator('[name="dailyBudgetCny"]').fill("12.345678");
    await form.locator('[name="exhaustedAction"]').selectOption("block");
    await action("POST", "/budgets", 201, () => form.locator('button[type="submit"]').click());
    const mode = page.locator("[data-budget-mode-rule-id]");
    await mode.waitFor();
    const id = await mode.getAttribute("data-budget-mode-rule-id");
    await action("PATCH", "/budgets/" + id + "/mode", 200, () => mode.selectOption("alert_only"));
    await page.waitForFunction(() => document.querySelector("[data-budget-mode-rule-id]")?.value === "alert_only"
      && !document.querySelector("[data-budget-mode-rule-id]")?.disabled);
    await form.locator('[name="name"]').fill("Browser budget updated " + tag);
    await form.locator('[name="dailyBudgetCny"]').fill("23.456789");
    await action("PATCH", "/budgets", 200, () => form.locator('button[type="submit"]').click());
    await page.locator('.budget-rule-table [data-label="日预算"]')
      .getByText("23.456789 CNY", { exact: true }).waitFor();
    const rules = await browserJson(page, "/budgets");
    assert.equal(rules.status, 200);
    assert.equal(rules.body.budget_rules.length, 1);
    assert.equal(rules.body.budget_rules[0].daily_budget_cny, "23.456789");
    record.budget = { rules: 1, amount: "23.456789", updated: true };
    await layout(page, tag + "-finance-budget");
  });

  await step(tag + ".finance-adjustment-reversal", async () => {
    await page.goto(origin + "/console.html#internal-finops");
    const initial = await browserJson(page, "/finops/ledger");
    assert.equal(initial.status, 200);
    assert.equal(initial.body.ledger_entries.length, 3);
    assert.equal(initial.body.totals.by_currency.CNY.net, "0.500000");
    assert.equal(initial.body.totals.by_currency.USD.net, "0.250000");
    await page.locator("#finops-open-adjustment").click();
    const form = page.locator("#finops-adjustment-form");
    await form.locator('[name="reason"]').fill("Cancelled browser draft");
    await page.locator("#finops-cancel-adjustment").click();
    await page.locator("#finops-open-adjustment").click();
    assert.equal(await form.locator('[name="reason"]').inputValue(), "");
    await form.locator('[name="adjustmentType"]').selectOption("manual_debit");
    await form.locator('[name="amount"]').fill("0.000001");
    await form.locator('[name="reason"]').fill("Browser acceptance debit");
    await layout(page, tag + "-finance-adjustment");
    const debit = await action("POST", "/finops/adjustments", 201, () => form.locator('button[type="submit"]').click());
    await page.locator("#finops-adjustment-dialog").waitFor({ state: "detached" });
    const rows = page.locator("[data-finops-ledger-row]");
    await page.waitForFunction(() => document.querySelectorAll("[data-finops-ledger-row]").length === 4);
    const debitRow = rows.filter({ has: page.locator('[data-finops-entry-id="' + debit.ledger_entry.finops_ledger_entry_id + '"]') });
    await debitRow.locator("[data-finops-entry-id]").click();
    await page.locator("#finops-detail-title").waitFor();
    await layout(page, tag + "-finance-detail");
    await debitRow.locator("[data-finops-reverse-entry-id]").click();
    assert.equal(await form.locator('[name="adjustmentType"]').inputValue(), "reversal");
    assert.equal(await form.locator('[name="adjustmentType"] option').count(), 1);
    await form.locator('[name="amount"]').fill("0.000001");
    await form.locator('[name="reason"]').fill("Browser acceptance reversal");
    await action("POST", "/finops/adjustments", 201, () => form.locator('button[type="submit"]').click());
    await page.locator("#finops-adjustment-dialog").waitFor({ state: "detached" });
    await page.waitForFunction(() => document.querySelectorAll("[data-finops-ledger-row]").length === 5);
    const ledger = await browserJson(page, "/finops/ledger");
    assert.equal(ledger.status, 200);
    assert.equal(ledger.body.ledger_entries.length, 5);
    for (const currency of ["CNY", "USD"]) {
      assert.equal(ledger.body.totals.by_currency[currency].net, initial.body.totals.by_currency[currency].net);
    }
    for (const original of initial.body.ledger_entries) {
      assert.deepEqual(ledger.body.ledger_entries.find((entry) => entry.finops_ledger_entry_id === original.finops_ledger_entry_id), original);
    }
    const adjustments = ledger.body.ledger_entries.filter((entry) => entry.entry_type !== "usage_charge");
    assert.equal(adjustments.length, 2);
    assert.deepEqual(new Set(adjustments.map((entry) => entry.entry_type)), new Set(["manual_debit", "reversal"]));
    assert.ok(adjustments.every((entry) => entry.amount === "0.000001"));
    record.ledger = { entries: 5, originalEntries: 3, adjustmentNet: "0.000000", cancelledDraftCleared: true };
  });

  await step(tag + ".finance-usage-export", async () => {
    await page.goto(origin + "/console.html#usage");
    const form = page.locator("#usage-filter-form");
    await form.waitFor();
    await form.locator('[name="consumerId"]').fill(fixture.consumer_id);
    await action("GET", "/usage/records", 200, () => form.locator('button[type="submit"]').click());
    await page.waitForFunction(() => document.querySelector(".usage-count")?.textContent.trim() === "2 / 2");
    const records = await browserJson(page, "/usage/records?consumer_id=" + fixture.consumer_id);
    assert.equal(records.body.total, 2);
    assert.ok(records.body.usage_records.every((row) => row.consumer_id === fixture.consumer_id));
    await layout(page, tag + "-finance-usage");
    const actionFonts = await page.locator(".usage-filter-actions button")
      .evaluateAll((buttons) => buttons.map((button) => Number.parseFloat(getComputedStyle(button).fontSize)));
    assert.ok(actionFonts.length > 0 && actionFonts.every((size) => size >= 16));
    record.usageActionMinPx = Math.min(...actionFonts);
    const received = page.waitForEvent("download");
    await page.locator("#usage-export-csv").click();
    const download = await received;
    assert.equal(await download.failure(), null);
    assert.match(download.suggestedFilename(), /^[A-Za-z0-9][A-Za-z0-9._-]*\.csv$/u);
    const chunks = [];
    for await (const chunk of await download.createReadStream()) chunks.push(chunk);
    const csv = Buffer.concat(chunks);
    const rows = JSON.parse(execFileSync(process.env.S1_PYTHON, ["-c",
      "import csv,io,json,sys; rows=list(csv.DictReader(io.StringIO(sys.stdin.buffer.read().decode('utf-8-sig')))); print(json.dumps([dict(request_id=r['request_id'],virtual_key_id=r['virtual_key_id']) for r in rows]))",
    ], { input: csv, encoding: "utf8" }));
    assert.equal(rows.length, 2);
    assert.deepEqual(new Set(rows.map((row) => row.request_id)), new Set(fixture.requests));
    assert.ok(rows.every((row) => row.virtual_key_id === fixture.virtual_key_id));
    assert.equal(await page.evaluate((id) => [...Object.values(localStorage), ...Object.values(sessionStorage)]
      .some((value) => value.includes(id)), fixture.requests[0]), false);
    record.usage = { filteredRows: 2, csvRows: rows.length, currencies: ["CNY", "USD"] };
    record.status = "passed";
  });
  await page.goto(origin + "/console.html#virtual-keys");
  await page.locator("#create-consumer-form").waitFor();
  return fixture;
}

export async function verifyUsageAfterDisable({ page, origin, fixture, browserJson }) {
  await page.goto(origin + "/console.html#usage");
  await page.locator("#usage-filter-form").waitFor();
  const response = await browserJson(page, "/usage/summary?consumer_id=" + fixture.consumer_id);
  assert.equal(response.status, 200);
  assert.equal(response.body.totals.request_count, 2);
  assert.equal(response.body.totals.total_tokens, 30);
  assert.equal(response.body.totals.cost_cny, 0.25);
  assert.equal(response.body.totals.cost_usd, 0.25);
  assert.equal(response.body.by_consumer[0].consumer_id, fixture.consumer_id);
}
