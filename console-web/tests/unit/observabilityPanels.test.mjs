// 本单测覆盖可观测性面板渲染，确保错误预览、接口文档和调试结果都做安全转义。
import assert from "node:assert/strict";
import test from "node:test";

import {
  renderApiDocsPanel,
  renderDebugPlaceholder,
  renderGatewayDebugConsole,
  renderErrorPagePreview,
} from "../../src/features/observability/renderObservabilityPanels.js";

test("renderErrorPagePreview shows trace fields and escapes unsafe text", () => {
  const html = renderErrorPagePreview({
    code: "infra.not_found",
    message: "<script>alert(1)</script>",
    requestId: "req_123",
    traceId: "trc_123",
    suggestion: "Check the API path and method.",
  });

  assert.match(html, /infra.not_found/);
  assert.match(html, /req_123/);
  assert.match(html, /trc_123/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
});

test("renderApiDocsPanel shows OpenAI compatible endpoints", () => {
  const html = renderApiDocsPanel({ gatewayUrl: "http://gateway.local" });

  assert.match(html, /http:\/\/gateway.local\/v1/);
  assert.match(html, /\/v1\/chat\/completions/);
  assert.match(html, /\/v1\/embeddings/);
  assert.match(html, /base_url/);
  assert.match(html, /baseURL/);
});

test("renderGatewayDebugConsole renders active form without leaking state key", () => {
  const html = renderGatewayDebugConsole(
    {
      model: "default-chat",
      message: "<unsafe>",
      result: {
        requestId: "req_gateway",
        traceId: "trc_gateway",
        preview: '{"ok":true}',
      },
    },
    { gatewayUrl: "http://gateway.local" },
  );

  assert.match(html, /gateway-debug-form/);
  assert.match(html, /type="password"/);
  assert.match(html, /req_gateway/);
  assert.match(html, /&lt;unsafe&gt;/);
  assert.doesNotMatch(html, /disabled/);
});

test("renderGatewayDebugConsole renders live stream controls and safely escapes preview", () => {
  const html = renderGatewayDebugConsole({
    model: "default-chat",
    message: "hello",
    stream: true,
    running: true,
    feedback: "接收中",
    virtualKey: "mhv_must_not_render",
    result: {
      status: "streaming",
      requestId: "req_live",
      traceId: "trc_live",
      preview: '<img src=x onerror="alert(1)">',
      previewTruncated: true,
      chunkCount: 3,
      byteCount: 128,
    },
  });

  assert.match(html, /gateway-debug-stop-button/);
  assert.match(html, /停止流式请求/);
  assert.match(html, /接收中/);
  assert.match(html, /gateway-debug-chunk-count[^>]*>3</);
  assert.match(html, /gateway-debug-byte-count[^>]*>128</);
  assert.match(html, /预览已截断/);
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.doesNotMatch(html, /mhv_must_not_render/);
});

test("renderGatewayDebugConsole keeps bounded preview visible for SSE errors", () => {
  const html = renderGatewayDebugConsole({
    stream: true,
    feedback: "请求失败",
    error: {
      code: "gateway.upstream_stream_timeout",
      message: "Upstream stream timed out.",
      requestId: "req_timeout",
      traceId: "trc_timeout",
      preview: "data: partial",
      chunkCount: 2,
      byteCount: 64,
    },
  });

  assert.match(html, /gateway.upstream_stream_timeout/);
  assert.match(html, /data: partial/);
  assert.match(html, /req_timeout/);
  assert.match(html, /失败/);
});

test("renderDebugPlaceholder keeps compatibility with active debug console", () => {
  const html = renderDebugPlaceholder();

  assert.match(html, /OpenAI 调试台/);
  assert.match(html, /gateway-debug-form/);
});
