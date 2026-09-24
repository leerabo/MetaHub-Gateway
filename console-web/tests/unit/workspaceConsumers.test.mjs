import assert from "node:assert/strict";
import test from "node:test";

import { renderVirtualKeyPanel } from "../../src/features/virtual-keys/renderVirtualKeyPanel.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

test("workspace admins use organization KEY flow and never render employee assignment controls", () => {
  const state = { state: "ok", virtual_keys: [] };
  const admin = withPermissionSnapshot({ user: { role: "admin" } }, [
    PERMISSIONS.USER_MANAGE, PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE,
  ]);
  const html = renderVirtualKeyPanel(state, admin);
  assert.match(html, /<h2 id="virtual-keys-title">组织 KEY<\/h2>/u);
  assert.match(html, /<summary>创建组织 KEY<\/summary>/u);
  assert.doesNotMatch(html, /name="consumerId"|consumer-form|option value="con_1"|Disabled Employee|Finance/iu);
  assert.match(html, /KEY 名称（可选）[\s\S]*name="name"/u);
  assert.ok(html.indexOf('name="name"') < html.indexOf('class="virtual-key-advanced"'));
  const developer = withPermissionSnapshot({ user: { role: "developer" } }, [
    PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE,
  ]);
  const technicalHtml = renderVirtualKeyPanel(state, developer);
  assert.doesNotMatch(technicalHtml, /consumer-form|consumerId|Disabled Employee|Finance/);
  assert.match(technicalHtml, /<summary>创建个人 KEY<\/summary>/u);
  assert.match(technicalHtml, /KEY 名称（可选）[\s\S]*name="name"[^>]*required/u);
  assert.equal((technicalHtml.match(/name="name"/gu) ?? []).length, 1);
  assert.doesNotMatch(technicalHtml, /class="virtual-key-advanced"/u);
  assert.doesNotMatch(technicalHtml, /自动按员工姓名命名|consumerId/iu);
});

test("one-time KEY display follows the organization ownership contract", () => {
  const auth = withPermissionSnapshot({ user: { role: "admin" } }, [
    PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE, PERMISSIONS.USER_MANAGE,
  ]);
  for (const operation of [undefined, "rotated"]) {
    const state = { state: "ok", virtual_keys: [], created: {
      plainValue: "synthetic-one-time-key", keyName: "<Named KEY>", operation,
      consumerId: null,
    } };
    const html = renderVirtualKeyPanel(state, auth);
    assert.match(html, /KEY 归属：当前组织 · &lt;Named KEY&gt;/u);
    assert.match(html, /仅配置到授权的组织系统中/u);
    assert.match(html, operation === "rotated" ? /KEY 已轮换/u : /KEY 已创建/u);
    assert.doesNotMatch(html, /发放给|交给这位员工|consumerId/iu);
  }
});
