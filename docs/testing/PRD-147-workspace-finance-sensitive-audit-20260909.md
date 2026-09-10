# PRD-147 财务迁接敏感审计

Workspace/legacy 使用单凭据合同，Platform/Support Cookie 混入拒绝；不按 Owner 文案赋予平台权限。写请求校验 Origin/CSRF，业务事务再次检查当前账号、成员、组织和权限，读返回前退出旧快照后复核。

前端使用 authenticatedResourceFetch 与资源生命周期，不以空 token 相等判断相同组织。离页或切组织清除原因、金额、幂等键、游标和选中分录，忽略旧请求与 DOM 提交。

静态资产入口由生产 manifest 白名单控制；前后端摘要同步并构建验证，不重新开放废弃 route。真实 HTTP 已验证无权限及退出失效；夹具没有用户会话或凭据，不能替代真实浏览器登录流程验收。保留用户三个浏览器标签及会话，临时覆盖已恢复。

2026-09-10 最终增量审计见 `.artifacts/s2-prd147-index-audit-20260910.json`：精确文件允许列表、UTF-8、逐文件 SHA-256 与高置信凭据模式。生产 manifest 从实际 index 的资产图生成，不直接暂存混有其他页面改动的工作区摘要。浏览器测试合成数据入口仅在显式 finance 夹具启用，限制隔离数据库、loopback、临时凭据与本轮组织；不是生产接口。测试诊断不保存异常正文、Cookie、原始 SQL 或一次性 KEY。
