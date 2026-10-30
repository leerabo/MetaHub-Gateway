import { readPlatformCsrfCookie } from "../../lib/platform.js";

export function initializeSystemUpdates(root, origin) {
  const initializePanel = () => {
    const panel = root.querySelector("[data-system-updates]");
    if (panel && !panel.dataset.initialized) {
      panel.dataset.initialized = "true";
      load(panel);
    }
  };
  const observer = new MutationObserver(initializePanel);
  observer.observe(root, { childList: true, subtree: true });
  initializePanel();
  async function request(options = {}) {
    // Bound status reads, including a response whose JSON body never finishes.
    // An install submission remains a separate, explicitly confirmed operation.
    const controller = options.method ? null : new AbortController();
    const timer = controller ? setTimeout(() => controller.abort(), 8000) : null;
    const { url = `${origin}/platform/system-updates`, ...requestOptions } = options;
    try {
      const response = await fetch(url, {
        credentials: "include", cache: "no-store", ...requestOptions,
        ...(controller ? { signal: controller.signal } : {}),
      });
      if (!response.ok) throw new Error("无法加载升级状态，请检查平台会话和升级服务。");
      return await response.json();
    } catch (error) {
      if (controller?.signal.aborted) throw new Error("读取升级状态超时，请稍后重试。");
      throw error;
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }
  async function load(panel) {
    panel.setAttribute("aria-busy", "true");
    try {
      const value = await request();
      if (!panel.isConnected) return;
      const releases = (value.releases ?? []).filter((item) => item.version !== value.current_version);
      const busy = value.upgrade_blocked;
      panel.innerHTML = `<div class="platform-section-heading"><div><p>系统版本 ${escape(value.current_version)}</p><h2>系统升级</h2></div><button type="button" data-update-refresh>刷新状态</button></div>
        <p>这里升级整套 SaaS 服务，包括控制台、控制面和网关。客户网关会随系统版本自动更新。升级前自动备份，安装期间会短暂暂停服务。</p>
        ${value.enabled && releases.length && !busy ? `<form class="platform-system-update-form" data-update-form><label><span>可安装版本</span><select name="version">${releases.map((item) => `<option value="${escape(item.version)}">${escape(item.version)}${item.summary ? ` · ${escape(item.summary)}` : ""}</option>`).join("")}</select></label><label class="platform-check"><input name="confirmed" type="checkbox" required /><span>已确认暂停服务并安装此版本</span></label><button type="submit" class="platform-primary">备份并升级</button></form>` : `<p>${busy ? "已有升级或重启任务正在处理，或存在需要人工恢复的失败任务。" : value.enabled ? "当前没有已备好的新版本。" : "宿主机升级服务尚未启用。"}</p>`}
        ${value.enabled && !busy ? `<form class="platform-system-restart-form" data-restart-form><label class="platform-check"><input name="confirmed" type="checkbox" required /><span>仅重启当前版本的 SaaS 服务</span></label><button type="submit">重启服务</button></form>` : ""}
        <p data-update-feedback role="status" aria-live="polite"></p>
        <div class="platform-system-update-jobs">${(value.jobs ?? []).map((job) => `<p><strong>${escape(job.operation === "restart" ? "服务重启" : `升级 ${job.version}`)}</strong> · ${escape(({queued:"排队中",running:"处理中",completed:"已完成",failed:"失败，需恢复",rejected:"已拒绝"})[job.status] ?? job.status)} · ${escape(job.message ?? "")}</p>`).join("") || "<p>暂无升级任务记录。</p>"}</div>`;
      panel.querySelector("[data-update-refresh]").onclick = () => load(panel);
      const form = panel.querySelector("[data-update-form]");
      if (form) form.onsubmit = async (event) => {
        event.preventDefault();
        const button = form.querySelector("button");
        const feedback = panel.querySelector("[data-update-feedback]");
        button.disabled = true;
        try {
          await request({ method: "POST", headers: {"Content-Type":"application/json", "X-CSRF-Token":readPlatformCsrfCookie()}, body: JSON.stringify({ version: new FormData(form).get("version"), confirmed: true }) });
          feedback.textContent = "升级任务已提交。服务恢复后请刷新状态。";
          setTimeout(() => { if (panel.isConnected) load(panel); }, 4000);
        } catch (error) {
          feedback.textContent = error.message;
          feedback.setAttribute("role", "alert");
          button.disabled = false;
        }
      };
      const restartForm = panel.querySelector("[data-restart-form]");
      if (restartForm) restartForm.onsubmit = async (event) => {
        event.preventDefault();
        const button = restartForm.querySelector("button");
        const feedback = panel.querySelector("[data-update-feedback]");
        button.disabled = true;
        try {
          await request({ method: "POST", headers: {"Content-Type":"application/json", "X-CSRF-Token":readPlatformCsrfCookie()}, body: JSON.stringify({ confirmed: true }), url: `${origin}/platform/system-restart` });
          feedback.textContent = "重启任务已提交。服务恢复后会自动刷新状态。";
          setTimeout(() => { if (panel.isConnected) load(panel); }, 4000);
        } catch (error) {
          feedback.textContent = error.message;
          feedback.setAttribute("role", "alert");
          button.disabled = false;
        }
      };
    } catch (error) {
      if (!panel.isConnected) return;
      panel.innerHTML = `<div class="platform-section-heading"><h2>系统升级</h2><button type="button" data-update-refresh>重试读取状态</button></div><p role="alert">${escape(error.message)}</p>`;
      panel.querySelector("[data-update-refresh]").onclick = () => load(panel);
    } finally {
      if (panel.isConnected) panel.setAttribute("aria-busy", "false");
    }
  }
}

function escape(value) {
  return String(value ?? "").replace(/[&<>"']/gu, (character) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[character]);
}
