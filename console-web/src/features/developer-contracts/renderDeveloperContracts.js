/** PRD-133 public API lifecycle projection for the existing API docs page. */

export function renderDeveloperContracts(state) {
  if (state?.status === "error") {
    return `<section class="panel developer-contracts-panel" aria-labelledby="developer-contracts-title">
      <h2 id="developer-contracts-title">API 版本与 SDK</h2>
      <p role="alert">暂时无法读取 API 文档，请重试。</p>
      <button type="button" data-developer-contract-retry>重试</button>
    </section>`;
  }
  if (!state) {
    return `
      <section class="panel developer-contracts-panel" aria-labelledby="developer-contracts-title">
        <header class="developer-contracts-heading">
          <div><p>公开接口契约</p><h2 id="developer-contracts-title">API 版本与 SDK</h2></div>
          <span class="developer-contracts-state">加载中</span>
        </header>
      </section>`;
  }
  const versions = state.versions ?? [];
  const releases = state.sdk_releases ?? [];
  const supplyChain = state.supply_chain ?? {};
  const entries = state.changelog?.entries ?? [];
  const schemas = state.schemas ?? [];
  const hasPublishedContent = versions.length > 0 || releases.length > 0 || entries.length > 0 || schemas.length > 0;
  return `
    <section class="panel developer-contracts-panel" aria-labelledby="developer-contracts-title">
      <header class="developer-contracts-heading">
        <div><p>公开接口契约</p><h2 id="developer-contracts-title">API 版本与 SDK</h2></div>
        <span class="developer-contracts-state">${number(versions.length)} 个版本</span>
      </header>
      <p class="developer-contracts-feedback" data-developer-contract-feedback aria-live="polite"></p>
      ${hasPublishedContent ? `<div class="developer-contracts-summary" aria-label="契约概览">
        ${summary("已发布 API", versions.filter((item) => item.status === "published").length)}
        ${summary("安全检查通过", releases.filter((item) => (
          supplyChain[item.sdk_release_id]?.report?.gate_outcome === "passed"
        )).length)}
        ${summary("Webhook 结构", schemas.length)}
        ${summary("版本事件", entries.length)}
      </div>` : ""}
      ${hasPublishedContent ? `<section class="developer-contracts-band" aria-labelledby="developer-api-versions-title">
        <div class="developer-contracts-section-heading">
          <div><p>兼容性</p><h3 id="developer-api-versions-title">API 版本</h3></div>
        </div>
        ${versions.length ? versionTable(versions) : empty("暂无已发布的 API 版本。")}
      </section>
      <section class="developer-contracts-band" aria-labelledby="developer-sdk-releases-title">
        <div class="developer-contracts-section-heading">
          <div><p>已验证制品</p><h3 id="developer-sdk-releases-title">SDK 发布版本</h3></div>
        </div>
        ${releases.length ? sdkTable(releases, supplyChain) : empty("暂无已签名的 SDK 版本。")}
      </section>
      <section class="developer-contracts-band" aria-labelledby="developer-security-evidence-title">
        <div class="developer-contracts-section-heading">
          <div><p>发布验证</p><h3 id="developer-security-evidence-title">供应链安全证据</h3></div>
        </div>
        ${securityEvidence(releases, supplyChain)}
      </section>
      <section class="developer-contracts-band" aria-labelledby="developer-changelog-title">
        <div class="developer-contracts-section-heading">
          <div><p>迁移与生命周期</p><h3 id="developer-changelog-title">更新记录</h3></div>
        </div>
        ${entries.length ? changelogList(entries) : empty("暂无版本变更记录。")}
      </section>
      <section class="developer-contracts-band" aria-labelledby="developer-webhook-schemas-title">
        <div class="developer-contracts-section-heading">
          <div><p>事件契约</p><h3 id="developer-webhook-schemas-title">Webhook 数据结构</h3></div>
        </div>
        ${schemas.length ? webhookTable(schemas) : empty("暂无已发布的 Webhook 数据结构。")}
      </section>` : `<section class="developer-contracts-empty-band" aria-label="发布内容为空">
        <h3>暂无已发布内容</h3>
        <p>当前还没有可供组织成员使用的 API 版本、SDK 或 Webhook 契约。发布后，版本、安全检查和更新记录会显示在这里。</p>
      </section>`}
    </section>`;
}

function versionTable(versions) {
  return `<div class="developer-contracts-table-wrap"><table>
    <thead><tr><th>版本</th><th>状态</th><th>兼容性</th><th>契约摘要</th><th>发布时间</th></tr></thead>
    <tbody>${versions.map((item) => `<tr>
      <td data-label="版本"><strong>${escapeHtml(item.version)}</strong><small>登记修订 ${number(item.registry_revision)}</small></td>
      <td data-label="状态">${status(item.status)}</td>
      <td data-label="兼容性"><strong>${escapeHtml(stateLabel(item.compatibility_outcome))}</strong>${item.breaking_reasons.length ? `<small>${escapeHtml(item.breaking_reasons.join(", "))}</small>` : ""}</td>
      <td data-label="契约摘要"><code>${escapeHtml(shortHash(item.contract_sha256))}</code></td>
      <td data-label="发布时间">${formatTime(item.published_at)}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function sdkTable(releases, supplyChain) {
  return `<div class="developer-contracts-table-wrap"><table>
    <thead><tr><th>SDK</th><th>API</th><th>冒烟测试</th><th>签名验证</th><th>安全检查</th><th>制品</th></tr></thead>
    <tbody>${releases.map((item) => `<tr>
      <td data-label="SDK"><strong>${escapeHtml(languageLabel(item.language))} ${escapeHtml(item.sdk_version)}</strong><small>${escapeHtml(item.runtime_version)}</small></td>
      <td data-label="API">${escapeHtml(item.api_version)}</td>
      <td data-label="冒烟测试"><strong>${escapeHtml(stateLabel(item.smoke_matrix.windows))} / ${escapeHtml(stateLabel(item.smoke_matrix.linux))}</strong><small>Windows / Linux · 当前及上一版本</small></td>
      <td data-label="签名验证"><code>${escapeHtml(shortHash(item.signature_sha256))}</code><small>${escapeHtml(item.signing_key_id ?? "-")}</small></td>
      <td data-label="安全检查">${securityCell(item, supplyChain[item.sdk_release_id])}</td>
      <td data-label="制品">${item.status === "published" ? `<button class="developer-contract-download" type="button" data-developer-sdk-download data-sdk-release-id="${escapeAttribute(item.sdk_release_id)}" data-language="${escapeAttribute(item.language)}" data-sdk-version="${escapeAttribute(item.sdk_version)}" data-artifact-sha256="${escapeAttribute(item.artifact_sha256)}" data-artifact-receipt-sha256="${escapeAttribute(item.artifact_receipt_sha256)}" title="下载已验证的 SDK">下载</button>` : status(item.status)}<small><code>${escapeHtml(shortHash(item.artifact_sha256))}</code></small></td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function securityCell(release, evidence) {
  if (release.status !== "published") return status("pending");
  if (!evidence || evidence.status === "missing") {
    return `${status("proof_missing")}<small>暂不可安装</small>`;
  }
  const report = evidence.report;
  return `${status(report.gate_outcome)}<small>${number(report.sbom.component_count)} 个组件</small>`;
}

function securityEvidence(releases, supplyChain) {
  const published = releases.filter((item) => item.status === "published");
  if (!published.length) return empty("暂无已发布制品。");
  return `<div class="developer-security-evidence-list">${published.map((release) => {
    const evidence = supplyChain[release.sdk_release_id];
    if (!evidence || evidence.status === "missing") {
      return `<article class="developer-security-evidence is-blocked">
        <header><div><strong>${escapeHtml(languageLabel(release.language))} ${escapeHtml(release.sdk_version)}</strong><small><code>${escapeHtml(shortHash(release.artifact_sha256))}</code></small></div>${status("proof_missing")}</header>
        <p>缺少已签名的安全证据，暂不可安装。</p>
      </article>`;
    }
    const report = evidence.report;
    const provenance = evidence.provenance;
    return `<article class="developer-security-evidence is-${escapeAttribute(report.gate_outcome)}">
      <header><div><strong>${escapeHtml(languageLabel(report.language))} ${escapeHtml(report.sdk_version)}</strong><small>生成于 ${formatTime(report.generated_at)}</small></div>${status(report.gate_outcome)}</header>
      <dl>
        <div><dt>组件清单</dt><dd>${escapeHtml(report.sbom.format)} ${escapeHtml(report.sbom.format_version)} · ${number(report.sbom.component_count)} 个组件</dd></div>
        <div><dt>签名</dt><dd>${escapeHtml(stateLabel(report.signature.verification_outcome))} / 客户端 ${escapeHtml(stateLabel(report.signature.client_verification_outcome))} · <code>${escapeHtml(shortHash(report.signature.attestation_sha256))}</code></dd></div>
        <div><dt>构建来源</dt><dd>${provenance.builder_verified ? "已验证" : "未验证"} · ${provenance.reproducible ? "可复现" : "不可复现"}</dd></div>
        <div><dt>安全问题</dt><dd>${number(report.blocking_finding_count)} 项阻断 · ${number(report.vulnerabilities.length)} 项待处理</dd></div>
      </dl>
      ${report.vulnerabilities.length ? `<ol class="developer-security-findings">${report.vulnerabilities.map(securityFinding).join("")}</ol>` : '<p class="developer-contracts-empty">暂无待处理的安全问题。</p>'}
      <button type="button" class="developer-security-download" data-developer-security-download data-sdk-release-id="${escapeAttribute(report.sdk_release_id)}" title="下载安全报告">下载报告</button>
    </article>`;
  }).join("")}</div>`;
}

function securityFinding(item) {
  const exceptions = item.exceptions.map((entry) => (
    `${stateLabel(entry.status)}，到期 ${formatTime(entry.expires_at)}`
  )).join(", ") || "无";
  const patches = item.patch_releases.map((entry) => (
    `${stateLabel(entry.status)} · 云端及边缘当前和上一版本验证通过`
  )).join(", ") || "无";
  return `<li><div><strong>${escapeHtml(item.advisory_code)}</strong>${status(item.severity)}</div>
    <span>${escapeHtml(stateLabel(item.remediation_state))} · 处理期限 ${formatTime(item.sla_deadline_at)}</span>
    <small>例外：${escapeHtml(exceptions)} · 补丁：${escapeHtml(patches)}</small></li>`;
}

function changelogList(entries) {
  return `<ol class="developer-contracts-changelog">${entries.map((entry) => {
    const guide = entry.event_type === "api.deprecated"
      ? `<span class="developer-contracts-guide">迁移指南：<code>${escapeHtml(entry.metadata.migration_guide_code)}</code></span>`
      : "";
    return `<li>
      <div><strong>${escapeHtml(eventLabel(entry.event_type))}</strong>${status(entry.event_type)}</div>
      <p>${escapeHtml(eventSummary(entry))}</p>
      ${guide}
      <small>${formatTime(entry.occurred_at)} · 修订 ${number(entry.source_revision)}</small>
    </li>`;
  }).join("")}</ol>`;
}

function webhookTable(schemas) {
  return `<div class="developer-contracts-table-wrap"><table>
    <thead><tr><th>事件</th><th>数据结构</th><th>API</th><th>发布时间</th></tr></thead>
    <tbody>${schemas.map((item) => `<tr>
      <td data-label="事件"><strong>${escapeHtml(item.event_name)}</strong></td>
      <td data-label="数据结构"><span>v${number(item.schema_version)}</span><small><code>${escapeHtml(shortHash(item.schema_sha256))}</code></small></td>
      <td data-label="API">${escapeHtml(item.api_version)}</td>
      <td data-label="发布时间">${formatTime(item.published_at)}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

function eventSummary(entry) {
  const metadata = entry.metadata ?? {};
  if (entry.event_type === "api.deprecated") {
    return `API ${metadata.api_version} 迁移至 ${metadata.replacement_version}；旧版本停用时间：${formatTime(metadata.sunset_at)}。`;
  }
  if (entry.event_type === "api.sunset_scheduled") {
    return `API ${metadata.api_version} 计划于 ${formatTime(metadata.sunset_at)} 停用。`;
  }
  if (entry.event_type === "sdk.released") {
    return `${languageLabel(metadata.language)} SDK ${metadata.sdk_version}，适用 API ${metadata.api_version}。`;
  }
  return `${metadata.event_name} 数据结构 v${metadata.schema_version}，适用 API ${metadata.api_version}。`;
}

function eventLabel(value) {
  return ({
    "api.deprecated": "API 已弃用",
    "api.sunset_scheduled": "API 计划停用",
    "sdk.released": "SDK 已发布",
    "webhook.schema_published": "Webhook 数据结构已发布",
  })[value] ?? value;
}

function languageLabel(value) {
  return value === "javascript" ? "JavaScript" : value === "python" ? "Python" : value;
}

function summary(label, value) {
  return `<div><span>${escapeHtml(label)}</span><strong>${number(value)}</strong></div>`;
}

function status(value) {
  return `<span class="developer-contracts-status is-${escapeAttribute(String(value).replaceAll(".", "-"))}">${escapeHtml(stateLabel(value))}</span>`;
}

function stateLabel(value) {
  return ({ published: "已发布", pending: "待处理", passed: "通过", failed: "失败",
    blocked: "已阻断", proof_missing: "缺少证据", compatible: "兼容", breaking: "不兼容",
    approved: "已批准", expired: "已过期", revoked: "已撤销", open: "待处理",
    patched: "已修复", critical: "严重", high: "高", medium: "中", low: "低" })[value]
    ?? eventLabel(String(value ?? "-"));
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
