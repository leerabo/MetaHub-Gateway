# Platform Infrastructure and Backup API

PRD-100 提供生产数据库、可丢失 Redis、S3-compatible 对象存储、KMS 信封加密和可验证备份的
Platform 运维边界。数据库、对象和密钥标识不通过 API 返回。

## 凭据与权限

- 所有接口只解释 active PlatformSession；Bearer、WorkspaceSession、SupportSession 或混合凭据在业务查询前
  以 `platform.credential_conflict` 失败。
- `platform_ops` 可读取状态和创建备份；`platform_security` 只读取状态、备份结果与密钥安全投影。
- 写请求必须来自唯一允许 Origin，并携带 Platform CSRF Cookie 对应的 `X-CSRF-Token`。
- 响应固定 `Cache-Control: no-store, private`，不返回 DSN、主机名、bucket、object key/URL、KMS key ID、
  证书、Cookie、Token 或 provider 异常。

## 接口

| 方法 | 路径 | 请求边界 | 结果 |
| --- | --- | --- | --- |
| GET | `/platform/infrastructure/status` | `platform.infrastructure.read`；无 query | 四类组件的枚举化健康投影与 RPO/RTO |
| POST | `/platform/backups` | `platform.backup.create`、CSRF、严格 JSON、幂等键 | 创建或重放同一 BackupSet |
| GET | `/platform/backups/{backup_id}` | `platform.infrastructure.read`；规范 Backup ID | 备份与隔离恢复校验状态 |

状态组件固定为 `database_cluster/cache_cluster/object_store/key_ring`。后端只返回
`sqlite_local/postgresql/redis/s3_compatible/kms` 枚举、受限版本摘要、延迟、验证时间和安全错误码。

## 创建备份

`Idempotency-Key` 长度为 16..128，首字符为字母或数字，其余只允许字母、数字、`.`、`_`、`:`、`-`。
请求正文固定为：

```json
{
  "backup_type": "full",
  "scope_kind": "platform",
  "workspace_id": null,
  "tenant_id": null,
  "retention_days": 30,
  "confirmed": true
}
```

状态机为 `requested -> snapshotting -> uploading -> verifying -> ready|failed`。worker 使用哈希 lease token 和
CAS 推进；网络、对象存储、KMS 和恢复均在数据库事务外执行。过期 lease 只进入固定失败状态，不重放未知
副作用。

完整备份使用数据库在线 snapshot，生成 canonical manifest，再以随机 DEK 和 AES-GCM 加密；DEK 只由 AWS
KMS 或显式 dev/test provider 包装。对象 key 由服务端生成且只保存在数据库，不进入 HTTP DTO。

`ready` 前必须下载对象、校验 manifest/content hash、解封 DEK，并在隔离目标执行真实恢复、schema head、
integrity/FK 和 Workspace/Tenant owner 抽样。该 API 不提供生产恢复动作。

## 错误

- `infrastructure.request_invalid/idempotency_conflict/backup_not_found/backup_conflict/`
  `pool_exhausted/unavailable`：输入、重放、状态或基础设施失败。
- `backup.*`：worker 的固定 snapshot、对象存储、KMS、hash、恢复或 lease 错误坐标。
- `platform.*`：凭据域、Origin、CSRF、Session 或权限失败。

错误响应和 durable event 只包含固定 code 与关联 ID，不保存 SQL、DSN、provider 文本、object key 或密钥。
