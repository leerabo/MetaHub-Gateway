# PRD-143 测试记录

状态：已完成。0135、Platform Provider Adapter 工程台、官方 Adapter release/固定合同证明、
内置错误目录 release、Workspace Provider 兼容投影与受限错误覆盖均已交付。旧 `adapter-specs`、
`adapter-contract-runs` 和顶级 `error-maps` 运行入口已卸载。本条保留独立设计、聚焦自动化、
真实浏览器证据、两轮实施自审、敏感审计和独立提交；下一权威项为
`PRD-144 - 平台套餐账务与企业用量财务边界重构`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0135 Migration | pristine/current head、0134 -> 0135、down/re-up、0046/0055/0056 历史 ID/version/event/outbox/fact 保真、权限矩阵、目标事实 guard 和 FK/integrity |
| Platform Provider Engineering | Adapter release、合同 run/case 固定投影、内置错误目录、四角色字段矩阵、严格 query/body/header、CAS/幂等/step-up、动态撤权、cross-owner 拒绝和安全错误合同 |
| Workspace Provider Engineering | Provider detail 兼容摘要、Workspace-scoped bounded override、owner/head 绑定、strict DTO、权限撤销和旧顶级错误写入口归零 |
| 10 万 case 门禁 | 两轮各 200 页 x 500 条，复合 keyset range seek，无重复/漏项，每页仅 3 条业务 SELECT，无 COUNT/JSON extraction，p95 `<500ms` |
| Console/Browser | Platform/Workspace 独立 client/store/controller/renderer、owner/epoch/abort、旧导航/import/DOM/prefetch 归零、桌面/移动布局和敏感值排除 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| 0135/API/导航专项 | `8/8 passed`，failure/error/skip 均为 `0`，总耗时 `1153.023s`；JUnit `.artifacts/prd143-tests/backend-focused-junit.xml`，SHA-256 `8f2fedba0def46b9dcb287fc50cf020fa2dd617ceb2b632f6c9e3e5ff238e42f` |
| 10 万 case 性能 | 两轮均完整读取 `100000` 条；每轮 `200` 页、每页 `500`；复合 keyset index/range seek；p95 `<500ms`；无重复/漏项、COUNT 或 JSON extraction |
| 受影响 Console | `46/46 passed`；PRD-063/064/056 退役链与 PRD-143 client、strict DTO、store/controller、renderer、late ticket、幂等/step-up 和 active runtime 归零 |
| Console lint | `console lint ok (448 files)` |
| Python static | PRD-143 新增业务/测试 Python 全规则 Ruff 通过；全部受影响 Python 的 `ruff --select F` 通过；compileall 通过 |
| Excel/路线图 | PRD-143 为“已完成”，下一项为 PRD-144；两个全局完成标志继续为 `false`；12 张表视觉通过、公式错误 `0`；SHA-256 `ce08eddb1ae73b1b0f2c80bf89ae836a97140f8879130b3d01836bcf9b2a1d36` |
| 路线图校验器 | `250 checks passed`；`next_prd=PRD-144` |
| 浏览器 | 8 项正式检查通过；Platform/Workspace 各 3 张正式 PNG，桌面/移动图像非空且无内容重叠；Workspace 图为只保留 PRD-143 面板的安全裁切 |
| 浏览器证据 | `.artifacts/prd143-browser/browser-acceptance.json`；SHA-256 `97bff6e9d89eae7a885fb17bf5a0e64dd33a821ee0178f8a94a70c61beaffc3c` |
| 自动化摘要 | `.artifacts/prd143-tests/test-summary.json`；SHA-256 `5b4e47edd7e0548ce93b45293a18364724aa48af9e376db306bfd309336501a4` |

## 浏览器结论

- Platform Owner 的 Provider Engineering 页面只显示 Adapter release、安全 artifact/SBOM/provenance/signature
  引用、固定合同聚合和 opaque keyset 控件；不显示 Runner、构建路径、原始 case 或错误条件正文。
- Workspace Provider detail 只显示本空间的 compatibility、health、Adapter release、builtin catalog 和
  override head，并提供 bounded override 列表；没有 Platform release、合同 CI 或内置目录发布控件。
- 旧 `adapter-specs`、`adapter-contract-runs` 和顶级 `error-maps` 不再进入活动导航、route dispatch、
  DOM、prefetch 或持久化恢复链。
- 1280/1440 桌面与 390 移动证据均无无序重叠；宽表只在所属容器内滚动。Workspace 原捕获顶部含
  loopback URL 和 Provider key 掩码，因此正式图只保留 PRD-143 业务面板，原图已随 `.tmp` 永久清理。

## 两轮实施自审

### 第一轮：迁移、唯一事实与 owner 边界

- 复核 0135 expand/inventory/backfill/quiesce/cutover/contract 和 guarded down，确认 0046/0055/0056
  的稳定 ID、版本、event/outbox、Gateway fact 和恢复水位不被复制或重写；目标域事实存在时 down 失败关闭。
- 复核 Platform/Workspace credential、permission、DTO、cursor 和 owner 绑定，修正历史 Adapter catalog
  version 与 release SemVer 的兼容边界，并增加 API 凭据撤销、动态撤权和 cross-owner 回归。
- 旧三个入口从外部 router、活动 shell dispatch、导航、prefetch 和顶级 writer 同步卸载；普通未知路径语义
  保持一致，内部兼容事实不形成可枚举退役 API。

第一轮修正后无未关闭 P0/P1。

### 第二轮：分页性能、前端隔离与证据安全

- 10 万合同 case 初测暴露隔离 ASGI fixture 缺少生产 GET 请求级 SQLite 连接复用，profile 中连接/查询
  重复成本主导耗时。测试 app 现使用与生产一致的 request scope，并在 fixture 结束关闭 idle pool；业务实现
  保持正常 DTO 校验，不用绕过验证换取性能。
- SQL 门禁排除连接池固定健康检查 `SELECT 1` 后仍严格验证每页 3 条业务 SELECT、无 COUNT、无 JSON
  extraction，查询计划命中复合 keyset range seek；两轮 100000 条均无重复/漏项且 p95 `<500ms`。
- 复核 Platform 与 Workspace store/controller 的 owner、epoch、AbortController、late ticket 和 step-up 一次性
  语义；动态数据全部转义，Platform 内部字段和 Workspace matcher 条件均不进入错误、DOM 或持久化。
- 原 Workspace 截图出现 loopback URL 与 `sk-...` 掩码后，未重置凭据也未把原图作为证据；正式 PNG
  裁切到 PRD-143 面板，并逐图复核、计算尺寸与 SHA-256，原图和裁切脚本随 `.tmp` 清理。

第二轮修正后无未关闭 P0/P1。

## 敏感审计与禁止文件

- staged diff 与 8 个 JUnit/JSON/PNG artifact 已统一扫描私钥 PEM、Provider key、JWT、Bearer、
  Authorization、Cookie、password、TOTP、session/token/API key 和真实账号标识；高置信秘密、结构化
  artifact 敏感值和禁止提交路径命中均为 `0`。临时数据库/API/event/log 不作为交付物，已随 `.tmp` 清理；
  敏感字段不落库、不进响应/event/log 的边界由专项集成断言覆盖。
- 测试使用的固定字母、fixture ID 和 header 占位符只验证长度/合同，不是可用凭据；不得把本地管理员账号、
  密码、TOTP secret、Cookie、Session、Token 或 Provider key 写入源码、文档、URL、Storage 或 artifact。
- `.env`、`.tmp`、`.logs`、数据库、日志、PEM/KEY 等禁止路径不进入提交；`.tmp` 已永久删除。未跟踪的
  PRD-101..105 用户文档与 `outputs/` 不暂存，提交只使用显式路径，不使用 `git add .`。

## 批次门禁

本条按 S7 节奏执行 0135、受影响 API/Console、静态、真实浏览器、路线图和敏感审计，没有重复执行完整
G7。PRD-144..147 继续共享 migration、Platform/Workspace owner、权限、DTO、scheduler 和前端组件；
PRD-147 独立提交后统一执行数据库矩阵、跨域集成/FLOW、三浏览器三缩放、性能、长稳、安全、兼容、
旧入口四层归零和必要回归。PRD-143 专项通过不能替代 G7，也不能宣称公开 GA；当前 G7 仍为 `pending`。
