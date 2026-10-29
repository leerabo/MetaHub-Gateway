import { listPartnerWorkspaces } from "../../lib/partnerChannel.js";
import { createPartnerPageController } from "./partnerPageState.js";
import { serviceOrigin } from "../../lib/runtimeOrigins.js";
import { installUserFacingTextGuard } from "../../lib/userFacingTextGuard.js";

const root = globalThis.document?.querySelector("#partner-app");
if (root) {
  installUserFacingTextGuard(root);
  const controlApiUrl = serviceOrigin(globalThis.window.location, 8001);
  const controller = createPartnerPageController({
    root,
    api: {
      workspaces: (input) => listPartnerWorkspaces({ controlApiUrl, ...input }),
    },
  });
  controller.mount();
  globalThis.window.addEventListener("pagehide", () => controller.destroy(), { once: true });
}
