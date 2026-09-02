/** Safe Platform projection for PRD-143 Provider Engineering. */

const STATUS_LABELS = Object.freeze({
  legacy_baseline: "Legacy baseline",
  candidate: "Candidate",
  testing: "Testing",
  review_required: "Review required",
  approved: "Approved",
  released: "Released",
  deprecated: "Deprecated",
  revoked: "Revoked",
  queued: "Queued",
  running: "Running",
  passed: "Passed",
  failed: "Failed",
  cancelled: "Cancelled",
  skipped: "Skipped",
  legacy_reconciled: "Legacy reconciled",
  security_review: "Security review",
});

const ACTION_LABELS = Object.freeze({
  create_candidate: "Create candidate",
  run_contract: "Run fixed contract suite",
  review_failure: "Review failure",
  release: "Release",
  deprecate: "Deprecate",
  revoke: "Revoke",
  submit_review: "Submit security review",
  security_approve: "Security approve",
});

export function renderPlatformProviderEngineering(state = {}, permissions = new Set()) {
  const adapters = state.adapters?.adapters ?? [];
  const selected = adapters.find(
    (item) => item.provider_adapter_release_id === state.selectedAdapterId,
  ) ?? null;
  const runs = state.runs?.runs ?? [];
  const cases = state.cases?.cases ?? [];
  const busy = Array.isArray(state.loading) && state.loading.length > 0;
  const failed = adapters.reduce((total, item) => total + item.contract.failed, 0);
  const released = adapters.filter((item) => item.status === "released").length;
  return `
    <div data-platform-provider-engineering-root>
      <header class="platform-page-heading">
        <div><p>Adapter releases / Fixed CI / Builtin catalog</p><h1>Provider Engineering</h1></div>
        <button class="platform-icon-button" type="button"
          data-platform-action="provider-engineering-refresh" title="Refresh" aria-label="Refresh"
          ${busy ? "disabled" : ""}>↻</button>
      </header>
      ${renderNotice(state)}
      <section class="platform-metrics" aria-label="Provider Engineering summary">
        ${metric("Adapters", adapters.length, "current page")}
        ${metric("Released", released, "verified heads")}
        ${metric("Failed runs", failed, "safe aggregate")}
        ${metric("Page", (state.adapterHistory?.length ?? 0) + 1, "keyset cursor")}
      </section>
      ${renderAdapterDirectory(state, adapters, selected, permissions, busy)}
      ${selected ? renderContractWorkspace(state, selected, runs, cases, permissions, busy) : ""}
      ${renderCatalogWorkspace(state, permissions, busy)}
    </div>`;
}

function renderNotice(state) {
  const error = state.error;
  if (error) {
    return `<div class="platform-feedback error" role="alert">
      ${escapeHtml(error.message)} · request_id: ${escapeHtml(error.requestId ?? "-")}
    </div>`;
  }
  if (state.feedback) {
    return `<div class="platform-feedback" role="status">${escapeHtml(state.feedback)}</div>`;
  }
  if (state.phase === "permission_revoked") {
    return '<div class="platform-feedback error" role="alert">Provider Engineering permission was revoked.</div>';
  }
  if (state.phase === "dependency_unavailable") {
    return '<div class="platform-feedback error" role="alert">Provider Engineering dependency is unavailable.</div>';
  }
  return "";
}

function renderAdapterDirectory(state, adapters, selected, permissions, busy) {
  const filters = state.filters ?? {};
  return `
    <section class="platform-band platform-provider-engineering-directory">
      <div class="platform-section-heading">
        <div><p>Signed artifact references only</p><h2>Adapter release directory</h2></div>
        <span>${number(adapters.length)} releases</span>
      </div>
      <form class="platform-filter-form platform-provider-engineering-filters"
        data-platform-provider-engineering-filter-form autocomplete="off">
        <label class="platform-filter-search"><span>Adapter or release ID</span>
          <input name="adapter" maxlength="64" value="${escapeAttribute(filters.adapter ?? "")}" /></label>
        <label><span>Status</span><select name="status">${options(filters.status, [
          ["", "All"], ["legacy_baseline", "Legacy baseline"], ["candidate", "Candidate"],
          ["testing", "Testing"], ["review_required", "Review required"], ["approved", "Approved"],
          ["released", "Released"], ["deprecated", "Deprecated"], ["revoked", "Revoked"],
        ])}</select></label>
        <label><span>Protocol</span><select name="protocol">${options(filters.protocol, [
          ["", "All"], ["openai_compatible", "OpenAI compatible"],
          ["anthropic_compatible", "Anthropic compatible"], ["generic_http", "Generic HTTP"],
          ["legacy_catalog", "Legacy catalog"],
        ])}</select></label>
        <label><span>Generation</span><input name="releaseGeneration" type="number" min="0"
          value="${escapeAttribute(filters.release_generation ?? "")}" /></label>
        <label><span>Compatibility SHA-256</span><input name="compatibility" minlength="64" maxlength="64"
          value="${escapeAttribute(filters.compatibility ?? "")}" /></label>
        <button class="platform-primary" type="submit" ${busy ? "disabled" : ""}>Apply</button>
      </form>
      <div class="platform-table-wrap">
        <table class="platform-provider-engineering-adapters">
          <thead><tr><th>Adapter</th><th>Release</th><th>Status</th><th>Contract</th><th>Readiness</th><th>Updated</th></tr></thead>
          <tbody>${adapters.map((item) => adapterRow(item, selected, busy)).join("")
            || emptyRow(state.phase === "loading" ? "Loading adapter releases" : "No adapter releases match the filters", 6)}</tbody>
        </table>
      </div>
      ${pagination("adapters", state.adapterHistory?.length > 0, Boolean(state.adapters?.next_cursor), busy)}
      ${renderAdapterDetail(selected, permissions, busy)}
      ${permissions.has("platform.provider_adapter.candidate_manage")
        ? renderCandidateForm(busy) : ""}
    </section>`;
}

function adapterRow(item, selected, busy) {
  const active = selected?.provider_adapter_release_id === item.provider_adapter_release_id;
  return `<tr class="${active ? "is-selected" : ""}">
    <td data-label="Adapter"><button type="button" class="platform-link-button"
      data-platform-provider-adapter-select="${escapeAttribute(item.provider_adapter_release_id)}"
      ${busy ? "disabled" : ""}><strong>${escapeHtml(item.adapter_code)}</strong></button>
      <small>${escapeHtml(item.protocol_family)}</small></td>
    <td data-label="Release"><strong>${escapeHtml(item.release_version)}</strong><small>generation ${number(item.release_generation)}</small></td>
    <td data-label="Status">${status(item.status)}<small>${item.is_head ? "current head" : "history"}</small></td>
    <td data-label="Contract"><strong>${number(item.contract.passed)} passed · ${number(item.contract.failed)} failed</strong>
      <small>${number(item.contract.running)} running · ${number(item.contract.queued)} queued</small></td>
    <td data-label="Readiness">${status(item.readiness)}</td>
    <td data-label="Updated">${formatTime(item.updated_at)}</td>
  </tr>`;
}

function renderAdapterDetail(item, permissions, busy) {
  if (!item) return "";
  return `<section class="platform-provider-engineering-detail" aria-labelledby="provider-adapter-detail-title">
    <div class="platform-section-heading"><div><p>Safe release projection</p>
      <h3 id="provider-adapter-detail-title">${escapeHtml(item.adapter_code)} ${escapeHtml(item.release_version)}</h3></div>
      ${status(item.status)}</div>
    <dl class="platform-provider-engineering-proof">
      ${proof("Artifact", item.artifact_sha256)}${proof("SBOM", item.sbom_sha256)}
      ${proof("Provenance", item.provenance_sha256)}${proof("Signature", item.signature_sha256)}
      ${proof("Spec manifest", item.spec_manifest_sha256)}${proof("Compatibility", item.compatibility_sha256)}
      <div><dt>Runtime / API</dt><dd>${escapeHtml(item.required_runtime_version)} · ${escapeHtml(item.required_api_version)}</dd></div>
      <div><dt>Revision</dt><dd>${number(item.revision)}</dd></div>
    </dl>
    <div class="platform-provider-engineering-actions">
      ${(item.actions ?? []).filter((action) => action !== "review_failure")
        .filter((action) => adapterPermission(permissions, action))
        .map((action) => adapterActionForm(item, action, busy)).join("")}
    </div>
  </section>`;
}

function renderCandidateForm(busy) {
  return `<details class="platform-provider-engineering-create">
    <summary>Create signed Adapter candidate</summary>
    <form data-platform-provider-adapter-candidate-form autocomplete="off">
      <label><span>Adapter code</span><input name="adapterCode" pattern="[a-z0-9][a-z0-9._-]{1,63}" maxlength="64" required /></label>
      <label><span>SDK release ID</span><input name="sdkReleaseId" pattern="sdr_[0-9a-f]{32}" required /></label>
      <label><span>Release version</span><input name="releaseVersion" pattern="[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?" required /></label>
      <label><span>Protocol</span><select name="protocolFamily"><option value="openai_compatible">OpenAI compatible</option><option value="anthropic_compatible">Anthropic compatible</option><option value="generic_http">Generic HTTP</option></select></label>
      <label><span>Spec manifest version</span><input name="specManifestVersion" maxlength="80" required /></label>
      ${hashInput("specManifestSha256", "Spec manifest SHA-256")}
      ${hashInput("compatibilitySha256", "Compatibility SHA-256")}
      <label><span>Required runtime</span><input name="requiredRuntimeVersion" maxlength="80" required /></label>
      <label><span>Required API</span><input name="requiredApiVersion" pattern="[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?" required /></label>
      ${stepUpFields()}
      <button class="platform-primary" type="submit" ${busy ? "disabled" : ""}>Create candidate</button>
    </form>
  </details>`;
}

function adapterActionForm(item, action, busy) {
  return `<form data-platform-provider-adapter-action-form autocomplete="off">
    <input type="hidden" name="adapterId" value="${escapeAttribute(item.provider_adapter_release_id)}" />
    <input type="hidden" name="action" value="${escapeAttribute(action)}" />
    <input type="hidden" name="expectedRevision" value="${number(item.revision)}" />
    ${stepUpFields()}
    <button class="${new Set(["revoke", "deprecate"]).has(action) ? "platform-danger" : "platform-primary"}"
      type="submit" ${busy ? "disabled" : ""}>${escapeHtml(ACTION_LABELS[action] ?? action)}</button>
  </form>`;
}

function renderContractWorkspace(state, selected, runs, cases, permissions, busy) {
  const filters = state.runFilters ?? {};
  const selectedRun = runs.find((item) => item.contract_run_id === state.selectedRunId) ?? null;
  return `<section class="platform-band platform-provider-engineering-contracts">
    <div class="platform-section-heading"><div><p>Fixed runner safe projection</p><h2>Contract runs</h2></div>
      <span>${escapeHtml(selected.adapter_code)} · ${number(runs.length)} runs</span></div>
    <form class="platform-filter-form" data-platform-provider-contract-filter-form autocomplete="off">
      <label><span>Scope</span><select name="scope">${options(filters.scope, [["", "All"], ["platform_release_ci", "Platform release CI"], ["legacy_tenant_merge_gate", "Legacy merge gate"], ["legacy_tenant_probe", "Legacy probe"]])}</select></label>
      <label><span>Status</span><select name="status">${options(filters.status, [["", "All"], ["queued", "Queued"], ["running", "Running"], ["passed", "Passed"], ["failed", "Failed"], ["cancelled", "Cancelled"]])}</select></label>
      <label><span>Suite</span><select name="suite">${options(filters.suite, [["", "All"], ["release_ci", "Release CI"], ["merge_gate", "Merge gate"], ["probe", "Probe"]])}</select></label>
      <label><span>Mode</span><select name="mode">${options(filters.mode, [["", "All"], ["isolated_runner", "Isolated runner"], ["mock", "Mock"], ["gateway_probe", "Gateway probe"]])}</select></label>
      <label><span>From UTC</span><input name="from" type="datetime-local" value="${localTime(filters.from)}" /></label>
      <label><span>To UTC</span><input name="to" type="datetime-local" value="${localTime(filters.to)}" /></label>
      <button class="platform-primary" type="submit" ${busy ? "disabled" : ""}>Apply</button>
    </form>
    <div class="platform-table-wrap"><table class="platform-provider-engineering-runs">
      <thead><tr><th>Run</th><th>Scope</th><th>Status</th><th>Cases</th><th>Proof</th><th>Completed</th></tr></thead>
      <tbody>${runs.map((run) => runRow(run, selectedRun, busy)).join("") || emptyRow("No contract runs", 6)}</tbody>
    </table></div>
    ${pagination("runs", state.runHistory?.length > 0, Boolean(state.runs?.next_cursor), busy)}
    ${selectedRun ? renderSelectedRun(state, selectedRun, cases, permissions, busy) : ""}
  </section>`;
}

function runRow(run, selected, busy) {
  const active = selected?.contract_run_id === run.contract_run_id;
  return `<tr class="${active ? "is-selected" : ""}">
    <td data-label="Run"><button type="button" class="platform-link-button"
      data-platform-provider-run-select="${escapeAttribute(run.contract_run_id)}" ${busy ? "disabled" : ""}>
      ${escapeHtml(shortId(run.contract_run_id))}</button><small>generation ${number(run.generation)}</small></td>
    <td data-label="Scope"><strong>${escapeHtml(run.scope)}</strong><small>${run.legacy ? "legacy projection" : run.mode}</small></td>
    <td data-label="Status">${status(run.status)}<small>${run.qualifies_release ? "qualifies release" : "does not qualify"}</small></td>
    <td data-label="Cases"><strong>${number(run.passed_cases)} / ${number(run.total_cases)}</strong><small>${number(run.failed_cases)} failed</small></td>
    <td data-label="Proof"><code>${escapeHtml(shortHash(run.attestation_sha256))}</code><small>${run.coverage_complete ? "coverage complete" : "coverage incomplete"}</small></td>
    <td data-label="Completed">${formatTime(run.completed_at)}</td>
  </tr>`;
}

function renderSelectedRun(state, run, cases, permissions, busy) {
  const canReview = permissions.has("platform.provider_adapter.failure_review")
    && run.status === "failed";
  return `<section class="platform-provider-engineering-cases" aria-labelledby="provider-contract-case-title">
    <div class="platform-section-heading"><div><p>${escapeHtml(shortId(run.contract_run_id))}</p>
      <h3 id="provider-contract-case-title">Contract cases</h3></div><span>${number(cases.length)} cases</span></div>
    ${canReview ? `<form data-platform-provider-failure-review-form autocomplete="off">
      <input type="hidden" name="adapterId" value="${escapeAttribute(state.selectedAdapterId)}" />
      <input type="hidden" name="contractRunId" value="${escapeAttribute(run.contract_run_id)}" />
      <input type="hidden" name="expectedRevision" value="${number(selectedAdapter(state)?.revision ?? 0)}" />
      <label><span>Disposition</span><select name="failureDisposition"><option value="reject">Reject</option><option value="approve_remediation">Approve remediation</option></select></label>
      ${stepUpFields()}
      <button class="platform-primary" type="submit" ${busy ? "disabled" : ""}>Review failure</button>
    </form>` : ""}
    <div class="platform-table-wrap"><table class="platform-provider-engineering-case-table">
      <thead><tr><th>#</th><th>Case proof</th><th>Kind / variant</th><th>Provider / Adapter</th><th>Status</th><th>Duration</th><th>Evidence</th></tr></thead>
      <tbody>${cases.map(caseRow).join("") || emptyRow(state.cases ? "No projected cases" : "Select a run to load cases", 7)}</tbody>
    </table></div>
    ${pagination("cases", state.caseHistory?.length > 0, Boolean(state.cases?.next_cursor), busy)}
  </section>`;
}

function caseRow(item) {
  return `<tr><td data-label="#">${number(item.ordinal)}</td>
    <td data-label="Case proof"><code>${escapeHtml(shortHash(item.case_key_sha256))}</code></td>
    <td data-label="Kind / variant"><strong>${escapeHtml(item.case_kind)}</strong><small>${escapeHtml(item.variant)}</small></td>
    <td data-label="Provider / Adapter"><strong>${escapeHtml(item.provider_code)}</strong><small>${escapeHtml(item.adapter_code)}</small></td>
    <td data-label="Status">${status(item.status)}<small>${escapeHtml(item.failure_code ?? "")}</small></td>
    <td data-label="Duration">${escapeHtml(item.duration_bucket)}</td>
    <td data-label="Evidence"><code>${escapeHtml(shortHash(item.evidence_sha256))}</code></td></tr>`;
}

function renderCatalogWorkspace(state, permissions, busy) {
  const catalog = state.catalog;
  const canCreate = permissions.has("platform.error_catalog.candidate_manage");
  const actions = catalog?.actions ?? [];
  return `<section class="platform-band platform-provider-engineering-catalog">
    <div class="platform-section-heading"><div><p>Immutable builtin rule identity</p><h2>Error catalog release</h2></div>
      ${catalog ? status(catalog.status) : '<span>No active command result</span>'}</div>
    ${catalog ? `<dl class="platform-provider-engineering-proof">
      <div><dt>Version</dt><dd>${escapeHtml(catalog.builtin_catalog_version)}</dd></div>
      <div><dt>Rules</dt><dd>${number(catalog.rule_count)}</dd></div>
      ${proof("Catalog", catalog.catalog_sha256)}${proof("Artifact", catalog.artifact_sha256)}
      ${proof("Compatibility", catalog.compatibility_sha256)}
      <div><dt>Revision / generation</dt><dd>${number(catalog.revision)} / ${number(catalog.release_generation)}</dd></div>
    </dl>
    <div class="platform-provider-engineering-actions">${actions
      .filter((action) => catalogPermission(permissions, action))
      .map((action) => catalogActionForm(catalog, action, busy)).join("")}</div>` : ""}
    ${canCreate ? `<details class="platform-provider-engineering-create"><summary>Create signed catalog candidate</summary>
      <form data-platform-provider-catalog-candidate-form autocomplete="off">
        <label><span>Builtin catalog version</span><input name="builtinCatalogVersion" maxlength="80" required /></label>
        <label><span>SDK release ID</span><input name="sdkReleaseId" pattern="sdr_[0-9a-f]{32}" required /></label>
        <label><span>Signed candidate ID</span><input name="signedCandidateId" pattern="[A-Za-z0-9][A-Za-z0-9._:-]{0,127}" maxlength="128" required /></label>
        ${stepUpFields()}
        <button class="platform-primary" type="submit" ${busy ? "disabled" : ""}>Create candidate</button>
      </form></details>` : ""}
  </section>`;
}

function catalogActionForm(catalog, action, busy) {
  return `<form data-platform-provider-catalog-action-form autocomplete="off">
    <input type="hidden" name="catalogId" value="${escapeAttribute(catalog.error_catalog_release_id)}" />
    <input type="hidden" name="action" value="${escapeAttribute(action)}" />
    <input type="hidden" name="expectedRevision" value="${number(catalog.revision)}" />
    ${stepUpFields()}
    <button class="${action === "deprecate" ? "platform-danger" : "platform-primary"}" type="submit"
      ${busy ? "disabled" : ""}>${escapeHtml(ACTION_LABELS[action] ?? action)}</button>
  </form>`;
}

function stepUpFields() {
  return `<label><span>Step-up</span><select name="method" data-platform-provider-engineering-step-up-method>
      <option value="totp">Authenticator code</option><option value="webauthn">Security key</option></select></label>
    <label data-platform-provider-engineering-step-up-code><span>Verification code</span>
      <input name="verificationCode" data-platform-provider-engineering-sensitive type="password"
        inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></label>`;
}

function hashInput(name, label) {
  return `<label><span>${escapeHtml(label)}</span><input name="${escapeAttribute(name)}" minlength="64" maxlength="64" pattern="[0-9a-f]{64}" required /></label>`;
}

function adapterPermission(permissions, action) {
  return permissions.has({
    run_contract: "platform.provider_adapter.contract_run",
    review_failure: "platform.provider_adapter.failure_review",
    release: "platform.provider_adapter.release",
    deprecate: "platform.provider_adapter.release",
    revoke: "platform.provider_adapter.release",
  }[action]);
}

function catalogPermission(permissions, action) {
  return permissions.has({
    submit_review: "platform.error_catalog.candidate_manage",
    security_approve: "platform.error_catalog.security_approve",
    release: "platform.error_catalog.release",
    deprecate: "platform.error_catalog.release",
  }[action]);
}

function selectedAdapter(state) {
  return state.adapters?.adapters?.find(
    (item) => item.provider_adapter_release_id === state.selectedAdapterId,
  ) ?? null;
}

function pagination(kind, canPrevious, canNext, busy) {
  return `<div class="platform-pagination"><span>Opaque keyset pagination</span><div>
    <button type="button" data-platform-provider-page="${kind}" data-direction="previous"
      ${canPrevious && !busy ? "" : "disabled"}>Previous</button>
    <button type="button" data-platform-provider-page="${kind}" data-direction="next"
      ${canNext && !busy ? "" : "disabled"}>Next</button></div></div>`;
}

function proof(label, value) {
  return `<div><dt>${escapeHtml(label)}</dt><dd><code>${escapeHtml(shortHash(value))}</code></dd></div>`;
}

function metric(label, value, hint) {
  return `<article><span>${escapeHtml(label)}</span><strong>${number(value)}</strong><small>${escapeHtml(hint)}</small></article>`;
}

function status(value) {
  const normalized = String(value ?? "unknown");
  return `<mark class="platform-status status-${escapeAttribute(normalized)}">${escapeHtml(STATUS_LABELS[normalized] ?? normalized)}</mark>`;
}

function options(selected, values) {
  return values.map(([value, label]) => `<option value="${escapeAttribute(value)}"${String(selected ?? "") === value ? " selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

function emptyRow(message, columns) {
  return `<tr><td colspan="${number(columns)}" class="platform-empty">${escapeHtml(message)}</td></tr>`;
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

function localTime(value) {
  const date = new Date(String(value ?? ""));
  return Number.isFinite(date.getTime()) ? date.toISOString().slice(0, 16) : "";
}

function number(value) {
  const normalized = Number(value);
  return Number.isFinite(normalized) ? new Intl.NumberFormat("en-US").format(normalized) : "0";
}

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
