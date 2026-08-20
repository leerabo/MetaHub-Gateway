import assert from "node:assert/strict";
import test from "node:test";

import { HUMAN_MOUSE_JOURNEYS, runHumanMouseJourneys, validateHumanMouseJourneys } from "./humanMouseJourneys.mjs";

test("FLOW mouse journey definitions cover desktop, mobile, collapse, visibility, and logout", () => {
  const summary = validateHumanMouseJourneys();
  assert.deepEqual(summary, { journeys: 3, all_108_prds_complete: false });
  assert.deepEqual(HUMAN_MOUSE_JOURNEYS.map((journey) => journey.viewport), ["desktop-1280x720", "desktop-1440x900", "mobile-390x844"]);
  assert.ok(HUMAN_MOUSE_JOURNEYS.every((journey) => journey.steps.some((step) => step.action === "mouse_click")));
  assert.ok(HUMAN_MOUSE_JOURNEYS.every((journey) => journey.steps.some((step) => step.action === "assert_no_visible_text" || step.action === "assert_attribute")));
});

test("FLOW runner passes only declared metadata to a safe browser adapter", async () => {
  const calls = [];
  const reports = [];
  const adapter = { async perform(operation) { calls.push(operation); }, async report(fact) { reports.push(fact); } };
  const result = await runHumanMouseJourneys(adapter);
  assert.equal(result.all_108_prds_complete, false);
  assert.equal(calls.length, HUMAN_MOUSE_JOURNEYS.reduce((total, journey) => total + journey.steps.length, 0));
  assert.ok(calls.every((call) => !JSON.stringify(call).includes("METAHUB_QA_PASSWORD=")));
  assert.ok(calls.every((call) => Number.isInteger(call.stepIndex) && call.stepIndex > 0));
  assert.deepEqual(reports, result.journeys);
});

test("FLOW runner rejects inline credentials and malformed definitions", () => {
  assert.throws(() => validateHumanMouseJourneys([{ ...HUMAN_MOUSE_JOURNEYS[0], id: "FLOW-01", viewport: "unknown", steps: [] }]));
  const unsafe = [{ ...HUMAN_MOUSE_JOURNEYS[0], id: "FLOW-99", steps: [{ action: "type_secret", secret: "inline-value" }, ...HUMAN_MOUSE_JOURNEYS[0].steps.slice(1) ] }];
  assert.throws(() => validateHumanMouseJourneys(unsafe), /journey_secret_source_invalid/);
});
