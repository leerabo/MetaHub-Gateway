import { createUniquePageVisitTracker } from "./consoleVisitTracker.js";
import { WORKSPACE_ROUTE_REGISTRY } from "./workspaceNavigation.js";
import { workspaceResourcePageOwner } from "./workspaceResourcePage.js";

export function createWorkspacePageVisitTracker({ current, recordVisit, createIdempotencyKey, onError }) {
  let active = null;
  let transmitTimer = null;
  let transmitTimerResolve = null;
  const TRANSMIT_DELAY_MS = 750;
  // A route transition aborts the browser-side fetch, but the server may still
  // be finishing the SQLite write. Keep telemetry writers serial so a burst of
  // navigation cannot create one stale writer per page.
  let transmissionTail = Promise.resolve();
  const tracker = createUniquePageVisitTracker({
    targetItemId: "workspace-page",
    recordVisit: async ({ visitContext }) => {
      if (!isCurrent(visitContext)) return;
      const transmit = async () => {
        if (!isCurrent(visitContext)) return;
        try {
          await recordVisit({ visit: visitContext.body, idempotencyKey: visitContext.idempotencyKey,
            signal: visitContext.controller.signal });
        } catch (error) {
          if (isCurrent(visitContext) && error?.name !== "AbortError") onError?.(error);
        }
      };
      // Task-home and resource pages may still be materializing their first
      // read model immediately after render. Keep best-effort telemetry out of
      // that critical writer window; leaving the page cancels the timer.
      const delayedTransmit = () => new Promise((resolve) => {
        transmitTimer = globalThis.setTimeout(() => {
          transmitTimer = null;
          transmitTimerResolve = null;
          void transmit().then(resolve, resolve);
        }, TRANSMIT_DELAY_MS);
        transmitTimerResolve = resolve;
      });
      const scheduled = transmissionTail.then(delayedTransmit, delayedTransmit);
      transmissionTail = scheduled.catch(() => undefined);
      await scheduled;
    },
  });

  function owner() {
    const state = current();
    const page = workspaceResourcePageOwner(state.session, state.shell, { hash: state.hash });
    const navigation = state.shell?.navigation;
    if (!page || state.renderedNavigation !== page.navigation
      || !["primary", "detail", "task_home", "developer_center"].includes(WORKSPACE_ROUTE_REGISTRY[page.route]?.placement)
      || !/^[0-9a-f]{64}$/u.test(navigation.page_visit_session_ref)) return null;
    return { page, navigation,
      key: JSON.stringify([page.key, navigation.page_visit_session_ref, navigation.contract_snapshot_id]) };
  }

  function isCurrent(visit) {
    const next = owner();
    return active === visit && !visit.controller.signal.aborted && next?.key === visit.key
      && next.page.session === visit.session;
  }

  function leave() {
    if (transmitTimer !== null) {
      globalThis.clearTimeout(transmitTimer);
      transmitTimer = null;
      transmitTimerResolve?.();
      transmitTimerResolve = null;
    }
    active?.controller.abort();
    active = null;
    tracker.leave();
  }

  function afterRender({ source = "direct_hash", viewportClass = "desktop" } = {}) {
    const next = owner();
    if (!next) {
      leave();
      return Promise.resolve();
    }
    if (active && (active.key !== next.key || active.session !== next.page.session)) leave();
    if (!active) {
      active = Object.freeze({ key: next.key, session: next.page.session,
        controller: new AbortController(), idempotencyKey: createIdempotencyKey("page-visit"),
        body: Object.freeze({
          page_visit_session_ref: next.navigation.page_visit_session_ref,
          contract_snapshot_id: next.navigation.contract_snapshot_id,
          permission_fingerprint: next.navigation.permission_fingerprint,
          navigation_epoch: next.navigation.navigation_epoch,
          route_code: next.page.route, source, viewport_class: viewportClass,
        }),
      });
    }
    return tracker.afterRender({ activeItemId: "workspace-page", sessionKey: active.key,
      source, visitContext: active });
  }

  return Object.freeze({ afterRender, leave });
}
