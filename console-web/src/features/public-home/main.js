import { icon } from "./icons.js";
import { legacyConsoleTarget } from "./entryRoute.js";

const redirect = legacyConsoleTarget(window.location);
if (redirect) {
  window.location.replace(redirect);
} else {
  for (const element of document.querySelectorAll("[data-icon]")) element.innerHTML = icon(element.dataset.icon);
  const navToggle = document.querySelector("[data-home-nav-toggle]");
  const mobileNav = document.querySelector("[data-home-mobile-nav]");
  const mobileBreakpoint = window.matchMedia("(max-width: 600px)");
  const setMobileNavOpen = (open, { restoreFocus = false } = {}) => {
    if (!navToggle || !mobileNav) return;
    const next = Boolean(open);
    mobileNav.hidden = !next;
    navToggle.setAttribute("aria-expanded", String(next));
    navToggle.textContent = next ? "关闭" : "菜单";
    if (next) mobileNav.querySelector("a")?.focus();
    else if (restoreFocus) navToggle.focus();
  };
  navToggle?.addEventListener("click", () => setMobileNavOpen(mobileNav?.hidden));
  mobileNav?.addEventListener("click", (event) => {
    if (event.target.closest("a")) setMobileNavOpen(false);
  });
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (mobileNav?.hidden || (target instanceof Element && target.closest(".home-header, [data-home-mobile-nav]"))) return;
    setMobileNavOpen(false, { restoreFocus: true });
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !mobileNav?.hidden) setMobileNavOpen(false, { restoreFocus: true });
  });
  mobileBreakpoint.addEventListener("change", (event) => {
    if (!event.matches) setMobileNavOpen(false);
  });
  window.addEventListener("hashchange", () => {
    setMobileNavOpen(false);
    const target = legacyConsoleTarget(window.location);
    if (target) window.location.replace(target);
  });
}
