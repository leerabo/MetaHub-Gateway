export function bindTenantDataLifecycleInteractions({ onRefresh, onExport, onDownload, onDelete, onCancelDelete }) {
  document.querySelector("[data-tenant-data-refresh]")?.addEventListener("click", () => void onRefresh?.());
  document.querySelector("[data-tenant-data-export]")?.addEventListener("click", () => void onExport?.());
  document.querySelectorAll("[data-tenant-data-download]").forEach((button) => button.addEventListener("click", () => void onDownload?.(button.dataset.tenantDataDownload)));
  document.querySelector("[data-tenant-data-delete]")?.addEventListener("click", () => void onDelete?.());
  document.querySelector("[data-tenant-data-cancel]")?.addEventListener("click", () => void onCancelDelete?.());
  document.querySelector("[data-tenant-data-dialog]")?.addEventListener("cancel", (event) => {
    const dialog = event.currentTarget;
    if (dialog?.getAttribute("aria-busy") === "true") { event.preventDefault(); return; }
    event.preventDefault();
    void onCancelDelete?.();
  });
  document.querySelector("[data-tenant-data-dialog]")?.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const dialog = event.currentTarget;
    const focusable = [...dialog.querySelectorAll("button:not([disabled]), input:not([disabled])")];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  document.querySelector("[data-tenant-data-deletion-form]")?.addEventListener("submit", (event) => { event.preventDefault(); const form = event.currentTarget; void onDelete?.(new FormData(form)); });
}
