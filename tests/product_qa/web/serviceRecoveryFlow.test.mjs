import assert from "node:assert/strict";
import test from "node:test";

import { SERVICE_RECOVERY_FLOW, safeRecoveryObservation, validateServiceRecoveryFlow } from "./serviceRecoveryFlow.mjs";

test("FLOW service outage and recovery has a user-visible error and retry path", () => {
  assert.deepEqual(validateServiceRecoveryFlow(), { steps: 10, service_outage: true, service_recovered: true, all_108_prds_complete: false });
  assert.equal(SERVICE_RECOVERY_FLOW.filter((step) => step.action === "mouse_click").length, 2);
  assert.ok(SERVICE_RECOVERY_FLOW.some((step) => step.action === "assert_visible_text" && step.values.includes("request_id")));
});

test("FLOW recovery observation is redacted and rejects incomplete recovery", () => {
  assert.deepEqual(safeRecoveryObservation({ status: "passed", outageVisible: true, recovered: true, requestIdPresent: true, secret: "must-not-return" }), {
    status: "passed", outageVisible: true, recovered: true, requestIdPresent: true, sensitiveBodyCaptured: false,
  });
  assert.equal(safeRecoveryObservation({ status: "failed" }).recovered, false);
});
