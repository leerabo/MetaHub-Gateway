# PRD-065 多角色首页与导航测试记录

状态：**已完成并通过专项回归、数据库迁移、真实 HTTP、真实 Edge 双视口和运行健康验收**。记录日期：
2026-08-01。

本记录对应 `PRD-065 / PAGE-092 / GET /dashboard/role-view / OBJ-087 / EVT-072`。结论只代表
PRD-065 已完成；全部 108 条 PRD 仍在持续开发，`all_108_prds_complete=false`。

## 1. 环境与执行边界

- 工作区：`D:/MetaHub`。
- Python：`C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。
- 开发数据库：`D:/MetaHub/.data/metahub-dev-prd031.db`。
- Gateway / Control API / Console：`http://localhost:8000`、`http://localhost:8001`、
  `http://localhost:5173`。
- pytest 使用 `-p no:cacheprovider`，`TEMP`、`TMP` 和 `--basetemp` 均限定在
  `D:/MetaHub/.artifacts/prd065-*`。
- 浏览器 profile、截图、隔离数据库和验收 JSON 均限定在 `D:/MetaHub/.artifacts/prd065-*`。
- 本轮不访问、不枚举、不修改 `D:/MetaHub/.tmp`，不使用 `C:/tmp`，不提权。
- Provider Key、Token、密码、Endpoint、Prompt、Response、Authorization 和上游自由错误正文不得进入日志、
  DOM、URL、Storage、数据库事件或验收 JSON。

## 2. 已验收实现范围

- 后端新增 tenant-scoped `RoleDashboardConfig`、五类固定视图、六角色默认映射和
  `GET /dashboard/role-view`；所有身份与权限均从当前认证上下文恢复。
- 可见视图是租户角色配置与实时 permission 快照的交集。显式越权视图后端返回 403；默认视图因实时撤权
  不可用时，只能安全回落到同一配置内的 `executive`。
- 六类卡片只返回当前租户有界聚合；明细链接同时受后端卡片权限和同轮导航快照约束。
- PAGE-092 支持后端下发范围内的内存视图切换；切换先清理旧卡，迟到响应按完整 authorization owner 淘汰。
  选择不写 URL、LocalStorage、SessionStorage 或数据库偏好。
- `dashboard.role_viewed` 仅在 GET 成功、owner 复核和 DOM 投影完成后提交，后端重新校验 revision、视图与权限。
- 0057 新增角色首页配置和新租户初始化，扩展页面事件枚举，并提供带 schema/data guard 的 down migration。
- 保留旧 `GET /dashboard/overview` 合同，不用旧接口回退绕过 PAGE-092 权限。

## 3. 自动化专项与受影响回归

下表结果来自实际命令输出。pytest 的 `--basetemp` 目录只用于隔离运行，不包含独立 JUnit/JSON，因此目录
存在本身不作为通过证据。

| 范围 | 实际结果 | 覆盖重点 |
| --- | ---: | --- |
| `test_prd065_role_dashboard.py` | `3 passed` | 六角色默认视图、admin 切换、第二租户、实时撤权、401/403/422、配置/依赖统一错误和事件防伪 |
| `test_prd065_db_migrations.py` | `19 passed` | fresh、0056 升级、down/re-up、新租户、owner/revision、非法配置、REPLACE/DELETE、schema drift 和 down guard |
| 直接受影响 Python 组合 | `17 passed` | 旧 Dashboard 聚合兼容及 Provider 错误归一化受影响节点 |
| `roleDashboard.test.mjs` | `14 passed` | API client、owner 状态机、XSS、实时撤权回落、401/403 清理、同视图 503 partial、事件 exact payload 和响应式源码门禁 |
| `dashboard.test.mjs` | `6 passed` | 旧 Dashboard client/renderer、PAGE-092 接线和侧栏兼容 |
| Provider Health 受影响组合 | `14 passed` | 角色首页复用 Provider 健康事实后，既有状态和权限合同保持兼容 |
| `prd065BrowserAcceptance.test.mjs` | `6 passed` | 工件根、loopback Origin、request method 关联、CAS 权限恢复、双视口布局和完整浏览器流程源码门禁 |
| Console lint | `178 files passed` | 当前 Console 源码与测试静态检查 |
| 中文注释门禁 | `comment coverage ok` | 模块、函数以及安全/权限/恢复关键分支中文说明 |

本次收口没有因局部失败反复执行根级全量回归。设计要求的主流程、异常、权限、租户、迁移、旧接口兼容和
PAGE-092 均由上述专项、真实 HTTP 及真实浏览器证据共同覆盖。

## 4. 真实 HTTP、权限与租户验收

### 4.1 六角色与第二租户

证据：
`D:/MetaHub/.artifacts/prd065-runtime/run-20260801T2102CST/http-role-tenant-acceptance.json`

SHA-256：`F4D157B09D748D9A5707982355A4F94CABEAF36C504DB05EA01BCD331672FADB`。

- `admin/developer/viewer/finance_admin/security_admin/ops_admin` 均通过真实登录和
  `GET /dashboard/role-view`，HTTP 均为 200。
- 六角色默认视图分别为 `executive/developer/executive/finance/security/operations`，每个响应均返回
  6 张卡片、调用方指定的 `request_id` 和非空 `trace_id`。
- 在第二租户写入可识别事实后，第一租户六类卡片保持不变，证明聚合查询 tenant-first 且无跨租户污染。
- 两租户 Endpoint canary、permission 列表泄漏均为 0，Token/密码未持久化。

### 4.2 异常、实时撤权与事件

证据：
`D:/MetaHub/.artifacts/prd065-runtime/run-20260801T2102CST/http-permission-event-acceptance.json`

SHA-256：`38AED96907B41C3C4D536108E52BF8D7FD9CF09CACE2489137A81CD62F205497`。

- admin 显式切换财务视图返回 200；viewer 显式越权返回
  `403 / auth.permission_denied`。
- 非法 query 返回 `422 / dashboard.query_invalid`；匿名非法 query 先返回
  `401 / auth.token_missing`，证明认证优先于参数探测。
- 实时撤销 `billing.anomaly.read` 后，显式财务视图返回 403，默认请求安全回落到
  `executive` 200；恢复原 permission 后财务视图重新返回 200。
- 合法 `dashboard.role_viewed` 返回 200；伪造 revision/view 返回
  `422 / console.event_invalid`。
- 所有异常响应均保留 `request_id` 和 `trace_id`，Token/密码未写入证据。

## 5. 真实 Edge 双视口验收

权威证据：
`D:/MetaHub/.artifacts/prd065-browser/run-20260801T225656-CST/browser-evidence.json`

SHA-256：`17392F2E266D1BA3C4786515A379EF311EAD1EFEB7B8C00C190DF1CF5D4EBBCA`。

- Microsoft Edge 中实际登录六角色；六个默认视图均验证 `1280x720` 和 `390x844` 布局，每个角色均显示
  6 张卡片。
- admin 从经营总览切换财务视图时旧卡立即清空，URL 不变化。
- finance_admin 在同一浏览器 Session 中由后端 revision CAS 撤销 `billing.anomaly.read`，页面回落
  `executive`；随后按当前 revision 恢复原权限并重新显示 `finance`。
- 网络 200 计数：login `6`、logout `6`、navigation `8`、role-view `9`、
  `dashboard.role_viewed` `9`、事件总数 `9`。
- URL、DOM、Storage、Console、外部请求和视图持久化隐私命中全部为 0；验收结束后 profile、browser-home
  和 Edge `Importer_0_4` 已清理。

截图：

| 视口 | 文件 | 结果 | SHA-256 |
| --- | --- | --- | --- |
| `1280x720` | `screenshots/page-092-desktop-1280x720.png` | 74,609 bytes、非空、颜色样本 480、横向溢出/卡片溢出/分段溢出/重叠均 0 | `B0CC2A614354E5D8EFA07599AFE26602AB5A29B7FF7A5D0114B0FF9B42C2F718` |
| `390x844` | `screenshots/page-092-mobile-390x844.png` | 29,230 bytes、非空、颜色样本 510、横向溢出/卡片溢出/分段溢出/重叠均 0 | `3DED2AC8D4A66A1D3EC765F328BE6228D544B2F0CE952466C3C2A5A0D0C11FFB` |

主 Agent 已复核两张最终 PNG：角色首页和分段视图均可见，文字未遮挡，桌面与移动布局没有不连贯重叠。

## 6. 数据库迁移与运行收口

隔离数据库最终证据：
`D:/MetaHub/.artifacts/prd065-browser-runtime/run-20260801T222423-CST/database-closeout-v2.json`

SHA-256：`076854CE4C24211093FEF44CE6B9BC8E5CDE6272DC823217945C8300F57326F8`。

- migration count `57`、failed `0`、latest `0057`。
- `integrity_check=ok`，外键违规 `0`。
- 2 个租户共有 12 条 canonical 角色首页配置；finance permission 已完整恢复。
- 隔离验收累计的 150 条活动 Session 已全部撤销，终态为 0。
- 数据库关闭后 WAL 为 0 bytes，WAL/SHM 文件均不存在。

正常开发库只读预检：
`D:/MetaHub/.artifacts/prd065-browser-runtime/run-20260801T222423-CST/dev-database-preflight.json`

SHA-256：`9206B821E84F65887F2D295A4DD140B8BB6DC1FA57325DFB4992E464E791C273`；结果为
`57/0057`、`integrity_check=ok`、外键违规 0、2 个租户和 12 条角色首页配置。该预检只读，没有修改共享开发库。

最终健康检查：Gateway、Control API、Console 分别为 HTTP `200/200/200`；Gateway 与 Control 健康响应均
带非空 `request_id`、`trace_id`。

## 7. 失败、定向修复与证据取舍

- 早期 Edge `run-20260801T2140CST` 只留下浏览器崩溃 profile，未形成可验收 JSON；不计为通过。
- `run-20260801T224900-CST` 只有截图而没有 evidence JSON；不计为最终证据。
- `run-20260801T225158-CST` 已通过当时检查，但缺少最终移动截图内容锚点；由 `225656-CST` 权威批次取代。
- 首份 `database-closeout.json` 因仍有 119 条活动 Session 标记为失败。继续验收后隔离库累计 150 条活动
  Session，最终由 `database-closeout-v2.json` 全部撤销并验证终态为 0；失败文件不作为通过证据。
- 浏览器验收器增强后，中文注释门禁首次报告 12 个关键分支缺少中文说明；只补充相关分支注释，再运行门禁
  得到 `comment coverage ok`，并定向复跑验收器合同得到 `6/6`。
- Control 恢复正常开发库进程后，首次健康轮询发生在服务启动完成之前；服务日志确认启动完成后只重新检查
  三个健康端点，最终为 `200/200/200`，没有重跑无关全量套件。

## 8. 最终结论

PRD-065 的设计与两轮编码前自审、tenant-scoped 数据模型、后端 RBAC、统一错误和 request/trace ID、0057
迁移、六角色主流程、异常/权限/租户流程、PAGE-092、兼容回归、真实 HTTP、真实 Edge 双视口、隐私扫描、
数据库终态和三服务健康均已通过。因此 PRD-065 可以标记完成。

下一项已按 Excel `01_PRD总表` 与 `03_M2-M3扩展交付` 交叉识别为
`PRD-066 | M2-企业增强 | P2 | 扩展层 | 企业Chat | 企业Chat演示入口`；
`all_108_prds_complete=false`。
