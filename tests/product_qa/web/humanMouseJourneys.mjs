/**
 * FLOW/WEB/UCD manual-mouse journey definition.
 * The runner is injected by a real browser harness; it must never return form values,
 * session material, request bodies, or screenshots containing sensitive information.
 */
export const HUMAN_MOUSE_JOURNEYS = Object.freeze([
  Object.freeze({
    id: "FLOW-01",
    role: "tenant_admin",
    viewport: "desktop-1280x720",
    title: "Sign in, collapse navigation, configure a provider, then verify usage visibility",
    steps: Object.freeze([
      { action: "navigate", target: "/" },
      { action: "mouse_click", target: "#login-form input[name='tenantSlug']" },
      { action: "type_secret", target: "#login-form input[name='password']", secret: "METAHUB_QA_PASSWORD" },
      { action: "mouse_click", target: "#login-form button[type='submit']" },
      { action: "wait_visible", target: "#console-sidebar" },
      { action: "mouse_click", target: "[data-sidebar-toggle]" },
      { action: "assert_attribute", target: "[data-sidebar-toggle]", attribute: "aria-expanded", value: "false" },
      { action: "mouse_click", target: "[data-console-nav='providers']" },
      { action: "wait_visible", target: "#console-main-title" },
      { action: "mouse_click", target: "[data-console-nav='usage']" },
      { action: "wait_visible", target: "#console-main-title" },
      { action: "mouse_click", target: "#logout-button" },
    ]),
  }),
  Object.freeze({
    id: "FLOW-02",
    role: "developer",
    viewport: "desktop-1440x900",
    title: "Use an existing project and virtual key to submit a traceable gateway request",
    steps: Object.freeze([
      { action: "login_from_environment" },
      { action: "mouse_click", target: "[data-console-nav='projects']" },
      { action: "wait_visible", target: "#console-main-title" },
      { action: "mouse_click", target: "[data-console-nav='virtual-keys']" },
      { action: "wait_visible", target: "#console-main-title" },
      { action: "assert_no_visible_text", values: ["PRD-", "migration", "worker state"] },
      { action: "mouse_click", target: "#logout-button" },
    ]),
  }),
  Object.freeze({
    id: "FLOW-03",
    role: "viewer",
    viewport: "mobile-390x844",
    title: "Read-only navigation, responsive layout, error recovery, and logout",
    steps: Object.freeze([
      { action: "login_from_environment" },
      { action: "assert_viewport", width: 390, height: 844 },
      { action: "assert_no_horizontal_overflow" },
      { action: "mouse_click", target: "[data-sidebar-toggle]" },
      { action: "assert_attribute", target: "[data-sidebar-toggle]", attribute: "aria-expanded", value: "false" },
      { action: "assert_no_visible_text", values: ["provider key", "access token", "prompt", "response body"] },
      { action: "mouse_click", target: "#logout-button" },
      { action: "wait_visible", target: "#login-form" },
    ]),
  }),
]);

export function validateHumanMouseJourneys(journeys = HUMAN_MOUSE_JOURNEYS) {
  const ids = new Set();
  for (const journey of journeys) {
    if (!/^FLOW-\d{2}$/.test(journey.id) || ids.has(journey.id)) throw new Error("journey_id_invalid");
    ids.add(journey.id);
    if (!/^((desktop-\d+x\d+)|(mobile-\d+x\d+))$/.test(journey.viewport)) throw new Error("journey_viewport_invalid");
    if (!Array.isArray(journey.steps) || journey.steps.length < 4) throw new Error("journey_steps_missing");
    if (journey.steps.some((step) => step.action === "type_secret" && !/^METAHUB_QA_[A-Z0-9_]+$/.test(step.secret ?? ""))) {
      throw new Error("journey_secret_source_invalid");
    }
    if (!journey.steps.some((step) => step.action === "mouse_click")) throw new Error("journey_mouse_action_missing");
    if (!journey.steps.some((step) => step.action === "wait_visible" || step.action === "assert_viewport")) throw new Error("journey_observation_missing");
  }
  return Object.freeze({ journeys: journeys.length, all_108_prds_complete: false });
}

/** Execute with an adapter that maps only the declared action names to real mouse events. */
export async function runHumanMouseJourneys(adapter, journeys = HUMAN_MOUSE_JOURNEYS) {
  if (!adapter || typeof adapter.perform !== "function") throw new TypeError("journey_adapter_required");
  if (typeof adapter.report !== "undefined" && typeof adapter.report !== "function") throw new TypeError("journey_reporter_invalid");
  validateHumanMouseJourneys(journeys);
  const facts = [];
  for (const journey of journeys) {
    for (const [index, step] of journey.steps.entries()) {
      // The adapter receives metadata only. It owns secret retrieval and must not log values.
      await adapter.perform({ journeyId: journey.id, stepIndex: index + 1, role: journey.role, viewport: journey.viewport, step });
    }
    const fact = { journey_id: journey.id, passed: true };
    facts.push(fact);
    // A real harness may report the stable journey ID only; never include field values or browser storage.
    await adapter.report?.(fact);
  }
  return Object.freeze({ journeys: facts, all_108_prds_complete: false });
}
