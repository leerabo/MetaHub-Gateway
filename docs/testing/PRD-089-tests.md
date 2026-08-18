# PRD-089 租户数据导出与注销测试记录

状态：已完成 PRD-089；没有启动 PRD-090。总体状态仍为 `all_108_prds_complete=false`。

## 设计与自审

- 设计落盘于 [PRD-089-tenant-data-export-deletion.md](../prd/PRD-089-tenant-data-export-deletion.md)。
- 第一轮自审覆盖租户隔离、幂等、Worker lease/generation/CAS、注销 quiesce、SQLite 排他事务、
  FK 环、manifest/trigger 完整性和 webhook terminal 条件。
- 第二轮自审覆盖 AES-GCM 独立密钥域、导出安全投影、日志/审计脱敏、统一错误码、
  `clear-before-abort` 前端生命周期和无横向溢出页面边界。

## 自动化专项

| 范围 | 结果 | 覆盖 |
| --- | --- | --- |
| `control-plane-api/tests/integration/test_prd089_tenant_data.py` | 2 passed | 严格 body/query、幂等重放、RBAC/tenant owner、加密 ZIP、下载 headers、确认短语、注销 Worker |
| `control-plane-api/tests/integration/test_prd089_db_migrations.py` | 2 passed | 0082 fresh-up、seed、RBAC 102/259、8 张生命周期表、guarded down/up、FK/integrity |
| `control-plane-api/tests/unit/test_prd089_console_navigation.py` | 2 passed | 后端 `tenant_data.read` 导航授权和运行时撤权 |
| `console-web/tests/unit/{tenantDataLifecycle,prd089BrowserAcceptance,prd089BrowserAcceptanceDriver}.test.mjs` | 6 passed | exact DTO、下载完整性、owner/generation、独立 lanes、撤权清理、无横向溢出合同 |
| `scripts/acceptance/prd089-browser-acceptance.mjs` | passed | 真实 Console bundle；桌面 `1280x720`、移动 `390x844`；Blob URL 创建/回收各 1 次、撤权清理、跨租户隔离、注销失败 Dialog |
| Console lint | `296 files` passed | 受影响 Console bundle 和共享脚本 |
| Python compile/import | passed | `tenant_data` service/routes/scheduler/models、shared crypto、Control app 路由注册 |
| 空 SQLite 实际迁移与 seed | passed | `0001..0082`、`latest_migration=0082`、`integrity_check=ok`、外键违规 `0`、seed tenant/admin 各 1 |
| Migration 0082 guarded down/up | passed | 无业务事实时 down、完整性/FK、再应用 0082；已有导出/注销/留存事实拒绝回退 |

专项测试使用仓库内 `.tmp/pytest-prd089-*` 作为临时目录，避免 Windows 全局临时目录权限差异；
没有把临时 SQLite、ZIP 或认证材料作为源码或验收证据提交。

## 实际运行验收

1. 空 SQLite 实际应用全部迁移并执行 seed；admin 创建导出任务，Worker 生成 owner-bound AES-GCM
   ZIP，状态查询返回固定 DTO，下载校验 MIME、`nosniff`、hash/size 和 `no-store, private`。
2. ZIP 仅含 `manifest.json`、`tenant.json`、Usage/Cost 汇总、失败汇总和审计汇总；Provider Key、
   Token、Prompt、Response、凭据密文与原始请求/响应均未进入制品、API、日志或事件。
3. 注销请求必须提交当前 tenant slug 和匹配 revision；Worker 先 quiesce，再在 `BEGIN EXCLUSIVE`
   中校验 manifest、临时移除并恢复目标触发器、归档受限计数、删除租户行、推进 cache generation，
   保留 tombstone、DeletionRequest、RetentionArchive、LifecycleEvent/Outbox。验收结果为 FK 违规 `0`、
   `integrity_check=ok`。
4. API 路径不接受 `tenant_id` body/query/path 选择器；跨租户或未知资源统一 not-found 语义，所有
   API 错误带 `request_id`/`trace_id`。
5. 独立端口真实 Control API 健康检查通过：`GET /health=200`、`GET /health/ready=200`；SQLite
   database check 为 `ok`，Redis/Provider 未配置时按可选依赖 skipped，不影响 ready。

## 安全审计

- 变更文件和测试证据已扫描禁止敏感文件：没有提交 `.env`、Provider Key、Token、Prompt、Response、
  SQLite 运行库、ZIP 制品或浏览器个人数据。
- `all_108_prds_complete=false` 在 README、设计、测试记录和运行验收中保持一致。
- Excel、README、`docs/prd` 和 `docs/testing` 交叉确认下一条未完成项为 PRD-090；PRD-090 未编码、
  未测试、未迁移、未登记为完成。
