# PRD-061 内部 Provider 状态页测试记录

状态：**通过，PRD-061 已达到完成门禁**。记录日期：2026-08-01。

本记录只陈述实际执行的 PRD-061 专项、直接受影响回归和运行证据；没有因局部缺口重复无关根级全量，
也不把未执行的根级结果写成全绿。全部 108 条 PRD 仍在进行，`all_108_prds_complete=false`。

## 1. 验收范围与环境

- 工作区：`D:/MetaHub`。
- Python：`C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。
- `PYTHONPATH`：`shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src`。
- pytest 的 `TEMP/TMP` 与 `--basetemp` 只使用 `D:/MetaHub/.artifacts/prd061-*`，并关闭 cacheprovider。
- 不使用工作区外临时目录、不访问外部网络、不提权；只执行 PRD-061 和直接受影响范围。

## 2. 设计与自审

权威设计：`docs/prd/PRD-061-internal-provider-status-page.md`。两轮编码前自审均为“修正后通过”：

| 轮次 | 关闭的主要边界 |
| --- | --- |
| 第一轮 | PAGE-088/Dashboard 分流、双权限、tenant 首条件、调用/流资格矩阵、独立 P95、完整探测分组、事件最小化 |
| 第二轮 | 严格分页 wire、坏终态失败关闭、历史流兼容、两阶段探测判窗、稳定排序、异步 owner、迁移原子守卫 |

运行收口又发现同一 request/trace、同一 `health_generation` 被两个独立探测事务复用时会产生重复序号。
四元分组修订、两条回归及 HTTP 复验已写入设计第 12 节。公开合同见 `docs/api/provider-status.md`。

## 3. 自动化结果

| 分组 | 最终直接结果 | 证据/结论 |
| --- | ---: | --- |
| Provider 状态集成专项 | `8 passed` | 主流程、租户、双权限、严格 query、维护备注、跨/同代次复用链路 ID |
| 本次分组最小回归 | `2 passed` | `.artifacts/prd061-provider-status-grouping-20260801.xml` |
| Provider 状态最终专项 | `8 passed` | `.artifacts/prd061-provider-status-final2-20260801.xml` |
| 0053 迁移专项 | `8 passed` | up/down/re-up、保真、索引计划、约束和原子 guard |
| Provider/Health/Audit 直接回归 | `12 passed` | 维护备注、探测和审计白名单未破坏相邻能力 |
| PRD-061 Console 与直接影响回归 | `44 passed` | PAGE-088、owner、筛选、事件、XSS 与 Dashboard 分流 |
| Console 全量 | 退出码 `0` | 全量 runner 通过；不从 dot 输出虚构用例数 |
| Console lint | `164 files passed` | 通过 |
| 浏览器验收器合同 | `10 passed` | 精确 query/POST/DTO/事件、DOM 替换、环境和 Storage 隐私、503 分类 |
| 中文注释门禁 | `comment coverage ok` | 正式源码根通过 |

专项第一次收口运行真实出现 `probe_total=3` 而期望 2。原因是旧测试夹具把同一事务的每个 Key attempt
写成不同 `created_at`，与正式 Provider Health 写入合同不一致。夹具改为组内共享 `created_at`、保留独立
`checked_at` 后，只重跑 Provider 状态专项并得到 `8 passed`；没有重跑无关全量。

## 4. 真实运行与 HTTP

- 非流式请求 `req_prd061_runtime_nonstream_final`：成功，`4 input / 6 output / 10 total`，Provider 为
  `local_openai`。
- SSE 请求 `req_prd061_runtime_stream_final`：`completed`，1 个持久 chunk、65 bytes；实际响应含
  `hello` 和 `[DONE]`。
- 人工探测 `req_prd061_runtime_probe_success`：`success`，494 ms。
- 状态投影 `req_prd061_status_projection_final`：HTTP 200，`Cache-Control: no-store`。
- 加载最新源码的临时 Control `127.0.0.1:18001` 直接读取开发库重复事实：HTTP 200、2 个 Provider、
  request/trace ID 与 no-store 均通过；验证后临时进程已停止。
- 清理受控重复事实后，既有 Control 8001 的 `/status/providers` 也返回 HTTP 200；Gateway、Control、
  Console 健康检查分别为 `200/200/200`。

Provider Key、Token、Prompt、Response 和上游错误正文没有进入状态响应、证据 JSON 或普通日志。

## 5. 权限、租户和异常

- admin/developer/ops_admin 的双权限读取通过；viewer/匿名由后端返回 403/401。
- 分别撤销 `provider.health.read` 或 `request_log.read` 后，旧 Session 下一请求立即 403。
- 另一租户 Provider、调用、流、探测和维护备注不进入列表、summary 或精确 ID 结果。
- 客户端 `tenant_id`、未知/重复筛选、非法状态以及带符号、浮点、空白、Unicode 或越界分页均返回本域
  422，并携带 request/trace ID。
- 缺表、坏时间、未知错误码、负延迟、计数矛盾和不可解释分组统一 503，响应不泄露 SQL、表名、路径或原值。
- `status.viewed` 只含固定导航、path 和 `source/hash/viewport_class`；Provider、筛选和 tenant 字段被拒绝。

## 6. Edge 页面验收

权威证据：
`D:/MetaHub/.artifacts/prd061-browser/run-20260801T040500-final/browser-acceptance.json`，`status=passed`。

- 1280x720：loading 与真实成功态；390x844：真实成功态。
- loading、真实 200、Provider 详情、筛选/重置、立即探测、模拟 503、空态、真实恢复、访问事件和注销通过。
- 3 张 PNG 均非空；两档视口横向溢出、导航遮挡和按钮裁切均为 0。
- 外部请求、page error、console error、敏感 canary URL/DOM/Storage 命中均为 0。
- Browser 子进程只继承启动必需环境白名单；Token 仅允许位于指定 Session Storage 槽位，注销后清空。

截图 SHA-256：

- loading desktop：`cfc4db093fc081eff6c580da998e92fcd937fffc4291638a744917d6c0cf5b34`
- success desktop：`8bb585966d237b75ac6c58e6cee797da76653644df8e8128adc4add49f2bd9b6`
- success mobile：`5cd8f61a5e4dced10b8623dce200945aaa97896a38e51b312135543bebeadcfb`

## 7. 迁移与收口

- 开发库迁移 `0001..0053` 共 53 条连续 applied；0053 checksum：
  `633a01523e67c730be3749f51de14327036462d6787f5c73412a58b96b96791b`。
- `PRAGMA wal_checkpoint(TRUNCATE) = 0,0,0`，`integrity_check=ok`，`foreign_key_check=0`。
- 核心服务保留运行供后续 PRD 使用：WAL 为 0 bytes、journal 不存在；SHM 仅由活动数据库连接持有，
  不代表待提交业务数据。
- 精确删除本任务 `req_prd061_generation_runtime_final` 的 2 条受控探测事实；真实调用与最终验收证据保留。
- 本任务时间窗内 3 个遗留 active Headless Edge Session 已撤销，2 个既有 revoked Session 保留。
- 临时 Control 和 fake upstream 已停止，8080 不可达；临时 Provider Key 已禁用，Provider Endpoint 已恢复。

## 8. 结论

TC-0121 的调用/流/探测聚合和 TC-0122 的租户、权限、异常、隐私均有自动化及真实运行证据；0053、
PAGE-088、Edge 双视口和运行收口全部通过。PRD-061 可以标记完成，但只代表这一条完成；
`all_108_prds_complete=false`。
