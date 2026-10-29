import { icon } from "../public-home/icons.js";

const MAX_SELECTED = 31;

export function renderFallbackModelPicker(models, selectedIds = [], defaultId = "", mode = "fixed") {
  const manualOrdering = mode === "fixed";
  const byId = new Map(models.map((model) => [model.model_id, model]));
  const selected = [...new Set(selectedIds)].filter((id) => byId.has(id) && id !== defaultId);
  const ordered = [...selected.map((id) => byId.get(id)), ...models.filter((model) => !selected.includes(model.model_id))];
  const renderRow = (model) => {
    const index = selected.indexOf(model.model_id);
    const checked = index >= 0;
    const isDefault = model.model_id === defaultId;
    const name = firstNonEmpty(model.display_name, model.provider_model_id, readableModelId(model.internal_model_id)) ?? "未命名模型";
    const price = formatModelPrice(model);
    const orderControls = `
      <div class="fallback-model-order" data-fallback-order ${checked && manualOrdering ? "" : "hidden"}>
        <button type="button" data-fallback-move="up" title="上移" aria-label="上移 ${escapeHtml(name)}">${icon("arrow-left")}</button>
        <button type="button" data-fallback-move="down" title="下移" aria-label="下移 ${escapeHtml(name)}">${icon("arrow-right")}</button>
        <button type="button" class="fallback-model-drag" data-fallback-drag title="拖动排序" aria-label="拖动排序 ${escapeHtml(name)}">${icon("grip-vertical")}</button>
      </div>`;
    return `<li class="fallback-model-row${isDefault ? " is-unavailable" : ""}" data-fallback-row>
      <label class="fallback-model-choice">
        <input type="checkbox" name="fallbackModelId" value="${escapeHtml(model.model_id)}" ${checked ? "checked" : ""} ${isDefault ? "disabled aria-disabled=\"true\"" : ""}>
        <span class="fallback-model-name"><strong>${escapeHtml(name)}</strong>${model.provider_name ? `<small>${escapeHtml(model.provider_name)}</small>` : ""}${price ? `<small class="fallback-model-price">${escapeHtml(price)}</small>` : ""}</span>
      </label>
      <span class="fallback-model-position" data-fallback-position>${checked && manualOrdering ? index + 1 : ""}</span>
      ${orderControls}
    </li>`;
  };
  return `<fieldset class="fallback-model-picker" data-fallback-picker data-routing-mode="${escapeHtml(mode)}">
    <legend>备用模型</legend>
    <div class="fallback-model-picker-heading">
      <span data-fallback-count>已选 ${selected.length}</span>
      <span class="fallback-model-mode-hint" data-fallback-mode-hint>${manualOrdering ? "可拖动调整顺序" : "顺序由系统自动决定"}</span>
    </div>
    <div class="fallback-model-lists">
      <ol class="fallback-model-list" data-fallback-selected aria-label="已选备用模型">${ordered.filter((model) => selected.includes(model.model_id)).map(renderRow).join("")}</ol>
      <ul class="fallback-model-list" data-fallback-available aria-label="可选备用模型">${ordered.filter((model) => !selected.includes(model.model_id)).map(renderRow).join("")}</ul>
      ${models.length ? "" : '<p class="fallback-model-empty">暂无可选模型</p>'}
    </div>
    <span class="fallback-model-announcement" data-fallback-announcement role="status" aria-live="polite"></span>
  </fieldset>`;
}

export function bindFallbackModelPicker(form, canInteract = () => true) {
  const picker = form?.querySelector("[data-fallback-picker]");
  if (!picker || picker.dataset.bound) return;
  picker.dataset.bound = "true";
  const selected = picker.querySelector("[data-fallback-selected]");
  const available = picker.querySelector("[data-fallback-available]");
  const rows = () => [...selected.children];
  const canChange = () => canInteract() && !form.querySelector('button[type="submit"]')?.disabled;
  const currentMode = () => form.querySelector('input[name="routingMode"]:checked')?.value ?? picker.dataset.routingMode ?? "fixed";
  const sync = () => {
    const manualOrdering = currentMode() === "fixed";
    picker.dataset.routingMode = currentMode();
    const modeHint = picker.querySelector("[data-fallback-mode-hint]");
    if (modeHint) modeHint.textContent = manualOrdering ? "可拖动调整顺序" : "顺序由系统自动决定";
    const defaultId = form.querySelector('[name="defaultModelId"]')?.value;
    for (const row of [...selected.children, ...available.children]) {
      const checkbox = row.querySelector('input[name="fallbackModelId"]');
      if (!checkbox) continue;
      // The current default cannot remain in the ordered fallback list. Do
      // this even when a stale rerender already cleared its checkbox; the row
      // itself is the source of ordering and must move back to available.
      if (checkbox.value === defaultId) {
        checkbox.checked = false;
        available.append(row);
      }
    }
    const chosen = rows();
    const count = picker.querySelector("[data-fallback-count]");
    if (count) count.textContent = `已选 ${chosen.length}`;
    for (const row of [...chosen, ...available.children]) {
      const checkbox = row.querySelector('input[name="fallbackModelId"]');
      if (!checkbox) continue;
      const index = chosen.indexOf(row);
      checkbox.disabled = checkbox.value === defaultId || (index < 0 && chosen.length >= MAX_SELECTED);
      row.classList.toggle("is-selected", index >= 0);
      row.classList.toggle("is-unavailable", checkbox.disabled);
      // A user can have an already-open dialog rendered by an older cached
      // module. Keep sync total: missing optional controls must not abort the
      // pass before the row containing the newly selected default is cleaned.
      const position = row.querySelector("[data-fallback-position]");
      if (position) position.textContent = index < 0 || !manualOrdering ? "" : String(index + 1);
      const order = row.querySelector("[data-fallback-order]");
      if (order) order.hidden = index < 0 || !manualOrdering;
      const moveUp = row.querySelector('[data-fallback-move="up"]');
      if (moveUp) moveUp.disabled = index <= 0;
      const moveDown = row.querySelector('[data-fallback-move="down"]');
      if (moveDown) moveDown.disabled = index < 0 || index === chosen.length - 1;
    }
  };
  const announce = (row) => {
    const index = rows().indexOf(row);
    const announcement = picker.querySelector("[data-fallback-announcement]");
    if (announcement) announcement.textContent = `${row.querySelector("strong").textContent}，第 ${index + 1} 位`;
    form.dispatchEvent(new Event("input", { bubbles: true }));
  };
  picker.addEventListener("change", (event) => {
    if (!canChange() || event.target.name !== "fallbackModelId") return;
    const row = event.target.closest("[data-fallback-row]");
    (event.target.checked ? selected : available).append(row);
    sync();
  });
  // Default model changes are a view-state change, not a mutation. Always
  // resynchronise so the previous default becomes selectable immediately,
  // even when another request has temporarily locked the submit button.
  form.querySelector('[name="defaultModelId"]')?.addEventListener("change", sync);
  // Keep this delegated listener as well: a parent form can replace the
  // select during a background refresh, while the picker itself remains
  // mounted. The current default must still be reflected immediately.
  form.addEventListener("change", (event) => {
    if (event.target?.name === "defaultModelId" || event.target?.name === "routingMode") sync();
  });
  picker.addEventListener("click", (event) => {
    const button = event.target.closest("[data-fallback-move]");
    if (!button || button.disabled || currentMode() !== "fixed" || !canChange()) return;
    const row = button.closest("[data-fallback-row]");
    const index = rows().indexOf(row);
    if (button.dataset.fallbackMove === "up" && index > 0) selected.insertBefore(row, rows()[index - 1]);
    if (button.dataset.fallbackMove === "down" && index < rows().length - 1) selected.insertBefore(rows()[index + 1], row);
    sync();
    announce(row);
    // A boundary arrow becomes disabled; keep keyboard focus on the other usable arrow.
    if (button.disabled) row.querySelector("[data-fallback-move]:not(:disabled)")?.focus({ preventScroll: true });
  });
  let drag = null;
  picker.addEventListener("pointerdown", (event) => {
    const handle = event.target.closest("[data-fallback-drag]");
    if (!handle || event.button !== 0 || currentMode() !== "fixed" || !canChange()) return;
    const row = handle.closest("[data-fallback-row]");
    if (row.parentElement !== selected) return;
    event.preventDefault();
    drag = { row, pointerId: event.pointerId, order: rows() };
    row.classList.add("is-dragging");
    picker.setPointerCapture(event.pointerId);
  });
  picker.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointerId || !canChange()) return;
    const rect = picker.querySelector(".fallback-model-lists").getBoundingClientRect();
    const scroller = picker.querySelector(".fallback-model-lists");
    if (event.clientY < rect.top + 32) scroller.scrollTop -= 16;
    if (event.clientY > rect.bottom - 32) scroller.scrollTop += 16;
    const peers = rows().filter((row) => row !== drag.row);
    const before = peers.find((row) => {
      const bounds = row.getBoundingClientRect();
      return event.clientY < bounds.top + bounds.height / 2;
    });
    selected.insertBefore(drag.row, before ?? null);
    sync();
  });
  const finishDrag = (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const current = drag;
    drag = null;
    if (event.type !== "pointerup" || !canChange()) current.order.forEach((row) => selected.append(row));
    current.row.classList.remove("is-dragging");
    if (picker.hasPointerCapture(event.pointerId)) picker.releasePointerCapture(event.pointerId);
    sync();
    if (canChange()) announce(current.row);
  };
  picker.addEventListener("pointerup", finishDrag);
  picker.addEventListener("pointercancel", finishDrag);
  picker.addEventListener("lostpointercapture", finishDrag);
  sync();
}

function formatModelPrice(model) {
  const input = Number(model?.input_price_per_1k ?? model?.input_price_per_million);
  const output = Number(model?.output_price_per_1k ?? model?.output_price_per_million);
  const cache = Number(model?.cache_price_per_1k ?? model?.cache_price_per_million ?? model?.input_price_per_1k ?? model?.input_price_per_million);
  if (![input, output, cache].every(Number.isFinite)) return "价格未配置";
  const currency = String(model?.currency || "CNY");
  const format = (value) => value.toFixed(6).replace(/0+$/, "").replace(/\.$/, "") || "0";
  return `输入 ${format(input)} / 输出 ${format(output)} / 缓存命中 ${format(cache)} ${currency} / 百万 Token`;
}

function firstNonEmpty(...values) {
  return values.find((value) => typeof value === "string" && value.trim())?.trim();
}

function readableModelId(value) {
  const text = typeof value === "string" ? value.trim() : "";
  return /^[0-9a-f]{8}-[0-9a-f-]{27,}$/iu.test(text) ? "" : text;
}

function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;").replaceAll("`", "&#096;");
}
