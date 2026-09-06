# S7/G7 批次验收报告

日期：2026-09-07

## 结论

S7/G7 分域验收的实现、跨环境复验与敏感审计已完成，**G7 专项通过**。PRD-148 **仍不得开始**：权威工作簿要求 G0..G7 全部通过，而 G6 的真实 72 小时观测仍缺失。PRD-139..147、Windows/SQLite 产品回归、真实三浏览器三缩放、Linux、PostgreSQL 17.11 原生迁移/业务与正式 Nginx 路由均有对应证据。外部入口沿用 r3 路由证据和最终日志变更专项的组合，不声称最终完整配置的新外部演练通过。

| 门禁 | 状态 | 结论 |
| --- | --- | --- |
| PRD-139..147 实现 | passed | 权威工作簿已标记 PRD-147 完成 |
| Windows + SQLite 产品 QA | passed | 15/15 任务通过 |
| 三浏览器三缩放 | passed | Product QA 27/27 journey；PRD-147 Platform 9/9 artifact |
| 四小时长稳 | passed | 首轮严格失败并保留；隔离重跑 113,344/113,344 通过 |
| Linux | passed | Console 1286/1286；修正后 Python 47/47；最终 SQLite-only 增量 12/12，原始失败保留 |
| PostgreSQL-native migration/runtime | passed | revision 4 原生迁移/事务边界 22/22，业务 20/20，2486/2486 探针；独立重新迁移生成摘要一致 |
| 正式 Control ingress | passed_local | Nginx 配置、独立镜像与不发布后端端口已落地；本地原生代理/部署及合同回归通过 |
| 外部 N/N-1 ingress | passed_routing | r3 正式 Nginx + 真实应用 + 外部 HTTPS 共 138 项通过；后续仅错误日志配置改变，路由字节未变；最终完整配置的新外部重跑未成功 |
| S7/G7 专项目标 | passed | 64 项导航处置、分域与历史保真验收完成；G6 未完成，不能据此进入 PRD-148 |

## 执行结果

| 范围 | 结果 |
| --- | --- |
| Product QA orchestrator | 15/15 任务，失败 0 |
| Backend | 8/8，failure/error/skip 均为 0 |
| Performance contracts | 9/9，failure/error/skip 均为 0 |
| Web contracts | 17/17，fail/skip/todo 均为 0 |
| Product QA browser matrix | Chromium/Edge/Firefox x DPR 1/1.25/1.5，27/27 journey，Console error 0，request failure 0，溢出/裁切/交互重叠 0 |
| PRD-147 Platform browser matrix | Chromium/Edge/Firefox x DPR 1/1.25/1.5，9/9 artifact，Console error 0，request failure 0 |
| Live/pressure | 30/30 和 600/600 成功，错误 0 |
| 97-case audit | 97/97 通过，失败 0 |
| Console | 全量 Node 1284/1284；生产面专项 8/8；lint 761 文件；production verifier 258 modules/272 shipped sources；build 272 assets |
| 修正后 S7 Python | 102/102；最终 PRD-145/146 与 case-audit 回归 15/15 |
| 静态检查 | 23 个 Python 文件编译通过，Ruff 无新增诊断；68 条与 HEAD 相同的历史超长行保留；JavaScript `node --check`、Console lint/build、`git diff --check` 通过 |
| 权威工作簿 | `01_PRD总表!B59=已完成`、`B60=未启动`；总状态区分 G7 通过和 G6 的 72 小时缺口 |

## 长稳结果与处置

首轮四小时观测保留在 `.artifacts/s7-g7/20260905-030323/stability-long.json`：观测 `14400.233s`，最大观测间隔 `1.19s`，防休眠生效，110,999 次请求中 110,998 次成功。第 1070 秒时间桶的 Gateway `/health` 出现 1 次 HTTP 502，因此原门禁正确记为 `failed`，对应的首轮 case audit 为 95/97。

Gateway 服务日志中没有 502、`request.failed` 或异常，`/health` 实现也不产生 502；原探针让 HTTPX 默认信任主机代理环境。探针现已对只允许 loopback 的目标显式使用 `trust_env=False`，专项测试通过。新一轮使用全新 0139 SQLite、随机端口和随机进程内凭据，证据写入 `.artifacts/s7-g7/20260905-030323/stability-long-rerun.json`。

最终重跑结果：`passed`。观测 `14400.062s`，113,344/113,344 次请求成功，错误 0，最大观测间隔 `0.583s`，防休眠生效。Console/Control/Gateway 分别完成 37,781/37,782/37,781 次请求，各自错误为 0；p95 分别为 `7.821ms`/`13.134ms`/`12.510ms`。最终 case audit 消费重跑证据后为 97/97。

## 修正项

- Workspace Navigation 响应从 `account_preferences` 投影权威 `account_locale`，Console 不再用混合 Cookie 跨域读取 Identity preferences。
- 修正 Firefox 对 HTML pattern regex `v` 模式中字面量连字符的兼容性。
- 浏览器鉴权负向探针改为校验实际 401 network response，不依赖浏览器 Console 日志差异。
- Product QA 的密码与签名 secret 改为每次运行随机生成；live/pressure 使用独立 0139 数据库、随机端口和独立服务栈。
- Case audit 的迁移头、数量和 pressure 阈值从历史值更新为 `0139/139/600`，并增加防回退测试。
- 历史 PRD-139..146 migration 测试明确区分“当前仓库 head”与“当时 PRD 迁移子集边界”。
- 发布资产变化后重新计算 `production_asset_set_sha256`，production build 对 272 个资产复验通过。

## 证据与安全边界

2026-09-07 补充：新版 baseline 原生迁移为 `postgresql-native-final-1ba72f0a8dd8496c8253878cece487f5-junit.xml`
（22/22），原生业务为 `postgresql-runtime-832918a100da4866942a0b117ddae893-junit.xml`（20/20），
Windows 最新迁移业务为 `windows-console-runtime-r2-junit.xml`（14/14）。历史迁移完整回归为
`historical-migration-final-junit.xml`（58/58）。Windows IAM 最终
`windows-platform-iam-r3-junit.xml` 为 3/3，保持 session 50ms、list 300ms 的原 p95 限制。
`pragma_table_list` 消除了重复 SQLite catalog 扫描；不引入可能过期的 schema 缓存。
Linux `linux-runtime-r2-junit.xml` 为 47/47，之后只修改 schema helper 与其测试，
由 `linux-schema-delta-junit.xml` 的 12/12 覆盖。两轮 provenance 均保留，24 份最终源码与 278 份迁移摘要核对一致。

前置 G6 的 `docs/testing/product-qa/REPORT-20260831.md` 明确只支持限定环境的 RC 和四小时长稳，
不包含 72 小时。README 已移除“完整 G6 已通过”的过度表述；G6/G8 的真实 72 小时要求仍待补齐。

补充修复、两轮复核与证据范围见 `docs/testing/G7-infrastructure-remediation.md`。当前扫描见
`sensitive-scan-increment.json`，当前 SHA-256 索引见下列证据清单。原始失败结果和旧扫描保留，不改写历史。

- Product QA：`.artifacts/product-qa/20260905-073827/`。
- S7/G7：`.artifacts/s7-g7/20260905-030323/`，Platform 浏览器终态在 `browsers-final/`。
- 环境能力：`.artifacts/s7-g7/20260905-030323/environment-capabilities.json`。
- 最终 case audit：`.artifacts/product-qa/20260905-073827/case-audit.json`。
- SHA-256 清单：`.artifacts/s7-g7/20260905-030323/evidence-sha256.json`。

测试凭据、TOTP/recovery 材料和签名密钥只在测试进程内存/环境中随机生成，不写入源码、URL、Storage、日志或正式证据。补充扫描覆盖全部明确列出的提交候选与证据目录，高置信敏感模式及禁止提交路径命中均为 0，数量与范围以 `sensitive-scan-increment.json` 为准；既有 mock DSN 单独列出，不作为真实凭据。该模式扫描不等价于穷尽式秘密检测。`.tmp/`、`.artifacts/`、`outputs/`、未跟踪 PRD-101..105、数据库、日志、截图和 trace 均不进入 Git 提交。`.tmp` 含持久 Linux VM 与复用数据库，不得整体清空。
