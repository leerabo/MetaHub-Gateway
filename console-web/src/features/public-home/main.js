import { icon } from "./icons.js";
import { legacyConsoleTarget } from "./entryRoute.js";

const redirect = legacyConsoleTarget(window.location);
if (redirect) {
  window.location.replace(redirect);
} else {
  for (const element of document.querySelectorAll("[data-icon]")) element.innerHTML = icon(element.dataset.icon);
  const toggle = document.querySelector("[data-motion-toggle]");
  let disposed = false;
  // The public shell keeps the static gateway visual. WebGL is reserved for
  // the authenticated access experience so quick sign-out redirects do not
  // leave a large module request running in the next page lifecycle.
  toggle.hidden = true;
  window.addEventListener("hashchange", () => {
    const target = legacyConsoleTarget(window.location);
    if (target) window.location.replace(target);
  });
  window.addEventListener("pagehide", (event) => {
    if (event.persisted) return;
    disposed = true;
  });
}
