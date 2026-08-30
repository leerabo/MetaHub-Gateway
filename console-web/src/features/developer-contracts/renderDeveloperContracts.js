/** PRD-133 public API lifecycle projection for the existing API docs page. */

export function renderDeveloperContracts(state) {
  if (!state) {
    return `
      <section class="panel developer-contracts-panel" aria-labelledby="developer-contracts-title">
        <header class="developer-contracts-heading">
          <div><p>Public contract registry</p><h2 id="developer-contracts-title">API versions and SDKs</h2></div>
          <span class="developer-contracts-state">Loading</span>
        </header>
      </section>`;
  }
  const versions = state.versions ?? [];
  const releases = state.sdk_releases ?? [];
  const entries = state.changelog?.entries ?? [];
  const schemas = state.schemas ?? [];
  return `
    <section class="panel developer-contracts-panel" aria-labelledby="developer-contracts-title">
      <header class="developer-contracts-heading">
        <div><p>Public contract registry</p><h2 id="developer-contracts-title">API versions and SDKs</h2></div>
        <span class="developer-contracts-state">${number(versions.length)} versions</span>
      </header>
      <p class="developer-contracts-feedback" data-developer-contract-feedback aria-live="polite"></p>
      <div class="developer-contracts-summary" aria-label="Contract summary">
        ${summary("Published APIs", versions.filter((item) => item.status === "published").length)}
        ${summary("Signed SDKs", releases.filter((item) => item.status === "published").length)}
        ${summary("Webhook schemas", schemas.length)}
        ${summary("Lifecycle events", entries.length)}
      </div>
      <section class="developer-contracts-band" aria-labelledby="developer-api-versions-title">
        <div class="developer-contracts-section-heading">
          <div><p>Compatibility window</p><h3 id="developer-api-versions-title">API versions</h3></div>
        </div>
        ${versions.length ? versionTable(versions) : empty("No published API versions.")}
      </section>
      <section class="developer-contracts-band" aria-labelledby="developer-sdk-releases-title">
        <div class="developer-contracts-section-heading">
          <div><p>Verified artifacts</p><h3 id="developer-sdk-releases-title">SDK releases</h3></div>
        </div>
        ${releases.length ? sdkTable(releases) : empty("No signed SDK releases.")}
      </section>
      <section class="developer-contracts-band" aria-labelledby="developer-changelog-title">
        <div class="developer-contracts-section-heading">
          <div><p>Migration and lifecycle</p><h3 id="developer-changelog-title">Changelog</h3></div>
        </div>
        ${entries.length ? changelogList(entries) : empty("No lifecycle events.")}
      </section>
      <section class="developer-contracts-band" aria-labelledby="developer-webhook-schemas-title">
        <div class="developer-contracts-section-heading">
          <div><p>Exact event contracts</p><h3 id="developer-webhook-schemas-title">Webhook schemas</h3></div>
        </div>
        ${schemas.length ? webhookTable(schemas) : empty("No published webhook schemas.")}
      </section>
    </section>`;
}

function versionTable(versions) {
  return `<div class="developer-contracts-table-wrap"><table>
    <thead><tr><th>Version</th><th>Status</th><th>Compatibility</th><th>Contract</th><th>Published</th></tr></thead>
    <tbody>${versions.map((item) => `<tr>
      <td data-label="Version"><strong>${escapeHtml(item.version)}</strong><small>registry r${number(item.registry_revision)}</small></td>
      <td data-label="Status">${status(item.status)}</td>
      <td data-label="Compatibility"><strong>${escapeHtml(item.compatibility_outcome)}</strong>${item.breaking_reasons.length ? `<small>${escapeHtml(item.breaking_reasons.join(", "))}</small>` : ""}</td>
      <td data-label="Contract"><code>${escapeHtml(shortHash(item.contract_sha256))}</code></td>
      <td data-label="Published">${formatTime(item.published_at)}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function sdkTable(releases) {
  return `<div class="developer-contracts-table-wrap"><table>
    <thead><tr><th>SDK</th><th>API</th><th>Smoke</th><th>Verification</th><th>Artifact</th></tr></thead>
    <tbody>${releases.map((item) => `<tr>
      <td data-label="SDK"><strong>${escapeHtml(languageLabel(item.language))} ${escapeHtml(item.sdk_version)}</strong><small>${escapeHtml(item.runtime_version)}</small></td>
      <td data-label="API">${escapeHtml(item.api_version)}</td>
      <td data-label="Smoke"><strong>${escapeHtml(item.smoke_matrix.windows)} / ${escapeHtml(item.smoke_matrix.linux)}</strong><small>N and N-1</small></td>
      <td data-label="Verification"><code>${escapeHtml(shortHash(item.signature_sha256))}</code><small>${escapeHtml(item.signing_key_id ?? "-")}</small></td>
      <td data-label="Artifact">${item.status === "published" ? `<button class="developer-contract-download" type="button" data-developer-sdk-download data-sdk-release-id="${escapeAttribute(item.sdk_release_id)}" data-language="${escapeAttribute(item.language)}" data-sdk-version="${escapeAttribute(item.sdk_version)}" data-artifact-sha256="${escapeAttribute(item.artifact_sha256)}" data-artifact-receipt-sha256="${escapeAttribute(item.artifact_receipt_sha256)}" title="Download verified SDK">Download</button>` : status(item.status)}<small><code>${escapeHtml(shortHash(item.artifact_sha256))}</code></small></td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function changelogList(entries) {
  return `<ol class="developer-contracts-changelog">${entries.map((entry) => {
    const guide = entry.event_type === "api.deprecated"
      ? `<span class="developer-contracts-guide">Migration guide: <code>${escapeHtml(entry.metadata.migration_guide_code)}</code></span>`
      : "";
    return `<li>
      <div><strong>${escapeHtml(eventLabel(entry.event_type))}</strong>${status(entry.event_type)}</div>
      <p>${escapeHtml(eventSummary(entry))}</p>
      ${guide}
      <small>${formatTime(entry.occurred_at)} · revision ${number(entry.source_revision)}</small>
    </li>`;
  }).join("")}</ol>`;
}

function webhookTable(schemas) {
  return `<div class="developer-contracts-table-wrap"><table>
    <thead><tr><th>Event</th><th>Schema</th><th>API</th><th>Published</th></tr></thead>
    <tbody>${schemas.map((item) => `<tr>
      <td data-label="Event"><strong>${escapeHtml(item.event_name)}</strong></td>
      <td data-label="Schema"><span>v${number(item.schema_version)}</span><small><code>${escapeHtml(shortHash(item.schema_sha256))}</code></small></td>
      <td data-label="API">${escapeHtml(item.api_version)}</td>
      <td data-label="Published">${formatTime(item.published_at)}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function eventSummary(entry) {
  const metadata = entry.metadata ?? {};
  if (entry.event_type === "api.deprecated") {
    return `API ${metadata.api_version} moves to ${metadata.replacement_version}; sunset ${formatTime(metadata.sunset_at)}.`;
  }
  if (entry.event_type === "api.sunset_scheduled") {
    return `API ${metadata.api_version} is scheduled for sunset ${formatTime(metadata.sunset_at)}.`;
  }
  if (entry.event_type === "sdk.released") {
    return `${languageLabel(metadata.language)} SDK ${metadata.sdk_version} for API ${metadata.api_version}.`;
  }
  return `${metadata.event_name} schema v${metadata.schema_version} for API ${metadata.api_version}.`;
}

function eventLabel(value) {
  return ({
    "api.deprecated": "API deprecated",
    "api.sunset_scheduled": "API sunset scheduled",
    "sdk.released": "SDK released",
    "webhook.schema_published": "Webhook schema published",
  })[value] ?? value;
}

function languageLabel(value) {
  return value === "javascript" ? "JavaScript" : value === "python" ? "Python" : value;
}

function summary(label, value) {
  return `<div><span>${escapeHtml(label)}</span><strong>${number(value)}</strong></div>`;
}

function status(value) {
  return `<span class="developer-contracts-status is-${escapeAttribute(String(value).replaceAll(".", "-"))}">${escapeHtml(String(value).replaceAll("_", " "))}</span>`;
}

function empty(message) {
  return `<p class="developer-contracts-empty">${escapeHtml(message)}</p>`;
}

function shortHash(value) {
  const text = String(value ?? "");
  return text ? `${text.slice(0, 10)}...${text.slice(-8)}` : "-";
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isFinite(date.valueOf())
    ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(date)
    : "-";
}

function number(value) {
  return Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN").format(Number(value)) : "0";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/gu, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#96;");
}
