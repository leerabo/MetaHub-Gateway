import { AuthApiError } from "./auth.js";

// A lost write response is not proof of failure. Never replay the write on timeout.
export async function withResourceMutationDeadline(request, signal, { timeoutMs = 15000, requestId } = {}) {
  const controller = new AbortController();
  let timer;
  let cancel;
  const deadline = new Promise((_, reject) => {
    cancel = () => {
      reject(signal.reason ?? new DOMException("Cancelled", "AbortError"));
      controller.abort();
    };
    if (signal?.aborted) return cancel();
    signal?.addEventListener("abort", cancel, { once: true });
    timer = setTimeout(() => {
      reject(new AuthApiError({ status: 0, code: "resource.mutation_unconfirmed",
        message: "提交结果尚未确认，请先刷新列表核对。", requestId }));
      controller.abort();
    }, timeoutMs);
  });
  try {
    if (signal?.aborted) return await deadline;
    return await Promise.race([deadline, request(controller.signal)]);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}
