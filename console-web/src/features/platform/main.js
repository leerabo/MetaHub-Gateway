import {
  approveSupportGrant,
  createBreakGlassSession,
  createPlatformOperator,
  createSupportGrant,
  createSupportSession,
  getPlatformNavigation,
  getPlatformSession,
  getSupportDiagnostics,
  listPlatformAudit,
  listPlatformOperators,
  listSupportGrants,
  openPlatformSession,
  revokeSupportGrant,
  updatePlatformOperator,
} from "../../lib/platform.js";
import { createPlatformPageController } from "./platformPageState.js";

const root = globalThis.document?.querySelector("#platform-app");
if (root) {
  const hostname = globalThis.window.location.hostname;
  const controlApiUrl = new Set(["localhost", "127.0.0.1", "::1"]).has(hostname)
    ? `${globalThis.window.location.protocol}//${hostname}:8001`
    : globalThis.window.location.origin;
  createPlatformPageController({
    root,
    api: {
      session: (input) => getPlatformSession({ controlApiUrl, ...input }),
      openSession: (input) => openPlatformSession({ controlApiUrl, ...input }),
      navigation: (input) => getPlatformNavigation({ controlApiUrl, ...input }),
      operators: (input) => listPlatformOperators({ controlApiUrl, ...input }),
      createOperator: (input) => createPlatformOperator({ controlApiUrl, ...input }),
      updateOperator: (input) => updatePlatformOperator({ controlApiUrl, ...input }),
      grants: (input) => listSupportGrants({ controlApiUrl, ...input }),
      createGrant: (input) => createSupportGrant({ controlApiUrl, ...input }),
      approveGrant: (input) => approveSupportGrant({ controlApiUrl, ...input }),
      revokeGrant: (input) => revokeSupportGrant({ controlApiUrl, ...input }),
      supportSession: (input) => createSupportSession({ controlApiUrl, ...input }),
      diagnostics: (input) => getSupportDiagnostics({ controlApiUrl, ...input }),
      breakGlass: (input) => createBreakGlassSession({ controlApiUrl, ...input }),
      audit: (input) => listPlatformAudit({ controlApiUrl, ...input }),
    },
  }).mount();
}
