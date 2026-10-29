import { iconNodes } from "../../vendor/lucide.js";

export function icon(name) {
  const nodes = iconNodes[name];
  if (!nodes) throw new TypeError("Unknown public icon");
  return `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${nodes.map(([tag, attributes]) => `<${tag} ${Object.entries(attributes).map(([key, value]) => `${key}="${value}"`).join(" ")}></${tag}>`).join("")}</svg>`;
}
