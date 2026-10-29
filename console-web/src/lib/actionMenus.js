/** Place action popovers in the top layer so tables cannot clip their menus. */
export function initializeActionMenus() {
  if (!globalThis.document || document.documentElement.dataset.actionMenusReady) return;
  document.documentElement.dataset.actionMenusReady = "true";
  const place = (menu) => {
    const trigger = document.querySelector(`[popovertarget="${CSS.escape(menu.id)}"]`);
    if (!trigger) return;
    const anchor = trigger.getBoundingClientRect();
    const bounds = menu.getBoundingClientRect();
    const gap = 12;
    menu.style.left = `${Math.max(gap, Math.min(anchor.right - bounds.width, innerWidth - bounds.width - gap))}px`;
    menu.style.top = `${Math.max(gap, anchor.bottom + bounds.height + 6 <= innerHeight - gap
      ? anchor.bottom + 6 : anchor.top - bounds.height - 6)}px`;
  };
  document.addEventListener("toggle", (event) => {
    const menu = event.target;
    if (!menu.matches?.("[data-action-menu]")) return;
    const open = event.newState === "open";
    const trigger = document.querySelector(`[popovertarget="${CSS.escape(menu.id)}"]`);
    trigger?.setAttribute("aria-expanded", String(open));
    if (open) place(menu);
  }, true);
  document.addEventListener("click", (event) => {
    const action = event.target.closest?.("[data-action-menu] button");
    if (action) action.closest("[data-action-menu]").hidePopover();
  });
  document.addEventListener("keydown", (event) => {
    const menu = event.target.closest?.("[data-action-menu]");
    if (!menu || !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const actions = [...menu.querySelectorAll("button:not(:disabled)")];
    if (!actions.length) return;
    event.preventDefault();
    const index = actions.indexOf(document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? actions.length - 1
      : (index + (event.key === "ArrowDown" ? 1 : -1) + actions.length) % actions.length;
    actions[next].focus();
  });
  const close = () => document.querySelectorAll("[data-action-menu]:popover-open").forEach((menu) => menu.hidePopover());
  addEventListener("resize", close);
  document.addEventListener("scroll", (event) => {
    if (!event.target.closest?.("[data-action-menu]")) close();
  }, true);
}
