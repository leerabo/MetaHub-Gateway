/** Fixed account destinations; organization management lives in console.html. */
export const IDENTITY_SECTIONS = Object.freeze([
  { id: "workspaces", label: "组织空间", icon: "building-2" },
  { id: "security", label: "账号安全", icon: "shield-check" },
  { id: "devices", label: "登录设备", icon: "monitor" },
  { id: "password", label: "修改密码", icon: "key-round" },
  { id: "preferences", label: "语言与显示", icon: "languages" },
]);

const LEGACY_SECTIONS = Object.freeze({
  "account-preferences": "preferences",
  "identity-memberships-title": "members",
  "identity-security-title": "security",
  "identity-onboarding-title": "onboarding",
});

// Keep old bookmarks useful without maintaining a second organization UI. The
// target routes are the same role-filtered console routes used by navigation.
const WORKSPACE_SECTION_ROUTES = Object.freeze({
  onboarding: "workspace.home.overview",
  members: "workspace.settings.members",
  billing: "workspace.finance.subscription",
  invoices: "workspace.finance.subscription",
  business: "workspace.settings.profile",
  enterprise: "workspace.settings.profile",
  scim: "workspace.settings.members",
  access: "workspace.governance.policies",
  governance: "workspace.governance.policies",
  privacy: "workspace.governance.policies",
  compliance: "workspace.governance.audit",
  machines: "workspace.settings.roles",
});
const LEGACY_WORKSPACE_SECTIONS = new Set(Object.keys(WORKSPACE_SECTION_ROUTES));

function sectionCandidate(value) {
  return String(value ?? "").replace(/^#/, "");
}

export function workspaceSectionRoute(value) {
  const candidate = sectionCandidate(value);
  const section = LEGACY_SECTIONS[candidate] ?? candidate;
  return WORKSPACE_SECTION_ROUTES[section] ?? null;
}

export function identitySection(value, fallback = "workspaces") {
  const candidate = sectionCandidate(value);
  const legacy = LEGACY_SECTIONS[candidate];
  return legacy
    ?? IDENTITY_SECTIONS.find((section) => section.id === candidate)?.id
    ?? (LEGACY_WORKSPACE_SECTIONS.has(candidate) ? candidate : null)
    ?? fallback;
}
