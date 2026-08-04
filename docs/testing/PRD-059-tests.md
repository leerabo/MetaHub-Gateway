# PRD-059 细粒度角色权限矩阵测试记录

测试日期：2026-07-31

状态：**已完成**。设计文档与两轮编码前自审、实现、0050 开发库迁移、PRD-059 专项、直接受影响
Console 回归、中文注释门禁、真实三服务 API、Microsoft Edge 双视口 Browser 和停服静止终态均已
通过。该状态只代表 PRD-059 完成；全部 108 条 PRD 仍在进行，`all_108_prds_complete=false`。

权威需求：`PRD-059 | M1-企业POC | P1 | 核心层 | RBAC增强 | 细粒度角色权限矩阵`。
设计和公开合同分别见 `docs/prd/PRD-059-fine-grained-role-permission-matrix.md` 与
`docs/api/role-permissions.md`。

## 1. 验收口径

- `TC-0117`：FinanceAdmin 不能修改 Provider；SecurityAdmin 不能修改模型价格；Developer 不能
  读取正式全租户 Billing；角色名不能替代后端 permission 快照。
- `TC-0118`：匿名、失权、畸形 wire、跨租户、职责越界、stale CAS、目录损坏和依赖故障返回统一
  错误码与 `request_id/trace_id`，不泄漏敏感正文或另一租户存在性。
- admin 拥有完整 48 权限且矩阵不可变；其余固定角色只能在产品授权上限内撤销或恢复。
- Permission、RolePermission、权限事件、AuditLog、用户和角色关系均由 tenant-qualified 查询、
  复合 FK 或 owner trigger 约束。
- 矩阵差异、revision/hash、AuditLog 和不可变 source event 同事务；no-op 不提升 revision、不写事件。
- Console 导航、预取、按钮和 PAGE-085 只消费后端权限快照，后端仍执行最终权限校验。
- Provider Key、Virtual Key、Token、Authorization、Cookie、Prompt、Response、Secret、DLP pattern
  和 Webhook 正文不得进入公开 DTO、事件、审计摘要、普通日志或测试证据。

## 2. 设计与实现范围

两轮编码前自审已在设计文档第 12、13 节完成，关闭了三角色 AuditLog CHECK、Developer Billing、
admin 恢复入口、产品授权上限、跨租户复合关系、strict wire、revision CAS、no-op、source event、
0049 rowid/导出保真、down guard、旧 Token 即时降权和前端 owner 等阻断项。

当前正式实现包括：

- `infra/migrations/0050_fine_grained_role_permissions.sql/.down.sql`：六角色、48 权限、125 条默认
  RolePermission、角色 revision/hash、tenant 初始化、权限事件和 0049 Audit/Export 保真。
- `shared-sdk/python/metahub_shared/rbac.py`：角色、Permission catalog、产品授权上限和规范 hash。
- `control-plane-api/src/metahub_control/permissions/`：GET/PATCH models、strict wire、矩阵完整性、
  CAS、审计、source event 与提交后 Webhook。
- `control-plane-api/src/metahub_control/auth/` 及直接受影响业务路由：逐请求数据库权限快照和稳定
  permission 守卫；Provider、价格、Usage/FinOps 等职责分离由后端执行。
- `control-plane-api/src/metahub_control/console/`：permission-based 导航和 PAGE-085。
- `console-web/src/lib/permissions.js`、`features/role-permissions/`、`app/main.js` 及受影响页面：
  精确权限快照、授权 owner、桌面矩阵、移动角色 Tab、dirty/no-op/CAS 状态。
- `scripts/acceptance/prd059-browser-acceptance.mjs`：仅允许三个 loopback origin 的真实 Chromium
  登录、矩阵、草稿、隐私、双视口截图和注销验收器；已使用本机 Microsoft Edge 正式通过。

## 3. 当前代码自动化结果

### 3.1 本轮直接结果

| 范围 | 结果 | 直接覆盖 |
| --- | ---: | --- |
| 0050 迁移专项 | `6/6 passed` | fresh/repeat、两向复合租户关系、admin 角色/关系不可变、0049 up/down/re-up、Webhook 与未知依赖 down guard |
| 权限事件 down guard 定点 | `1/1 passed` | 真实 PATCH 生成 Audit/source 后 0050 down 原子拒绝且事实保留 |
| PRD-059 认证/权限/路由 | `13/13 passed` | 六角色登录、旧 Token、降权、矩阵损坏、职责分离、strict wire、跨租户、CAS、事务回滚和路由映射 |
| Console 已修正直接回归 | `102/102 passed` | 12 个旧角色 fixture 改为显式 permission snapshot 后的页面/导航合同 |
| Console auth + PAGE-085 | `14/14 passed` | 精确快照、导航 fingerprint、GET/PATCH client、owner、immutable/grantable DOM |
| Console 直接受影响合计 | `116/116 passed` | 不重复运行前述 102 项；本轮只补跑剩余 14 项 |
| Console lint | `156 files passed` | 仓库 Console JavaScript/CSS 静态检查 |
| Python 语法 | `passed` | 两个本轮修改测试文件 `py_compile` |
| Browser 脚本语法 | `passed` | `node --check scripts/acceptance/prd059-browser-acceptance.mjs` |
| 中文注释门禁 | `passed` | 最终代码执行 `scripts/ci/check-comments.py`，结果为 `comment coverage ok` |
| PAGE-085 真实 Browser | `passed` | Edge 登录、6 角色/48 权限、Admin 锁定、草稿放弃、隐私、注销和双视口 2 张非空 PNG |
| 停服静止终态 | `passed` | 三端口、WAL/SHM、pending 权限事件均为 0；完整性和外键复核通过 |
| Ruff | `未执行` | 推荐 Python 环境没有安装 `ruff`；未联网安装、未申请权限，不能写成通过 |

本轮没有执行根级 Python 全量，也没有重复已经通过的旧迁移整批，符合用户“只跑 PRD-059 专项和
直接受影响回归”的约束。pytest 均设置 `TEMP/TMP=D:/MetaHub/.tmp`、使用独占 `--basetemp` 和
`-p no:cacheprovider`。

### 3.2 JUnit 证据

| 文件 | 结果 | SHA-256 |
| --- | ---: | --- |
| `.tmp/prd059-db-hardening-20260731a.xml` | `6 tests / 0 failure / 0 error` | `9F6F0B012DFE11856CF668B0D5DD3307187DD061B3CF5FD6526EBFA0B4A78445` |
| `.tmp/prd059-event-guard-20260731a.xml` | `1 test / 0 failure / 0 error` | `CD8C8D70EA3CCFA31579F3695490B34FB72E861F63BEAD77B6FDB551A0703626` |
| `.tmp/prd059-backend-final-20260731a.xml` | `13 tests / 0 failure / 0 error` | `CFB16E655EDB7B54053144CFF3C456632D3A7E1F550BC5B70628CA88E104AE39` |

## 4. 迁移与开发库验收

开发库：`D:/MetaHub/.data/metahub-dev-prd031.db`。

- 迁移前栈顶为 `0049/audit_log_immutable_export`。
- 正式迁移器运行成功，run ID 为 `dbm_f57c803676dc42c19c7774351cc50aef`，只新增 `0050`，
  `0001..0049` 全部 skipped。
- 迁移后栈顶为 `0050/applied`。
- `PRAGMA integrity_check=ok`，`PRAGMA foreign_key_check` 为 `0` 条违规。
- 默认租户真实投影为 `6 roles / 48 permissions / 125 role_permissions`。
- 0050 up/down 重新创建 PRD-058 AuditExport owner trigger 时保持 0049 的 conflict trigger 优先级；
  `INSERT OR REPLACE` 仍稳定收敛为 `audit_export_immutable`。

## 5. 真实三服务与 API 验收

运行地址：Gateway `http://localhost:8000`、Control API `http://localhost:8001`、Console
`http://localhost:5173`。三个服务均使用同一 0050 开发库在沙箱会话内启动。

| 流程 | 结果 |
| --- | --- |
| Gateway `/health` | HTTP 200，响应包含统一 `request_id` |
| Control API `/health` | HTTP 200，响应包含统一 `request_id` |
| Console `/` | HTTP 200 |
| 管理员真实登录 | HTTP 200，角色为 `admin`；Token 未打印 |
| `GET /permissions` | 48 权限、6 角色、125 关系；响应 `request_id=req_prd059_runtime_matrix` |
| no-op PATCH | `changed=false`、`event_id=null`、`request_id=req_prd059_runtime_noop` |
| 匿名 `GET /permissions` | HTTP 401、`auth.token_missing`；Header/Body 均为 `req_prd059_runtime_unauthorized` |

服务 startup 日志对 `METAHUB_DATABASE_URL` 和认证 Secret 均显示为 `masked`。实际日志没有 Provider
Key、Token、Prompt、Response 或 Cookie；本记录不复制登录响应和 Session ID。

## 6. Browser 与静止终态验收

### 6.1 历史失败闭环

1. 首选应用内 Browser 和 computer-use 共用的 UI 控制内核在执行浏览器代码前返回
   `failed to write kernel assets: 系统找不到指定的路径`；未申请权限或写入沙箱外目录。
2. Playwright 默认 Chromium 可执行文件不存在；系统 Chrome 又在 GPU 子进程启动阶段失败。
3. 这些失败目录继续保留为诊断证据，不能替代最终成功证据；未使用 `--no-sandbox`、
   `--disable-gpu-sandbox`、提权、`C:/tmp`、下载或外部网络。
4. 本轮复用桌面运行库中已经存在的 Playwright 1.60 和本机 Microsoft Edge。第一次使用
   `127.0.0.1:5173` 时因开发配置只允许 `http://localhost:5173` 精确 CORS Origin，登录阶段安全失败；
   改为产品配置中的 `localhost` 后，同一验收器直接通过，未修改业务代码或弱化浏览器沙箱。

### 6.2 正式 Browser 结果

权威目录：`.tmp/prd059-edge-acceptance-20260731b`。

- `browser-acceptance.json`：`status=passed`，SHA-256
  `48B8B088CBF470EF104230E5AAFA575E4EC15EF27A89965437F3503A58F0E262`。
- HTTP：登录 200、`GET /permissions` 200、注销 200；blocked request、外部请求、Page error 和
  Console error 均为 0。
- 矩阵：6 角色、48 权限、Admin 不可变；可编辑角色草稿修改后成功放弃，PATCH 请求数为 0。
- 隐私：URL/DOM/Storage 敏感命中为 0；注销后认证 Storage 和受保护 DOM 均已清理。
- 桌面 `1280x720`：页面宽 1280，无横向溢出、导航重叠或按钮文字溢出；PNG 非空，SHA-256
  `FF4DD30406FCA60A4208D7263E8A354AE1B9F5D3774205AC8CE1FD0E7F26A715`。
- 移动 `390x844`：页面宽 390，移动列表生效且桌面矩阵隐藏，无横向溢出、导航重叠或按钮文字溢出；
  PNG 非空，SHA-256 `2C358079FCB20706BB6FDCC99F5A2F1AF194014B7F888BD88BED06A6D3CE5D24`。
- 两张截图已逐张人工复核，页面非空，正文、导航和操作区无不一致重叠。

### 6.3 停服静止终态

- 正常验收完成后停止本任务启动的 Gateway、Control API 和 Console；8000、8001、5173 监听均为 0。
- 使用 SQLite 自身 `wal_checkpoint(TRUNCATE)` 收敛开发库，结果 `(0, 0, 0)`；WAL/SHM 均为 0。
- `PRAGMA integrity_check=ok`，外键违规 0；`role_permission_events` 没有 pending 事实。
- 正式 Browser JSON 中管理员密码、Bearer 和 Token 键名命中均为 0；证据目录只有一份 JSON 和两张 PNG。
- 静止验收未删除任何测试证据，未触碰用户禁止的两个 PRD-059 文件，也未创建 `.tmp/*.patch` 或 handoff。

## 7. 完成清单

- [x] 设计文档先于编码落盘，两轮编码前自审完成。
- [x] 六角色、表驱动矩阵、租户隔离、后端权限、统一错误和 request/trace ID 已实现。
- [x] 0050 fresh/历史/up-down-re-up、Admin 不可变、两向复合关系和 down guard 通过。
- [x] 主流程、异常、权限、跨租户、strict wire、CAS、no-op、事务和事件有直接测试。
- [x] Console permission snapshot、PAGE-085、受影响页面和 lint 通过。
- [x] 开发库迁移到 0050，三服务健康检查和真实 API 主流程通过。
- [x] 最终代码后的中文注释门禁复跑。
- [x] 1280x720 与 390x844 实际 Edge PAGE-085 浏览器验收及正式截图通过。
- [x] 最终静止终态、证据复核和两轮实现后收口复核完成。
- [x] README/目录已登记 PRD-059 完成，并从 Excel 两张核心表识别下一项为 PRD-060。

当前结论：PRD-059 **已完成**；全部 108 条 PRD 仍在进行，
`all_108_prds_complete=false`。
