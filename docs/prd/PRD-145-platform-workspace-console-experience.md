# PRD-145 Platform / Workspace 控制台体验分域

状态：既有实现的聚焦测试曾通过，但 2026-09-07 真实入口复核发现 Cookie 消费端缺口。
本条五视图 owner 与 API origin 已补修；主入口启动及批次真实流程仍未验收，S7/G7 未通过。

PRD-145 将 Platform 运营控制台、Workspace 管理控制台和 GlobalAccount 偏好拆成三个独立边界。同名页面
不得共享路由、DTO、client、store、AbortController、navigation epoch 或权限 owner；页面切换只加载当前
后端授权导航项所需的数据。

## 1. 范围与非目标

本条交付：

- Platform 与 Workspace 各自独立的总览、只读助手和审批队列。
- GlobalAccount 级语言偏好，跨 Workspace 复用同一 revision。
- Workspace 部署诊断摘要，只公开白名单化状态，不公开配置路径、内部 Token 状态或开发环境变量。
- 0137 迁移、投影版本/头指针、审批来源游标、事件/outbox、read audit、scheduler lease 与迁移对账。
- Console 前端独立 client/controller/store/renderer，以及离页、撤权、迟到响应和版本回退保护。

非目标：

- 助手不执行写操作，不接受任意数据源、URL、SQL、脚本或历史对话。
- 聚合审批队列不代替来源域决策命令。
- 本条不单独执行 S7 全批次浏览器、性能、安全和长稳 G7；这些在 PRD-147 后一次执行。

## 2. 域边界与 owner

| 域 | 身份 owner | 权限 owner | 状态边界 |
| --- | --- | --- | --- |
| Platform | operator + platform session revision | Platform role/permission snapshot | 独立 navigation epoch、projection generation、cursor、AbortController |
| Workspace | account + tenant + workspace session | permission revision + 64 位 authorization fingerprint | 独立 navigation epoch、projection generation、cursor、AbortController |
| GlobalAccount | account + global session | ETag/revision | 独立 CAS intent、幂等键和 AbortController |

前端只接收后端授权导航；renderer 仍对当前权限执行一次 fail-closed 交集校验。角色名称不能直接授予页面访问。
owner 任一字段漂移都会中止在途请求并清空受保护快照。响应 navigation epoch 或 permission revision 回退、
相同 revision 下 fingerprint 漂移时，store 终止 busy 并进入固定 conflict 状态。

## 3. 0137 数据模型

迁移按职责划分为：

- Platform/Workspace Console permission extension 与 role permission matrix。
- Platform/Workspace dashboard projection versions、heads 与 sources。
- Platform/Workspace domain approval queue、source cursor 与 rejection。
- Platform/Workspace assistant query projection。
- GlobalAccount preference、command、event、outbox 与 legacy source mapping。
- Workspace deployment diagnostic versions 与 heads。
- Platform/Workspace console event 与 outbox。
- Console read audit、scheduler state、migration cursor 与 reconciliation。

投影版本不可原地改写，head 以 generation/revision CAS 推进。Workspace 审批来源 revision 回退写入拒绝事实，
不能覆盖现有队列。outbox 达到重试上限后保留持久失败状态。降级脚本先检查事实损失；存在不能恢复的新事实时
明确拒绝回滚。

## 4. API 与 DTO 合同

相对 API 合同：

- Platform：GET /platform/overview、POST /platform/assistant/query、GET /platform/approvals。
- Workspace：GET /workspace/overview、POST /workspace/assistant/query、
  GET /workspace/approvals、GET /workspace/deployment-summary。
- GlobalAccount：GET/PATCH /identity/preferences。

所有 DTO 使用 strict/extra-forbid。私有响应必须为 private/no-store、Pragma no-cache、Vary Cookie，并回传
权限 fingerprint；偏好响应使用与 revision 一致的 ETag。浏览器请求只使用 Cookie Session，不发送 Bearer。
写请求使用对应域 CSRF Cookie；偏好更新还必须携带 If-Match 与 16..128 字符幂等键。

助手请求精确只含 question，最多 2,000 Unicode code points；响应只包含枚举 query type/outcome、结构化
claim、citation 和 source status。审批 cursor 只在内存 ticket 中流转，不进入公开 snapshot、URL、Storage
或正式证据。

## 5. 前端结构

Platform、Workspace、GlobalAccount 分别拥有独立 client 和 store/controller。Platform 页面由
platform/main.js 注入，Workspace 页面由活动导航 dispatcher 注入，Identity 页面负责 GlobalAccount 偏好。

Workspace 的 overview、admin-assistant、approvals、system-status 和 config-summary
均进入新 controller；旧角色首页、旧助手和旧审批 loader 不在活动 dispatcher 或其前置刷新路径中调用。
语言设置入口显示本地化迁移说明并跳转账号设置，不再写租户级偏好。

离页、hash 变化、登出、Session 失效、导航切换和权限撤销都会清理 controller。响应完成后必须同时通过
ticket generation、owner、AbortSignal、navigation epoch、permission revision 和 fingerprint 校验，
否则不得更新 DOM。

## 6. 安全、审计与恢复

- 普通角色只看到枚举状态、计数、reason/action code 和不可逆摘要。
- Provider Key、真实 Endpoint、配置路径、环境变量、Token 状态、问题/回答正文均不进入事件、日志或证据。
- 每次读取写入低敏 read audit；命令、投影更新和失败 outbox 保留 request/trace chain。
- Account preference 使用 revision CAS；5xx/网络不确定重试复用同一幂等 intent，确定性 4xx 清理 intent。
- scheduler 每轮提交复核 lease generation/token，过期 worker 不能推进 head 或 ACK outbox。

## 7. 性能与批次门禁

目标为页面切换 p75 < 1.5 秒、普通读取 p95 < 300 ms、在线撤权 < 2 秒。单 PRD 聚焦测试只验证边界、
状态机、迁移和活动调用隔离；真实数据库/流程/三浏览器三缩放/性能/安全/必要回归统一进入 S7 G7。

正式单条记录见 docs/testing/PRD-145-tests.md 和
.artifacts/prd145-tests/test-summary.json。.tmp 只保存可再生 pytest 数据，不作为正式证据。

## 8. 两轮设计与实现自审

### 第一轮：分域、数据事实与回滚

- 确认三个身份域没有复用 owner、cursor、CSRF 或 store。
- 确认投影 head 只以 CAS 推进，审批来源 revision 回退和 migration fact loss 均 fail closed。
- 确认语言偏好归 GlobalAccount，legacy 来源只保留不可逆 mapping。
- 确认 poison outbox 不被静默丢弃。

### 第二轮：活动调用、撤权与界面

- 删除 Workspace 刷新入口的旧 overview 预取和旧助手早退。
- 对旧助手/审批 DOM 保留逻辑增加新页面排除，防止后台刷新绕过新 controller。
- 为五个 Workspace 新页面补 renderer 权限交集，修复伪造导航仍渲染审批筛选器的问题。
- 修复三个 store 对回退响应保留 busy 的问题，并增加固定冲突态。
- 补齐语言迁移面板中英文文案，避免英文 Console 出现中文操作入口。

### 2026-09-09 现行迁移夹具复核

第一轮：测试启动应核对当前 SQLite 迁移目录全部 version/checksum，不再把 max(version)
写死为历史 0143；业务、授权和审计断言保持不变。第二轮：继续只复制可选测试基线到新的
pytest 目录，在副本上补迁移和种子，原基线不能被写入。空轮询修复归 PRD-147 独立交付。
