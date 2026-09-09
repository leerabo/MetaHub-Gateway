const PUBLIC_ANCHORS = new Set(["", "#capabilities", "#approach", "#main"]);

export function legacyConsoleTarget(location) {
  if (PUBLIC_ANCHORS.has(location.hash ?? "")) return null;
  return `/console.html${location.search ?? ""}${location.hash}`;
}
