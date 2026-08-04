# PRD-025 测试记录 - 基础事件订阅与推送

## 覆盖范围

- 控制面 Webhook API：`GET /webhooks`、`POST /webhooks`、`POST /webhooks/{id}/test`。
- Webhook 安全：一次性 `signing_secret`、AES-GCM 密文落库、secret prefix/fingerprint 展示、HMAC-SHA256 签名和验签。
- 发送记录：`webhook_deliveries` 保存最终状态，`webhook_delivery_attempts` 保存最多 3 次尝试记录。
- 权限与租户边界：admin/developer 可管理，viewer 后端拒绝，请求体中的 `tenant_id` 不参与写入。
- 控制台：Webhook 设置页、创建表单、一次性 secret 展示、测试按钮、发送记录、HTML 转义和 viewer 隐藏。
- 迁移：`0017_webhooks.sql` 和 down 迁移。
- 注释：全仓 `.py/.js/.mjs/.sql/.css` 中文注释门禁。

## 已执行命令

```powershell
python -m compileall shared-sdk\python\metahub_shared\runtime shared-sdk\python\metahub_shared\db control-plane-api\src\metahub_control gateway-data-plane\src\metahub_gateway
python scripts\ci\check-comments.py
python -m pytest control-plane-api\tests\integration\test_webhooks.py control-plane-api\tests\integration\test_db_migrations.py -q
node --test console-web\tests\unit\webhooks.test.mjs console-web\tests\unit\dlp.test.mjs
node --test console-web\tests\unit\*.mjs
node scripts\lint.mjs  # 在 console-web 目录执行
python -m pytest -q --basetemp D:\MetaHub\.tmp\pytest -p no:cacheprovider
```

## 当前结果

- Python 编译检查：通过。
- 中文注释门禁：通过。
- 后端 PRD-025 聚焦测试：通过，覆盖本地 HTTP 接收端 HMAC 验签、viewer 权限拒绝、非法 URL/事件类型、缺失 Endpoint、超时重试 3 次和迁移回滚。
- 前端 PRD-025 聚焦测试：通过，覆盖 API client、统一错误映射、一次性 secret 展示、发送记录渲染、viewer 隐藏和导航 active content。
- 后端全量 pytest：通过。当前 sandbox 下需把 pytest 临时目录放到 `D:\MetaHub\.tmp`，避免默认用户 Temp 和 `.pytest_cache` 权限拒绝。
- 前端全量 Node test：通过，85 个测试全部通过。
- 控制台 lint：通过，`console lint ok (62 files)`。