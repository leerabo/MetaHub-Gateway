/** Safe Platform projection for PRD-143 Provider Engineering. */

const STATUS_LABELS = Object.freeze({
  legacy_baseline: "历史基线",
  candidate: "候选版本",
  testing: "测试中",
  review_required: "待复核",
  approved: "已批准",
  released: "已发布",
  deprecated: "已弃用",
  revoked: "已撤销",
  queued: "排队中",
  running: "运行中",
  passed: "通过",
  failed: "失败",
  cancelled: "已取消",
  skipped: "已跳过",
  legacy_reconciled: "历史记录已对账",
  security_review: "安全复核",
});

const ACTION_LABELS = Object.freeze({
  create_candidate: "创建候选版本",
  run_contract: "执行契约测试",
  review_failure: "复核失败",
  release: "版本",
  deprecate: "弃用",
  revoke: "撤销",
  submit_review: "提交安全复核",
  security_approve: "安全批准",
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
        <div><p>接入规范、契约测试与内置目录</p><h1>供应商工程</h1></div>
        <button class="platform-icon-button" type="button"
          data-platform-action="provider-engineering-refresh" title="刷新" aria-label="刷新"
          ${busy ? "disabled" : ""}>↻</button>
      </header>
      ${renderNotice(state)}
      <section class="platform-metrics" aria-label="供应商工程摘要">
        ${metric("接入规范", adapters.length, "当前页")}
        ${metric("已发布", released, "已验证的当前版本")}
        ${metric("失败测试", failed, "汇总记录")}
        ${metric("页码", (state.adapterHistory?.length ?? 0) + 1, "分页位置")}
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
        <div><p>已签名版本</p><h2>接入规范版本目录</h2></div>
        <span>${number(adapters.length )} 个版本</span>
      </div>
      <form class="platform-filter-form platform-provider-engineering-filters"
        data-platform-provider-engineering-filter-form autocomplete="off">
        <label class="platform-filter-search"><span>接入规范或版本编号</span>
          <input name="adapter" maxlength="64" value="${escapeAttribute(filters.adapter ?? "")}" /></label>
        <label><span>状态</span><select name="status">${options(filters.status, [
          ["", "全部"], ["legacy_baseline", "历史基线"], ["candidate", "候选版本"],
          ["testing", "测试中"], ["review_required", "待复核"], ["approved", "已批准"],
          ["released", "已发布"], ["deprecated", "已弃用"], ["revoked", "已撤销"],
        ])}</select></label>
        <label><span>协议</span><select name="protocol">${options(filters.protocol, [
          ["", "全部"], ["openai_compatible", "OpenAI 兼容协议"],
          ["anthropic_compatible", "Anthropic 兼容协议"], ["generic_http", "通用 HTTP"],
          ["legacy_catalog", "历史目录"],
        ])}</select></label>
        <label><span>发布代数</span><input name="releaseGeneration" type="number" min="0"
          value="${escapeAttribute(filters.release_generation ?? "")}" /></label>
        <label><span>兼容性校验值</span><input name="compatibility" minlength="64" maxlength="64"
          value="${escapeAttribute(filters.compatibility ?? "")}" /></label>
        <button class="platform-primary" type="submit" ${busy ? "disabled" : ""}>筛选</button>
      </form>
      <div class="platform-table-wrap">
        <table class="platform-provider-engineering-adapters">
          <thead><tr><th>接入规范</th><th>版本</th><th>状态</th><th>契约测试</th><th>就绪状态</th><th>更新时间</th></tr></thead>
          <tbody>${adapters.map((item) => adapterRow(item, selected, busy)).join("")
            || emptyRow(state.phase === "loading" ? "正在加载接入规范版本" : "没有符合筛选条件的接入规范版本", 6)}</tbody>
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
    <td data-label="接入规范"><button type="button" class="platform-link-button"
      data-platform-provider-adapter-select="${escapeAttribute(item.provider_adapter_release_id)}"
      ${busy ? "disabled" : ""}><strong>${escapeHtml(item.adapter_code)}</strong></button>
      <small>${escapeHtml(item.protocol_family)}</small></td>
    <td data-label="版本"><strong>${escapeHtml(item.release_version)}</strong><small>generation ${number(item.release_generation)}</small></td>
    <td data-label="状态">${status(item.status)}<small>${item.is_head ? "current head" : "history"}</small></td>
    <td data-label="契约测试"><strong>${number(item.contract.passed)} passed · ${number(item.contract.failed)} failed</strong>
      <small>${number(item.contract.running)} running · ${number(item.contract.queued)} queued</small></td>
    <td data-label="就绪状态">${status(item.readiness)}</td>
    <td data-label="更新时间">${formatTime(item.updated_at)}</td>
  </tr>`;
}

function renderAdapterDetail(item, permissions, busy) {
  if (!item) return "";
  return `<section class="platform-provider-engineering-detail" aria-labelledby="provider-adapter-detail-title">
    <div class="platform-section-heading"><div><p>版本信息</p>
      <h3 id="provider-adapter-detail-title">${escapeHtml(item.adapter_code)} ${escapeHtml(item.release_version)}</h3></div>
      ${status(item.status)}</div>
    <dl class="platform-provider-engineering-proof">
      ${proof("产物校验", item.artifact_sha256)}${proof("SBOM", item.sbom_sha256)}
      ${proof("来源证明", item.provenance_sha256)}${proof("签名校验", item.signature_sha256)}
      ${proof("规范清单", item.spec_manifest_sha256)}${proof("兼容性", item.compatibility_sha256)}
      <div><dt>运行时与接口</dt><dd>${escapeHtml(item.required_runtime_version)} · ${escapeHtml(item.required_api_version)}</dd></div>
      <div><dt>修订号</dt><dd>${number(item.revision)}</dd></div>
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
    <summary>创建已签名接入规范版本</summary>
    <form data-platform-provider-adapter-candidate-form autocomplete="off">
      <label><span>接入规范代码</span><input name="adapterCode" pattern="[a-z0-9][a-z0-9._-]{1,63}" maxlength="64" required /></label>
      <label><span>SDK 版本编号</span><input name="sdkReleaseId" pattern="sdr_[0-9a-f]{32}" required /></label>
      <label><span>版本号</span><input name="releaseVersion" pattern="[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?" required /></label>
      <label><span>协议</span><select name="protocolFamily"><option value="openai_compatible">OpenAI 兼容协议</option><option value="anthropic_compatible">Anthropic 兼容协议</option><option value="generic_http">通用 HTTP</option></select></label>
      <label><span>规范清单版本</span><input name="specManifestVersion" maxlength="80" required /></label>
      ${hashInput("specManifestSha256", "规范清单校验值")}
      ${hashInput("compatibilitySha256", "兼容性校验值")}
      <label><span>所需运行时版本</span><input name="requiredRuntimeVersion" maxlength="80" required /></label>
      <label><span>所需接口版本</span><input name="requiredApiVersion" pattern="[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?" required /></label>
      ${stepUpFields()}
      <button class="platform-primary" type="submit" ${busy ? "disabled" : ""}>创建候选版本</button>
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
    <div class="platform-section-heading"><div><p>契约测试结果</p><h2>契约测试记录</h2></div>
      <span>${escapeHtml(selected.adapter_code)} · ${number(runs.length )} 次执行</span></div>
    <form class="platform-filter-form" data-platform-provider-contract-filter-form autocomplete="off">
      <label><span>适用范围</span><select name="scope">${options(filters.scope, [["", "全部"], ["platform_release_ci", "平台发布测试"], ["legacy_tenant_merge_gate", "历史合并检查"], ["legacy_tenant_probe", "历史探测"]])}</select></label>
      <label><span>状态</span><select name="status">${options(filters.status, [["", "全部"], ["queued", "排队中"], ["running", "运行中"], ["passed", "通过"], ["failed", "失败"], ["cancelled", "已取消"]])}</select></label>
      <label><span>测试集</span><select name="suite">${options(filters.suite, [["", "全部"], ["release_ci", "发布测试"], ["merge_gate", "合并检查"], ["probe", "探测"]])}</select></label>
      <label><span>运行模式</span><select name="mode">${options(filters.mode, [["", "全部"], ["isolated_runner", "独立运行器"], ["mock", "模拟"], ["gateway_probe", "网关探测"]])}</select></label>
      <label><span>开始时间（UTC）</span><input name="from" type="datetime-local" value="${localTime(filters.from)}" /></label>
      <label><span>结束时间（UTC）</span><input name="to" type="datetime-local" value="${localTime(filters.to)}" /></label>
      <button class="platform-primary" type="submit" ${busy ? "disabled" : ""}>筛选</button>
    </form>
    <div class="platform-table-wrap"><table class="platform-provider-engineering-runs">
      <thead><tr><th>执行记录</th><th>适用范围</th><th>状态</th><th>用例数</th><th>验证证明</th><th>完成时间</th></tr></thead>
      <tbody>${runs.map((run) => runRow(run, selectedRun, busy)).join("") || emptyRow("暂无契约测试记录", 6)}</tbody>
    </table></div>
    ${pagination("runs", state.runHistory?.length > 0, Boolean(state.runs?.next_cursor), busy)}
    ${selectedRun ? renderSelectedRun(state, selectedRun, cases, permissions, busy) : ""}
  </section>`;
}

function runRow(run, selected, busy) {
  const active = selected?.contract_run_id === run.contract_run_id;
  return `<tr class="${active ? "is-selected" : ""}">
    <td data-label="执行记录"><button type="button" class="platform-link-button"
      data-platform-provider-run-select="${escapeAttribute(run.contract_run_id)}" ${busy ? "disabled" : ""}>
      ${escapeHtml(shortId(run.contract_run_id))}</button><small>generation ${number(run.generation)}</small></td>
    <td data-label="适用范围"><strong>${escapeHtml(run.scope)}</strong><small>${run.legacy ? "历史记录" : run.mode}</small></td>
    <td data-label="状态">${status(run.status)}<small>${run.qualifies_release ? "满足发布要求" : "尚不满足发布要求"}</small></td>
    <td data-label="用例数"><strong>${number(run.passed_cases)} / ${number(run.total_cases)}</strong><small>${number(run.failed_cases)} failed</small></td>
    <td data-label="验证证明"><code>${escapeHtml(shortHash(run.attestation_sha256))}</code><small>${run.coverage_complete ? "覆盖完整" : "覆盖不完整"}</small></td>
    <td data-label="完成时间">${formatTime(run.completed_at)}</td>
  </tr>`;
}

function renderSelectedRun(state, run, cases, permissions, busy) {
  const canReview = permissions.has("platform.provider_adapter.failure_review")
    && run.status === "failed";
  return `<section class="platform-provider-engineering-cases" aria-labelledby="provider-contract-case-title">
    <div class="platform-section-heading"><div><p>${escapeHtml(shortId(run.contract_run_id))}</p>
      <h3 id="provider-contract-case-title">契约测试用例</h3></div><span>${number(cases.length )} 个用例</span></div>
    ${canReview ? `<form data-platform-provider-failure-review-form autocomplete="off">
      <input type="hidden" name="adapterId" value="${escapeAttribute(state.selectedAdapterId)}" />
      <input type="hidden" name="contractRunId" value="${escapeAttribute(run.contract_run_id)}" />
      <input type="hidden" name="expectedRevision" value="${number(selectedAdapter(state)?.revision ?? 0)}" />
      <label><span>处理结论</span><select name="failureDisposition"><option value="reject">拒绝</option><option value="approve_remediation">批准修复</option></select></label>
      ${stepUpFields()}
      <button class="platform-primary" type="submit" ${busy ? "disabled" : ""}>复核失败</button>
    </form>` : ""}
    <div class="platform-table-wrap"><table class="platform-provider-engineering-case-table">
      <thead><tr><th>#</th><th>用例证明</th><th>类型与变体</th><th>供应商与接入规范</th><th>状态</th><th>耗时</th><th>验证证据</th></tr></thead>
      <tbody>${cases.map(caseRow).join("") || emptyRow(state.cases ? "暂无测试用例" : "选择执行记录以查看用例", 7)}</tbody>
    </table></div>
    ${pagination("cases", state.caseHistory?.length > 0, Boolean(state.cases?.next_cursor), busy)}
  </section>`;
}

function caseRow(item) {
  return `<tr><td data-label="#">${number(item.ordinal)}</td>
    <td data-label="用例证明"><code>${escapeHtml(shortHash(item.case_key_sha256))}</code></td>
    <td data-label="类型与变体"><strong>${escapeHtml(item.case_kind)}</strong><small>${escapeHtml(item.variant)}</small></td>
    <td data-label="供应商与接入规范"><strong>${escapeHtml(item.provider_code)}</strong><small>${escapeHtml(item.adapter_code)}</small></td>
    <td data-label="状态">${status(item.status)}<small>${escapeHtml(item.failure_code ?? "")}</small></td>
    <td data-label="耗时">${escapeHtml(item.duration_bucket)}</td>
    <td data-label="验证证据"><code>${escapeHtml(shortHash(item.evidence_sha256))}</code></td></tr>`;
}

function renderCatalogWorkspace(state, permissions, busy) {
  const catalog = state.catalog;
  const canCreate = permissions.has("platform.error_catalog.candidate_manage");
  const actions = catalog?.actions ?? [];
  return `<section class="platform-band platform-provider-engineering-catalog">
    <div class="platform-section-heading"><div><p>内置规则版本</p><h2>错误目录版本</h2></div>
      ${catalog ? status(catalog.status) : '<span>暂无当前执行结果</span>'}</div>
    ${catalog ? `<dl class="platform-provider-engineering-proof">
      <div><dt>版本</dt><dd>${escapeHtml(catalog.builtin_catalog_version)}</dd></div>
      <div><dt>规则数</dt><dd>${number(catalog.rule_count)}</dd></div>
      ${proof("目录校验", catalog.catalog_sha256)}${proof("产物校验", catalog.artifact_sha256)}
      ${proof("兼容性", catalog.compatibility_sha256)}
      <div><dt>修订号与发布代数</dt><dd>${number(catalog.revision)} / ${number(catalog.release_generation)}</dd></div>
    </dl>
    <div class="platform-provider-engineering-actions">${actions
      .filter((action) => catalogPermission(permissions, action))
      .map((action) => catalogActionForm(catalog, action, busy)).join("")}</div>` : ""}
    ${canCreate ? `<details class="platform-provider-engineering-create"><summary>创建目录候选版本</summary>
      <form data-platform-provider-catalog-candidate-form autocomplete="off">
        <label><span>内置目录版本</span><input name="builtinCatalogVersion" maxlength="80" required /></label>
        <label><span>SDK 版本编号</span><input name="sdkReleaseId" pattern="sdr_[0-9a-f]{32}" required /></label>
        <label><span>已签名候选编号</span><input name="signedCandidateId" pattern="[A-Za-z0-9][A-Za-z0-9._:-]{0,127}" maxlength="128" required /></label>
        ${stepUpFields()}
        <button class="platform-primary" type="submit" ${busy ? "disabled" : ""}>创建候选版本</button>
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
  return `<label><span>再次验证</span><select name="method" data-platform-provider-engineering-step-up-method>
      <option value="totp">动态验证码</option><option value="webauthn">安全密钥</option></select></label>
    <label data-platform-provider-engineering-step-up-code><span>验证码</span>
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
  return `<div class="platform-pagination"><span>分页浏览</span><div>
    <button type="button" data-platform-provider-page="${kind}" data-direction="previous"
      ${canPrevious && !busy ? "" : "disabled"}>上一页</button>
    <button type="button" data-platform-provider-page="${kind}" data-direction="next"
      ${canNext && !busy ? "" : "disabled"}>下一页</button></div></div>`;
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
  return `<tr><td colspan="${number(columns)}" class="platform-empty"><span class="platform-empty-label">${escapeHtml(message)}</span></td></tr>`;
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
