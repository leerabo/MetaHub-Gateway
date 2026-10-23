// Both workbenches share a browser preference; it contains no account data.
const THEME_KEY = "metahub.appearance.theme";
let installed = false;

export function renderThemeToggle() {
  const light = globalThis.document?.documentElement?.dataset.theme === "light";
  return `<button class="theme-toggle" type="button" data-theme-toggle aria-label="${light ? "切换到深色模式" : "切换到浅色模式"}">${light ? "深色模式" : "浅色模式"}</button>`;
}

export function initializeTheme() {
  const doc = globalThis.document;
  if (!doc || installed) return;
  installed = true;
  const apply = (theme) => {
    const light = theme === "light";
    doc.documentElement.dataset.theme = light ? "light" : "dark";
    doc.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.textContent = light ? "深色模式" : "浅色模式";
      button.setAttribute("aria-label", light ? "切换到深色模式" : "切换到浅色模式");
    });
  };
  let saved;
  try { saved = globalThis.localStorage.getItem(THEME_KEY); } catch { /* Storage can be disabled. */ }
  apply(saved);
  doc.addEventListener("click", (event) => {
    if (!event.target.closest?.("[data-theme-toggle]")) return;
    const next = doc.documentElement.dataset.theme === "light" ? "dark" : "light";
    apply(next);
    try { globalThis.localStorage.setItem(THEME_KEY, next); } catch { /* Keep the current tab usable. */ }
  });
  globalThis.addEventListener("storage", (event) => {
    if (event.key === THEME_KEY || event.key === null) apply(event.newValue);
  });
}
