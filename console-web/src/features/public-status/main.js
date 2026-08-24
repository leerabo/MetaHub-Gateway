import {
  getPublicStatus,
  mutatePublicStatusSubscription,
} from "../../lib/publicStatus.js";

const root = globalThis.document?.querySelector("#status-app");
if (root) mount(root);

export async function mount(target, options = {}) {
  const hostname = globalThis.window?.location?.hostname ?? "localhost";
  const protocol = globalThis.window?.location?.protocol ?? "http:";
  const local = new Set(["localhost", "127.0.0.1", "::1"]).has(hostname);
  const statusApiUrl = options.statusApiUrl ?? (local ? `${protocol}//${hostname}:8002` : globalThis.location.origin);
  const controlApiUrl = options.controlApiUrl ?? (local ? `${protocol}//${hostname}:8001` : globalThis.location.origin);
  const params = new URLSearchParams(globalThis.location?.search ?? "");
  target.replaceChildren(loadingView());
  const operation = params.has("verify") ? "verify" : params.has("unsubscribe") ? "unsubscribe" : null;
  if (operation) {
    const token = params.get(operation);
    try {
      await mutatePublicStatusSubscription({ controlApiUrl, operation, token });
      globalThis.history?.replaceState?.({}, "", globalThis.location.pathname);
    } catch {
      // Token operations intentionally render the same outcome and never expose lookup state.
    }
  }
  try {
    const value = await getPublicStatus({ statusApiUrl });
    target.replaceChildren(statusView(value));
  } catch {
    target.replaceChildren(errorView());
    return false;
  }
  const form = target.querySelector("[data-status-subscription]");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const intent = params.get("subscribe_intent");
    const contact = new FormData(form).get("contact");
    if (!intent || typeof contact !== "string") return;
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      await mutatePublicStatusSubscription({
        controlApiUrl,
        operation: "create",
        createIntent: intent,
        contact,
        scope: { components: [], regions: [], severities: [] },
      });
      form.replaceChildren(message("Check your inbox to confirm the subscription."));
    } catch {
      form.replaceChildren(message("The subscription request could not be completed."));
    }
  });
  return true;
}

function statusView(value) {
  const shell = element("div", "status-shell");
  const header = element("header", "status-header");
  const brand = element("a", "status-brand", "MetaHub");
  brand.href = "/status.html";
  const updated = element("p", "status-updated", `Updated ${formatTime(value.updated_at)}`);
  header.append(brand, updated);
  const summary = element("section", `status-summary status-${value.overall_status}`);
  summary.setAttribute("aria-labelledby", "overall-status-title");
  const marker = element("span", "status-summary-marker");
  marker.setAttribute("aria-hidden", "true");
  const heading = element("h1", "", statusLabel(value.overall_status));
  heading.id = "overall-status-title";
  summary.append(marker, heading);
  const componentSection = section("Components", "status-components");
  const componentList = element("ul", "status-component-list");
  for (const component of value.components) {
    const item = element("li", "status-component");
    item.append(
      element("span", "status-component-name", component.name),
      element("span", `status-pill status-${component.status}`, statusLabel(component.status)),
    );
    componentList.append(item);
  }
  componentSection.append(componentList);
  const incidentSection = section("Incidents", "status-incidents");
  incidentSection.append(value.incidents.length
    ? list(value.incidents.map(incidentView), "status-event-list")
    : empty("No active or recent incidents."));
  const maintenanceSection = section("Scheduled maintenance", "status-maintenance");
  maintenanceSection.append(value.maintenance_windows.length
    ? list(value.maintenance_windows.map(maintenanceView), "status-event-list")
    : empty("No scheduled maintenance."));
  const subscriptionSection = section("Status notifications", "status-subscribe");
  const intent = new URLSearchParams(globalThis.location?.search ?? "").get("subscribe_intent");
  if (intent) {
    const form = element("form", "status-subscription-form");
    form.dataset.statusSubscription = "";
    const label = element("label", "", "Email address");
    label.htmlFor = "status-contact";
    const input = document.createElement("input");
    input.id = "status-contact";
    input.name = "contact";
    input.type = "email";
    input.autocomplete = "email";
    input.required = true;
    input.maxLength = 320;
    const submit = element("button", "status-subscribe-button", "Subscribe");
    submit.type = "submit";
    form.append(label, input, submit);
    subscriptionSection.append(form);
  } else {
    const link = element("a", "status-manage-link", "Manage subscriptions");
    link.href = "/index.html#notifications";
    subscriptionSection.append(link);
  }
  shell.append(header, summary, componentSection, incidentSection, maintenanceSection, subscriptionSection);
  return shell;
}

function incidentView(incident) {
  const article = element("article", `status-event severity-${incident.severity}`);
  const header = element("header", "status-event-header");
  header.append(element("h3", "", incident.title), element("span", "status-reference", incident.reference));
  const meta = element("p", "status-event-meta", `${statusLabel(incident.status)} · ${formatTime(incident.updated_at)}`);
  const timeline = element("ol", "status-timeline");
  for (const update of [...incident.updates].reverse()) {
    const item = element("li", "");
    item.append(element("time", "", formatTime(update.published_at)), element("p", "", update.message));
    timeline.append(item);
  }
  article.append(header, meta, timeline);
  if (incident.postmortem) {
    const details = document.createElement("details");
    details.append(element("summary", "", "Postmortem"), element("p", "", incident.postmortem.summary));
    article.append(details);
  }
  return article;
}

function maintenanceView(window) {
  const article = element("article", "status-event maintenance-event");
  const header = element("header", "status-event-header");
  header.append(element("h3", "", window.title), element("span", "status-reference", window.reference));
  article.append(
    header,
    element("p", "status-event-meta", `${formatTime(window.starts_at)} – ${formatTime(window.ends_at)}`),
    element("p", "", window.message),
  );
  return article;
}

function loadingView() {
  const main = element("div", "status-state");
  main.setAttribute("role", "status");
  main.append(element("strong", "status-brand", "MetaHub"), element("p", "", "Loading current status…"));
  return main;
}
function errorView() {
  const main = element("div", "status-state");
  main.setAttribute("role", "alert");
  main.append(element("strong", "status-brand", "MetaHub"), element("h1", "", "Status unavailable"),
    element("p", "", "Current service status could not be loaded."));
  return main;
}
function section(title, className) {
  const value = element("section", `status-section ${className}`);
  value.append(element("h2", "", title));
  return value;
}
function list(items, className) {
  const value = element("div", className);
  value.append(...items);
  return value;
}
function empty(text) { return element("p", "status-empty", text); }
function message(text) { const value = element("p", "status-form-message", text); value.setAttribute("role", "status"); return value; }
function element(tag, className = "", text = "") {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text) value.textContent = text;
  return value;
}
function statusLabel(value) {
  return ({
    operational: "All systems operational",
    degraded_performance: "Degraded performance",
    partial_outage: "Partial outage",
    major_outage: "Major outage",
    maintenance: "Maintenance in progress",
    published: "Investigating",
    monitoring: "Monitoring",
    resolved: "Resolved",
    postmortem_published: "Resolved",
  })[value] ?? "Status update";
}
function formatTime(value) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium", timeStyle: "short", timeZoneName: "short",
  }).format(new Date(value));
}
