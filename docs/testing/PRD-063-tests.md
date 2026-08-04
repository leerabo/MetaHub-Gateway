# PRD-063 国内模型 Endpoint 适配规范测试记录

状态：**通过，PRD-063 已达到完成门禁**。记录日期：2026-08-01。

本记录只汇总实际执行的 PRD-063 专项、直接受影响回归、0055 实迁移、真实 API/Stub 和双视口浏览器验收。局部修正只复测相关节点，没有因小范围问题重复无关全量。全部 108 条 PRD 仍在进行，`all_108_prds_complete=false`。

## 1. 环境与证据边界

- 工作区：`D:/MetaHub`。
- Python：`C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。
- 开发数据库：`D:/MetaHub/.data/metahub-dev-prd031.db`。
- Gateway / Control / Console：`http://localhost:8000`、`http://localhost:8001`、`http://localhost:5173`。
- pytest 均使用 `-p no:cacheprovider` 和位于 `D:/MetaHub/.artifacts/prd063-*` 的独立 `--basetemp`。
- Runtime、浏览器 profile、截图和脱敏 JSON 只写入 `D:/MetaHub/.artifacts/prd063-*`。
- 全程未读取、枚举、写入或删除 `D:/MetaHub/.tmp`，未使用 `C:/tmp`，未提权。

冻结目录身份：

| 字段 | 值 |
| --- | --- |
| version | `adapter-spec-v1` |
| spec count | `9` |
| canonical UTF-8 bytes | `13633` |
| SHA-256 | `9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870` |

## 2. 自动化专项与直接回归

| 范围 | 结果 | 已验证内容 |
| --- | ---: | --- |
| Shared 冻结目录 | `44 passed` | 九类唯一性、分类、auth/model/usage/error/stream/price/probe 合同、canonical bytes/hash、损坏目录失败关闭 |
| Shared Webhook | `16 passed` | `adapter.spec_updated` exact envelope/payload、tenant/object/actor/time/hash/count 边界、敏感字段拒绝 |
| Gateway PRD-063 | `30 passed` | 六类国内 Provider、客户 Endpoint、迁移入口、工厂选择、模型映射、鉴权、流式选项、目录身份绑定 |
| Gateway 直接受影响回归 | `12 passed` | Adapter、KeyPool、统一错误、流式 Usage、失败事实、敏感日志 |
| Control Adapter Spec | `7 passed` | Session tenant、实时权限、严格 query、内部 Token、正文上限、幂等/冲突、事件 |
| 0055 专项迁移 | `3 passed` | fresh/up/down/re-up、bootstrap、新租户、约束、回滚保真 |
| Provider / Health | `14 passed` | create/update/import/test/probe 前统一 spec/auth 校验、DNS 固定连接与旧对象兼容 |
| Webhook 直接回归 | `8 passed` | 提交后投递、补投保留原 `source_request_id`、失败不回滚、日志脱敏 |
| Console PAGE-090 | `32 passed` | API 白名单 DTO、owner/generation、三分类九类、XSS、权限裁剪、Provider Status 不回归 |
| 浏览器验收器合同 | `8 passed` | 固定证据根、loopback 网络、三类请求、tenant 批次一致、布局、隐私和完整工作流 |

补充门禁：

- `node --check`：PRD-063 浏览器验收器和对应测试通过。
- Console 定向格式检查通过。
- `scripts/ci/check-comments.py`：`comment coverage ok`。
- 关键 Python 模块语法编译通过。

## 3. 迁移回归说明

公共迁移回归首次执行结果为 `54 passed / 2 failed`。两个失败都不是迁移行为失败，而是 `test_db_migrations.py` 仍把完整迁移范围写死为 `0031..0049`；当前仓库已经连续演进到 `0055`。

只修正这两处旧上界为 `0031..0055` 后，定向复测两个失败节点为 `2 passed`。按照局部修正只测相关节点的约束，没有再次运行整套迁移回归，也没有把结果表述成一次新的“全量零失败”。

## 4. 开发库实际迁移

权威迁移证据：`D:/MetaHub/.artifacts/prd063-runtime-final/migration-metadata.json`。

- 开发库从最高 `0053` 实际升级至 `0055`，共 `55` 条迁移。
- `schema_migrations`：`COUNT=55`、`MAX(version)=0055`。
- `PRAGMA integrity_check=ok`。
- `PRAGMA foreign_key_check` 违规数为 `0`。
- 两个现有 tenant 各有且只有一个 active activation；version/hash/spec_count 均与冻结目录一致。
- active activation 身份漂移数为 `0`。
- 迁移前备份：`D:/MetaHub/.artifacts/prd063-runtime-final/metahub-dev-before-0054-0055.db`。
- 备份 SHA-256：`74293e048ff78c5ae648495565153a62c77a02854eccb2e59d93c7becb734c75`。

## 5. 真实 API、权限、租户与 Gateway 验收

权威证据：`D:/MetaHub/.artifacts/prd063-runtime-final/runtime-acceptance.json`，状态为 `passed`。

Control API：

- Admin `200`、Developer `200`、Viewer `403`、匿名 `401`。
- `tenant_id` query、重复 query 均为 `422`，tenant 只能从 Session 恢复。
- 内部激活缺 Token `401`、错 Token和混合身份 `403`、非法 query/超限正文 `422`。
- 同版本激活返回 `changed=false`；同 request/body 重放一致；同 request/不同 body 返回 `409`。
- 成功及所有错误均携带统一错误码、`request_id/trace_id` 和 `no-store`。

Gateway 与本地无日志 Stub：

- DeepSeek 非流式真实调用 `200`。
- 通义流式真实调用 `200`，上游实际收到 `stream_options.include_usage=true`。
- 上游 `429` 归一为 `gateway.upstream_rate_limited`。
- `RequestLog=3`、`UsageLedger=2`；失败请求已记录。
- DeepSeek `16` tokens、通义 `10` tokens，tenant、request 和 trace 可对应。
- 临时 Provider、Key、Model、Alias、Virtual Key 和用户均已禁用。

隐私检查：

- 持久库完整敏感 canary 命中 `0`。
- 历史 Prompt/Response/错误正文前缀命中 `0`。
- 明文 Provider Key 行和明文 Virtual Key hash 行均为 `0`。
- API 未回显 Provider Key 或上游自由错误正文。

## 6. 真实浏览器验收

验收器：`D:/MetaHub/scripts/acceptance/prd063-browser-acceptance.mjs`。

权威批次：`D:/MetaHub/.artifacts/prd063-browser/run-20260801T100000-final/browser-acceptance.json`，状态为 `passed`。

| 视口 | 页面状态 | 截图 | 结果 |
| --- | --- | --- | --- |
| `1280x720` | 默认上游，6 类国内厂商 | `default-desktop-1280x720.png` | 非空、页面宽度 1280、无页面横向溢出、导航与主区重叠 0 |
| `390x844` | 迁移兼容，1 类入口及竞品边界 | `migration-mobile-390x844.png` | 非空、页面宽度 390、合同表单列化、无横向溢出或按钮裁切 |

浏览器实际完成：

- 真实管理员表单登录、`/auth/me`、后端导航和真实注销均为 `200`。
- 三次 `/adapter-specs` 读取分别返回 `6/9/1` 条；页面实际显示 `6/2/1` 条，三分类合计九类。
- 目录 version/hash、请求追踪、完整 DTO、tenant 批次一致性通过。
- OpenRouter、硅基流动只显示为竞品/迁移兼容平台，不显示为默认模型厂家。
- 外部请求 `0`、非法目录请求 `0`、page error `0`、console error `0`。
- DOM、URL、Local/Session Storage 敏感命中 `0`；注销后认证槽和受保护 DOM 均清空。

人工查看两张最终 PNG，标题、目录身份、分类控件、竞品边界和移动端单列合同均正确取景，无元素重叠或文本遮挡。

## 7. 服务日志与健康检查

- Gateway、Control API、Console 最新源码健康检查均为 HTTP `200`。
- 浏览器验收后的三服务增量日志扫描中，浏览器 canary、管理员密码、Authorization、Bearer Token 和历史私有前缀命中均为 `0`。
- Gateway/Control 保留 request/trace、tenant、对象 ID、状态和用量事实；不记录 Key、Authorization、Endpoint、Prompt、Response 或上游自由错误正文。

## 8. 完成结论

PRD-063 的设计、两轮编码前自审、实现、两轮实现后复审、专项和直接回归、0055 实迁移、主流程、异常、权限、租户、隐私、三服务 Runtime 与双视口 Browser 已全部通过，可以标记 PRD-063 完成。

Excel `01_PRD总表` 与 `02_M0-M1核心开发` 的下一条为 `PRD-064 | M1-企业POC | P1 | 契约测试 | Provider Adapter契约测试框架`。该结论不代表全部 108 条完成，`all_108_prds_complete=false`。
