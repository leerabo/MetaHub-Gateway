import assert from "node:assert/strict";
import test from "node:test";
import { renderFallbackModelPicker } from "../../src/features/model-aliases/fallbackModelPicker.js";

const models = [
  { model_id: "a", display_name: "Alpha", provider_name: "Service A" },
  { model_id: "b", display_name: "Beta", provider_name: "Service B" },
  { model_id: "c", display_name: "Gamma" },
];

test("fallback checkbox rendering preserves saved priority rather than catalog order", () => {
  const html = renderFallbackModelPicker(models, ["c", "b"], "a");
  const selected = html.split('aria-label="已选备用模型">')[1].split("</ol>")[0];
  assert.ok(selected.indexOf('value="c"') < selected.indexOf('value="b"'));
  assert.equal((selected.match(/ checked/g) ?? []).length, 2);
  assert.match(html, /value="a"\s+disabled/);
  assert.match(html, /fallback-model-row is-unavailable[\s\S]*value="a"/);
  assert.match(selected, /data-fallback-move="up"/);
  assert.match(selected, /data-fallback-drag/);
});

test("picker removes duplicate and default targets and escapes names and identifiers", () => {
  const html = renderFallbackModelPicker([
    ...models, { model_id: 'unsafe"<id>', display_name: "<script>test</script>", provider_name: "<service>" },
  ], ["b", "b", "a", "missing"], "a");
  assert.match(html, /已选 1/);
  assert.match(html, /unsafe&quot;&lt;id&gt;/);
  assert.match(html, /&lt;script&gt;test&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test("every available row keeps sync controls so a cached dialog cannot stop default cleanup", () => {
  const html = renderFallbackModelPicker(models, ["b"], "a");
  const available = html.split('aria-label="可选备用模型">')[1].split("</ul>")[0];
  assert.match(available, /value="a"[\s\S]*data-fallback-position/);
  assert.match(available, /value="a"[\s\S]*data-fallback-order/);
});

test("automatic routing modes keep candidate selection but remove manual ordering controls", () => {
  const html = renderFallbackModelPicker(models, ["c", "b"], "a", "cost_first");
  assert.match(html, /data-routing-mode="cost_first"/);
  assert.match(html, /顺序由系统自动决定/);
  assert.match(html, /data-fallback-order hidden/);
  assert.equal((html.match(/type="checkbox" name="fallbackModelId"[^>]*checked/g) ?? []).length, 2);
});
