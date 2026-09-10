# PRD-147 用量页面迁移敏感审计

- 用量请求使用 authenticatedResourceFetch，Workspace Cookie 与旧凭据互斥。导出创建/下载显式 CSRF，AbortSignal 延续到轮询与最终下载。
- 页面离开、身份变化、空间切换清理资源代次；迟到导出不能触发另一个空间的下载。不把结果或凭据持久化到 Storage。
- 使用者标签、ID 和错误信息经现有 HTML/属性转义；未授权账单主体零 DOM，姓名/部门权限由服务端裁剪。
- 发布摘要与实际可达资产一致，不仅修改 manifest 来通过检查；重新构建 289 资产成功。
- 工作树与最终前端报告扫描见 `.artifacts/g6-security-20260907/workspace-usage-worktree-audit-20260909.json`。该扫描非整仓或最终暂存审计，真实浏览器敏感数据生命周期仍需全流程证据。
