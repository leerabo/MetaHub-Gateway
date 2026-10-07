/** Keep unconfigured prices absent; legacy field names now carry per-million
 * prices throughout the API and ledger. */
export function readModelEditValues(data, now = new Date()) {
  const values = {
    displayName: String(data.get("displayName") ?? "").trim() || null,
    providerModelId: String(data.get("providerModelId") ?? "").trim(),
    description: String(data.get("description") ?? "").trim() || null,
  };
  if (!values.providerModelId) throw new Error("请填写模型名称。");
  if (data.get("pricingEnabled") !== "on") return values;
  const price = (name) => {
    const raw = String(data.get(name) ?? data.get(name.replace("PerMillion", "Per1k")) ?? "").trim();
    const value = Number(raw);
    if (!raw || !Number.isFinite(value) || value < 0) throw new Error("请填写有效的输入、输出和缓存命中价格。");
    return value;
  };
  const multiplierRaw = String(data.get("priceMultiplier") ?? "1").trim();
  const multiplier = Number(multiplierRaw);
  if (!Number.isFinite(multiplier) || multiplier < 0) throw new Error("请填写有效的价格倍数。");
  return {
    ...values,
    inputPricePer1k: price("inputPricePerMillion") * multiplier,
    outputPricePer1k: price("outputPricePerMillion") * multiplier,
    cachePricePer1k: price("cachePricePerMillion") * multiplier,
    currency: String(data.get("currency") ?? "CNY"),
    priceSchemaVersion: `manual-${now.toISOString()}`,
  };
}

export function bindModelEditPricing(form) {
  const toggle = form?.querySelector('[name="pricingEnabled"]');
  const fields = form?.querySelector("[data-model-edit-price-fields]");
  if (!toggle || !fields) return;
  const sync = () => {
    fields.hidden = !toggle.checked;
    for (const input of fields.querySelectorAll("input, select")) input.disabled = !toggle.checked;
  };
  toggle.addEventListener("change", sync);
  sync();
}
