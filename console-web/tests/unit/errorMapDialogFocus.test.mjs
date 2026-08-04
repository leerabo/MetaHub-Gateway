/**
 * 测试职责：验证 PAGE-081 对话框在局部重绘后的焦点恢复、owner 绑定和选择器注入防护。
 * 安全边界：测试使用公开规则 ID；完整身份只驻留内存，且不会写入 DOM、URL 或 Storage。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createErrorMapDialogRestoreState,
  resolveErrorMapDialogRestoreTarget,
} from "../../src/features/error-maps/errorMapDialogFocus.js";

// 测试身份包含 tenant、用户、角色、Session 与 revision，模拟 PAGE-081 的完整 owner 快照。
const OWNER_IDENTITY = JSON.stringify(["tenant_056", "user_056", "admin", "session_056", "8"]);

/**
 * 构造记录查询选择器的最小 PAGE-081 面板替身。
 *
 * @returns {object} 支持连接、包含关系和固定选择器查询的测试面板。
 */
function createFakePanel() {
  return {
    ownerDocument: {},
    isConnected: true,
    members: new Set(),
    selectors: [],
    createTarget: null,
    candidates: new Map(),
    matches(selector) {
      return selector === ".error-maps-panel";
    },
    contains(target) {
      return this.members.has(target);
    },
    querySelector(selector) {
      this.selectors.push(selector);
      return selector === "button#error-map-create" ? this.createTarget : null;
    },
    querySelectorAll(selector) {
      this.selectors.push(selector);
      return this.candidates.get(selector) ?? [];
    },
  };
}

/**
 * 构造属于指定 PAGE-081 面板的最小按钮替身。
 *
 * @param {object} panel PAGE-081 面板。
 * @param {object} options 按钮身份、连接状态、面板归属、标签、禁用状态和 focus 能力。
 * @returns {object} 可供捕获与解析器校验的按钮。
 */
function createButton(
  panel,
  {
    id = "",
    attributes = {},
    connected = true,
    inside = true,
    tagName = "BUTTON",
    disabled = false,
    focusable = true,
  } = {},
) {
  const target = {
    id,
    tagName,
    ownerDocument: panel.ownerDocument,
    isConnected: connected,
    disabled,
    getAttribute(name) {
      return attributes[name] ?? null;
    },
  };
  if (focusable) {
    target.focus = () => {};
  }
  if (inside) {
    panel.members.add(target);
  }
  return target;
}

test("redraw resolves exact edit trigger without interpolating its public id", () => {
  const panel = createFakePanel();
  const maliciousId = 'pem_"]#logout-button,[data-error-map-edit="';
  const original = createButton(panel, {
    attributes: { "data-error-map-edit": maliciousId },
  });
  const restoreState = createErrorMapDialogRestoreState(original, {
    ownerIdentity: OWNER_IDENTITY,
    navigation: "error-maps",
    panel,
  });
  assert.equal(Object.isFrozen(restoreState), true);
  assert.equal("node" in restoreState, false);

  original.isConnected = false;
  const unrelated = createButton(panel, {
    attributes: { "data-error-map-edit": "pem_22222222222222222222222222222222" },
  });
  const replacement = createButton(panel, {
    attributes: { "data-error-map-edit": maliciousId },
  });
  panel.candidates.set("button[data-error-map-edit]", [unrelated, replacement]);
  assert.equal(resolveErrorMapDialogRestoreTarget(restoreState, {
    ownerIdentity: OWNER_IDENTITY,
    navigation: "error-maps",
    panel,
  }), replacement);
  assert.deepEqual(panel.selectors, ["button[data-error-map-edit]"]);

  panel.selectors.length = 0;
  assert.equal(resolveErrorMapDialogRestoreTarget(restoreState, {
    ownerIdentity: OWNER_IDENTITY + "-other",
    navigation: "error-maps",
    panel,
  }), null);
  assert.equal(resolveErrorMapDialogRestoreTarget(restoreState, {
    ownerIdentity: OWNER_IDENTITY,
    navigation: "request-logs",
    panel,
  }), null);
  assert.deepEqual(panel.selectors, []);
});

test("restore rejects disconnected, disabled, outside-panel and non-button targets", () => {
  const panel = createFakePanel();
  const errorMapId = "pem_11111111111111111111111111111111";
  const original = createButton(panel, {
    attributes: { "data-error-map-status": errorMapId },
  });
  const restoreState = createErrorMapDialogRestoreState(original, {
    ownerIdentity: OWNER_IDENTITY,
    navigation: "error-maps",
    panel,
  });
  original.isConnected = false;
  const candidates = [
    createButton(panel, {
      attributes: { "data-error-map-status": errorMapId },
      disabled: true,
    }),
    createButton(panel, {
      attributes: { "data-error-map-status": errorMapId },
      connected: false,
    }),
    createButton(panel, {
      attributes: { "data-error-map-status": errorMapId },
      focusable: false,
    }),
    createButton(panel, {
      attributes: { "data-error-map-status": errorMapId },
      inside: false,
    }),
  ];
  panel.candidates.set("button[data-error-map-status]", candidates);
  assert.equal(resolveErrorMapDialogRestoreTarget(restoreState, {
    ownerIdentity: OWNER_IDENTITY,
    navigation: "error-maps",
    panel,
  }), null);
  assert.equal(createErrorMapDialogRestoreState(
    createButton(panel, {
      tagName: "DIV",
      attributes: { "data-error-map-edit": errorMapId },
    }),
    { ownerIdentity: OWNER_IDENTITY, navigation: "error-maps", panel },
  ), null);

  const createOriginal = createButton(panel, { id: "error-map-create" });
  const createState = createErrorMapDialogRestoreState(createOriginal, {
    ownerIdentity: OWNER_IDENTITY,
    navigation: "error-maps",
    panel,
  });
  createOriginal.isConnected = false;
  panel.createTarget = createButton(panel, { id: "error-map-create" });
  assert.equal(resolveErrorMapDialogRestoreTarget(createState, {
    ownerIdentity: OWNER_IDENTITY,
    navigation: "error-maps",
    panel,
  }), panel.createTarget);
});

test("main validates the live owner before opening state and consumes restore state once", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  assert.match(
    source,
    /const restoreState = errorMapDialogRestoreState;[\s\S]*errorMapDialogRestoreState = null;[\s\S]*ownerIdentity: owner\.identity[\s\S]*navigation: owner\.navigation/,
  );
  assert.match(
    source,
    /const restoreState = captureCurrentErrorMapDialogRestoreState\(button, identity\)[\s\S]*if \(!restoreState\)[\s\S]*if \(errorMapPageMemory\.openEdit[\s\S]*errorMapDialogRestoreState = restoreState/,
  );
  assert.doesNotMatch(source, /errorMapDialogRestoreTarget\s*=/);
});
