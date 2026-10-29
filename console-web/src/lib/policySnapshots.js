import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

/**
 * 模块职责：封装策略快照公共管理 API，并保留发布所需的强 ETag 与统一链路标识。
 * 安全边界：客户端永远不提交 tenant_id、DSL 或编译产物；租户、角色和候选图必须由后端重新确认。
 */

/**
 * 读取当前线上快照与 active 候选摘要。
 *
 * @param {object} options 请求参数。
 * @param {string} options.controlApiUrl Control API 根地址。
 * @param {string} options.token 当前登录令牌。
 * @param {AbortSignal} [options.signal] 页面离开或身份切换时使用的取消信号。
 * @returns {Promise<object>} 当前快照、候选摘要以及发布双 CAS 所需的两个强 ETag。
 */
export async function getCurrentPolicySnapshot({ controlApiUrl, authentication, token, signal }) {
  const { payload, response } = await policySnapshotRequest(
    `${controlApiUrl}/policy-snapshots/current`,
    {
      signal,
      authentication,
      token,
    },
  );
  return {
    state: "ok",
    ...payload,
    headEtag: response.headers.get("ETag"),
    candidateEtag: response.headers.get("X-Policy-Candidate-ETag"),
  };
}

/**
 * 按版本倒序读取当前租户的不可变快照历史。
 *
 * @param {object} options 请求参数；分页仅作为白名单 query 发送，不接受 tenant_id。
 * @returns {Promise<object>} 历史列表和分页元数据。
 */
export async function listPolicySnapshots({
  controlApiUrl,
  authentication,
  token,
  limit = 20,
  offset = 0,
  signal,
}) {
  const url = new URL(`${controlApiUrl}/policy-snapshots`);
  url.searchParams.set("limit", String(limit));
  url.searchParams.set("offset", String(offset));
  const { payload } = await policySnapshotRequest(url.toString(), {
    signal,
    authentication,
    token,
  });
  return { state: "ok", ...payload };
}

/**
 * 显式发布 active Policy 候选。
 *
 * @param {object} options 写请求参数；requestId 必须由页面未决操作内存提供，网络重试时不得重新生成。
 * @returns {Promise<object>} 新快照摘要、响应 head ETag 与幂等回放标记。
 */
export async function publishPolicySnapshot({
  controlApiUrl,
  authentication,
  token,
  reason,
  headEtag,
  candidateEtag,
  requestId,
  signal,
}) {
  const { payload, response } = await policySnapshotRequest(
    `${controlApiUrl}/policy-snapshots/publish`,
    {
      method: "POST",
      authentication,
      token,
      signal,
      headers: {
        "Content-Type": "application/json",
        "If-Match": requireStrongEtag(headEtag, "head"),
        "If-Policy-Candidate-Match": requireStrongEtag(candidateEtag, "candidate"),
        "X-Request-ID": requireRequestId(requestId),
      },
      body: JSON.stringify({ reason: String(reason ?? "") }),
    },
  );
  return {
    state: "ok",
    ...payload,
    headEtag: response.headers.get("ETag"),
  };
}

/**
 * 把当前 head 的上一激活版本复制为新的单调版本。
 *
 * @param {object} options 写请求参数；rollback 不读取 active 候选，因此只携带 head CAS。
 * @returns {Promise<object>} 新生成的回滚快照摘要。
 */
export async function rollbackPolicySnapshot({
  controlApiUrl,
  authentication,
  token,
  policySnapshotId,
  reason,
  headEtag,
  requestId,
  signal,
}) {
  const snapshotId = encodeURIComponent(String(policySnapshotId ?? ""));
  const { payload, response } = await policySnapshotRequest(
    `${controlApiUrl}/policy-snapshots/${snapshotId}/rollback`,
    {
      method: "POST",
      authentication,
      token,
      signal,
      headers: {
        "Content-Type": "application/json",
        "If-Match": requireStrongEtag(headEtag, "head"),
        "X-Request-ID": requireRequestId(requestId),
      },
      body: JSON.stringify({ reason: String(reason ?? "") }),
    },
  );
  return {
    state: "ok",
    ...payload,
    headEtag: response.headers.get("ETag"),
  };
}

/**
 * 生成控制台快照写操作的稳定 request_id。
 *
 * 返回值只在当前内存会话保存；随机源不可用时使用时间与随机数组合，仍不把身份或租户信息编码进 ID。
 */
export function createPolicySnapshotRequestId() {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) {
    return `req_console_policy_snapshot_${uuid.replaceAll("-", "")}`;
  }
  const random = Math.random().toString(36).slice(2, 14);
  return `req_console_policy_snapshot_${Date.now().toString(36)}_${random}`;
}

/**
 * 调用策略快照 API，并把后端统一错误映射为 AuthApiError。
 *
 * 网络异常与 AbortError 原样抛出，调用方据此区分“服务端已明确拒绝”和“提交结果未知”，
 * 后一种情况必须保留原 request_id 才能安全重试。
 */
async function policySnapshotRequest(url, options = {}) {
  const response = await authenticatedResourceFetch(url, options);
  const payload = await readJson(response);
  if (!response.ok) {
    // 只暴露后端白名单错误字段，不把响应头、请求正文或可能含 DSL 的内部细节拼进页面。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `策略快照接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { payload, response };
}

/**
 * 容错读取 JSON；空响应或非 JSON 错误体不会覆盖 HTTP 状态对应的统一错误。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/**
 * 校验页面内存中的强 ETag，防止缺失或弱 ETag 被静默降级为无条件写入。
 */
function requireStrongEtag(value, name) {
  const normalized = String(value ?? "");
  if (!/^"[^"\r\n]+"$/.test(normalized)) {
    // 缺失、弱格式或含换行的 ETag 不能降级为无条件写入，必须在发网前失败关闭。
    throw new TypeError(`策略快照 ${name} ETag 不可用`);
  }
  return normalized;
}

/**
 * 校验写操作 request_id；该校验只保护前端调用合同，幂等归属与冲突仍由后端强制执行。
 */
function requireRequestId(value) {
  const normalized = String(value ?? "").trim();
  if (!normalized || normalized.length > 128 || /[\r\n]/.test(normalized)) {
    // 非法幂等键不得进入请求头，避免头注入或失去同一操作稳定重试语义。
    throw new TypeError("策略快照 request_id 不可用");
  }
  return normalized;
}
