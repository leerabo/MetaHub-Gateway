import { selectRepresentativeProbeAttempt } from "../../lib/providerHealth.js";

/** Read the catalog without replacing a model name the user has started entering. */
export function bindModelDiscovery(form, { request }) {
  if (!form) return;
  const provider = form.querySelector("[name='providerId']");
  const input = form.querySelector("[name='providerModelId']");
  const select = form.querySelector("[data-discovered-models]");
  const discover = form.querySelector("[data-discover-models]");
  const manual = form.querySelector("[data-model-manual]");
  const feedback = form.querySelector("[data-model-discovery-feedback]");
  const inputPrice = form.querySelector("[name='inputPricePer1k']");
  const outputPrice = form.querySelector("[name='outputPricePer1k']");
  if (!provider || !input || !select || !discover || !feedback) return;
  const showManual = () => {
    select.hidden = true; select.disabled = true; select.required = false;
    input.hidden = false; input.disabled = false; input.required = true;
    select.removeAttribute("name"); input.name = "providerModelId";
    if (manual) manual.hidden = true;
  };
  let discoveryRun = 0;
  const chooseManual = () => {
    discoveryRun += 1;
    showManual();
    discover.disabled = false; discover.textContent = "重新读取模型列表";
    feedback.textContent = "请填写厂家提供的准确模型名称。";
  };
  const runDiscovery = async () => {
    const providerId = String(provider.value || "").trim();
    if (!providerId) {
      feedback.textContent = "请先选择模型服务。";
      return;
    }
    const run = ++discoveryRun;
    discover.disabled = true; discover.textContent = "读取中…";
    feedback.textContent = "正在从模型服务读取可用模型…";
    try {
      const result = await request(providerId);
      // Provider 切换后，丢弃旧请求的结果，避免模型列表归属错位。
      if (run !== discoveryRun || String(provider.value || "").trim() !== providerId) return;
      if (result?.result_status && result.result_status !== "success") {
        showManual();
        const attempt = selectRepresentativeProbeAttempt(result);
        feedback.textContent = attempt?.http_status === 401
          ? "厂家未接受已保存的 API KEY，请到模型服务检查密钥后重试。"
          : attempt?.http_status === 403 ? "厂家拒绝读取模型列表，请检查密钥权限，或手动填写。"
            : attempt?.http_status === 429 ? "厂家暂时限制了请求，请稍后重新读取，或手动填写。"
              : "暂未读取到模型列表，请重试或手动填写厂家模型名称。";
        return;
      }
      const ids = Array.isArray(result?.model_ids) ? result.model_ids.filter((id) => typeof id === "string" && id) : [];
      if (!ids.length) { showManual(); feedback.textContent = "服务没有返回模型列表，请手动填写厂家模型名称。"; return; }
      select.replaceChildren(new Option("请选择厂家模型", ""), ...[...new Set(ids)].map((id) => new Option(id, id)));
      select.dataset.officialPrices = JSON.stringify(result?.prices ?? {});
      select.hidden = false; select.disabled = false; select.required = true;
      input.hidden = true; input.disabled = true; input.required = false;
      select.name = "providerModelId"; input.removeAttribute("name");
      // Provider catalog may include an official public price snapshot. It is only a
      // suggestion; the administrator can still edit it and apply a multiplier.
      const selectedPrice = result?.prices?.[ids[0]];
      if (selectedPrice && inputPrice && outputPrice) {
        if (Number.isFinite(Number(selectedPrice.input_per_1k))) inputPrice.value = String(selectedPrice.input_per_1k);
        if (Number.isFinite(Number(selectedPrice.output_per_1k))) outputPrice.value = String(selectedPrice.output_per_1k);
      }
      if (manual) manual.hidden = false;
      feedback.textContent = result.catalog_truncated ? "已读取前 200 个模型，请选择或手动填写。" : `已读取 ${ids.length} 个模型，请选择。`;
    } catch (error) {
      if (run !== discoveryRun) return;
      showManual();
      feedback.textContent = error?.code === "provider_health.catalog_unsupported"
        ? "此模型服务不支持自动读取，请手动填写厂家模型名称。"
        : `自动读取失败，请手动填写。${error?.requestId ? ` request_id: ${error.requestId}` : ""}`;
    } finally {
      if (run === discoveryRun) { discover.disabled = false; discover.textContent = "重新读取模型列表"; }
    }
  };
  manual?.addEventListener("click", chooseManual);
  input.addEventListener("input", chooseManual);
  provider.addEventListener("change", () => {
    discoveryRun += 1;
    showManual();
    input.value = "";
    select.replaceChildren();
    feedback.textContent = "模型服务已更换，请读取列表或手动填写。";
    void runDiscovery();
  });
  select.addEventListener("change", () => {
    input.value = select.value;
    try {
      const price = JSON.parse(select.dataset.officialPrices || "{}")?.[select.value];
      if (price && inputPrice && outputPrice) {
        if (Number.isFinite(Number(price.input_per_1k))) inputPrice.value = String(price.input_per_1k);
        if (Number.isFinite(Number(price.output_per_1k))) outputPrice.value = String(price.output_per_1k);
      }
    } catch { /* ignore malformed optional price hints */ }
  });
  discover.addEventListener("click", () => { void runDiscovery(); });
  // 单个已选服务可以直接开始读取，用户进入表单即可看到可选模型。
  if (provider.value) void runDiscovery();
}
