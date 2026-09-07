import { isWorkspaceNavigationOwner, workspaceNavigationOwner } from "../features/workspace-navigation/workspaceNavigationStore.js";

const OWNER_FIELDS = ["workspaceId", "role", "globalSessionId", "revokeGeneration", "globalExpiresAt",
  "workspaceExpiresAt", "permissionRevision", "membershipRevision", "permissionFingerprint"];

export function workspaceConsoleSessionFromShell(shell) {
  const owner = workspaceNavigationOwner(shell?.workspaceIdentity, {
    context: shell?.workspaceContext, authorization: shell?.workspaceAuthorization,
  });
  if (!owner || shell.state !== "ok" || shell.workspaceNavigationContract !== true
    || OWNER_FIELDS.some((field) => owner[field] !== shell.workspaceOwner?.[field])
    || shell.navigation?.permission_revision !== owner.permissionRevision
    || shell.navigation?.membership_revision !== owner.membershipRevision
    || shell.navigation?.permission_fingerprint !== owner.permissionFingerprint) return null;
  if (!Array.isArray(shell.workspaceAuthorization.permission_codes)) return null;
  const session = Object.freeze({ authentication: "workspace", owner,
    context: Object.freeze({ ...shell.workspaceAuthorization,
      permission_codes: Object.freeze([...shell.workspaceAuthorization.permission_codes]) }) });
  return hasWorkspaceConsoleSession(session) ? session : null;
}

export function hasWorkspaceConsoleSession(session) {
  const owner = session?.owner;
  return Boolean(session?.authentication === "workspace" && isWorkspaceNavigationOwner(owner)
    && !Object.hasOwn(session, "token")
    && Array.isArray(session.context?.permission_codes)
    && session.context.permission_codes.every((code) => typeof code === "string" && code.length > 0)
    && session.context.permission_revision === owner.permissionRevision
    && session.context.membership_revision === owner.membershipRevision
    && session.context.permission_fingerprint === owner.permissionFingerprint
    && Date.parse(owner.globalExpiresAt) > Date.now()
    && Date.parse(owner.workspaceExpiresAt) > Date.now());
}

export function sameWorkspaceConsoleSession(left, right) {
  return hasWorkspaceConsoleSession(left) && hasWorkspaceConsoleSession(right)
    && OWNER_FIELDS.every((field) => left.owner[field] === right.owner[field])
    && JSON.stringify([...left.context.permission_codes].sort())
      === JSON.stringify([...right.context.permission_codes].sort());
}

// A refresh may start without authentication; only a validated shell can establish it.
export function isConsoleRefreshOwnerCurrent(owner, current) {
  return Boolean(owner && current && Number.isSafeInteger(owner.generation) && owner.generation > 0
    && Object.hasOwn(owner, "session") && Object.hasOwn(current, "session")
    && owner.generation === current.generation && owner.session === current.session);
}
