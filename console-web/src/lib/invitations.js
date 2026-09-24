/**
 * PRD-060 Invitation 客户端。
 *
 * 管理创建使用当前 Bearer；公开接受明确不携带 Cookie 或 Authorization。原始 Token 只在
 * 调用栈和私有页面状态中短暂存在，不进入 Storage、错误文本、request_id 或日志。
 */

// 32 字节随机值编码后固定为 43 字符；宽松长度会扩大 Token 枚举和日志脱敏边界。
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
// 公开凭据只允许进入同源 fragment，不能进入 query、服务端 Console path 或第三方 URL。
const INVITATION_HASH_PREFIX = "#accept-invitation/";

/** Invitation API 的稳定错误，不保留原始 fetch 异常或含 Token URL。 */
export class InvitationApiError extends Error {
  /** 构造可供页面展示和 request_id 排障的安全错误。 */
  constructor({ status = 0, code = "invitation.unavailable", message, requestId, traceId }) {
    super(message || "邀请服务暂不可用");
    this.name = "InvitationApiError";
    this.status = Number(status) || 0;
    this.code = String(code || "invitation.unavailable");
    this.requestId = String(requestId || "-");
    this.traceId = String(traceId || "-");
  }
}

/**
 * 在认证 bootstrap 前截获公开邀请 fragment，并立即从地址栏清除凭据。
 *
 * 返回对象通过闭包保存 Token；调用方只能按需读取或销毁，无法把它随状态快照展开到 DOM。
 */
export function captureInvitationRoute(browserWindow) {
  const hash = String(browserWindow?.location?.hash ?? "");
  let publicMode = hash === "#accept-invitation" || hash.startsWith(INVITATION_HASH_PREFIX);
  let token = null;
  if (publicMode) {
    const candidate = hash.slice(INVITATION_HASH_PREFIX.length);
    token = TOKEN_PATTERN.test(candidate) ? candidate : null;
    const cleanLocation = `${browserWindow.location.pathname}${browserWindow.location.search}#accept-invitation`;
    // 保留无凭据哨兵，使刷新后仍停留在固定失效页；replaceState 不新增历史项，也不会再次暴露 Token。
    browserWindow.history.replaceState(browserWindow.history.state, "", cleanLocation);
  }
  return Object.freeze({
    /** 当前启动是否由公开邀请路由接管。 */
    isPublicMode() {
      return publicMode;
    },
    /** 仅在提交接受请求的同步调用点读取原始 Token。 */
    readToken() {
      return token;
    },
    /** 页面离开、成功或登录跳转时立即销毁闭包中的凭据。 */
    clear() {
      token = null;
    },
    /** 结束公开模式；通常随后刷新页面进入现有登录流程。 */
    leave() {
      token = null;
      publicMode = false;
      const cleanLocation = `${browserWindow.location.pathname}${browserWindow.location.search}`;
      // 登录或离开公开页时必须同时移除哨兵，否则刷新会再次被无 Token 的公开路由接管。
      browserWindow.history.replaceState(browserWindow.history.state, "", cleanLocation);
    },
  });
}

/** 为当前租户创建邀请；tenant_id、actor 和状态都不允许由前端提交。 */
export async function createInvitation({
  controlApiUrl,
  token,
  email,
  role,
  expiresInHours = 72,
}) {
  return invitationRequest(`${controlApiUrl}/invitations`, {
    method: "POST",
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "X-Request-ID": createInvitationRequestId("create"),
    },
    body: JSON.stringify({
      email,
      role,
      expires_in_hours: expiresInHours,
    }),
  });
}

/**
 * 公开接受邀请；该请求即使浏览器中已有管理员 Session，也绝不附加其身份。
 */
export async function previewInvitation({ controlApiUrl, token, signal }) {
  if (!TOKEN_PATTERN.test(String(token ?? ""))) {
    throw new InvitationApiError({ status: 410, code: "invitation.not_available",
      message: "邀请链接已失效" });
  }
  return invitationRequest(`${controlApiUrl}/invitations/preview`, {
    method: "POST", credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer",
    headers: { "Content-Type": "application/json", "X-Request-ID": createInvitationRequestId("preview") },
    body: JSON.stringify({ token }), signal,
  }, { hideNetworkDetails: true });
}

export async function acceptInvitation({
  controlApiUrl,
  token,
  displayName,
  password,
}) {
  // 客户端格式失败与未知/过期 Token 使用相同 410，不能在发请求前泄漏差异状态。
  if (!TOKEN_PATTERN.test(String(token ?? ""))) {
    // 固定异常不保留含 Token 的 URL 或原始输入，供公开页安全展示。
    throw new InvitationApiError({
      status: 410,
      code: "invitation.not_available",
      message: "邀请链接已失效",
    });
  }
  return invitationRequest(
    `${controlApiUrl}/invitations/${token}/accept`,
    {
      method: "POST",
      credentials: "omit",
      cache: "no-store",
      referrerPolicy: "no-referrer",
      headers: {
        "Content-Type": "application/json",
        "X-Request-ID": createInvitationRequestId("accept"),
      },
      body: JSON.stringify({ display_name: displayName, password }),
    },
    { hideNetworkDetails: true },
  );
}

/** 构造同源 fragment 链接；Token 不进入 query、真实 path 或第三方 URL。 */
export function buildInvitationAcceptLink(consoleUrl, token) {
  const normalizedToken = String(token ?? "");
  if (!TOKEN_PATTERN.test(normalizedToken)) {
    // 创建响应 Token 合同损坏时整包拒绝，不能生成可复制的异常链接。
    throw new InvitationApiError({ code: "invitation.unavailable" });
  }
  const url = new URL("/", consoleUrl);
  url.hash = `accept-invitation/${normalizedToken}`;
  return url.toString();
}

/** 生成不含邮箱、Token 或 Session 的请求追踪 ID。 */
function createInvitationRequestId(operation) {
  const entropy = globalThis.crypto?.randomUUID?.().replaceAll("-", "")
    ?? `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `console_invitation_${operation}_${entropy.slice(0, 32)}`;
}

/** 执行 Invitation 请求，并把所有异常转换成不含 URL/正文的固定错误结构。 */
async function invitationRequest(url, options, { hideNetworkDetails = false } = {}) {
  let response;
  try {
    response = await fetch(url, options);
  } catch {
    // 原始 TypeError 可能包含带 Token 的 URL，公开接受路径绝不能向上层传播该异常对象。
    // 新异常只保留固定领域信息，避免 stack/cause 再次携带原请求 URL。
    throw new InvitationApiError({
      code: "invitation.unavailable",
      message: hideNetworkDetails ? "邀请服务暂不可用" : "创建邀请暂不可用",
    });
  }
  const payload = await readJson(response);
  if (!response.ok) {
    // 业务错误只保存稳定字段，绝不把请求 URL、正文或底层 Response 对象挂到异常上。
    throw new InvitationApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || "邀请操作失败",
      requestId: payload.request_id || response.headers.get("X-Request-ID"),
      traceId: payload.trace_id || response.headers.get("X-Trace-ID"),
    });
  }
  return payload;
}

/** 安全读取 JSON；空响应统一由上层按固定错误处理。 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
