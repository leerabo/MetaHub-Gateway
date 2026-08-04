import { PERMISSIONS } from "../../src/lib/permissions.js";

export { PERMISSIONS };

/**
 * 为既有测试身份附加一份真实形状的后端授权快照。
 *
 * 测试必须显式列出 permission code，不能通过角色名推导默认权限；这样可同时覆盖
 * FinanceAdmin/SecurityAdmin/OpsAdmin 正向授权，以及 admin 缺码时的失败关闭行为。
 */
export function withPermissionSnapshot(baseContext = {}, permissionCodes = [], options = {}) {
  const normalizedCodes = [...permissionCodes].map(String).sort();
  const permissionRevision = Number(options.permissionRevision ?? 1);
  const fingerprint = String(
    options.authorizationFingerprint
      ?? `test_authz_${baseContext?.user?.role ?? "unknown"}_${permissionRevision}`,
  );
  return {
    ...baseContext,
    user: {
      ...(baseContext.user ?? {}),
      permission_codes: normalizedCodes,
      permission_revision: permissionRevision,
      authorization_fingerprint: fingerprint,
    },
    permission_codes: normalizedCodes,
    permission_revision: permissionRevision,
    authorization_fingerprint: fingerprint,
  };
}
