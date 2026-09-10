import { AuthApiError } from "./auth.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const WRITES = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const PROTECTED_HEADERS = ["authorization", "proxy-authorization", "cookie", "x-csrf-token"];
const HEADER_NAMES = Object.freeze({
  accept: "Accept", authorization: "Authorization", "content-type": "Content-Type",
  "x-csrf-token": "X-CSRF-Token", "x-request-id": "X-Request-ID",
});

// Authentication selects a transport, not an authorization decision or a browser owner.
export function authenticatedResourceFetch(url, {
  authentication,
  token,
  method = "GET",
  csrfRequired = false,
  headers: inputHeaders,
  ...options
} = {}) {
  const headers = new Headers(inputHeaders);
  if (PROTECTED_HEADERS.some((name) => headers.has(name))) invalidCredentials();
  const verb = String(method).toUpperCase();
  if (!new Set(["GET", "HEAD", ...WRITES]).has(verb)) invalidCredentials();
  if (authentication !== undefined && authentication !== "workspace" && authentication !== "legacy") {
    invalidCredentials();
  }
  const workspace = authentication === "workspace";
  if (workspace) {
    if (token !== undefined && token !== null) invalidCredentials("workspace_session.credential_conflict");
    if (WRITES.has(verb) || csrfRequired) {
      try { headers.set("X-CSRF-Token", readWorkspaceCsrfCookie()); }
      catch { invalidCredentials("workspace_session.csrf_invalid", 403); }
    }
  } else {
    if (typeof token !== "string" || !token || /[\s\x00-\x1f\x7f,]/u.test(token)) {
      invalidCredentials("auth.token_missing");
    }
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (!headers.has("Accept")) headers.set("Accept", "application/json");
  return fetch(url, {
    ...options,
    method: verb,
    headers: Object.fromEntries([...headers].map(([name, value]) => [HEADER_NAMES[name] ?? name, value])),
    credentials: workspace ? "include" : "omit",
    cache: "no-store",
    redirect: "error",
    // Same-origin GET omits Origin; send only the origin for CSRF-protected exports.
    ...(workspace && verb === "GET" && csrfRequired ? { referrerPolicy: "origin" } : {}),
  });
}

function invalidCredentials(code = "auth.credential_invalid", status = 401) {
  throw new AuthApiError({ status, code, message: "Authentication context is unavailable." });
}
