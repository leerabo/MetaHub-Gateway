# S1 组织入口试用发布

## 本次发布

产品提交 `58385b7` 修复创建组织后跳转被其他账号面板预加载阻塞。验收工具提交 `b9c85a9` 分离源码/生产构建来源，并补充静态分发边界和失败诊断。

发布保留现有 S2 资源，只替换与独立被测构建字节一致的 `src/features/identity/identityPageState.js`，其余 288 个资源与旧版本相同。没有用 S1-only 构建覆盖当前试用环境。

- 旧试用摘要：`02ca3886b2150ce1cca03f94d323a3bc6a567cd14c20d93593858820f677856d`。
- 新试用摘要：`ae330ca0d89d53c7c3f5ef0dec05c1e38a8600aad8fcb9aab662a25155e1ca78`。
- 2026-09-09 23:23（北京时间）HTTP 逐项验证 289 个已发布资源哈希通过：`.artifacts/g6-security-20260907/s1-entry-live-assets-20260909.json`。
- 控制服务恢复后 `/health/live`、`/health/ready` 和公开注册策略均为 200；匿名 WorkspaceSession 和平台策略接口为 401。记录：`.artifacts/g6-security-20260907/s1-entry-live-runtime-20260909.json`。
- 前端 5173、Gateway 8000、Status 8002 没有重启。Control 8001 由原 PID 22396 切换为 PID 21432，使用原启动脚本、配置及数据库，不改用户凭据。

## 可恢复性和边界

旧构建和两份旧元数据保存在 `.tmp/s1-entry-release-20260909`。未删除用户数据或测试失败证据。工作树中的组合构建摘要不混入 PRD-093 独立源码提交。

独立构建已有桌面和手机完整流程通过证据，但初次加载仍有不稳定失败。PostgreSQL 使用者验证、性能及跨浏览器兼容性尚未全部完成，不能将这次可用增量发布等同于整个 Excel 计划或 S1 总验收完成。
