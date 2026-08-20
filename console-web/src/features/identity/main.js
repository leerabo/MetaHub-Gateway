import {
  getIdentityMe,
  listIdentitySessions,
  loginIdentity,
  logoutIdentity,
  revokeIdentitySession,
  rotateIdentityPassword,
} from "../../lib/identity.js";
import { createIdentityPageController } from "./identityPageState.js";

const root = globalThis.document?.querySelector("#identity-app");
if (root) {
  const hostname = globalThis.window.location.hostname;
  const controlApiUrl = new Set(["localhost", "127.0.0.1", "::1"]).has(hostname)
    ? `${globalThis.window.location.protocol}//${hostname}:8001`
    : globalThis.window.location.origin;
  const controller = createIdentityPageController({
    root,
    api: {
      login: (input) => loginIdentity({ controlApiUrl, ...input }),
      me: (input) => getIdentityMe({ controlApiUrl, ...input }),
      sessions: (input) => listIdentitySessions({ controlApiUrl, ...input }),
      revoke: (input) => revokeIdentitySession({ controlApiUrl, ...input }),
      logout: (input) => logoutIdentity({ controlApiUrl, ...input }),
      rotate: (input) => rotateIdentityPassword({ controlApiUrl, ...input }),
    },
  });
  controller.mount();
}
