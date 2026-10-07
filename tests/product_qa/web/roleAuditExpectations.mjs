export const forbiddenPagePatterns = [
  /(?:provider[ _-]?key|access[ _-]?token)["']?\s*[:=]\s*["']?(?!\[redacted\]|\*|<)[a-z0-9_./+-]{12,}/i,
  /\bsk-[a-z0-9_-]{16,}\b/i,
  /response body/i,
  /sqlite_master/i,
  /PRD-\d{3}/i,
  /\b(?:request_id|trace_id|error_code|source_revision|tenant_id|workspace_id|user_id|provider_id|model_id|revision|etag|fingerprint|idempotency[_ -]?key|sha-?256)\b\s*[:：=]\s*["'`]?((?:req|trc|ten|wsp|mbr|ses|usr|gac|prv|vky|dpl|dep|chn|rul|evt|job|inv|wbh|ast|cse|console)_[a-z0-9][a-z0-9_-]{3,}|[a-f0-9]{16,})/i,
  /\b(?:req|trc|ten|wsp|mbr|ses|usr|gac|prv|vky|dpl|dep|chn|rul|evt|job|inv|wbh|ast|cse|console)_[a-z0-9][a-z0-9_-]{3,}\b/i,
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
