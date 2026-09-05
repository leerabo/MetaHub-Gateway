# S7/G7 批次验收报告

日期：2026-09-05

## 结论

S7/G7 **未通过**，PRD-148 **不得开始**。PRD-139..147 实现状态、Windows/SQLite 下的产品回归、真实三浏览器三缩放矩阵和静态检查已完成；当前验收环境没有 Linux 运行时、PostgreSQL 原生迁移执行器和外部 N/N-1 ingress，三项均记为 `not_executed/environment_unavailable`，不能由 Windows、SQLite 或本地 loopback 证据代替。

| 门禁 | 状态 | 结论 |
| --- | --- | --- |
| PRD-139..147 实现 | passed | 权威工作簿已标记 PRD-147 完成 |
| Windows + SQLite 产品 QA | passed | 15/15 任务通过 |
| 三浏览器三缩放 | passed | Product QA 27/27 journey；PRD-147 Platform 9/9 artifact |
| 四小时长稳 | passed | 首轮严格失败并保留；隔离重跑 113,344/113,344 通过 |
| Linux | not_executed/environment_unavailable | 当前主机无 Docker/WSL Linux runtime |
| PostgreSQL-native migration | not_executed/environment_unavailable | 无 PostgreSQL client 和原生历史迁移 runner |
| 外部 N/N-1 ingress | not_executed/environment_unavailable | 本机 loopback 不构成外部入口验收 |
| S7/G7 总门禁 | blocked | 必需环境证据缺失，禁止进入 PRD-148 |

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
| 静态检查 | 全部变更 Python Ruff、JavaScript `node --check`、Console lint/build、`git diff --check` 通过 |
| 权威工作簿 | `01_PRD总表!B59=已完成`；`00_变更说明!B5` 保留 G7 未通过与 PRD-148 阻断；公式错误 0 |

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

- Product QA：`.artifacts/product-qa/20260905-073827/`。
- S7/G7：`.artifacts/s7-g7/20260905-030323/`，Platform 浏览器终态在 `browsers-final/`。
- 环境能力：`.artifacts/s7-g7/20260905-030323/environment-capabilities.json`。
- 最终 case audit：`.artifacts/product-qa/20260905-073827/case-audit.json`。
- SHA-256 清单：`.artifacts/s7-g7/20260905-030323/evidence-sha256.json`。

测试凭据、TOTP/recovery 材料和签名密钥只在测试进程内存/环境中随机生成，不写入源码、URL、Storage、日志或正式证据。最终 35 个提交候选路径的高置信凭据模式和禁止路径命中均为 0；30 个正式证据的高置信敏感命中和禁止后缀也均为 0。SHA-256 清单覆盖 31 个文件，复算不匹配为 0。`.tmp/`、`.artifacts/`、`outputs/`、未跟踪 PRD-101..105、数据库、日志、截图和 trace 均不进入 Git 提交。
