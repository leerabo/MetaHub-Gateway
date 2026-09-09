import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

export function listConsumers(options) {
  const query = new URLSearchParams({ limit: "50", offset: String(options.offset ?? 0) });
  return consumerRequest(options, `/consumers?${query}`);
}

export function createConsumer(options) {
  return consumerRequest(options, "/consumers", "POST", {
    display_name: options.displayName, department: options.department,
  });
}

export function updateConsumer(options) {
  return consumerRequest(options, `/consumers/${encodeURIComponent(options.consumerId)}`, "PATCH", {
    display_name: options.displayName, department: options.department,
    status: options.status, revision: options.revision,
  });
}

async function consumerRequest(options, path, method = "GET", body) {
  const response = await authenticatedResourceFetch(`${options.controlApiUrl}${path}`, {
    authentication: options.authentication, token: options.token, signal: options.signal, method,
    headers: { "Content-Type": "application/json" },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  let result;
  try { result = await response.json(); } catch (error) {
    if (error?.name === "AbortError") throw error;
    result = {};
  }
  if (!response.ok) throw new AuthApiError({
    status: response.status, code: result.code, message: result.message || "使用者管理暂不可用",
    requestId: result.request_id || response.headers.get("X-Request-ID") || "-",
  });
  return { state: "ok", ...result };
}
