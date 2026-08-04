# PRD-056 Provider 错误归一化规则库测试记录

测试日期：2026-07-29 至 2026-07-30

当前状态：已完成。设计与两轮编码前自审、实现后两轮复核、0046、主流程/异常/权限/租户专项、
最终回归、真实 Runtime、Browser、中文注释门禁和停服静止终态均通过。该结论只适用于
PRD-056，`all_108_prds_complete=false`。

运行环境：Windows PowerShell；Python 使用
`C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。
所有 pytest 的 `TEMP`、`TMP`、`--basetemp` 和缓存均位于 `D:/MetaHub/.tmp`；未使用
`C:/tmp`，未请求提权。局部失败只重跑原失败 node 及其直接兼容回归，没有从头重跑已通过全量。

权威证据目录：`artifacts/prd056/20260730T022600/`。

## 1. 验收口径

- Excel 权威项：`PRD-056 - Provider错误归一化规则库`，`M1-企业POC / P1`。
- 页面/API/对象/事件：`PAGE-081`、`PAGE-082`、`GET/POST /error-maps`、
  `PATCH /error-maps/{error_map_id}`、`ProviderErrorMap`、`error_map.updated`。
- `TC-0111`：覆盖 401、403、普通 404、模型不存在、429、余额不足和 5xx；修改规则后
  Gateway 不重启即可使用新 revision，普通 Endpoint 404 与模型不存在严格分离。
- `TC-0112`：按 `request_id/trace_id` 追踪到冻结规则 Fact；原始 Provider 正文、Prompt、
  Response、Key、Token、Header 和 Endpoint 不进入日志、API、Webhook、Audit、DB 或 DOM。
- admin 可写、developer 只读、viewer 后端 403；viewer 前端无导航、无 DOM、无预取。
- tenant 只来自可信认证上下文；规则、Provider、attempt/session Fact、操作账本和内部快照
  均执行 tenant-qualified owner 校验。
- 失败请求保留统一错误码、`request_id/trace_id`、RoutingAttempt/StreamSession、用量与成本终态；
  规则同步失败只能降低诊断精度，不能绕过鉴权、预算、DLP 或路由。

完整合同见 [PRD-056 技术设计](../prd/PRD-056-provider-error-normalization-rule-library.md)
和 [Provider 错误归一化规则 API](../api/provider-error-maps.md)。

## 2. 设计与实现后复核

- 编码前第一轮关闭不可覆盖 guard、HTTP/output 矩阵、原始正文持久化、不可变 versions/head、
  租户 owner、Source Outbox 和幂等顺序等 P0/P1。
- 编码前第二轮关闭 attempt/session 二选一 Fact、归一码与 owner 终态双码、revision-0、滚动
  builtin catalog、旧 Adapter 签名、wire 有界读取、cache singleflight、Console stale 409 和
  Runtime/Browser 同批证据等 P0/P1。
- 实现后第一轮逐项复核后端 RBAC、可信 tenant、SQL owner、Fact 事务、Source Outbox、Audit、
  统一错误和 request_id；未发现未关闭 P0/P1。
- 实现后第二轮复核 Gateway 请求期单次 lease、缓存取消/关闭、原文脱敏、PAGE-081/082 owner、
  对话框焦点、注销清理和历史迁移兼容；发现的旧迁移测试栈顶假设均按具体失败 node 修正并闭环。

## 3. 测试资产

| 范围 | 节点/文件 | 主要覆盖 |
| --- | ---: | --- |
| Shared matcher | `28` | builtin canonical hash、规则编译、确定顺序、安全 guard、404/模型/余额分类、原文脱敏 |
| Shared 运行集成 | `31` | ErrorSpec、双语、诊断、Webhook、AppConfig、wire 与旧调用兼容 |
| Control 0046 | `8` | 空库/升级/幂等/down-up、owner、append-only、Fact、Webhook、未知后续依赖 |
| Control API | `5` | admin/developer/viewer/anonymous、租户隔离、严格 wire、CAS/幂等、内部 200/304 |
| Control DTO | `4` | PAGE-082 attempt/session Fact、迟到 revision/catalog、同形错误与隐私字段 |
| Gateway Fact | `2` | owner 二选一、同事务提交/回滚、反向冻结 |
| Gateway cache | `8` | 首次非阻塞、singleflight、200/304、LKG、上限、取消与有界 close |
| Gateway 归一化 | `5` | 旧签名、单次 lease、主备同引用、stream/nonstream Fact 与本地错误排除 |
| Runtime 合同 | `15` | 回环边界、迁移/隐私、真实上游/Webhook、清理、失败关闭证据写入 |

上述 `106` 个 PRD-056 Python node 全部包含在最终 `1472` 个唯一 node 的分段覆盖中。

Console 资产：`errorMaps.test.mjs` 12 个、`errorMapDialogFocus.test.mjs` 3 个、
`prd056BrowserAcceptance.test.mjs` 10 个；同时覆盖 RequestLog、导航、注销和模型评分既有回归。

## 4. 最终回归与定向闭环

### 4.1 Python

根级收集结果为 `1472` 个唯一 node。第一次根级进程在前 433 个节点全通过后被环境中止，
退出码为 `-1`；没有把该历史改写为一次 `1472 passed`，而是按收集顺序无重叠续跑：

| 分段 | 初次结果 | 定向闭环 |
| --- | ---: | ---: |
| 根级前段 | `433 passed`，随后进程中止 | 无失败 node |
| 续跑前 144 | `140 passed / 4 failed` | 长临时路径改为短 `basetemp`，原 4 node `4/4 passed` |
| batch 01 | `99 passed / 1 failed` | 旧 Request 替身补 `state`，原 node `1/1 passed` |
| batch 02 | `100/100 passed` | 无 |
| batch 03 | `48 passed / 52 failed` | 0046 栈顶兼容修正后代表 `1/1`、其余 `51/51 passed` |
| batch 04 | `100/100 passed` | 无 |
| batch 05 | `79 passed / 21 failed` | PRD-053/054/055 迁移 helper 兼容 0046，代表 `3/3`、其余 `18/18 passed` |
| batch 06 | `100/100 passed` | 无 |
| batch 07 | `100/100 passed` | 无 |
| batch 08 | `100/100 passed` | 无 |
| batch 09 | `95/95 passed` | 无 |

收集列表、`433 + 144 + 8*100 + 95 = 1472` 的分段切片和每个定向失败集合由
`final-audit.json` 自动验证；各分段参数文件互不重叠且完整覆盖收集列表。

### 4.2 Node、Console 与静态门禁

- Node 示例：`33/33 passed`。
- Console 一次全量真实结果：`451 = 450 passed / 1 failed`；失败是旧模型评分测试的源码切片
  误包含随后新增 PAGE-081 loader。切片边界修正后只跑对应文件，`10/10 passed`，未重跑全量。
- Console lint：`147 files passed`。
- 最新源码中文注释门禁 r3：`comment coverage ok`。

## 5. 数据库迁移与运行验收

- 运行库包含 `46` 条 applied 迁移，最高 `0046`；0046 canonical SHA-256 为
  `d278ea3aab5257e4d4faed0124937af8fac6ee092f76361da878842431208731`。
- `PRAGMA integrity_check=ok`，`foreign_key_check` 为 0 行；0046 空库、0045 -> 0046、重复 apply、
  受保护 down/up、append-only、owner 与未知后续依赖均有测试。
- Runtime：`status=passed`，SHA-256
  `72FC7B860E987BED60F5F441EA4465A069937DB091C5E6C45F789EBAA0EFB245`。
- Runtime 真实验证三服务健康、三角色、第二租户、规则 CRUD/CAS/幂等、无需重启热更新、
  401/403/普通 404/模型不存在/429/余额不足/5xx、真实流式非 2xx、10 个可追踪失败请求、
  2 个 Source Event 和 2 次签名 Webhook 投递。
- Runtime 隐私扫描覆盖运行库/sidecar 和 6 份服务日志，共 12 个动态敏感标记；Provider 正文、
  Prompt、密码、Session/Virtual Key、Webhook secret 和 Endpoint 未泄漏。

## 6. Browser 页面验收

- Browser：`status=passed`，SHA-256
  `4CC280D0878F66DB191180D6D52F8BAA65B7C4DE727EC5B73202A9CE9D0BB380`，并绑定同批 Runtime hash。
- 正式截图 6 张：1280x720 admin 列表、409 冲突、developer 只读、PAGE-082 Fact、viewer 无导航，
  以及 390x844 admin 编辑对话框；文件 hash、尺寸、非空像素均通过。
- admin 可写、developer 控件只读且后端 POST 403、viewer 无导航/DOM/预取且后端 GET 403。
- 验证三 request-id stale 409、最新版本接纳、新 request 成功、attempt/session Fact 展示、
  原始正文缺失、焦点 trap/Escape/恢复、Storage/URL 隐私和三角色注销清理。
- blocked request、unexpected Browser error、unexpected Console error 和 viewer prefetch 均为 0；
  3 条预期 HTTP Console error 与 403/409 用例一致。

## 7. 静止终态与链接清理

- 精确撤销 Browser 遗留的唯一 active Session 后，active Session 为 0；19 个 PRD-056 临时用户
  全部 disabled，临时非 revoked Session 为 0。
- Source Outbox `12/12 dispatched`，lease residue 0；Webhook `2/2 success`；两个 spool 均为 0。
- Gateway/Control/Console 的 8000/8001/5173 端口均关闭。
- `wal_checkpoint(TRUNCATE)` 返回 `0|0|0`；连接关闭后 `-wal/-shm/-journal` 均不存在。
- 全工作区 `.tmp` 预检识别 584 个链接节点：580 个 pytest `current`、3 个 JavaScript 安全夹具
  SymbolicLink、1 个静态归档安全夹具 Junction。仅用非递归文件/目录 API 删除这些节点；
  584 个目标全部保留，普通目录删除数 0，剩余 ReparsePoint 0。
- 历史 `pytest-prd054-migration-main-r1` 经属性确认是普通目录；因沙箱不可遍历而精确跳过，
  未提权、未删除，也未把它误判为链接。

链接清理证据 SHA-256：
`6256AA21F027EB17F7C4E345DB3F9367EBA2CB2AEDE03AD6C2D3B78ABD7F59F5`。

## 8. 最终结论与证据

最终审计 [final-audit.json](../../artifacts/prd056/20260730T022600/final-audit.json)
为 `status=passed`，SHA-256
`1BAC341FD4FDEEA2ED4CDB32508F667D8CFB171952D0748F0D3AA458BA0B224B`。
Runtime、Browser、运行库、正式截图、回归日志和链接清理明细均位于
`artifacts/prd056/20260730T022600/`，复制前后关键文件 hash 完全一致。

PRD-056 已满足设计、实现、测试、文档、迁移和实际运行验收门禁，可以标记完成。下一项为
`PRD-057 | M1-企业POC | P1 | Webhook死信与持久重放`；总体目标仍在进行，
`all_108_prds_complete=false`。
