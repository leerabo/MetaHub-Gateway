# PRD-100 PostgreSQL、Redis、对象存储与 KMS 生产基础设施

状态：实现完成，自动化测试通过；真实 Docker Compose 待可用环境验收
阶段：S3-生产基础设施
优先级：P0
依赖：PRD-090、PRD-099

## 1. 目标与边界

本条把当前 SQLite-only 运行时扩展为 PostgreSQL 16 生产适配器，Redis 7 只承载可丢失缓存/分布式租约，
S3-compatible 对象存储承载导出与备份对象，KMS 信封加密承载数据密钥。SQLite 保留为本地开发和测试后端，
不能在生产配置中静默回退。Redis 不成为账本、Session、幂等、权限或迁移真值；对象存储不公开 bucket；
KMS 不在应用日志、状态 API 或备份元数据中暴露明文密钥。

## 2. 适配器与配置

`DatabaseBackend` 冻结 `sqlite/postgresql` 方言能力；repository 继续使用参数化查询和显式事务 API，不向
业务层泄露驱动 connection。PostgreSQL 使用连接池、UTC、statement/lock timeout、TLS 和独立 migration
账号。所有 SQLite 专属 SQL 必须经过 capability 检查或方言实现；禁止字符串替换 SQL。

Redis key 必须含 deployment+tenant/workspace generation，值带 schema/version/TTL；断连时授权、账本、
配额和幂等失败关闭或回源数据库，非关键展示缓存可降级。ObjectStore 仅接收服务端生成 object key 和
content hash；KMS provider 只返回 key id/version 与密文信封。

## 3. 0092 数据模型

- `infrastructure_resources`：DatabaseCluster/CacheCluster/ObjectStore/KeyRing 的非敏感期望与探测摘要。
- `migration_leases`：backend、owner、token、generation、expires_at；PostgreSQL advisory lock 与表租约双证。
- `backup_sets`：类型、watermark、object key、size/hash、KMS key version、状态、保留期；无下载凭据。
- `backup_restore_verifications`：隔离恢复校验结果、schema head、integrity/hash 和安全错误码。

0092 是 SQLite 基线迁移；每个迁移必须有 SQLite/PostgreSQL 等价 DDL 或显式 unsupported fail-fast。当前
历史迁移链尚未提供 PostgreSQL-native DDL，因此标准 migrate/rollback CLI 在建立 PostgreSQL 连接前返回
`db.migration_backend_unsupported`，不得把 SQLite SQL 交给 PostgreSQL 驱动或误报迁移成功。迁移只允许
单 owner，checksum 与顺序不一致立即停止启动。

## 4. 备份与恢复

`POST /platform/backups` 要求 platform_ops、Origin/CSRF、Idempotency-Key、固定 scope 和确认。数据库
snapshot、对象 manifest 和 KMS key version 在一个 BackupSet 状态机中关联：
`requested -> snapshotting -> uploading -> verifying -> ready|failed`。网络/KMS 操作均在事务外 worker 执行，
但每步由 lease/CAS 固定；重放不产生第二份成功事实。

ready 前必须在隔离目标执行真实恢复、迁移 head、FK/integrity、manifest hash 和 tenant owner 抽样。恢复到
生产属于不可逆高风险操作，不在本条 HTTP API 自动执行。

## 5. HTTP 与可观测

| 方法与路径 | 主体 | 返回边界 |
| --- | --- | --- |
| `GET /platform/infrastructure/status` | platform_ops/security | 组件枚举、版本、健康、延迟、最近验证时间 |
| `POST /platform/backups` | platform_ops | 创建幂等 BackupSet |
| `GET /platform/backups/{backup_id}` | platform_ops/security | 状态、hash、watermark；无 object URL/Secret |

状态 API 不回显 DSN、host 用户名、Redis key、bucket、KMS ARN 全文、证书或异常堆栈。健康探测有超时和
并发上限。指标按 backend/operation/status 聚合，不把 SQL、参数或对象 key 作为 label。

## 6. 性能与发布

PostgreSQL 关键 API 性能不得比 SQLite 基准回退超过 20%，连接池耗尽必须有界 503；Redis 缓存命中不改变
授权结果。PostgreSQL 发布必须先交付并评审独立 native migration bundle，再执行 shadow
migration/checksum、单 writer 和滚动应用；native bundle 未交付前生产发布失败关闭。N/N-1 代码必须识别
schema capability。compose 的 PostgreSQL/Redis/MinIO profile 用于本地适配器集成，不把示例密码当生产默认。

## 7. 专项测试

覆盖 SQLite fresh/up/down、PostgreSQL migration fail-fast、并发 migration lease、连接池泄漏、statement
timeout、Redis 丢失/污染/过期、对象 hash、KMS key rotation、备份重复/失败/隔离恢复、平台凭据负矩阵、
配置 Secret 脱敏、真实 compose 集成和故障注入。PostgreSQL fresh/up/down 由后续 native migration bundle
作为生产放行门禁补齐。

## 8. 第一轮自审：真值与故障域

结论：通过，P0/P1 已修正。

- P0：把 Redis 当 Session/幂等真值会在清缓存后放宽授权或重复收费。修正为数据库真值，Redis 只缓存。
- P0：生产数据库失败后回退 SQLite 会形成分叉账本。修正为 prod fail-fast，SQLite 仅显式开发模式。
- P0：自动生产 restore 可能覆盖现网数据。修正为本条只创建/验证备份，不暴露生产恢复命令。
- P1：应用代码拼接方言 SQL 不可审计。修正为结构化 backend adapter 与迁移双实现。

## 9. 第二轮自审：密钥、备份与观测

结论：通过，P0/P1 已修正。

- P0：状态接口返回 DSN/bucket/KMS 标识会泄露基础设施拓扑。修正为枚举化安全投影。
- P0：备份只校验上传成功不能证明可恢复。修正为隔离恢复、schema/integrity/hash 联合门禁。
- P1：KMS 失败时用本地固定 key 降级会破坏密钥域。修正为加密写失败关闭，读按记录 key version 解封。
- P1：迁移只靠 advisory lock 在断连后语义不清。修正为 advisory lock 与持久 generation lease 双证。
