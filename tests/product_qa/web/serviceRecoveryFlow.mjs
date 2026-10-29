/** FLOW service interruption/recovery contract for the real browser adapter. */
export const SERVICE_RECOVERY_FLOW = Object.freeze([
  { action: "navigate", target: "/" },
  { action: "assert_visible", target: "#login-form" },
  { action: "inject_service_outage", route: "/auth/login", status: 503 },
  { action: "mouse_click", target: "#login-form button[type='submit']" },
  { action: "assert_visible", target: "#login-form [role='alert']" },
  { action: "assert_visible_text", target: "#login-form [role='alert']", values: ["request_id"] },
  { action: "restore_service", route: "/auth/login", status: 200 },
  { action: "mouse_click", target: "#login-form button[type='submit']" },
  { action: "wait_visible", target: ".console-shell" },
  { action: "assert_no_visible_text", values: ["Bearer ", "provider key", "prompt", "response"] },
]);

export function validateServiceRecoveryFlow(flow = SERVICE_RECOVERY_FLOW) {
  const actions = flow.map((step) => step.action);
  for (const required of ["inject_service_outage", "restore_service", "mouse_click", "wait_visible"]) {
    if (!actions.includes(required)) throw new Error(`recovery_${required}_missing`);
  }
  if (actions.indexOf("restore_service") < actions.indexOf("inject_service_outage")) throw new Error("recovery_order_invalid");
  return Object.freeze({ steps: flow.length, service_outage: true, service_recovered: true, all_108_prds_complete: false });
}

export function safeRecoveryObservation(observation = {}) {
  return Object.freeze({
    status: observation.status === "passed" ? "passed" : "failed",
    outageVisible: observation.outageVisible === true,
    recovered: observation.recovered === true,
    requestIdPresent: observation.requestIdPresent === true,
    sensitiveBodyCaptured: false,
  });
}
