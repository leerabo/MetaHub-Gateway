# PRD-100 测试记录

状态：实现、自动化门禁、真实浏览器验收和最终候选审计已完成。当前主机没有 Docker CLI，真实
Compose 容器联调未执行；PostgreSQL-native 历史迁移链也未交付，迁移入口按设计失败关闭。权威下一项为
`PRD-101 - Cloud Gateway Pool 与区域路由`。

## 已执行门禁

| 测试组 | 实际结果 | 覆盖 |
| --- | --- | --- |
| DB | migration `4 passed` | 0091->0092、fresh install、pristine down/up、非 pristine 回退阻断、PostgreSQL 连接前 fail-fast |
| API | infrastructure `1 passed` | PlatformSession/TOTP、权限负矩阵、状态安全投影、备份幂等/lease/CAS、失败与 ready 状态机 |
| Runtime | infrastructure + DB lifecycle `16 passed` | 有界 PostgreSQL pool、错误脱敏、SQLite 兼容 row/request scope、AES-GCM/KMS、对象 hash、隔离恢复 |
| Deploy | Compose contract `9 passed` | PostgreSQL/Redis/MinIO/worker 服务、MinIO init、健康检查、volume 与 disposable Redis 边界 |
| WEB | Console full suite `1013 passed` | 严格 DTO、CSRF/幂等、权限裁剪、转义、敏感 DOM、现有页面全量兼容 |
| Static | Console lint `323 files`、Ruff、compileall、pip check | PRD-100 Python/Console 候选与本地运行依赖 |

所有 pytest 命令均使用新的显式 `--basetemp`。迁移、API、Runtime 和 Deploy 四组共 `30 passed`。

## 数据库与迁移结论

- SQLite 0092 创建基础设施资源、迁移 lease、BackupSet、恢复验证和 outbox；fresh、升级、down/up、完整性与
  外键门禁通过。
- PostgreSQL 连接池对初始化失败、耗尽、执行/提交/读取错误只暴露固定错误；不回显 DSN、驱动异常或
  provider 文本。row facade 保留 SQLite 重名列和大小写不敏感访问合同。
- 仓库 `0001..0092` 仍包含大量 SQLite-native DDL。标准 migrate/rollback CLI 对 PostgreSQL URL 在建立
  连接前返回 `db.migration_backend_unsupported`，不把 SQLite SQL 交给 PostgreSQL 驱动。生产发布必须先
  交付覆盖完整历史的 PostgreSQL-native migration bundle。

## 基础设施与备份结论

- Redis 关闭持久化和 volume，只承载可丢失缓存/短租约；数据库仍是 Session、授权、幂等、账本和迁移真值。
- S3-compatible 对象只使用服务端生成 key；备份 envelope 与 manifest 使用 exact-key/type 校验，AES-GCM
  数据密钥只通过 KMS 包装。状态 API 和 Console 不返回 object key、bucket、KMS ID、DSN 或拓扑地址。
- ready 前执行隔离恢复、schema head、integrity/FK、manifest hash 和 tenant owner 抽样。生产恢复没有 HTTP
  或自动执行入口。
- Compose YAML 解析为 11 个服务；MinIO server/client 固定 tag 已通过 registry manifest 校验。当前主机
  缺少 Docker CLI，因此未声称真实容器启动、健康检查和故障注入已通过。

## 真实浏览器验收

- 直接使用本地测试 GlobalSession、PlatformSession 和 TOTP 完成登录，不在证据、日志或提交中记录凭据、
  TOTP、Cookie 或 Session 值。
- 390x844、1280x900、1440x900 均显示 Infrastructure 导航、4 类组件状态和 owner 备份表单；未发现页面级
  横向溢出、顶层区块重叠或控件文本裁切，Console error/warning 为 0。
- 三个视口的状态表与备份表单均存在；DOM 未出现 object key 或 KMS key ID。未配置 Redis/S3/KMS 以
  `not configured` 降级，不伪装为健康。
- 截图保存在 `.tmp/prd100-browser-evidence/`：
  - `infrastructure-390.png` `de7076b4c92b6c979ead3cf5d8cf1e9cd6e19df17d15b0a392959847ca3e3557`
  - `infrastructure-1280.png` `c42ddf795bb42410a6b377045f3d4b757893810a9d08ae0ec63136392a8d4b03`
  - `infrastructure-1440.png` `9b27932f70af8cc7a63d99fc9384890e6dce581b2ece4555f432ecec1762b555`

## 发布边界

- 当前 `compose-full` 可用于 PostgreSQL 连接池、Redis、MinIO 和 Backup Worker 适配器集成，但应用数据库仍
  固定使用 SQLite。依赖服务启动不代表 PostgreSQL 生产发布已放行。
- PostgreSQL-native full-chain migration、真实 Docker Compose 启动、TLS 依赖和故障注入必须在有 Docker
  与 PostgreSQL 验收环境中作为后续生产门禁完成。
