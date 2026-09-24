export const forbiddenPagePatterns = [
  /(?:provider[ _-]?key|access[ _-]?token)["']?\s*[:=]\s*["']?(?!\[redacted\]|\*|<)[a-z0-9_./+-]{12,}/i,
  /\bsk-[a-z0-9_-]{16,}\b/i,
  /response body/i,
  /sqlite_master/i,
  /PRD-\d{3}/i,
];

// Account actions and advanced entitlements are independent of sidebar groups.
export function allowedAuditRoutes(navigation, advancedTools) {
  const allowed = new Set([
    ...navigation.groups.flatMap(group => group.items.map(item => item.route_code)),
    ...navigation.detail_route_codes,
    ...navigation.developer_route_codes,
    ...navigation.account_action_codes,
  ]);
  if (navigation.advanced_tools_available) {
    for (const tool of advancedTools?.tools ?? []) {
      if (tool.action_enabled === true && tool.availability === "available") allowed.add(tool.route_code);
    }
  }
  return allowed;
}
