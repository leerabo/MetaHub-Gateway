import { icon } from "./icons.js";
import { legacyConsoleTarget } from "./entryRoute.js";

const redirect = legacyConsoleTarget(window.location);
if (redirect) {
  window.location.replace(redirect);
} else {
  for (const element of document.querySelectorAll("[data-icon]")) element.innerHTML = icon(element.dataset.icon);
  const navToggle = document.querySelector("[data-home-nav-toggle]");
  const mobileNav = document.querySelector("[data-home-mobile-nav]");
  const setMobileNavOpen = (open) => {
    if (!navToggle || !mobileNav) return;
    const next = Boolean(open);
    mobileNav.hidden = !next;
    navToggle.setAttribute("aria-expanded", String(next));
    navToggle.textContent = next ? "关闭" : "菜单";
  };
  navToggle?.addEventListener("click", () => setMobileNavOpen(mobileNav?.hidden));
  mobileNav?.addEventListener("click", (event) => {
    if (event.target.closest("a")) setMobileNavOpen(false);
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setMobileNavOpen(false);
  });
  window.addEventListener("hashchange", () => {
    setMobileNavOpen(false);
    const target = legacyConsoleTarget(window.location);
    if (target) window.location.replace(target);
  });
}
