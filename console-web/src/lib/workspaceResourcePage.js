import { hasEveryPermission } from "./permissions.js";
import { WORKSPACE_ROUTE_REGISTRY } from "./workspaceNavigation.js";
import { sameWorkspaceConsoleSession, workspaceConsoleSessionFromShell } from "./workspaceConsoleSession.js";

export function workspaceResourcePageOwner(session, shell, { navigationId, permissions = [], hash } = {}) {
  if (!sameWorkspaceConsoleSession(session, workspaceConsoleSessionFromShell(shell))
    || (permissions.length > 0 && !hasEveryPermission(session.context, permissions))) return null;
  const id = shell.activeNavigationItemId;
  const route = shell.activeRouteCode;
  const navigation = shell.navigation;
  const items = navigation?.navigation_items?.filter((item) => item?.id === id) ?? [];
  const item = items[0];
  if (!id || (navigationId && navigationId !== id) || (hash && hash !== id)
    || WORKSPACE_ROUTE_REGISTRY[route]?.sourceId !== id
    || navigation.active_navigation_item_id !== id || navigation.active_route_code !== route
    || items.length !== 1 || item.route_code !== route || item.target_section !== id
    || item.visible !== true || item.disabled !== false) return null;
  return Object.freeze({ session, navigation: id, route, key: JSON.stringify([session.owner, id, route]) });
}

// Read generations are independent of rendered bindings and pending writes.
export function createWorkspaceResourcePage({ current }) {
  let page = null;
  let renderGeneration = 0;
  let dirty = false;
  let mutation = null;
  const reads = new Map();

  function owner(permissions = [], navigationId) {
    const state = current();
    return workspaceResourcePageOwner(state.session, state.shell, {
      permissions, navigationId, hash: state.hash,
    });
  }

  function currentPage(permissions = []) {
    const next = owner(permissions);
    return Boolean(page && next && page.session === next.session && page.key === next.key);
  }

  function cancelReads() {
    for (const read of reads.values()) read.controller.abort();
    reads.clear();
  }

  function clear() {
    cancelReads();
    mutation?.controller.abort();
    mutation = null;
    page = null;
    dirty = false;
    renderGeneration += 1;
  }

  function enter() {
    const next = owner();
    const unchanged = Boolean(page && next && page.session === next.session && page.key === next.key);
    if (!unchanged) {
      clear();
      page = next;
    }
    return { valid: Boolean(next), changed: !unchanged };
  }

  function render() {
    renderGeneration += 1;
    dirty = false;
  }

  function bind(element, permissions = [], navigationId) {
    if (!element?.isConnected || !currentPage(permissions)
      || (navigationId && page.navigation !== navigationId)) return null;
    return Object.freeze({ page, element, permissions: Object.freeze([...permissions]), renderGeneration });
  }

  function isBindingCurrent(binding) {
    return Boolean(binding && binding.page === page && binding.renderGeneration === renderGeneration
      && binding.element.isConnected && currentPage(binding.permissions));
  }

  function beginRead(lane, permissions) {
    if (!currentPage(permissions)) return null;
    reads.get(lane)?.controller.abort();
    const ticket = Object.freeze({ lane, page, permissions: [...permissions],
      refreshGeneration: current().refreshGeneration, controller: new AbortController() });
    reads.set(lane, ticket);
    return ticket;
  }

  function isReadCurrent(ticket) {
    return Boolean(ticket && ticket.page === page && reads.get(ticket.lane) === ticket
      && !ticket.controller.signal.aborted && currentPage(ticket.permissions)
      && ticket.refreshGeneration === current().refreshGeneration);
  }

  function finishRead(ticket) {
    if (ticket && reads.get(ticket.lane) === ticket) reads.delete(ticket.lane);
  }

  function beginMutation(binding) {
    if (mutation || !isBindingCurrent(binding)) return null;
    mutation = Object.freeze({ binding, controller: new AbortController() });
    return mutation;
  }

  function isMutationCurrent(ticket) {
    return Boolean(ticket && ticket === mutation && !ticket.controller.signal.aborted
      && isBindingCurrent(ticket.binding));
  }

  function finishMutation(ticket) {
    if (mutation === ticket) mutation = null;
  }

  function markDirty(binding) {
    if (isBindingCurrent(binding)) dirty = true;
  }

  return Object.freeze({ enter, clear, cancelReads, render, bind, isBindingCurrent,
    beginRead, isReadCurrent, finishRead, beginMutation, isMutationCurrent, finishMutation, markDirty,
    blocksReplacement: (background) => currentPage() && Boolean(mutation || (background && dirty)),
    navigation: () => page?.navigation,
  });
}
