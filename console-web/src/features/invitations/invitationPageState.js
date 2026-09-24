/** 管理 PAGE-087 邀请弹窗的一次性密文、身份 owner 和迟到响应。 */

import { authorizationIdentitySnapshot, PERMISSIONS } from "../../lib/permissions.js";
import { buildInvitationAcceptLink } from "../../lib/invitations.js";

/** 创建只在当前模块闭包保存原始链接和邮件文案的页面状态。 */
export function createInvitationPageState() {
  let generation = 0;
  let owner = "";
  let sessionToken = null;
  let open = false;
  let busy = false;
  let dirty = false;
  let invitation = null;
  let feedback = null;
  let secretLink = null;
  let secretMailText = null;
  let workspaceMode = false;

  /** 清除一次性密文；任何公开快照都不能返回这两个变量。 */
  function clearSecrets() {
    secretLink = null;
    secretMailText = null;
  }

  /** 清空当前 owner 的 UI 与异步代次。 */
  function reset() {
    generation += 1;
    open = false;
    busy = false;
    dirty = false;
    invitation = null;
    feedback = null;
    clearSecrets();
    workspaceMode = false;
  }

  /** 同步后端授权身份、Session 和导航 owner；任一变化都销毁密文。 */
  function syncOwner({ authContext, token, activeNavigation, workspaceContext, workspaceOwner }) {
    const workspacePermissions = Array.isArray(authContext?.permission_codes)
      ? authContext.permission_codes
      : [];
    const isWorkspaceOwner = activeNavigation === "users"
      && workspaceContext?.workspace_id
      && workspaceOwner?.workspaceId
      && workspaceOwner.workspaceId === workspaceContext.workspace_id
      && workspacePermissions.includes(PERMISSIONS.USER_MANAGE);
    const nextOwner = isWorkspaceOwner
      ? JSON.stringify([
          workspaceOwner.workspaceId,
          workspaceOwner.membershipRevision,
          workspaceOwner.permissionRevision,
          workspaceOwner.permissionFingerprint,
        ])
      : activeNavigation === "users"
        ? authorizationIdentitySnapshot(authContext, PERMISSIONS.USER_MANAGE)
        : "";
    const nextToken = typeof token === "string" && token ? token : null;
    // 权限快照、导航或 Session 任一不完整都立即失权，不能按角色名或旧 owner 回退。
    if (!nextOwner || (!nextToken && !isWorkspaceOwner)) {
      reset();
      owner = "";
      sessionToken = null;
      return false;
    }
    // 完整授权 owner 或 Session Token 漂移时销毁草稿和一次性链接，淘汰所有迟到响应。
    if (owner && (owner !== nextOwner || sessionToken !== nextToken)) {
      reset();
    }
    owner = nextOwner;
    sessionToken = nextToken;
    workspaceMode = Boolean(isWorkspaceOwner);
    return true;
  }

  return Object.freeze({
    syncOwner,
    /** 打开弹窗前再次复核完整 owner。 */
    openDialog(input) {
      if (!syncOwner(input)) return false;
      open = true;
      feedback = null;
      return true;
    },
    /** 表单变化会暂停后台刷新，避免用户输入被重建。 */
    markDirty() {
      if (open) dirty = true;
    },
    /** 创建异步票据；票据不包含 Session Token 或邀请密文。 */
    beginCreate(input, intent) {
      if (!syncOwner(input) || !open || busy) return null;
      const tenantId = String(
        input?.authContext?.user?.tenant_id ?? input?.authContext?.tenant?.tenant_id ?? "",
      ).trim();
      const email = String(intent?.email ?? "").trim().toLowerCase();
      const role = String(intent?.role ?? "").trim();
      // 租户、邮箱或角色意图缺失时不创建异步票据，避免响应无法与提交边界复核。
      const workspaceId = String(input?.workspaceContext?.workspace_id ?? "").trim();
      if ((!tenantId && !workspaceId) || !email || !role) return null;
      generation += 1;
      busy = true;
      dirty = false;
      invitation = null;
      feedback = null;
      clearSecrets();
      // 票据只冻结非敏感意图和 owner，不复制 Session Token 或返回的一次性邀请凭据。
      return Object.freeze({ generation, owner, tenantId, workspaceId, email, role, workspaceMode });
    },
    /** 只有当前代次和身份可接收创建结果，并把链接存入闭包。 */
    resolveCreate(ticket, result, consoleUrl) {
      if (!ticket || ticket.generation !== generation || ticket.owner !== owner || !open) {
        return false;
      }
      const createdInvitation = result?.invitation;
      // 后端响应必须仍对应提交时冻结的租户和邀请意图；漂移时整包拒绝，不能复制错误租户的凭据。
      const membershipInvitation = Boolean(
        createdInvitation?.invitation_id && createdInvitation?.role
        && createdInvitation?.status === "pending"
        && ticket.workspaceMode,
      );
      if (
        (!membershipInvitation && String(createdInvitation?.tenant_id ?? "") !== ticket.tenantId)
        || (!membershipInvitation && String(createdInvitation?.email ?? "").trim().toLowerCase() !== ticket.email)
        || String(createdInvitation?.role ?? "").trim() !== ticket.role
        || createdInvitation?.status !== "pending"
      ) {
        // 固定错误不拼接漂移响应字段，防止另一租户的邮箱、角色或 Token 进入页面反馈。
        throw new Error("邀请响应校验失败");
      }
      const link = buildInvitationAcceptLink(consoleUrl, result?.accept_token);
      invitation = createdInvitation
        ? {
            email: String(createdInvitation.email ?? ticket.email),
            role: String(createdInvitation.role ?? ""),
            expiresAt: String(createdInvitation.expires_at ?? ""),
          }
        : null;
      secretLink = link;
      secretMailText = [
        "MetaHub 团队邀请",
        `受邀邮箱：${invitation?.email ?? ""}`,
        `角色：${invitation?.role ?? ""}`,
        `接受邀请：${link}`,
      ].join("\n");
      busy = false;
      feedback = { state: "success", message: "邀请链接已生成", requestId: result?.request_id ?? "-" };
      return true;
    },
    /** 迟到错误同样受票据门禁；错误对象不进入长期状态。 */
    rejectCreate(ticket, error) {
      if (!ticket || ticket.generation !== generation || ticket.owner !== owner || !open) {
        return false;
      }
      busy = false;
      feedback = {
        state: "error",
        message: String(error?.message || "创建邀请失败"),
        requestId: String(error?.requestId || "-"),
      };
      return true;
    },
    /** Clipboard handler 在用户手势内读取，返回值不得写入 DOM 或 Storage。 */
    readAcceptLink() {
      return open ? secretLink : null;
    },
    /** Clipboard handler 在用户手势内读取一次性邮件文案。 */
    readMailText() {
      return open ? secretMailText : null;
    },
    /** 关闭即销毁密文和草稿；创建中拒绝关闭，避免唯一一次成功响应因竞态丢失。 */
    close() {
      if (busy) return false;
      reset();
      return true;
    },
    /** 登出、离页或权限变化使用同一彻底清理入口。 */
    clear() {
      reset();
      owner = "";
      sessionToken = null;
    },
    /** 后台刷新不得重建活动、脏、忙或持有密文的弹窗。 */
    isBlockingRefresh() {
      return open || busy || dirty || Boolean(secretLink);
    },
    /** DOM 只消费不含 Token、链接或邮件正文的安全快照。 */
    snapshot() {
      return Object.freeze({
        open,
        busy,
        dirty,
        hasSecret: Boolean(secretLink),
        invitation: invitation ? { ...invitation } : null,
        feedback: feedback ? { ...feedback } : null,
      });
    },
  });
}
