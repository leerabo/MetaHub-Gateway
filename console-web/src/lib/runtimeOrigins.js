const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);

/**
 * A bare RFC1918 address is a single-host trial deployment, not a public
 * reverse-proxy domain. Its companion Control, Gateway, and Status services
 * run on their fixed local ports.
 */
export function isDirectServiceHost(hostname) {
  const normalized = String(hostname ?? "").trim().toLowerCase();
  if (LOOPBACK_HOSTS.has(normalized)) return true;
  const octets = normalized.split(".").map((part) => Number(part));
  if (octets.length !== 4 || octets.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) {
    return false;
  }
  return octets[0] === 10
    || (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31)
    || (octets[0] === 192 && octets[1] === 168);
}

export function serviceOrigin(location, port) {
  // Module-only consumers such as lifecycle tests have no browser location.
  // Preserve the Console's local-development default rather than passing an
  // empty origin into the Workspace Cookie boundary.
  if (!location) return `http://localhost:${port}`;
  if (!isDirectServiceHost(location?.hostname)) return location?.origin ?? "";
  return `${location?.protocol ?? "http:"}//${location?.hostname ?? "localhost"}:${port}`;
}

export function gatewayServiceOrigin(location) {
  if (isDirectServiceHost(location?.hostname)) return serviceOrigin(location, 8000);
  return "http://localhost:8000";
}
