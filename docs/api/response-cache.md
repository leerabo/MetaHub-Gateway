# Response Cache API

本文档描述 PRD-076 的精确 Response Cache 管理、调用日志、Webhook 与运维合同。该能力只服务公开
OpenAI-compatible 非流式 Chat 的精确请求缓存；不实现语义缓存、向量相似度、流式缓存、Batch、
Enterprise Chat 或实验流量缓存。全局开关和每个 Model Alias 的策略均默认关闭。

## 1. 通用安全合同

- Control API 使用 `Authorization: Bearer <session-token>`，`tenant_id` 和 actor 只从后端 Session 恢复。
  Cookie、请求正文或自定义租户 Header 不能覆盖 owner。
- `GET /cache-policies` 要求 `response_cache.read`；策略写入还要求 `response_cache.write`；主动失效还要求
  `response_cache.invalidate`。Console 导航和按钮可见性不构成授权。
- 所有成功和错误响应使用统一 `request_id`/`trace_id`，响应 Header 与 JSON body 必须一致，并带
  `Cache-Control: no-store, private`。
- 写接口要求单个 `Idempotency-Key`，长度为 16..128 个可见 ASCII 字符。同一 actor、相同 key 和相同
  intent 可以重放；相同 key 对应不同 intent 返回 409。
- 管理 API 不返回缓存正文、Cache Key、Prompt hash、route/credential digest、密文、source request、
  PriceSnapshot、Usage/FinOps owner、Provider Key 或 Token。
- GET 只允许文档列出的 query 且禁止 body；写请求必须是单个 UTF-8 `application/json` object，拒绝重复
  JSON key、未知字段、浮点数、非法布尔值、超深/超大正文和重复安全 Header。认证和权限校验先于正文解析。

## 2. 查询策略

```http
GET /cache-policies?limit=50&cursor=<opaque>
Authorization: Bearer <session-token>
X-Request-ID: <request-id>
```

`limit` 默认为 50，范围 1..100。cursor 是绑定 tenant、Alias 快照上界、排序和 limit 的私有 HMAC 值。
响应按 `(environment_code, alias, model_alias_id)` 稳定排序，包含当前 tenant 的 active Alias；尚未配置的
Alias 返回 `cache_policy: null`。

```json
{
  "tenant_id": "tnt_example",
  "items": [
    {
      "tenant_id": "tnt_example",
      "model_alias_id": "mla_example",
      "alias": "chat-default",
      "environment_id": "env_example",
      "environment_code": "prod",
      "cache_policy": {
        "cache_policy_id": "rcp_example",
        "tenant_id": "tnt_example",
        "model_alias_id": "mla_example",
        "status": "enabled",
        "data_class": "internal",
        "single_data_class_confirmed": true,
        "ttl_seconds": 900,
        "charge_cache_hits": false,
        "tags": ["finance"],
        "version": 3,
        "ready_entry_count": 12,
        "updated_at": "2026-08-10T00:00:00Z"
      }
    }
  ],
  "limit": 50,
  "next_cursor": null,
  "has_more": false,
  "global_enabled": true,
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

envelope、item 和 policy 的 `tenant_id` 必须相同，policy 的 `model_alias_id` 必须等于父 item。
`ready_entry_count` 只返回当前 tenant/policy 的聚合计数，不暴露 entry 标识。

## 3. 创建或更新策略

```http
PATCH /cache-policies
Authorization: Bearer <session-token>
Idempotency-Key: <stable-intent-key>
Content-Type: application/json
X-Request-ID: <request-id>
```

请求是完整 CAS 快照；新建使用 `expected_version=0`，已有策略必须提交当前版本。

```json
{
  "model_alias_id": "mla_example",
  "status": "enabled",
  "data_class": "internal",
  "single_data_class_confirmed": true,
  "ttl_seconds": 900,
  "charge_cache_hits": false,
  "tags": ["finance"],
  "expected_version": 2
}
```

- `ttl_seconds` 范围为 60..86400。
- `data_class` 和 tag 是最多 64 字符的小写 machine code；tag 最多 16 个且不得重复。
- enabled 策略必须提供可信的单一 `data_class`，并显式提交 `single_data_class_confirmed=true`。
  disabled 策略必须提交 false；重新启用需要再次确认。
- enabled 写入要求全局开关和四个独立 Secret 域配置完整。配置不可用时返回 503，不写入策略。
- 策略真实变化会推进 generation、删除旧 entry、围栏在途 fill，并在同一事务形成 Audit 与 source outbox。

成功返回 200：

```json
{
  "tenant_id": "tnt_example",
  "cache_policy": {
    "cache_policy_id": "rcp_example",
    "tenant_id": "tnt_example",
    "model_alias_id": "mla_example",
    "status": "enabled",
    "data_class": "internal",
    "single_data_class_confirmed": true,
    "ttl_seconds": 900,
    "charge_cache_hits": false,
    "tags": ["finance"],
    "version": 3,
    "ready_entry_count": 0,
    "updated_at": "2026-08-10T00:00:00Z"
  },
  "changed": true,
  "idempotent_replay": false,
  "invalidated_entry_count": 12,
  "fenced_fill_count": 1,
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

幂等重放使用本次 request/trace，但复用首次提交的 policy/version/counts，并返回
`idempotent_replay=true`；不会按当前数据库状态重算历史结果。

## 4. 主动失效

```http
POST /cache/invalidate
Authorization: Bearer <session-token>
Idempotency-Key: <stable-intent-key>
Content-Type: application/json
X-Request-ID: <request-id>
```

按 tag 的 all-match 失效：

```json
{
  "scope": "tags",
  "match_mode": "all",
  "tags": ["tag:finance"],
  "reason_code": "manual_finops"
}
```

全 tenant 失效使用 `scope="all"`、`match_mode="all"`、`tags=[]`。tag scope 要求 1..16 个 canonical
selector；允许的 namespace 为 `policy/model_alias/model/virtual_key/data_class/tag`，资源 owner 由后端按
当前 tenant 校验。reason 只允许 `manual_finops/manual_security/manual_configuration/manual_other`。

```json
{
  "tenant_id": "tnt_example",
  "invalidation": {
    "cache_invalidation_id": "rci_example",
    "tenant_id": "tnt_example",
    "scope": "tags",
    "match_mode": "all",
    "tag_count": 1,
    "matched_entry_count": 4,
    "deleted_entry_count": 4,
    "fenced_fill_count": 1,
    "reason_code": "manual_finops",
    "created_at": "2026-08-10T00:00:00Z"
  },
  "idempotent_replay": false,
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

0 match 仍返回 200，并形成不可变 invalidation、Audit、event/outbox 和 generation fence。响应只返回 tag
数量与聚合计数，不返回 selector 或 tag value。

## 5. Gateway 与财务语义

- Cache Key 身份至少绑定 tenant、Virtual Key、可信 data class、Alias/实际 model、完整请求 HMAC、
  route/credential revision 和独立 key generation。任一 owner 漂移均为 miss。
- lookup 位于 Policy、DLP、Intent、Session Risk 和 Rate Limit 之后；hit 仍执行当前 Output Safety、预算和
  原子结算。active PRD-075 实验 scope、stream、direct model、Policy `route_to`、Batch 和内部调用均旁路。
- miss 只有 initial route 成功、Output Safety 放行且 RequestLog、Usage 与 FinOps 已提交时才加密填充；
  fallback、Provider 失败、spooled Usage、取消或超大响应不填充。
- hit 在一个事务内提交 RequestLog、逻辑 Usage、PriceSnapshot、FinOps、预算、hit 与 event/outbox。
  `charge_cache_hits=false` 仍产生可追踪的零收费 Usage/FinOps；true 使用当前冻结参考价。
- `saved_cost_cny = current_reference_cost_cny - billed_cost_cny`。历史 source generation 成本单独展示，
  不得冒充 saved；缺少可信价格时状态为 `unavailable` 且金额为 null。
- cache 依赖故障在尚未交付正文前安全旁路 Provider；只有已选定 hit 但原子结算无法确定时返回
  `gateway.response_cache_settlement_unavailable`，并由 HMAC failure spool 恢复 exactly-once 终态。

## 6. RequestLog 投影

既有 `GET /request-logs/{request_id}` 增加可空 `response_cache`。历史或不适用请求为 null；hit 示例：

```json
{
  "tenant_id": "tnt_example",
  "status": "hit",
  "hit": true,
  "charge_cache_hits": false,
  "cache_policy_id": "rcp_example",
  "cache_policy_version": 3,
  "source_generation_cost_status": "known",
  "source_generation_cost_cny": "0.010000",
  "current_reference_cost_status": "known",
  "current_reference_cost_cny": "0.012300",
  "saved_cost_status": "known",
  "saved_cost_cny": "0.012300",
  "billed_cost_status": "known",
  "billed_cost_cny": "0.000000"
}
```

金额只能是非负六位小数字符串或 null。hit 的四组状态为 `known|unavailable`；非 hit 的四组状态全部为
`not_applicable` 且金额为 null。status 为
`disabled/ineligible/bypassed_experiment/unavailable_bypass/corrupt_miss/miss_owner/miss_non_owner/`
`fill_stored/fill_skipped/hit/hit_blocked/settlement_failed` 之一。投影不返回正文、tag、data class、内部
digest 或账务 owner。

## 7. Webhook

`cache.hit` 的 payload exact 字段为 schema/version、hit/policy owner、charge flag 与四组成本证据；
event ID 使用持久 `rcev_`，actor 为 null。`cache.invalidated` 只包含 invalidation owner、scope、match mode、
tag count、matched/deleted/fenced counts 与 reason code。

系统安全淘汰只使用 `system_corrupt/system_output_blocked`，Policy 变更使用 `policy_changed`。普通 TTL 和容量
淘汰不发送 `cache.invalidated`。两种事件都不包含 Prompt、Response、Cache Key、Prompt hash、selector、
tag value、data class、Virtual Key、Provider Key、Token、Endpoint、nonce/ciphertext 或 source request ID。

## 8. 统一错误码

| HTTP | code | 含义 |
| ---: | --- | --- |
| 401 | `auth.token_missing/auth.token_invalid/auth.token_expired/auth.session_revoked` | Bearer Session 无效 |
| 403 | `auth.permission_denied` | 后端实时权限不足 |
| 404 | `response_cache.policy_not_found` | 当前 tenant 不存在目标 policy/Alias owner |
| 409 | `response_cache.version_conflict` | CAS 版本冲突 |
| 409 | `response_cache.request_conflict` | Idempotency-Key 对应不同 intent |
| 422 | `response_cache.input_invalid` | body、Header、media type 或字段非法 |
| 422 | `response_cache.query_invalid` | GET query 非法 |
| 503 | `response_cache.configuration_unavailable` | feature 或独立 Secret/key version 不可用 |
| 503 | `response_cache.unavailable` | Control 存储、owner 或 outbox 不可用 |
| 503 | `gateway.response_cache_settlement_unavailable` | hit 原子结算暂时不可用 |

错误使用全局统一 envelope，并携带与响应 Header 一致的 `request_id`。错误消息不拼接输入值、Header、
selector、金额、数据库异常或任何敏感材料。

## 9. 配置与停服维护

`METAHUB_RESPONSE_CACHE_ENABLED=false` 是默认值。生产启用前必须分别生成 HMAC、加密、Control 幂等和
failure spool 四个独立 Secret，并配置各自正整数 version；不得复用 Auth、Provider、Session Risk、
Cache Affinity 或其他业务 Secret。Compose 和 `.env.example` 列出容量、明文上限、fill lease 与 spool
轮询边界，示例值不能用于生产。

HMAC/encryption Secret 不支持在线替换。轮换步骤为：停掉所有 Gateway/Control writer、关闭全局开关、
证明 failure spool 的 pending/processing/quarantine 全为 0，然后运行：

```powershell
.\.venv\Scripts\python.exe scripts\maintenance\response-cache-key-rotate.py --confirm-stopped
```

runner 使用 `BEGIN EXCLUSIVE`、0069/schema trigger hash、key-state CAS、Policy/generation fence、派生数据
清理和数据库完整性检查。Secret 只来自环境，不接受命令行参数，也不会进入输出。

0069 down 只能在相同停服与 spool 门禁下通过受信入口执行：

```powershell
.\.venv\Scripts\python.exe scripts\maintenance\response-cache-down.py --confirm-stopped
```

存在任何 PRD-076 业务事实、非默认 key-state 或外部 spool 记录时，down 会在 DDL 前失败关闭。生产运维禁止
直接调用原始 down SQL 绕过双层门禁。
