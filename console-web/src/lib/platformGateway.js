// Platform Gateway 客户端执行严格 DTO、CSRF、CAS 与幂等合同；不得读取或发送 Bearer 凭据。
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

// 这些正则冻结公开 ID、Cell、驻留版本、幂等键和链路标识边界，任何扩展字段由严格 DTO 拒绝。
const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const CELL_CODE = /^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/u;
const POLICY_VERSION = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const LINK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;

// 读取平台授权范围内的 Cell 安全投影，响应出现未知字段时失败关闭。
export function getPlatformGatewayCells({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/gateway-cells", { signal }, validCellList);
}

// 发布 Workspace placement，浏览器只发送服务端允许的显式字段和安全并发控制头。
export function updatePlatformGatewayPlacement({
  controlApiUrl,
  workspaceId,
  revision,
  placement,
  idempotencyKey,
  signal,
} = {}) {
  const workspace = String(workspaceId ?? "");
  const expected = Number(revision);
  const key = String(idempotencyKey ?? "");
  if (!WORKSPACE_ID.test(workspace) || !Number.isSafeInteger(expected) || expected < 0
    || !IDEMPOTENCY_KEY.test(key)) invalidRequest();
  const payload = checkedPlacement(placement);
  return request(
    controlApiUrl,
    `/platform/workspaces/${encodeURIComponent(workspace)}/gateway-placement`,
    {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": readPlatformCsrfCookie(),
        "If-Match": `"${expected}"`,
        "Idempotency-Key": key,
      },
      body: JSON.stringify(payload),
      signal,
    },
    validPlacementResponse,
  );
}

// 统一执行 no-store Cookie 请求并把网络或响应错误折叠为固定 Platform 错误。
async function request(controlApiUrl, path, options, validator) {
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path), {
      method: options?.method ?? "GET",
      headers: { Accept: "application/json", ...(options?.headers ?? {}) },
      body: options?.body,
      credentials: "include",
      cache: "no-store",
      signal: options?.signal,
    });
  } catch (error) {
    // 页面切换的主动取消必须原样传播，不能伪装成服务故障或触发用户重试。
    if (error?.name === "AbortError") throw error;
    // 其他网络异常统一折叠，不把浏览器、代理或目标地址细节带入页面状态。
    throw new PlatformApiError({
      code: "platform_gateway.unavailable",
      message: "暂时无法连接 Gateway 管理服务。",
    });
  }
  const payload = await readJson(response);
  if (!response.ok) {
    // 非成功响应只采用后端固定错误合同，未知正文不得进入平台反馈。
    throw new PlatformApiError({
      status: response.status,
      code: typeof payload.code === "string" ? payload.code : "platform_gateway.unavailable",
      message: typeof payload.message === "string" ? payload.message : "Gateway 请求未完成。",
      requestId: typeof payload.request_id === "string" ? payload.request_id : "-",
    });
  }
  if (!validator(payload)) {
    // 成功响应若偏离严格 DTO 也失败关闭，避免敏感扩展字段进入 DOM。
    throw new PlatformApiError({
      status: 502,
      code: "platform_gateway.response_invalid",
      message: "Gateway 响应格式无效。",
    });
  }
  return payload;
}

// 对写入 DTO 做 exact-key 与状态组合校验，阻止额外 owner、Region 或签名字段穿透。
function checkedPlacement(value) {
  if (!plainObject(value) || !exactKeys(value, [
    "primary_region_code", "primary_cell_code", "failover_policy",
    "failover_cell_code", "residency_policy_version", "reason_code", "confirmed",
  ])) invalidRequest();
  if (!CELL_CODE.test(String(value.primary_region_code))
    || !CELL_CODE.test(String(value.primary_cell_code))
    || !new Set(["deny", "explicit"]).has(value.failover_policy)
    || (value.failover_cell_code !== null && !CELL_CODE.test(String(value.failover_cell_code)))
    || !POLICY_VERSION.test(String(value.residency_policy_version))
    || !new Set(["initial_placement", "capacity_rebalance", "regional_recovery"])
      .has(value.reason_code)
    || value.confirmed !== true
    || (value.failover_policy === "deny") !== (value.failover_cell_code === null)
    || value.failover_cell_code === value.primary_cell_code) invalidRequest();
  return value;
}

// Cell 列表只接受有界数组和 request/trace 关联字段。
function validCellList(value) {
  return plainObject(value) && exactKeys(value, ["cells", "request_id", "trace_id"])
    && Array.isArray(value.cells) && value.cells.length <= 100
    && value.cells.every(validCell) && links(value);
}

// 单 Cell 仅允许公开 code、枚举健康和数字水位，不接受内部拓扑扩展。
function validCell(value) {
  return plainObject(value) && exactKeys(value, [
    "cell_code", "region_code", "capacity_class", "capacity_band", "status",
    "health_status", "release_watermark", "config_watermark", "revision", "updated_at",
  ]) && CELL_CODE.test(String(value.cell_code)) && CELL_CODE.test(String(value.region_code))
    && new Set(["standard", "high", "burst"]).has(value.capacity_class)
    && new Set(["available", "busy", "full"]).has(value.capacity_band)
    && new Set(["active", "draining", "unavailable"]).has(value.status)
    && new Set(["healthy", "degraded", "unavailable"]).has(value.health_status)
    && [value.release_watermark, value.config_watermark].every(
      (item) => Number.isSafeInteger(item) && item >= 0,
    ) && Number.isSafeInteger(value.revision) && value.revision >= 1
    && timestamp(value.updated_at);
}

// Placement 响应拒绝未声明字段，防止签名或租户 owner 被意外下发到浏览器。
function validPlacementResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "placement", "idempotent_replay", "request_id", "trace_id",
  ]) && typeof value.idempotent_replay === "boolean" && validPlacement(value.placement)
    && links(value);
}

// 校验安全 placement 投影的 owner ID、枚举、版本和时间边界。
function validPlacement(value) {
  return plainObject(value) && exactKeys(value, [
    "workspace_id", "primary_region_code", "primary_cell_code", "failover_policy",
    "failover_cell_code", "residency_policy_version", "revision",
    "snapshot_generation", "updated_at",
  ]) && WORKSPACE_ID.test(String(value.workspace_id))
    && CELL_CODE.test(String(value.primary_region_code))
    && CELL_CODE.test(String(value.primary_cell_code))
    && new Set(["deny", "explicit"]).has(value.failover_policy)
    && (value.failover_cell_code === null || CELL_CODE.test(String(value.failover_cell_code)))
    && POLICY_VERSION.test(String(value.residency_policy_version))
    && Number.isSafeInteger(value.revision) && value.revision >= 1
    && Number.isSafeInteger(value.snapshot_generation) && value.snapshot_generation >= 1
    && timestamp(value.updated_at);
}

// 内部 API 基址禁止凭据 URL 和非 HTTP 协议，避免 Cookie 请求被重定向到异常目标。
function apiUrl(controlApiUrl, path) {
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { invalidRequest(); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest();
  }
  return new URL(path, base).toString();
}

// 本地校验失败统一使用公开错误，不拼接原始字段值。
function invalidRequest() {
  throw new PlatformApiError({
    code: "platform_gateway.request_invalid",
    message: "Gateway placement 请求字段无效。",
  });
}

// 请求关联 ID 使用受限字符集，避免控制字符进入界面反馈。
function links(value) {
  return LINK_ID.test(String(value.request_id ?? ""))
    && LINK_ID.test(String(value.trace_id ?? ""));
}

// DTO 只接受普通对象，拒绝带原型的可执行或污染对象。
function plainObject(value) {
  return Boolean(value) && Object.getPrototypeOf(value) === Object.prototype;
}

// exact-key 比较阻断后端意外扩展敏感字段。
function exactKeys(value, keys) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index]);
}

// 时间字段限制长度并要求可解析，避免无界文本进入 DOM。
function timestamp(value) {
  return typeof value === "string" && value.length <= 40 && Number.isFinite(Date.parse(value));
}

// 非对象或不可解析响应按空对象处理，随后由固定错误合同收口。
async function readJson(response) {
  try {
    const value = await response.json();
    return plainObject(value) ? value : {};
  } catch {
    return {};
  }
}
