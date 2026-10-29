/** PRD-132 Workspace ticket DOM bindings and private draft cleanup. */

export function bindSupportTicketInteractions({
  root = globalThis.document,
  onFilter,
  onRefresh,
  onSelect,
  onCreate,
  onPatch,
  onAttachment,
  onEscalation,
} = {}) {
  const page = root?.querySelector?.("[data-support-ticket-root]");
  if (!page) return false;

  page.querySelector("[data-support-ticket-filter-form]")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    void onFilter?.({ status: String(data.get("status") ?? ""), severity: String(data.get("severity") ?? "") });
  });
  page.querySelector("[data-support-ticket-refresh]")?.addEventListener("click", () => void onRefresh?.());
  page.querySelectorAll("[data-support-ticket-select]").forEach((button) => {
    button.addEventListener("click", () => void onSelect?.(String(button.dataset.supportTicketSelect ?? "")));
  });

  const create = page.querySelector("[data-support-ticket-create-form]");
  const relatedKind = create?.querySelector("[data-support-related-kind]");
  const syncRelatedReference = () => {
    const reference = create?.elements?.namedItem?.("related_reference");
    if (!reference || !("disabled" in reference)) return;
    reference.disabled = relatedKind?.value === "none";
    reference.required = relatedKind?.value !== "none";
    if (reference.disabled) reference.value = "";
  };
  relatedKind?.addEventListener("change", syncRelatedReference);
  syncRelatedReference();
  create?.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (!valid(form)) return;
    const data = new FormData(form);
    const input = {
      category: String(data.get("category") ?? ""),
      severity: String(data.get("severity") ?? ""),
      subject: String(data.get("subject") ?? "").trim(),
      message: String(data.get("message") ?? "").trim(),
      relatedKind: String(data.get("related_kind") ?? "none"),
      relatedReference: String(data.get("related_reference") ?? "").trim() || null,
    };
    clearPrivateInputs(form);
    syncRelatedReference();
    void onCreate?.(input);
  });

  page.querySelector("[data-support-ticket-reply-form]")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (!valid(form)) return;
    const data = new FormData(form);
    const input = ticketRevision(data);
    input.changes = { message: String(data.get("message") ?? "").trim() };
    clearPrivateInputs(form);
    void onPatch?.(input, "reply");
  });
  page.querySelector("[data-support-ticket-status-form]")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    void onPatch?.({ ...ticketRevision(data), changes: { status: String(data.get("status") ?? "") } }, "status");
  });
  page.querySelector("[data-support-ticket-survey-form]")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (!valid(form)) return;
    const data = new FormData(form);
    const comment = String(data.get("survey_comment") ?? "").trim();
    const changes = { survey_rating: Number(data.get("survey_rating")) };
    if (comment) changes.survey_comment = comment;
    clearPrivateInputs(form);
    void onPatch?.({ ...ticketRevision(data), changes }, "survey");
  });
  page.querySelector("[data-support-ticket-attachment-form]")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (!valid(form)) return;
    const data = new FormData(form);
    const input = {
      ...ticketRevision(data),
      fileId: String(data.get("file_id") ?? ""),
      intentCode: String(data.get("intent_code") ?? ""),
      confirmed: data.get("confirmed") === "on",
    };
    clearPrivateInputs(form);
    void onAttachment?.(input);
  });
  page.querySelector("[data-support-ticket-escalation-form]")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (!valid(form)) return;
    const data = new FormData(form);
    void onEscalation?.({
      ...ticketRevision(data),
      reasonCode: String(data.get("reason_code") ?? ""),
      confirmed: data.get("confirmed") === "on",
    });
  });
  return true;
}

export function clearSupportTicketDom(root = globalThis.document) {
  const panel = root?.querySelector?.(".support-tickets-panel");
  clearPrivateInputs(panel);
  panel?.replaceChildren?.();
}

function ticketRevision(data) {
  return {
    ticketId: String(data.get("ticket_id") ?? ""),
    expectedRevision: Number(data.get("revision")),
  };
}
function valid(form) {
  if (form.checkValidity()) return true;
  form.reportValidity?.();
  return false;
}
function clearPrivateInputs(root) {
  root?.querySelectorAll?.("textarea, input:not([type='hidden']):not([type='checkbox']):not([type='radio'])").forEach((input) => {
    input.value = "";
  });
  root?.querySelectorAll?.("input[type='checkbox'], input[type='radio']").forEach((input) => {
    input.checked = false;
  });
}
