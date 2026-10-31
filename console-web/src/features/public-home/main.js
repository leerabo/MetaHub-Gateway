import { icon } from "./icons.js";
import { legacyConsoleTarget } from "./entryRoute.js";
import {
  initializeLocale,
  localeToggleLabel,
  readPreferredLocale,
  writePreferredLocale,
} from "../../i18n/locale.js";

const COPY = {
  "zh-CN": {
    "home.skip": "跳到主要内容",
    "home.aria": "MetaHub 首页",
    "home.nav": "主导航",
    "home.capabilities": "产品能力",
    "home.status": "服务状态",
    "home.menu": "菜单",
    "home.close": "关闭",
    "home.login": "登录",
    "home.eyebrow": "企业 AI 网关",
    "home.title": "MetaHub",
    "home.description": "连接每一种模型，掌握每一次调用。",
    "home.register": "创建账号",
    "home.models": "模型 · 密钥 · 团队",
    "home.capability_intro": "产品能力",
    "home.heading": "自由接入，有序管理",
    "home.heading_description": "从模型到团队，一个入口。",
    "home.byok": "自带密钥 BYOK",
    "home.byok_description": "连接已有模型服务，密钥集中管理。",
    "home.keys": "统一 KEY",
    "home.keys_description": "一人一 Key，统一接口与模型权限。",
    "home.routing": "路由分发",
    "home.routing_description": "策略选路、权重调度与故障回退。",
    "home.dlp": "敏感数据防护",
    "home.dlp_description": "内容检测与脱敏，访问留痕可审计。",
    "home.finops": "精细化财务",
    "home.finops_description": "用量计费、部门归集与预算控制。",
    "home.edge": "边缘 Agent",
    "home.edge_description": "私有网关部署、健康检查与升级。",
    "home.identity": "团队与身份",
    "home.identity_description": "组织隔离、员工档案与企业 SSO。",
    "home.insights": "调用洞察",
    "home.insights_description": "日志、指标与告警，定位调用异常。",
    "home.footer": "AI 的统一入口",
  },
  "en-US": {
    "home.skip": "Skip to main content",
    "home.aria": "MetaHub home",
    "home.nav": "Primary navigation",
    "home.capabilities": "Capabilities",
    "home.status": "Service status",
    "home.menu": "Menu",
    "home.close": "Close",
    "home.login": "Sign in",
    "home.eyebrow": "Enterprise AI gateway",
    "home.title": "MetaHub",
    "home.description": "Connect every model. Control every call.",
    "home.register": "Create account",
    "home.models": "Models · Keys · Teams",
    "home.capability_intro": "Capabilities",
    "home.heading": "Connect freely. Manage clearly.",
    "home.heading_description": "One entry point from models to teams.",
    "home.byok": "Bring your own keys",
    "home.byok_description": "Connect existing model services and manage keys centrally.",
    "home.keys": "Unified keys",
    "home.keys_description": "One key per person with consistent API and model access.",
    "home.routing": "Smart routing",
    "home.routing_description": "Policy routing, weighted scheduling, and failover.",
    "home.dlp": "Sensitive data protection",
    "home.dlp_description": "Inspect and redact content with auditable access trails.",
    "home.finops": "Fine-grained FinOps",
    "home.finops_description": "Usage billing, cost attribution, and budget controls.",
    "home.edge": "Edge agents",
    "home.edge_description": "Private gateway deployment, health checks, and upgrades.",
    "home.identity": "Teams and identity",
    "home.identity_description": "Workspace isolation, employee profiles, and enterprise SSO.",
    "home.insights": "Call insights",
    "home.insights_description": "Logs, metrics, and alerts for faster diagnosis.",
    "home.footer": "One gateway for AI",
  },
};

function renderLocale(locale) {
  const normalized = locale === "en-US" ? "en-US" : "zh-CN";
  const copy = COPY[normalized];
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    const value = copy[element.dataset.i18n];
    if (value !== undefined) element.textContent = value;
  });
  document.querySelectorAll("[data-i18n-aria]").forEach((element) => {
    const value = copy[element.dataset.i18nAria];
    if (value !== undefined) element.setAttribute("aria-label", value);
  });
  const description = normalized === "en-US"
    ? "MetaHub enterprise AI gateway. Bring your own keys, connect models, and control every call."
    : "MetaHub 企业级 AI 网关。自带密钥、统一接入、策略路由，让安全、成本与每一次 AI 调用尽在掌控。";
  document.querySelector("[data-i18n-meta=description]")?.setAttribute("content", description);
  document.querySelectorAll("[data-locale-toggle]").forEach((element) => {
    element.textContent = localeToggleLabel(normalized);
    element.setAttribute("aria-label", normalized === "en-US" ? "切换到中文" : "Switch to English");
    element.title = element.getAttribute("aria-label");
  });
  document.title = normalized === "en-US" ? "MetaHub · One gateway for AI" : "MetaHub · AI 的统一入口";
}

const redirect = legacyConsoleTarget(window.location);
if (redirect) {
  window.location.replace(redirect);
} else {
  const initialLocale = initializeLocale();
  renderLocale(initialLocale);
  for (const element of document.querySelectorAll("[data-icon]")) element.innerHTML = icon(element.dataset.icon);
  document.querySelector("[data-locale-toggle]")?.addEventListener("click", () => {
    renderLocale(writePreferredLocale(readPreferredLocale() === "en-US" ? "zh-CN" : "en-US"));
  });
  const navToggle = document.querySelector("[data-home-nav-toggle]");
  const mobileNav = document.querySelector("[data-home-mobile-nav]");
  const mobileBreakpoint = window.matchMedia("(max-width: 600px)");
  const setMobileNavOpen = (open, { restoreFocus = false } = {}) => {
    if (!navToggle || !mobileNav) return;
    const next = Boolean(open);
    mobileNav.hidden = !next;
    navToggle.setAttribute("aria-expanded", String(next));
    navToggle.textContent = next ? COPY[readPreferredLocale()]["home.close"] : COPY[readPreferredLocale()]["home.menu"];
    if (next) mobileNav.querySelector("a")?.focus();
    else if (restoreFocus) navToggle.focus();
  };
  navToggle?.addEventListener("click", () => setMobileNavOpen(mobileNav?.hidden));
  mobileNav?.addEventListener("click", (event) => {
    if (event.target.closest("a")) setMobileNavOpen(false);
  });
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (mobileNav?.hidden || (target instanceof Element && target.closest(".home-header, [data-home-mobile-nav]"))) return;
    setMobileNavOpen(false, { restoreFocus: true });
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !mobileNav?.hidden) setMobileNavOpen(false, { restoreFocus: true });
  });
  mobileBreakpoint.addEventListener("change", (event) => {
    if (!event.matches) setMobileNavOpen(false);
  });
  window.addEventListener("hashchange", () => {
    setMobileNavOpen(false);
    const target = legacyConsoleTarget(window.location);
    if (target) window.location.replace(target);
  });
}
