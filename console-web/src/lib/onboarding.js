import { AuthApiError } from "./auth.js";

/** Read backend-authoritative initialization progress for the signed-in tenant. */
export async function getTenantOnboardingSummary({ controlApiUrl, token, signal }) {
  const response = await fetch(`${controlApiUrl}/tenant/onboarding/summary`, {
    headers: {
      Authorization: `Bearer ${token}`,
      "X-Request-ID": `console_onboarding_${Date.now()}`,
    },
    signal,
  });
  const payload = await readJson(response);
  if (!response.ok) {
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `初始化状态接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
