# 企业 Prompt 模板 API

本文档冻结 PRD-067 PAGE-094 模板管理与 PAGE-093 模板调用合同。模板能力建立在 PRD-066
企业 Chat 完整治理链之上，不提供绕过 Policy、DLP、预算、路由、Fallback、用量和成本记录的
Provider 直连入口，也不扩展为复杂 Prompt IDE。

## 1. 认证、权限和敏感信息边界

- `prompt_template.read`：读取模板安全列表、详情、不可变版本历史和授权解密定义。
- `prompt_template.write`：创建模板、发布新版本、修改名称/描述以及启用或禁用。
- `prompt_template.use`：仅允许在企业 Chat 获取最小调用投影并使用当前版本；不授予 PAGE-094、
  历史版本、原始定义或候选管理目录读取能力。
- 所有权限由后端按当前 Session、当前 tenant 和数据库实时矩阵执行。角色名、前端导航和按钮状态
  不能补权；权限依赖先于 query、path 资源解析和 JSON 正文读取。
- 请求不能提交 `tenant_id`、actor、角色、Provider、Provider Key、Virtual Key、Endpoint、密文、
  system message 或审计身份。跨租户资源与不存在资源返回同形错误。
- Prompt 正文、变量 schema/值和变更说明只允许存在于授权的 `no-store` 响应或当前调用栈；不得进入
  普通日志、AuditLog 摘要、Webhook、RequestLog、URL、Storage、Console 或测试证据。
- 所有响应使用统一错误合同并携带 `request_id`、`trace_id`；模板域成功响应均设置
  `Cache-Control: no-store`。
- 模板写请求 wire 上限为 1 MiB；若携带 `Content-Length`，必须是唯一、无符号、无前导零的
  ASCII 十进制并与实际流长度完全一致。重复/非规范声明、断流、非 bytes 流块和声明/实际不一致
  均折叠为 `prompt_template.request_invalid`，不得穿透底层传输详情。

## 2. 定义合同

模板定义由以下字段组成：

```json
{
  "content": "请为 ${customer_name} 生成合规回复。",
  "variables": [
    {
      "name": "customer_name",
      "label": "客户名称",
      "required": true,
      "max_chars": 80
    }
  ],
  "project_scope": "selected",
  "project_ids": ["prj_example"],
  "recommended_model_alias_ids": ["mal_balanced", "mal_quality"],
  "change_summary": "初始化客户回复模板"
}
```

- 占位符只允许完整 `${lower_snake_case}`；不接受 `$name`、`$$`、表达式、属性访问、函数、
  `__proto__` 等保留键或自动转义求值。
- 占位符集合和变量声明必须 exact-set。变量最多 32 个；单值和渲染后正文均有字符及 UTF-8
  字节硬上限。
- `project_scope=all` 时 `project_ids` 必须为空；`selected` 时至少一个项目。项目和推荐模型别名
  必须属于当前 tenant 且处于可绑定状态。
- 推荐模型仅影响 UI 标记和显示顺序，不是调用白名单，也不会修改 ChatSession 已选模型或 Gateway
  路由决策。
- 完整 canonical definition 使用独立 Prompt 根密钥和 tenant/template/version/number AAD 加密；
  明文、变量 schema 和 change summary 不写普通数据库列。

## 3. GET /prompt-templates

需要 `prompt_template.read`。支持 exact-set query：

- `status=active|disabled`
- `search=<name-or-description>`
- `limit=<ASCII decimal>`
- `offset=<ASCII decimal>`

未知或重复 query、符号数字、Unicode 数字、GET 正文均返回
`prompt_template.request_invalid`。列表只返回 metadata 和计数，不返回 definition：

```json
{
  "prompt_templates": [
    {
      "prompt_template_id": "ptpl_example",
      "name": "客户回复",
      "description": "客户成功团队模板",
      "status": "active",
      "metadata_revision": 2,
      "current_version_id": "ptv_example_v2",
      "current_version_number": 2,
      "project_count": 1,
      "recommended_model_count": 2,
      "created_at": "2026-08-02T08:00:00Z",
      "updated_at": "2026-08-02T09:00:00Z"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0,
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

## 4. GET /prompt-templates/options

具有 `prompt_template.read` 或 `prompt_template.write` 任一权限时可读取。响应只投影模板表单需要的
active Project 和 ModelAlias ID、名称、环境及状态，不授予 Project、模型或路由管理权限。

```json
{
  "projects": [{"project_id": "prj_example", "name": "客户中心", "status": "active"}],
  "model_aliases": [
    {
      "model_alias_id": "mal_balanced",
      "alias": "balanced",
      "display_name": "企业均衡模型",
      "environment_id": "env_prod",
      "status": "active"
    }
  ],
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

接口不接受 query 或正文。它不返回 Provider、Endpoint、内部 Model ID、Key 或成本配置。

## 5. POST /prompt-templates

需要 `prompt_template.write`。请求是“名称、描述 + 完整定义”的 exact-set JSON；成功在一个
`BEGIN IMMEDIATE` 事务内创建模板、封印 v1、写入 AuditLog、不可变 Operation 和发布 source event。

```json
{
  "name": "客户回复",
  "description": "客户成功团队模板",
  "content": "请为 ${customer_name} 生成合规回复。",
  "variables": [
    {"name": "customer_name", "label": "客户名称", "required": true, "max_chars": 80}
  ],
  "project_scope": "all",
  "project_ids": [],
  "recommended_model_alias_ids": ["mal_balanced"],
  "change_summary": "初始化版本"
}
```

首次创建返回 `201`。同一 tenant、actor、`X-Request-ID` 和 canonical intent 的网络重放返回同一
Operation/version 事实及当前模板投影，HTTP `200`、`replayed=true`；相同 request ID 的不同意图
返回 `prompt_template.idempotency_conflict`。重放不会重复写审计、版本或事件。

## 6. GET /prompt-templates/{template_id}

需要 `prompt_template.read`。返回模板 metadata 和按版本号倒序排列的全部不可变版本元数据；版本项
包含项目/推荐模型 ID、安全计数和定义密文字节数，但不包含正文、变量 schema 或变更说明。

不存在和跨 tenant 的模板统一返回 `prompt_template.not_found`。接口不接受 query 或正文。

## 7. GET /prompt-templates/{template_id}/versions/{version_id}

需要 `prompt_template.read`。仅该接口返回一份授权解密 definition：

```json
{
  "prompt_template_id": "ptpl_example",
  "prompt_template_version_id": "ptv_example_v2",
  "version_number": 2,
  "definition": {
    "content": "请为 ${customer_name} 生成合规回复。",
    "variables": [
      {"name": "customer_name", "label": "客户名称", "required": true, "max_chars": 80}
    ],
    "project_scope": "all",
    "project_ids": [],
    "recommended_model_alias_ids": ["mal_balanced"],
    "change_summary": "优化回复约束"
  },
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

密钥缺失返回 `prompt_template.unavailable`；密文认证、AAD 或 schema 损坏返回
`prompt_template.definition_unavailable`，不得回显密码学、数据库或正文细节。

## 8. PATCH /prompt-templates/{template_id}

需要 `prompt_template.write`。使用 metadata revision CAS；至少提交 `name`、`description`、`status`
之一，显式 `null` 和空 PATCH 非法。空描述是合法更新。

```json
{
  "expected_revision": 2,
  "status": "disabled"
}
```

真实变化递增 revision 并写 `prompt_template.updated` 审计；no-op 返回 `changed=false`，不产生
revision、AuditLog、Operation 或事件。stale revision 返回 `prompt_template.revision_conflict`。
禁用只阻止新的模板调用，历史版本仍可由具有 read 权限的管理员审计读取。

## 9. POST /prompt-templates/{template_id}/versions

需要 `prompt_template.write`。请求为 `expected_revision + 完整定义`，不提供增量 patch。成功原子创建
下一版本、一次封印、更新 current head/revision、写发布 AuditLog、Operation 和 source event。
首次返回 `201`，同意图重放返回 `200`；幂等和 CAS 语义与创建接口一致。

已发布版本 header、正文密文、Project/Model 绑定、版本号、发布链路及 source event 都不可更新、
删除或 `REPLACE`。服务层和数据库触发器共同执行该边界。

## 10. 企业 Chat 调用

`GET /chat/options` 仍只要求 `chat.use`。实时具有 `prompt_template.use` 时，响应额外返回当前 tenant
active 模板的最小调用投影：当前 version ID/number、名称、变量表单、适用项目和推荐模型；无该权限
时投影为空，普通 Chat 不受影响。

选择模板后，`POST /chat/sessions/{chat_session_id}/messages` 必须额外提交且仅提交成对字段：

```json
{
  "client_message_id": "d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0",
  "history": [],
  "prompt_template_version_id": "ptv_example_v2",
  "template_variables": {"customer_name": "示例客户"}
}
```

Control 在持锁事务内实时复核 `prompt_template.use`、active/current、项目适用性、owner 和变量 exact-set，
创建 in-flight Invocation 并把一次性 grant 交给 Gateway。Gateway 消费 grant 前再次复核同一权限、
tenant、版本、状态和项目，解密并在内存渲染可信 final user message，然后进入既有 DLP/Policy/预算/
路由/Provider 链。成功响应的 `rendered_user` 仅供当前浏览器内存历史使用，并强制 `no-store`。
Console 的变量状态和最终请求 map 均使用 null-prototype 对象并逐键复制；不使用对象展开、
`Object.assign` 或 `Object.fromEntries` 处理变量 map，原型保留名在 options 净化和 API 发送边界均失败关闭。

## 11. 错误合同

| HTTP | code | 场景 |
| ---: | --- | --- |
| 401 | `auth.token_missing/invalid/expired` | Console Session 缺失、无效或过期 |
| 403 | `auth.permission_denied` | 实时缺少 read/write/use 或 chat.use |
| 404 | `prompt_template.not_found` | 模板/版本不存在或跨 tenant |
| 404 | `prompt_template.binding_not_found` | 创建/发布引用的 Project/Model 不存在或跨 tenant |
| 409 | `prompt_template.name_conflict` | 当前 tenant 模板名称冲突 |
| 409 | `prompt_template.revision_conflict` | metadata revision CAS 失败 |
| 409 | `prompt_template.idempotency_conflict` | 同 request ID 对应不同写意图 |
| 409 | `prompt_template.disabled` | 调用时模板已禁用 |
| 409 | `prompt_template.version_conflict` | 调用版本不是 current head |
| 409 | `prompt_template.not_applicable` | 当前 Chat Project 不在 selected scope |
| 422 | `prompt_template.request_invalid` | wire、query、JSON、字段或严格类型非法 |
| 422 | `prompt_template.definition_invalid` | 语法、变量 schema、绑定或资源状态非法 |
| 422 | `prompt_template.variables_invalid` | Chat 变量缺失、额外、类型或长度非法 |
| 500 | `prompt_template.definition_unavailable` | 已认证密文/AAD/schema 损坏 |
| 500 | `prompt_template.integrity_failed` | head、版本、计数、owner 或不可变关系损坏 |
| 503 | `prompt_template.unavailable` | 数据库或独立模板密钥暂不可用 |

错误正文只返回固定 message、`request_id` 和 `trace_id`，不包含 SQL、路径、repr、Prompt、变量名/值、
密文、绑定目标或另一 tenant 的存在性。

## 12. 审计、事件与迁移

- AuditLog 动作为 `prompt_template.created`、`prompt_template.updated`、
  `prompt_template.version_published`；摘要只含安全 ID、revision、状态和计数。
- `prompt_template.published` Webhook payload 固定七字段：template/version ID、version number、
  template revision、project scope/count、recommended model count。名称、正文、变量、绑定清单、
  change summary、密文和正文 hash 均禁止出现。
- 0059 将 RBAC catalog 提升到 revision 4，新增 read/write/use；Admin、Developer 获得三项权限，
  Viewer 仅获得 use，Finance/Security/Ops 和自定义矩阵不自动扩权。
- 0059 新增 tenant-scoped Template、Version、Project/Model binding、Invocation、Operation 和 source
  event 表，以及完整 owner/immutable trigger。down 只允许 canonical 空模板状态，任何业务事实、
  相关审计/Webhook、自定义授权、未知后续依赖或完整性异常都会在首个 DDL 前原子拒绝。

本文合同已随 PRD-067 完成并由专项、必要受影响回归、开发库 0059 实迁移、真实三服务和双视口
浏览器验收共同验证。最终结果、证据路径和 SHA-256 见
`D:/MetaHub/docs/testing/PRD-067-tests.md`；总体 108 条目标仍在进行。
