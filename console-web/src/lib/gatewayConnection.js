/** Derive protocol paths from the configured endpoint without inventing a host. */
export function gatewayConnectionAddresses(value) {
  if (typeof value !== "string" || /[\s\\]/u.test(value)) return null;
  try {
    const url = new URL(value);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) return null;
    const pathname = url.pathname.replace(/\/+$/u, "");
    if (!pathname.endsWith("/v1")) return null;
    const service = `${url.origin}${pathname.slice(0, -3).replace(/\/+$/u, "")}`;
    return { service, openai: `${url.origin}${pathname}`, anthropic: service };
  } catch {
    return null;
  }
}
