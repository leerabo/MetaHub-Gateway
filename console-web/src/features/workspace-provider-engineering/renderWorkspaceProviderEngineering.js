/** Provider-detail projection for Workspace-owned PRD-143 overrides. */

export function renderWorkspaceProviderEngineering(state = {}, provider = {}) {
  const response = state.response;
  const compatibility = response?.compatibility ?? null;
  const overrides = response?.overrides ?? [];
  const head = response?.head ?? null;
  return `<section class="workspace-provider-engineering" data-workspace-provider-engineering-root
    aria-labelledby="workspace-provider-engineering-title">
    <header class="workspace-provider-engineering-heading">
      <div><p>Provider detail / Workspace scope</p>
        <h3 id="workspace-provider-engineering-title">Compatibility and error overrides</h3>
        <span>${escapeHtml(provider.name ?? provider.provider_code ?? state.providerId)}</span></div>
      <div class="workspace-provider-engineering-heading-actions">
        <button type="button" data-workspace-provider-engineering-action="refresh" ${state.loading ? "disabled" : ""}>Refresh</button>
        <button type="button" data-workspace-provider-engineering-action="close">Close</button>
      </div>
    </header>
    ${notice(state)}
    ${compatibility ? compatibilityView(compatibility, head) : loadingState(state)}
    ${response ? overrideWorkspace(state, overrides, head) : ""}
  </section>`;
}

export function renderWorkspaceProviderErrorMaps(state = {}) {
  const response = state.response;
  const overrides = response?.overrides ?? [];
  const head = response?.head ?? null;
  return `<section class="panel workspace-provider-engineering workspace-provider-error-maps"
    data-workspace-provider-error-maps-root data-workspace-provider-engineering-root
    aria-labelledby="workspace-provider-error-maps-title">
    <header class="workspace-provider-engineering-heading">
      <div><p>Workspace scope / Provider engineering</p>
        <h2 id="workspace-provider-error-maps-title">Provider error overrides</h2>
        <span>Review and govern bounded error mappings without restoring the retired Error Maps runtime.</span></div>
      <div class="workspace-provider-engineering-heading-actions">
        <button type="button" data-workspace-provider-engineering-action="refresh"
          ${state.loading ? "disabled" : ""}>Refresh</button>
      </div>
    </header>
    ${notice(state)}
    ${response
      ? overrideWorkspace(state, overrides, head, { globalScope: true })
      : '<div class="empty-state">Loading Workspace Provider error overrides.</div>'}
  </section>`;
}

function compatibilityView(item, head) {
  return `<section class="workspace-provider-compatibility" aria-label="Provider compatibility">
    <div><span>Compatibility</span>${status(item.compatibility_status)}<small>${escapeHtml((item.reason_codes ?? []).join(" · ") || "verified")}</small></div>
    <div><span>Health</span>${status(item.health_status)}<small>${formatTime(item.projected_at)}</small></div>
    <div><span>Adapter release</span><strong>${escapeHtml(shortId(item.provider_adapter_release_id))}</strong><small>${escapeHtml(item.provider_catalog_version ?? "catalog version unknown")}</small></div>
    <div><span>Builtin catalog</span><strong>${escapeHtml(shortId(item.builtin_error_catalog_release_id))}</strong><small>${escapeHtml(head?.builtin_catalog_version ?? "-")}</small></div>
    <div><span>Override head</span><strong>Revision ${number(item.override_head_revision)}</strong><small>${escapeHtml(shortHash(head?.content_sha256))}</small></div>
  </section>`;
}

function overrideWorkspace(state, overrides, head, { globalScope = false } = {}) {
  const filters = state.filters ?? {};
  return `<section class="workspace-provider-overrides" aria-labelledby="workspace-provider-overrides-title">
    <div class="workspace-provider-overrides-title"><div><p>Bounded matcher conditions</p>
      <h4 id="workspace-provider-overrides-title">Error overrides</h4></div>
      <span>${number(overrides.length)} rules · head ${number(head?.revision)}</span></div>
    <form class="workspace-provider-override-filters" data-workspace-provider-override-filter-form>
      ${globalScope ? `<label><span>Provider ID</span><input name="providerId" maxlength="128"
        value="${escapeAttribute(filters.provider_id ?? "")}" /></label>` : ""}
      <label><span>Status</span><select name="status">${options(filters.status, [["", "All"], ["active", "Active"], ["disabled", "Disabled"], ["archived", "Archived"]])}</select></label>
      <label><span>Output code</span><input name="outputErrorCode" maxlength="128"
        value="${escapeAttribute(filters.output_error_code ?? "")}" /></label>
      <button type="submit" ${state.loading ? "disabled" : ""}>Apply</button>
    </form>
    <div class="workspace-provider-override-list">
      ${overrides.map((item) => overrideRow(
        item,
        state.canManage,
        head?.revision,
        state.loading,
        globalScope,
      )).join("")
        || '<div class="empty-state">No Workspace override matches this Provider and filter.</div>'}
    </div>
    <div class="workspace-provider-override-pagination">
      <span>Opaque keyset pagination</span><div>
        <button type="button" data-workspace-provider-engineering-action="previous"
          ${state.history?.length && !state.loading ? "" : "disabled"}>Previous</button>
        <button type="button" data-workspace-provider-engineering-action="next"
          ${state.response?.next_cursor && !state.loading ? "" : "disabled"}>Next</button>
      </div>
    </div>
    ${state.canManage
      ? createForm(state.providerId, head?.revision, state.loading, globalScope)
      : ""}
  </section>`;
}

function overrideRow(item, canManage, headRevision, busy, showProvider = false) {
  return `<article class="workspace-provider-override" data-workspace-provider-override="${escapeAttribute(item.provider_error_override_id)}">
    <header><div><strong>${escapeHtml(item.output_error_code)}</strong>
      <span>${showProvider ? `${escapeHtml(item.provider_name ?? item.provider_code ?? "Workspace default")} · ` : ""}Priority ${number(item.priority)} · version ${number(item.version)}</span></div>${status(item.status)}</header>
    <dl>
      <div><dt>HTTP</dt><dd>${escapeHtml(item.http_statuses.join(", ") || item.http_status_class || "-")}</dd></div>
      <div><dt>Field</dt><dd>${escapeHtml([item.error_field, item.field_operator].filter(Boolean).join(" · ") || "-")}</dd></div>
      <div><dt>Condition</dt><dd>${escapeHtml(item.field_pattern ?? "-")}</dd></div>
      <div><dt>Content proof</dt><dd><code>${escapeHtml(shortHash(item.content_sha256))}</code></dd></div>
      <div><dt>Updated</dt><dd>${formatTime(item.updated_at)}</dd></div>
    </dl>
    ${canManage ? `<div class="workspace-provider-override-actions">
      ${(item.actions ?? []).filter((action) => new Set(["enable", "disable", "archive"]).has(action))
        .map((action) => transitionForm(item, action, headRevision, busy)).join("")}
      ${(item.actions ?? []).includes("update") ? updateForm(item, headRevision, busy) : ""}
    </div>` : ""}
  </article>`;
}

function transitionForm(item, action, headRevision, busy) {
  return `<form data-workspace-provider-override-transition-form autocomplete="off">
    <input type="hidden" name="action" value="${escapeAttribute(action)}" />
    <input type="hidden" name="overrideId" value="${escapeAttribute(item.provider_error_override_id)}" />
    <input type="hidden" name="expectedVersion" value="${number(item.version)}" />
    <input type="hidden" name="headRevision" value="${number(headRevision)}" />
    ${stepUpFields()}
    <button type="submit" ${busy ? "disabled" : ""}>${escapeHtml(actionLabel(action))}</button>
  </form>`;
}

function updateForm(item, headRevision, busy) {
  return `<details><summary>Edit rule</summary>
    <form data-workspace-provider-override-update-form autocomplete="off">
      <input type="hidden" name="overrideId" value="${escapeAttribute(item.provider_error_override_id)}" />
      <input type="hidden" name="providerId" value="${escapeAttribute(item.provider_id)}" />
      <input type="hidden" name="expectedVersion" value="${number(item.version)}" />
      <input type="hidden" name="headRevision" value="${number(headRevision)}" />
      ${conditionFields(item)}${stepUpFields()}
      <button type="submit" ${busy ? "disabled" : ""}>Update rule</button>
    </form></details>`;
}

function createForm(providerId, headRevision, busy, globalScope = false) {
  return `<details class="workspace-provider-override-create"><summary>Create override</summary>
    <form data-workspace-provider-override-create-form autocomplete="off">
      ${globalScope
        ? `<label><span>Provider ID</span><input name="providerId" maxlength="128"
          pattern="[A-Za-z0-9][A-Za-z0-9._:-]{0,127}" required /></label>`
        : `<input type="hidden" name="providerId" value="${escapeAttribute(providerId)}" />`}
      <input type="hidden" name="headRevision" value="${number(headRevision)}" />
      ${conditionFields({ priority: 500, http_statuses: [], output_error_code: "provider_error" })}
      ${stepUpFields()}
      <button type="submit" ${busy ? "disabled" : ""}>Create override</button>
    </form></details>`;
}

function conditionFields(item) {
  return `<label><span>Priority</span><input name="priority" type="number" min="1" max="1000" value="${number(item.priority)}" required /></label>
    <label><span>HTTP statuses</span><input name="httpStatuses" maxlength="127" value="${escapeAttribute((item.http_statuses ?? []).join(","))}" placeholder="400,404,429" /></label>
    <label><span>HTTP class</span><select name="httpStatusClass">${options(item.http_status_class, [["", "None"], ["4xx", "4xx"]])}</select></label>
    <label><span>Error field</span><select name="errorField">${options(item.error_field, [["", "None"], ["error.code", "error.code"], ["error.type", "error.type"], ["code", "code"], ["type", "type"]])}</select></label>
    <label><span>Operator</span><select name="fieldOperator">${options(item.field_operator, [["", "None"], ["equals", "Equals"], ["contains", "Contains"]])}</select></label>
    <label><span>Safe value</span><input name="fieldPattern" maxlength="240" value="${escapeAttribute(item.field_pattern ?? "")}" /></label>
    <label><span>Output code</span><input name="outputErrorCode" maxlength="128" pattern="[A-Za-z0-9][A-Za-z0-9._:-]{0,127}" value="${escapeAttribute(item.output_error_code ?? "")}" required /></label>`;
}

function stepUpFields() {
  return `<label><span>Step-up</span><select name="method" data-workspace-provider-step-up-method>
      <option value="totp">Authenticator code</option><option value="webauthn">Security key</option></select></label>
    <label data-workspace-provider-step-up-code><span>Verification code</span>
      <input name="verificationCode" data-workspace-provider-sensitive type="password"
        inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></label>`;
}

function loadingState(state) {
  if (state.phase === "loading") return '<div class="empty-state">Loading current Workspace compatibility.</div>';
  if (state.phase === "permission_revoked") return '<div class="empty-state error-state">This Workspace role cannot read Provider overrides.</div>';
  return '<div class="empty-state">Compatibility is not loaded.</div>';
}

function notice(state) {
  if (state.error) {
    return `<div class="provider-action-error" role="alert">${escapeHtml(state.error.message)} request_id: ${escapeHtml(state.error.requestId ?? "-")}</div>`;
  }
  return state.feedback ? `<div class="provider-test-result" role="status">${escapeHtml(state.feedback)}</div>` : "";
}

function actionLabel(value) {
  return { enable: "Enable", disable: "Disable", archive: "Archive" }[value] ?? value;
}

function status(value) {
  const normalized = String(value ?? "unknown");
  return `<mark class="status-mark status-${escapeAttribute(normalized)}">${escapeHtml(normalized)}</mark>`;
}

function options(selected, values) {
  return values.map(([value, label]) => `<option value="${escapeAttribute(value)}"${String(selected ?? "") === value ? " selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

function shortHash(value) {
  const text = String(value ?? "");
  return text.length > 16 ? `${text.slice(0, 10)}...${text.slice(-6)}` : text || "-";
}

function shortId(value) {
  const text = String(value ?? "");
  return text.length > 20 ? `${text.slice(0, 12)}...${text.slice(-6)}` : text || "-";
}

function formatTime(value) {
  const date = new Date(String(value ?? ""));
  return Number.isFinite(date.getTime()) ? date.toISOString().replace("T", " ").slice(0, 19) + "Z" : "-";
}

function number(value) {
  return Number.isFinite(Number(value)) ? String(Number(value)) : "0";
}

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
