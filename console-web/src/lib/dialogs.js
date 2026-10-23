/** Viewport-safe positioning and optional title-bar dragging for both modal families. */
export function constrainDialogOffset(rect, offset, viewport, gap = 16) {
  return {
    x: Math.min(viewport.width - gap - rect.right, Math.max(gap - rect.left, offset.x)),
    y: Math.min(viewport.height - gap - rect.bottom, Math.max(gap - rect.top, offset.y)),
  };
}

let installed = false;
export function initializeDialogs() {
  const doc = globalThis.document;
  if (!doc || installed) return;
  installed = true;
  const states = new WeakMap();
  const tracked = new Set();
  const selector = 'dialog, [aria-modal="true"][role="dialog"], [aria-modal="true"][role="alertdialog"]';
  const headerSelector = 'header, [class$="-dialog-heading"], [class$="-dialog-header"], .request-log-detail-heading, .policy-editor-heading, .modal__header, .dlp-dialog-heading';
  const interactive = 'button, a, input, select, textarea, summary, [contenteditable="true"]';
  const viewport = () => ({ width: doc.documentElement.clientWidth, height: globalThis.innerHeight });
  function updateHeadingInset(state) {
    if (!state.heading?.parentElement) return;
    const inset = parseFloat(globalThis.getComputedStyle(state.heading.parentElement).paddingTop) || 0;
    state.heading.style.setProperty('--modal-header-inset', `${inset}px`);
  }
  function move(surface, state, offset) {
    const box = surface.getBoundingClientRect();
    const base = { left: box.left - state.x, right: box.right - state.x,
      top: box.top - state.y, bottom: box.bottom - state.y };
    const next = constrainDialogOffset(base, offset, viewport());
    state.x = next.x;
    state.y = next.y;
    surface.style.setProperty('--modal-offset-x', `${next.x}px`);
    surface.style.setProperty('--modal-offset-y', `${next.y}px`);
  }
  function prepare(surface) {
    let state = states.get(surface);
    if (!state) {
      state = { x: 0, y: 0, open: false };
      states.set(surface, state);
      tracked.add(surface);
      surface.setAttribute('data-modal-surface', '');
      if (globalThis.ResizeObserver) {
        state.resize = new ResizeObserver(() => {
          updateHeadingInset(state);
          if (state.open && (state.x || state.y) && surface.getClientRects().length) {
            move(surface, state, state);
          }
        });
        state.resize.observe(surface);
      }
    }
    if (!state.heading || !surface.contains(state.heading)) {
      state.heading = null;
      // Anchor dragging to the dialog title, not a nested record's <header>.
      const title = surface.querySelector('h2, h3');
      const titleBar = title?.closest(headerSelector);
      const heading = titleBar && titleBar !== surface && surface.contains(titleBar)
        ? titleBar : title ?? surface.querySelector(headerSelector);
      if (heading) {
        state.heading = heading;
        heading.setAttribute('data-modal-drag-handle', '');
        heading.setAttribute('title', '拖动标题可移动弹窗；双击可居中');
        if (!heading.hasAttribute('tabindex')) heading.tabIndex = 0;
        heading.setAttribute('aria-keyshortcuts', 'ArrowUp ArrowDown ArrowLeft ArrowRight Home');
        updateHeadingInset(state);
        heading.addEventListener('pointerdown', (event) => {
          if (event.button !== 0 || event.target.closest(interactive)) return;
          const origin = { x: event.clientX, y: event.clientY, offsetX: state.x, offsetY: state.y };
          heading.setPointerCapture(event.pointerId);
          surface.setAttribute('data-modal-dragging', '');
          const drag = (next) => {
            if (next.pointerId !== event.pointerId) return;
            move(surface, state, { x: origin.offsetX + next.clientX - origin.x,
              y: origin.offsetY + next.clientY - origin.y });
          };
          const stop = () => {
            surface.removeAttribute('data-modal-dragging');
            heading.removeEventListener('pointermove', drag);
            heading.removeEventListener('pointerup', stop);
            heading.removeEventListener('pointercancel', stop);
            heading.removeEventListener('lostpointercapture', stop);
          };
          heading.addEventListener('pointermove', drag);
          heading.addEventListener('pointerup', stop);
          heading.addEventListener('pointercancel', stop);
          heading.addEventListener('lostpointercapture', stop);
          event.preventDefault();
        });
        heading.addEventListener('dblclick', (event) => {
          if (!event.target.closest(interactive)) move(surface, state, { x: 0, y: 0 });
        });
        heading.addEventListener('keydown', (event) => {
          if (event.target !== heading) return;
          const delta = { ArrowLeft: [-16, 0], ArrowRight: [16, 0], ArrowUp: [0, -16], ArrowDown: [0, 16] }[event.key];
          if (!delta && event.key !== 'Home') return;
          event.preventDefault();
          move(surface, state, delta ? { x: state.x + delta[0], y: state.y + delta[1] } : { x: 0, y: 0 });
        });
      }
    }
    const native = surface.tagName === 'DIALOG';
    updateHeadingInset(state);
    const layer = native ? null : surface.parentElement;
    const open = native ? surface.open : !surface.closest('[hidden]');
    // Popover puts the existing backdrop in the top layer without moving DOM nodes.
    // Feature-owned listeners, focus traps, query scopes and removal still work.
    if (layer && /backdrop/u.test(layer.className)) {
      layer.setAttribute('data-modal-viewport-layer', '');
      if (typeof layer.showPopover === 'function') {
        layer.setAttribute('popover', 'manual');
        if (open && !layer.matches(':popover-open')) layer.showPopover();
        if (!open && layer.matches(':popover-open')) layer.hidePopover();
      }
    }
    if (open && !state.open) {
      surface.scrollTop = 0;
      state.x = state.y = 0;
      surface.style.setProperty('--modal-offset-x', '0px');
      surface.style.setProperty('--modal-offset-y', '0px');
    } else if (open && (state.x || state.y)) move(surface, state, state);
    state.open = open;
  }
  const scan = () => {
    for (const surface of tracked) {
      if (surface.isConnected) continue;
      states.get(surface)?.resize?.disconnect();
      tracked.delete(surface);
    }
    doc.querySelectorAll(selector).forEach(prepare);
  };
  const observer = new MutationObserver(scan);
  observer.observe(doc.documentElement, { subtree: true, childList: true, attributes: true, attributeFilter: ['open', 'hidden'] });
  globalThis.addEventListener('resize', scan);
  scan();
}
