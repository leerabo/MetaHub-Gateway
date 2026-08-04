# PRD-003 测试说明

## 覆盖范围

主流程：

- 空库执行迁移成功，创建 MVP 表和 `schema_migrations`。
- 重复执行迁移时已执行版本被跳过，结果幂等。
- seed 创建默认租户、管理员、测试 Provider、模型和 Virtual Key。
- 重复执行 seed 不产生重复租户、用户、Provider、模型或 Virtual Key。
- 回滚最近迁移会删除 MVP 表，并记录迁移状态。

异常/安全：

- 缺少 `METAHUB_DATABASE_URL` 返回 `db.config_missing`。
- 已执行迁移文件 checksum 被修改时返回 `db.migration_checksum_changed`。
- Provider Key、Virtual Key、管理员密码不会明文落库。
- 所有业务表默认包含 `tenant_id`。

## 运行命令

```powershell
.\scripts\ci\check.ps1
```

当前验证结果：

- 后端 pytest：`18 passed`
- 控制台 Node test：`4 passed`
- 控制台 lint：通过

## 本地 CLI 验证

```powershell
$env:METAHUB_DATABASE_URL="sqlite:///./.data/metahub.db"
.\scripts\db\migrate.ps1
.\scripts\db\seed.ps1
```
