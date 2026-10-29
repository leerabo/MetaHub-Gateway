/** Shared PRD-126 visual vocabulary. This module contains no session or request state. */

const HEALTH = Object.freeze({
  healthy: ["健康", "healthy"],
  degraded: ["需关注", "warning"],
  critical: ["严重", "critical"],
  offline: ["离线", "offline"],
  revoked: ["已吊销", "revoked"],
  unknown: ["未知", "unknown"],
});

const SIGNALS = Object.freeze({
  healthy: "正常", current: "当前", converged: "已收敛", passed: "通过",
  warning: "预警", expiring: "即将到期", outdated: "版本落后", lagging: "延迟",
  critical: "严重", expired: "已到期", drifted: "存在漂移", stale: "陈旧",
  failed: "失败", offline: "离线", revoked: "已吊销", missing: "暂无数据",
  unknown: "未知", not_published: "未发布", pending: "进行中",
});

const COMMANDS = Object.freeze({
  "runtime.version": "运行时版本",
  "runtime.watermarks": "同步水位",
  "runtime.disk_budget": "磁盘预算",
  "runtime.connectivity_summary": "连接摘要",
});

const DIAGNOSTIC_STATUS = Object.freeze({
  pending_customer: "等待客户批准",
  active: "已批准",
  authorized: "等待 Agent",
  leased: "Agent 已领取",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  expired: "已过期",
  exhausted: "次数已用完",
  revoked: "已撤销",
  timed_out: "已超时",
});

export function edgeHealthLabel(code) { return HEALTH[code]?.[0] ?? "未知"; }
export function edgeHealthTone(code) { return HEALTH[code]?.[1] ?? "unknown"; }
export function edgeSignalLabel(code) { return SIGNALS[code] ?? "未知"; }
export function edgeCommandLabel(code) { return COMMANDS[code] ?? "未知诊断"; }
export function edgeDiagnosticStatusLabel(code) { return DIAGNOSTIC_STATUS[code] ?? "未知"; }

export function renderEdgeHealthScore(score, healthCode, { compact = false } = {}) {
  const value = Number.isSafeInteger(score) ? Math.max(0, Math.min(100, score)) : 0;
  const tone = edgeHealthTone(healthCode);
  return `<div class="edge-health-score tone-${tone}${compact ? " compact" : ""}" role="img" aria-label="健康分 ${value}，${edgeHealthLabel(healthCode)}">
    <span style="--edge-health-score:${value}"><strong>${value}</strong><small>/ 100</small></span>
    <em>${edgeHealthLabel(healthCode)}</em>
  </div>`;
}

export function renderEdgeSignalStrip(health) {
  const items = [
    ["证书", health?.certificate_signal],
    ["版本", health?.version_signal],
    ["容量", health?.capacity_signal],
    ["配置", health?.config_signal],
    ["遥测", health?.telemetry_signal],
    ["验证", health?.validation_signal],
  ];
  return `<dl class="edge-health-signals">${items.map(([label, value]) => {
    const tone = edgeHealthTone(value === "healthy" || value === "current"
      || value === "converged" || value === "passed" ? "healthy"
      : value === "warning" || value === "expiring" || value === "outdated"
        || value === "lagging" || value === "pending" ? "degraded"
        : value === "critical" || value === "expired" || value === "drifted"
          || value === "stale" || value === "failed" ? "critical" : "unknown");
    return `<div class="tone-${tone}"><dt>${label}</dt><dd>${edgeSignalLabel(value)}</dd></div>`;
  }).join("")}</dl>`;
}
