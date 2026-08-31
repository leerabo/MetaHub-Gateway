import { listPartnerWorkspaces } from "../../lib/partnerChannel.js";
import { createPartnerPageController } from "./partnerPageState.js";

const root = globalThis.document?.querySelector("#partner-app");
if (root) {
  const hostname = globalThis.window.location.hostname;
  const controlApiUrl = new Set(["localhost", "127.0.0.1", "::1"]).has(hostname)
    ? `${globalThis.window.location.protocol}//${hostname}:8001`
    : globalThis.window.location.origin;
  const controller = createPartnerPageController({
    root,
    api: {
      workspaces: (input) => listPartnerWorkspaces({ controlApiUrl, ...input }),
    },
  });
  controller.mount();
  globalThis.window.addEventListener("pagehide", () => controller.destroy(), { once: true });
}
