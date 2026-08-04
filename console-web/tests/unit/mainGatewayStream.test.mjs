// 本单测覆盖调试流生命周期，确保连续提交、停止和离页不会留下旧连接或旧回调。
import assert from "node:assert/strict";
import test from "node:test";

import { createGatewayDebugStreamLifecycle } from "../../src/app/main.js";

test("gateway debug lifecycle aborts the previous stream before a consecutive submit", () => {
  const lifecycle = createGatewayDebugStreamLifecycle();
  const first = lifecycle.begin();
  const second = lifecycle.begin();

  assert.equal(first.controller.signal.aborted, true);
  assert.equal(second.controller.signal.aborted, false);
  assert.equal(lifecycle.isCurrent(first.generation), false);
  assert.equal(lifecycle.isCurrent(second.generation), true);

  // 旧请求 finally 不得清理或取消新请求持有的 controller。
  lifecycle.finish(first.generation);
  assert.equal(lifecycle.stop(), second.generation);
  assert.equal(second.controller.signal.aborted, true);
});

test("gateway debug lifecycle stops the current stream without misclassifying its callback as stale", () => {
  const lifecycle = createGatewayDebugStreamLifecycle();
  const current = lifecycle.begin();

  assert.equal(lifecycle.stop(), current.generation);
  assert.equal(current.controller.signal.aborted, true);
  assert.equal(lifecycle.isCurrent(current.generation), true);
  assert.equal(lifecycle.stop(), null);
});

test("gateway debug lifecycle aborts and invalidates callbacks when leaving the page", () => {
  const lifecycle = createGatewayDebugStreamLifecycle();
  const current = lifecycle.begin();

  lifecycle.leave();

  assert.equal(current.controller.signal.aborted, true);
  assert.equal(lifecycle.isCurrent(current.generation), false);
  assert.equal(lifecycle.stop(), null);
});
