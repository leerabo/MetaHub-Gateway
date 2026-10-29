// 企业认证客户端强制区分 Workspace 与 Platform 凭据域，并对敏感请求和响应执行严格白名单。
import { IdentityApiError } from "./identity.js";
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

// 标识、枚举和掩码规则与后端 DTO 保持一致，浏览器不能扩展服务端允许的业务取值。
const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const INTENT_ID = /^bvi_[0-9a-f]{32}$/u;
const EVIDENCE_ID = /^bve_[0-9a-f]{32}$/u;
const HASH = /^[0-9a-f]{64}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const MASK = /^\*{4}(?:[A-Z0-9]{4})?$/u;
const STATUSES = new Set(["submitted", "in_review", "approved", "rejected", "expired"]);
const SCAN_STATUSES = new Set(["passed", "blocked", "pending", "failed"]);
const ENTITY_TYPES = new Set(["company", "nonprofit", "government"]);
const EVIDENCE_TYPES = new Set([
  "business_license",
  "registration_certificate",
  "authorization_letter",
]);
const MIME_TYPES = new Set(["application/pdf", "image/png", "image/jpeg"]);

/** 读取当前 Workspace 的脱敏认证摘要，workspace_id 只用于 owner 绑定路径。 */
export function getWorkspaceBusinessVerification({ controlApiUrl, workspaceId, signal } = {}) {
  const owner = checkedId(workspaceId, WORKSPACE_ID, "workspace");
  return request(controlApiUrl, `/workspaces/${owner}/business-verification`, {
    signal,
    responseType: "workspace",
  }, "workspace");
}

/** 提交法定主体和证据描述，并在请求结束后主动擦除调用栈中的敏感副本。 */
export async function submitWorkspaceBusinessVerification({
  controlApiUrl,
  workspaceId,
  submission,
  idempotencyKey,
  signal,
} = {}) {
  const owner = checkedId(workspaceId, WORKSPACE_ID, "workspace");
  let body = checkedSubmission(submission);
  let serialized = JSON.stringify(body);
  try {
    return await request(controlApiUrl, `/workspaces/${owner}/business-verifications`, {
      method: "POST",
      csrf: true,
      idempotencyKey: checkedKey(idempotencyKey, "workspace"),
      serialized,
      signal,
      responseType: "workspace",
    }, "workspace");
  } finally {
    scrubSubmission(body);
    body = null;
    serialized = "";
    submission = null;
  }
}

/** 以 CAS 和幂等键更新 Workspace 开票资料，响应只接受掩码和配置状态。 */
export async function updateWorkspaceBusinessBillingProfile({
  controlApiUrl,
  workspaceId,
  billingProfile,
  revision,
  idempotencyKey,
  signal,
} = {}) {
  const owner = checkedId(workspaceId, WORKSPACE_ID, "workspace");
  let body = checkedBilling(billingProfile);
  let serialized = JSON.stringify(body);
  try {
    return await request(controlApiUrl, `/workspaces/${owner}/billing-profile`, {
      method: "PATCH",
      csrf: true,
      revision: checkedRevision(revision, true, "workspace"),
      idempotencyKey: checkedKey(idempotencyKey, "workspace"),
      serialized,
      signal,
      responseType: "billing",
    }, "workspace");
  } finally {
    scrubBilling(body);
    body = null;
    serialized = "";
    billingProfile = null;
  }
}

/** 查询 Platform 安全审核队列，分页条件不允许携带 Workspace 或 tenant 标识。 */
export function listPlatformBusinessVerifications({
  controlApiUrl,
  status = "",
  limit = 20,
  offset = 0,
  signal,
} = {}) {
  const parameters = new URLSearchParams({
    limit: String(boundedInteger(limit, 1, 100, "platform")),
    offset: String(boundedInteger(offset, 0, 10_000, "platform")),
  });
  if (status) {
    if (!STATUSES.has(String(status))) invalid("platform");
    parameters.set("status", String(status));
  }
  return request(controlApiUrl, `/platform/business-verifications?${parameters}`, {
    signal,
    responseType: "platformList",
  }, "platform");
}

/** 追加固定审核决定，Platform 权限和案件 revision 仍由后端执行最终校验。 */
export function reviewPlatformBusinessVerification({
  controlApiUrl,
  intentId,
  revision,
  decision,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  const id = checkedId(intentId, INTENT_ID, "platform");
  const normalizedDecision = String(decision ?? "");
  const normalizedReason = String(reasonCode ?? "");
  if (!new Set(["approve", "reject"]).has(normalizedDecision)
    || (normalizedDecision === "approve") !== (normalizedReason === "verified")
    || !/^(?:verified|document_invalid|identity_mismatch|scan_failed|profile_changed)$/u
      .test(normalizedReason)) invalid("platform");
  return request(controlApiUrl, `/platform/business-verifications/${id}/review`, {
    method: "POST",
    csrf: true,
    revision: checkedRevision(revision, false, "platform"),
    idempotencyKey: checkedKey(idempotencyKey, "platform"),
    serialized: JSON.stringify({
      decision: normalizedDecision,
      reason_code: normalizedReason,
      confirmed: true,
    }),
    signal,
    responseType: "review",
  }, "platform");
}

/** 签发短时证据下载授权；token 只返回调用方内存，不进入持久化存储。 */
export function issuePlatformEvidenceDownloadToken({
  controlApiUrl,
  intentId,
  evidenceId,
  signal,
} = {}) {
  const intent = checkedId(intentId, INTENT_ID, "platform");
  const evidence = checkedId(evidenceId, EVIDENCE_ID, "platform");
  return request(
    controlApiUrl,
    `/platform/business-verifications/${intent}/evidence/${evidence}/download-token`,
    {
      method: "POST",
      csrf: true,
      serialized: "{}",
      signal,
      responseType: "downloadToken",
    },
    "platform",
  );
}

/** 统一发送 Cookie 凭据域请求，并在解析后按响应类型执行精确字段验证。 */
async function request(controlApiUrl, path, options, domain) {
  const headers = { Accept: "application/json" };
  if (options.serialized !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) {
    headers["X-CSRF-Token"] = domain === "platform"
      ? readPlatformCsrfCookie()
      : readWorkspaceCsrfCookie();
  }
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  if (options.revision !== undefined) headers["If-Match"] = `"${options.revision}"`;
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path, domain), {
      method: options.method ?? "GET",
      headers,
      body: options.serialized,
      credentials: "include",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    // 用户主动取消必须保留 AbortError；其他网络细节统一收敛，避免泄露底层地址或异常。
    if (error?.name === "AbortError") throw error;
    throw clientError(domain, "business_verification.network_unavailable", 0);
  }
  const payload = await readJson(response);
  // HTTP 失败只传播经过格式约束的错误码和 request_id，不信任服务端自由字段。
  if (!response.ok) {
    throw clientError(
      domain,
      safeCode(payload.code) || "business_verification.request_failed",
      response.status,
      payload.message,
      payload.request_id,
    );
  }
  // 成功响应也必须命中精确白名单，防止后端误投影密文或 owner 字段进入页面状态。
  if (!validResponse(payload, options.responseType)) {
    throw clientError(domain, "business_verification.response_invalid", 502);
  }
  return payload;
}

/** 校验 Workspace 主体提交的精确字段、枚举和证据数量，拒绝浏览器声明 owner。 */
function checkedSubmission(value) {
  if (!plainObject(value) || !exactKeys(value, [
    "country_code",
    "entity_type",
    "legal_name",
    "registration_number",
    "evidence",
    "reason_code",
    "confirmed",
  ])) invalid("workspace");
  const country = String(value.country_code ?? "");
  const entityType = String(value.entity_type ?? "");
  const legalName = boundedText(value.legal_name, 2, 240, "workspace");
  const registrationNumber = boundedText(value.registration_number, 3, 128, "workspace");
  if (!/^[A-Z]{2}$/u.test(country) || !ENTITY_TYPES.has(entityType)
    || !new Set(["initial_submission", "resubmission"]).has(value.reason_code)
    || value.confirmed !== true || !Array.isArray(value.evidence)
    || value.evidence.length < 1 || value.evidence.length > 5) invalid("workspace");
  const evidence = value.evidence.map(checkedEvidence);
  if (new Set(evidence.map((item) => item.evidence_type)).size !== evidence.length
    || new Set(evidence.map((item) => item.object_reference)).size !== evidence.length) {
    invalid("workspace");
  }
  return {
    country_code: country,
    entity_type: entityType,
    legal_name: legalName,
    registration_number: registrationNumber,
    evidence,
    reason_code: value.reason_code,
    confirmed: true,
  };
}

/** 校验受信扫描服务生成的证据描述，浏览器不得放宽对象、摘要或状态范围。 */
function checkedEvidence(value) {
  if (!plainObject(value) || !exactKeys(value, [
    "evidence_type",
    "object_reference",
    "content_sha256",
    "mime_type",
    "size_bytes",
    "kms_key_version",
    "malware_status",
    "dlp_status",
    "scan_attestation",
  ])) invalid("workspace");
  const result = {
    evidence_type: String(value.evidence_type ?? ""),
    object_reference: String(value.object_reference ?? ""),
    content_sha256: String(value.content_sha256 ?? ""),
    mime_type: String(value.mime_type ?? ""),
    size_bytes: Number(value.size_bytes),
    kms_key_version: Number(value.kms_key_version),
    malware_status: String(value.malware_status ?? ""),
    dlp_status: String(value.dlp_status ?? ""),
    scan_attestation: String(value.scan_attestation ?? ""),
  };
  if (!EVIDENCE_TYPES.has(result.evidence_type)
    || !/^obj_[A-Za-z0-9._:-]{8,252}$/u.test(result.object_reference)
    || !HASH.test(result.content_sha256) || !MIME_TYPES.has(result.mime_type)
    || !Number.isSafeInteger(result.size_bytes) || result.size_bytes < 1
    || result.size_bytes > 10_485_760 || !Number.isSafeInteger(result.kms_key_version)
    || result.kms_key_version < 1 || !SCAN_STATUSES.has(result.malware_status)
    || !SCAN_STATUSES.has(result.dlp_status) || !HASH.test(result.scan_attestation)) {
    invalid("workspace");
  }
  return result;
}

/** 校验开票字段并要求银行名称和账号原子出现，避免形成半条敏感资料。 */
function checkedBilling(value) {
  if (!plainObject(value) || !exactKeys(value, [
    "invoice_title",
    "tax_number",
    "address",
    "phone",
    "bank_name",
    "bank_account",
    "confirmed",
  ])) invalid("workspace");
  const result = {
    invoice_title: boundedText(value.invoice_title, 2, 240, "workspace"),
    tax_number: boundedText(value.tax_number, 3, 128, "workspace"),
    address: optionalText(value.address, 2, 500, "workspace"),
    phone: optionalText(value.phone, 3, 80, "workspace"),
    bank_name: optionalText(value.bank_name, 2, 240, "workspace"),
    bank_account: optionalText(value.bank_account, 3, 128, "workspace"),
    confirmed: value.confirmed,
  };
  if (result.confirmed !== true
    || (result.bank_name === null) !== (result.bank_account === null)) invalid("workspace");
  return result;
}

/** 按响应用途选择严格验证器，并先验证统一链路标识的安全格式。 */
function validResponse(payload, type) {
  // request_id 与 trace_id 必须是有界安全标识，不能把任意响应文本带入页面错误区。
  if (!plainObject(payload) || !linkId(payload.request_id) || !linkId(payload.trace_id)) {
    return false;
  }
  if (type === "workspace") return validWorkspace(payload);
  if (type === "billing") return validBillingResponse(payload);
  if (type === "platformList") return validPlatformList(payload);
  if (type === "review") return validReview(payload);
  // 下载 token 使用独立最小响应合同，禁止复用包含客户主体字段的案件投影。
  if (type === "downloadToken") return validDownloadToken(payload);
  return false;
}

/** 验证 Workspace 组合摘要，只允许当前主体、认证、合同和开票的脱敏投影。 */
function validWorkspace(value) {
  return exactKeys(value, [
    "workspace_id", "legal_entity", "verification", "contract", "billing_profile",
    "idempotent_replay", "request_id", "trace_id",
  ]) && WORKSPACE_ID.test(String(value.workspace_id))
    && (value.legal_entity === null || validLegalEntity(value.legal_entity))
    && (value.verification === null || validIntent(value.verification))
    && (value.contract === null || validContract(value.contract))
    && (value.billing_profile === null || validBilling(value.billing_profile))
    && typeof value.idempotent_replay === "boolean";
}

/** 验证法定主体摘要不包含名称明文，并保留登记号掩码和 CAS revision。 */
function validLegalEntity(value) {
  return plainObject(value) && exactKeys(value, [
    "country_code", "entity_type", "legal_name_configured", "registration_number_masked",
    "revision", "updated_at",
  ]) && /^[A-Z]{2}$/u.test(String(value.country_code)) && ENTITY_TYPES.has(value.entity_type)
    && value.legal_name_configured === true && MASK.test(String(value.registration_number_masked))
    && positiveInteger(value.revision) && timestamp(value.updated_at);
}

/** 验证认证 intent 的冻结摘要、扫描聚合和时间边界，不接受原始证据引用。 */
function validIntent(value) {
  return plainObject(value) && exactKeys(value, [
    "business_verification_intent_id", "status", "reason_code", "profile_revision",
    "profile_sha256", "evidence_manifest_sha256", "evidence", "revision", "submitted_at",
    "expires_at", "reviewed_at",
  ]) && INTENT_ID.test(String(value.business_verification_intent_id))
    && STATUSES.has(value.status) && positiveInteger(value.profile_revision)
    && HASH.test(String(value.profile_sha256)) && HASH.test(String(value.evidence_manifest_sha256))
    && validEvidenceStatus(value.evidence) && positiveInteger(value.revision)
    && timestamp(value.submitted_at) && timestamp(value.expires_at)
    && (value.reviewed_at === null || timestamp(value.reviewed_at));
}

/** 验证证据数量和聚合扫描状态，空状态只能由服务端摘要明确表达。 */
function validEvidenceStatus(value) {
  return plainObject(value) && exactKeys(value, [
    "evidence_count", "malware_status", "dlp_status",
  ]) && Number.isSafeInteger(value.evidence_count) && value.evidence_count >= 0
    && value.evidence_count <= 5
    && new Set([...SCAN_STATUSES, "none"]).has(value.malware_status)
    && new Set([...SCAN_STATUSES, "none"]).has(value.dlp_status);
}

/** 验证合同响应只含不可变版本引用和签约状态，不接受合同正文或主体明文。 */
function validContract(value) {
  return plainObject(value) && exactKeys(value, [
    "contract_profile_id", "signing_status", "immutable_version_reference", "effective_at",
    "expires_at",
  ]) && /^ctp_[0-9a-f]{32}$/u.test(String(value.contract_profile_id))
    && new Set(["verified", "signed", "expired"]).has(value.signing_status)
    && /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u.test(String(value.immutable_version_reference))
    && timestamp(value.effective_at) && (value.expires_at === null || timestamp(value.expires_at));
}

/** 验证开票更新响应的 owner、幂等标记和最小掩码投影。 */
function validBillingResponse(value) {
  return exactKeys(value, [
    "workspace_id", "billing_profile", "idempotent_replay", "request_id", "trace_id",
  ]) && WORKSPACE_ID.test(String(value.workspace_id)) && validBilling(value.billing_profile)
    && typeof value.idempotent_replay === "boolean";
}

/** 验证开票摘要只显示配置状态与末四位掩码，任何明文字段都会失败关闭。 */
function validBilling(value) {
  return plainObject(value) && exactKeys(value, [
    "invoice_title_configured", "tax_number_masked", "address_configured", "phone_configured",
    "bank_name_configured", "bank_account_masked", "verification_status", "revision", "updated_at",
  ]) && value.invoice_title_configured === true && MASK.test(String(value.tax_number_masked))
    && ["address_configured", "phone_configured", "bank_name_configured"]
      .every((key) => typeof value[key] === "boolean")
    && (value.bank_account_masked === null || MASK.test(String(value.bank_account_masked)))
    && new Set(["unverified", "verified", "stale"]).has(value.verification_status)
    && positiveInteger(value.revision) && timestamp(value.updated_at);
}

/** 验证 Platform 队列分页和案件集合，并递归拒绝客户 owner 与敏感字段。 */
function validPlatformList(value) {
  return exactKeys(value, ["cases", "limit", "offset", "total", "request_id", "trace_id"])
    && Array.isArray(value.cases) && value.cases.length <= 100
    && value.cases.every(validPlatformCase) && Number.isSafeInteger(value.limit)
    && value.limit >= 1 && value.limit <= 100 && nonnegativeInteger(value.offset)
    && nonnegativeInteger(value.total) && !hasForbiddenPlatformField(value);
}

/** 验证单个 Platform 案件只含冻结摘要和受控证据元数据。 */
function validPlatformCase(value) {
  return plainObject(value) && exactKeys(value, [
    "business_verification_intent_id", "country_code", "entity_type", "status", "reason_code",
    "profile_revision", "profile_sha256", "evidence_manifest_sha256", "evidence",
    "evidence_items", "revision", "submitted_at", "expires_at",
  ]) && INTENT_ID.test(String(value.business_verification_intent_id))
    && /^[A-Z]{2}$/u.test(String(value.country_code)) && ENTITY_TYPES.has(value.entity_type)
    && STATUSES.has(value.status) && positiveInteger(value.profile_revision)
    && HASH.test(String(value.profile_sha256)) && HASH.test(String(value.evidence_manifest_sha256))
    && validEvidenceStatus(value.evidence) && Array.isArray(value.evidence_items)
    && value.evidence_items.length <= 5 && value.evidence_items.every(validPlatformEvidence)
    && positiveInteger(value.revision) && timestamp(value.submitted_at)
    && timestamp(value.expires_at);
}

/** 验证 Platform 证据项不携带对象引用、下载地址、文件名或密文。 */
function validPlatformEvidence(value) {
  return plainObject(value) && exactKeys(value, [
    "verification_evidence_id", "evidence_type", "mime_type", "size_bytes",
    "malware_status", "dlp_status",
  ]) && EVIDENCE_ID.test(String(value.verification_evidence_id))
    && EVIDENCE_TYPES.has(value.evidence_type) && MIME_TYPES.has(value.mime_type)
    && Number.isSafeInteger(value.size_bytes) && value.size_bytes >= 1
    && value.size_bytes <= 10_485_760 && SCAN_STATUSES.has(value.malware_status)
    && SCAN_STATUSES.has(value.dlp_status);
}

/** 验证审核响应仍遵循安全案件投影，并只允许返回不可变合同引用。 */
function validReview(value) {
  return exactKeys(value, [
    "case", "contract", "idempotent_replay", "request_id", "trace_id",
  ]) && validPlatformCase(value.case) && (value.contract === null || validContract(value.contract))
    && typeof value.idempotent_replay === "boolean" && !hasForbiddenPlatformField(value);
}

/** 验证一次性下载 token 的最小响应字段、有效期和 intent revision。 */
function validDownloadToken(value) {
  return exactKeys(value, [
    "verification_evidence_id", "download_token", "expires_at", "intent_revision",
    "request_id", "trace_id",
  ]) && EVIDENCE_ID.test(String(value.verification_evidence_id))
    && /^[A-Za-z0-9_-]{32,128}$/u.test(String(value.download_token))
    && timestamp(value.expires_at) && positiveInteger(value.intent_revision);
}

/** 递归拒绝 Platform 响应中的 owner、主体明文、对象引用、地址和密文字段。 */
function hasForbiddenPlatformField(value, depth = 0) {
  // 有界深度让恶意嵌套响应失败关闭，避免递归耗尽浏览器执行栈。
  if (depth > 8) return true;
  if (Array.isArray(value)) return value.some((item) => hasForbiddenPlatformField(item, depth + 1));
  if (!plainObject(value)) return false;
  return Object.entries(value).some(([key, item]) => (
    /^(?:workspace_id|tenant_id|legal_name|registration_number|tax_number|bank_account|object_reference|ciphertext|object_url)$/u.test(key)
    || hasForbiddenPlatformField(item, depth + 1)
  ));
}

/** 请求结束后擦除法定名称、登记号、对象引用和扫描证明的短生命周期副本。 */
function scrubSubmission(value) {
  if (!value) return;
  value.legal_name = "";
  value.registration_number = "";
  for (const evidence of value.evidence ?? []) {
    evidence.object_reference = "";
    evidence.scan_attestation = "";
  }
}

/** 请求结束后清空所有开票明文字段，避免控制器或异常对象继续持有敏感值。 */
function scrubBilling(value) {
  if (!value) return;
  for (const key of [
    "invoice_title", "tax_number", "address", "phone", "bank_name", "bank_account",
  ]) value[key] = null;
}

/** 构造只允许 HTTP(S) 且不含 URL 凭据的控制面地址，拒绝隐式跨域凭据泄露。 */
function apiUrl(controlApiUrl, path, domain) {
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { invalid(domain); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalid(domain);
  }
  return new URL(path, base).toString();
}

/** 校验业务资源标识必须匹配固定命名空间，避免路径拼接接受任意文本。 */
function checkedId(value, pattern, domain) {
  const text = String(value ?? "");
  if (!pattern.test(text)) invalid(domain);
  return text;
}

/** 校验幂等键为有界安全字符集，服务端仍负责 owner 和载荷冲突判断。 */
function checkedKey(value, domain) {
  const text = String(value ?? "");
  if (!IDEMPOTENCY_KEY.test(text)) invalid(domain);
  return text;
}

/** 校验 CAS revision 为安全整数，并按创建或更新语义决定是否允许零。 */
function checkedRevision(value, allowZero, domain) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < (allowZero ? 0 : 1)) invalid(domain);
  return number;
}

/** 校验分页和大小等整数处于显式边界，拒绝浮点、溢出和字符串歧义。 */
function boundedInteger(value, minimum, maximum, domain) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < minimum || number > maximum) invalid(domain);
  return number;
}

/** 规范必填文本并拒绝控制字符，敏感值只在当前调用栈短暂存在。 */
function boundedText(value, minimum, maximum, domain) {
  const text = String(value ?? "").trim();
  if (text.length < minimum || text.length > maximum || /[\u0000-\u001f]/u.test(text)) {
    invalid(domain);
  }
  return text;
}

/** 将空可选字段统一为 null，非空字段继续复用严格文本边界。 */
function optionalText(value, minimum, maximum, domain) {
  return value === null || value === undefined || value === ""
    ? null
    : boundedText(value, minimum, maximum, domain);
}

/** 比较对象键的精确集合，成功响应和请求都不允许未知字段旁路。 */
function exactKeys(value, keys) {
  if (!plainObject(value)) return false;
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

/** 只接受无自定义原型的普通对象，阻止数组或原型链对象混入 DTO。 */
function plainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
    && Object.getPrototypeOf(value) === Object.prototype;
}

/** 以下原子校验器约束 revision、分页、时间、链路标识和安全错误码的最小格式。 */
function positiveInteger(value) { return Number.isSafeInteger(value) && value >= 1; }
function nonnegativeInteger(value) { return Number.isSafeInteger(value) && value >= 0; }
function timestamp(value) { return typeof value === "string" && !Number.isNaN(Date.parse(value)); }
function linkId(value) { return /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/u.test(String(value ?? "")); }
function safeCode(value) { return /^[a-z][a-z0-9_.]{1,127}$/u.test(String(value ?? "")) ? value : ""; }

/** 将所有客户端请求校验失败映射到对应凭据域的统一错误对象。 */
function invalid(domain) {
  // 校验失败不附带原始输入，避免把主体、对象或开票明文写入错误状态。
  throw clientError(domain, "business_verification.request_invalid", 0);
}

/** 构造 Workspace 或 Platform 统一错误，限制自由消息并清洗 request_id。 */
function clientError(domain, code, status, message, requestId) {
  const input = {
    status,
    code,
    message: typeof message === "string" && message.length <= 300
      ? message
      : "企业认证请求未完成。",
    requestId: linkId(requestId) ? requestId : "-",
  };
  return domain === "platform" ? new PlatformApiError(input) : new IdentityApiError(input);
}

/** 解析 JSON 失败时返回空对象，由上层精确合同统一判定为无效响应。 */
async function readJson(response) {
  try {
    const value = await response.json();
    return plainObject(value) ? value : {};
  } catch {
    return {};
  }
}
