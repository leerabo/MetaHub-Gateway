import assert from "node:assert/strict";
import test from "node:test";
import { allowedAuditRoutes, forbiddenPagePatterns } from "./roleAuditExpectations.mjs";

test("audit distinguishes documentation labels from credential-shaped assignments", () => {
  const forbidden = text => forbiddenPagePatterns.some(pattern => pattern.test(text));
  for (const text of ["Provider Key is held by administrators", "Access token", "Provider Key: [redacted]", "provider_key=<YOUR_KEY>"]) assert.equal(forbidden(text), false, text);
  for (const text of ["Provider Key: fixtureSecret12345", 'access_token="fixtureToken987654"', '{"provider_key":"fixtureSecret12345"}', "sk-fixtureOnly123456789", "sqlite_master", "PRD-148"]) assert.equal(forbidden(text), true, text);
});

test("audit uses account actions and available entitlements without granting blocked tools", () => {
  const navigation = { groups: [{ items: [{ route_code: "home" }] }], detail_route_codes: ["detail"], developer_route_codes: ["sdk"], account_action_codes: ["identity.preferences"], advanced_tools_available: true };
  const tools = { tools: [
    { route_code: "available", action_enabled: true, availability: "available" },
    { route_code: "blocked", action_enabled: false, availability: "blocked" },
    { route_code: "revoked", action_enabled: true, availability: "revoked" },
    { route_code: "disabled", action_enabled: false, availability: "available" },
  ] };
  assert.deepEqual([...allowedAuditRoutes(navigation, tools)], ["home", "detail", "sdk", "identity.preferences", "available"]);
  assert.equal(allowedAuditRoutes({ ...navigation, advanced_tools_available: false }, tools).has("available"), false);
});
