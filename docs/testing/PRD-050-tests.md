# PRD-050 Prompt Cache 感知粘性路由测试记录

测试日期：2026-07-20 至 2026-07-26

当前状态：已完成。设计、两轮编码前自审、实现、0040、当前源码相关测试、真实 Runtime、真实 Browser、日志隐私、资源清理和停止后 SQLite 终态审计均已通过。权威批次为 `artifacts/prd050/20260725T005725`；该结论只表示 PRD-050 完成，不代表全部 108 条 PRD 已完成，也不表示 PRD-051 已启动。

运行环境：Windows PowerShell；Python 使用 `C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`；本轮所有 TEMP/TMP/TMPDIR 均位于 `D:/MetaHub/.tmp`。没有使用 `C:/tmp`、提权、CIM、`Get-NetTCPConnection` 或端口 owner 扫描。

## 1. 验收口径

- Excel `TC-0099`：按 tenant、Virtual Key、可信 data_class、endpoint revision 与长 Prompt 指纹形成隔离域；同域请求优先复用同一 Provider Key/节点并记录预计命中。
- Excel `TC-0100`：不实现语义缓存；覆盖缓存投毒、跨租户、无权限、异常输入、依赖故障、统一错误码和可追踪状态。
- 所有业务事实 tenant-qualified。Control 写配置只允许 admin；developer 只读；viewer 由后端拒绝。前端隐藏按钮或零 DOM 只是附加证据。
- API 成功/错误必须携带统一 `request_id/trace_id`；跨租户对象与不存在对象保持同形响应。
- Provider Key、Virtual Key、Token、密码、Prompt、Response、data_class、内部 digest、Credential/Binding/Scope ID 和 Endpoint URL 不得进入公开日志、DOM、URL、Storage、Webhook 或验收结果。
- 自动化、迁移、真实 Runtime、真实 Browser、日志审计、清理和静止数据库分别取证；任一缺失都不能标记完成。

完整语义、数据表、API、页面、错误码、安全边界及两轮自审见 [PRD-050 设计文档](../prd/PRD-050-prompt-cache-aware-sticky-routing.md)；公开 wire/RBAC/幂等/事件合同见 [CacheAffinity API](../api/cache-affinity.md)。

## 2. 设计与实现审查

- 第一轮编码前自审修正 Endpoint revision、独立 HMAC Secret、pending/warm 区分、Provider 前 fail-closed、流式终态、REPLACE/down guard、容量和事件变体。
- 第二轮编码前自审补齐 scope 指针、target/lease generation、跨 Alias TTL、动态模式软偏好、Candidate reason 保真、单 Gateway Secret 边界、启动恢复和 PAGE-073 隐私。
- 实现期交叉审查修复 Route 二次 terminal、迟到 callback、主错被 close 覆盖、cleanup 并发、Context close 后迟到浏览器错误、`page.unroute` 接纳窗口及 Chrome RegExp `v` 模式连字符。
- 最终 Browser 失败定位又发现验收器对 developer 已收敛 PAGE-072 重复导航，异步整壳重绘与首张截图发生竞态；修复只作用于验收器，不改变产品权限或业务 API。
- 最终源码把 Browser attempt 名称限制为 `^[a-z][a-z0-9_]{0,31}$`，重试写入同批次独立目录且保留旧失败证据；reader 的 ready/layout/capture 使用固定脱敏 stage。

## 3. 风险分层测试结果

本轮遵循“只验证相关变化”的策略，没有因 Browser 验收器局部修复重跑 921 条 Python 全量。旧全量结果只作为变更前基线，当前源码以定向合同、真实 Runtime/Browser 和终态审计闭合。

| 范围 | 结果 | 证据性质 |
| --- | --- | --- |
| Python 根级全量 | `921/921 passed`，failures/errors/skipped 均为 0 | 2026-07-24 变更前基线：`artifacts/prd050/failed-attempts/20260724T090322-process-contract/python-full-summary.json`；本轮未重跑 |
| Console 全量 | `354/354 passed` | 同一 2026-07-24 基线；本轮未重跑 |
| Browser/Routing 单测 | `71/71 passed` | 同一 2026-07-24 基线；本轮未重跑 |
| Node 示例 | `33/33 passed` | 同一 2026-07-24 基线；相关源码未变化 |
| Console lint | `118 files passed` | 同一 2026-07-24 基线；本轮未重跑 |
| 当前 Browser 修复合同 | `4/4 passed` | attempt 隔离、截图聚焦、developer 不重复导航、固定 stage/角色证据 |
| 当前 CacheAffinity + Dashboard | `15/15 passed` | 当前源码直接执行；另有 Dashboard 单项 `1/1` 证据 |
| 当前静态 verifier | 首轮 `20/21`，唯一旧 Browser SHA 绑定失败；三处同步后失败用例复测 `1/1` | 失败结果和 JUnit 保留；组合证明当前 21 条均通过 |
| Browser 脚本语法 | 退出码 0 | 当前源码直接执行 |
| 中文注释 | `comment coverage ok` | 当前源码最终门禁 |
| 真实 Runtime | `status=passed` | 当前权威批次 |
| 真实 Browser | `status=passed` | 当前权威批次 `browser-final_sandbox` |

当前定向原始输出位于 `artifacts/prd050/20260725T005725/targeted/`。首轮 verifier 失败未被覆盖：`static-verifier-attempt1-junit.xml` 记录 21 tests/1 failure；`static-verifier-rerun-junit.xml` 记录修复后 1 test/0 failure。

## 4. 0040 与静止数据库

- 当前迁移源 loader checksum 为 `dc4289d44091e231f2c288398c58ab8eccc075fd7bcbbc869f2b7b7c8257f090`，与运行库 `schema_migrations` 完全一致；原文件字节 SHA-256 为 `03094c3fb55d35d76d3a0732b500d120ae1d8f93ed15ba586934aa91d352ac3c`。
- 旧测试记录的 `635b...` loader checksum 和 `dbe181...` 文件 SHA 已确认是迁移后续加固前的陈旧值。首次终审因此失败并保留 `final-database-audit-stale-doc-checksum.exitcode`，随后以当前源/loader 规则重新审计通过。
- 运行库共有 40 条 applied migration，最高 `0040 prompt_cache_affinity_routing`；五张 CacheAffinity 表、8 个运行时关键 trigger、10 个结构关键 trigger、29 个名称含 `prd050` 的 trigger 和两个历史保真索引均存在。
- `integrity_check=ok`、外键违规 0、pending Selection 0、active selection 合计 0、live lease 0、started attempt 0、active stream 0、临时 Browser 用户/Session 0。
- checkpoint 为 `[0,0,0]`，WAL/SHM/journal 均不存在；最终数据库 4,136,960 bytes，SHA-256 为 `5b895e79fc11dda8c77d1a9131bebc407ae9e68acc6102bf2883d742a25238c7`。
- 正反迁移、空事实 up/down/up、非空 down 拒绝、Candidate reason 保真、REPLACE/rowid/跨租户守卫和失败原子回滚已由 PRD-050 专项及 2026-07-24 全量基线覆盖。

权威终态文件：`final-database-audit.json` 与 `final-database-audit.exitcode`。

## 5. 真实 Runtime

- `runtime-state.json` 与 `runtime.stdout.json` 均为 `passed`，三服务在 Runtime 前健康检查均为 HTTP 200。
- 本地 OpenAI-compatible 上游收到 5 次真实 Provider 调用；数据库形成 4 条 Selection 和 5 个唯一 Webhook 事件。
- 相同长 Prompt 的 pending 复用、warm 复用、disabled Key 重绑、短 Prompt 旧 KeyPool 路径、fixed 不重排和 Provider `cache_tokens` 独立性均通过。
- admin 读取/PATCH 通过；developer 只读；viewer 在正文解析前拒绝；跨租户 Alias 同形隐藏。
- 配置版本冲突、同 request_id 异意图冲突、幂等重放、严格 extra 拒绝、configuration_changed 与 runtime selection HMAC 事件均通过。
- 凭据 cleanup 完成并复核；Runtime 结果只公开 ID/布尔/计数，不保存 Key、Token、Prompt 或 Response。

## 6. 真实 Browser 与 RBAC

最终重试：`browser-final_sandbox/browser-result.json`，退出码 0，结果 SHA-256 为 `d9320823e04fd241b70e1accd288902e7cce86e66467ed39aadcd7a79467697e`。

- admin/developer/viewer 截图为 `30/12/3`，共 45 张；1280x720、900x800、390x844 各 15 张。
- 45 张实际 PNG signature/IHDR、尺寸和 bytes 均与元数据一致；45 个实际 SHA 全部唯一且与结果一致；`nonBlank=true`、`privacyMaskApplied=true` 均为 45/45。
- 45 条 layout evidence 的 horizontal overflow、clipped element 和 overlap 均为 0。
- admin 覆盖 PAGE-072 loading/empty/configured/editing/saving/conflict/error 与 PAGE-073 pending/warm/rebound；developer 覆盖 PAGE-072 只读和 PAGE-073 三态；viewer 对两受限页面均回退 overview。
- 后端 RBAC：admin write/detail 200；developer write 403、detail 200 且 target write 0；viewer list/write/detail 均 403，target request/response 0。
- 三角色均 logout 200 且认证 Storage 清空；两个临时用户禁用，额外需撤销 Session 为 0；配置恢复、请求事实未改、FK/integrity 均通过。
- DOM/Storage/URL 敏感命中、外部请求和 browser error 均为 0。
- Browser stderr 仅 169 bytes Node SQLite ExperimentalWarning，不含敏感信息且退出码为 0。
- 本地图片查看器因 sandbox setup refresh 未能额外打开 PNG；该动作未被伪称完成，也不作为通过条件。真实 Chrome 截图、PNG 像素合同和布局合同已经自动化验证。

## 7. 日志、清理与停服

- 六份服务日志在 PTY 换行重组后共有 2,044 条结构化记录、2,040 条访问记录、8 条 Uvicorn 生命周期记录和 343 条 Console 访问记录。
- 结构化/访问 5xx 为 0，ERROR/CRITICAL 为 0；21 条 WARNING 均为验收器允许的受控 warning。
- 精确管理员口令、环境 Secret、Bearer、Virtual/Provider Key、Webhook Secret、加密串、Prompt/Response sentinel、敏感 JSON 字段和同批次非服务日志命中均为 0。
- 六份日志独占打开成功，locked count 0。Gateway、Control、Console 先以 `Ctrl+C` 停服；两个已核对启动时间/路径但滞留清理的 Python PID 和一个 Console PID 仅按已知身份终止，没有执行系统级端口或进程 owner 扫描。
- 三服务停服后端点均不可达；SQLite 二次 checkpoint 后 sidecar 为 0。

权威文件：`final-log-audit.json`、`final-visual-audit.json` 及各自 exitcode。

## 8. 失败证据保留与修复闭环

- 原 `browser/` 失败保留：admin 30 PNG、developer/viewer 0；日志证明 developer 初始只读行已成功，重复点击当前 `routing-policies` 后异步重绘与首张截图竞态。
- `browser-final-retry.*` 记录第一次本地启动管道未送入口令即 EOF 的失败；没有进入 Browser 流程，也没有业务状态变更。
- 数据库首次终审因旧文档 checksum 失败，日志终审的多次失败分别来自 PTY JSON 逐行假设、PowerShell tokenization 和 StrictMode 可选字段访问；所有失败 exitcode 均以描述性文件名保留。
- 修复后结果写入独立 attempt/正式 audit 文件，旧失败文件未删除、覆盖或挪作成功证据。
- 测试方案始终限定 `D:/MetaHub`；没有为临时目录、浏览器、pytest 或停服请求沙箱外权限。

## 9. 最终制品

- Runtime：`runtime-state.json`、`runtime.stdout.json`、`runtime.exitcode`
- Browser：`browser-final_sandbox/browser-result.json`、45 PNG、`browser-final-sandbox.*`
- 当前相关测试：`targeted/`
- 数据库：`final-database-audit.json`
- 日志：`final-log-audit.json`
- 视觉：`final-visual-audit.json`
- 汇总：`final-manifest.json`
- 设计/API：`docs/prd/PRD-050-prompt-cache-aware-sticky-routing.md`、`docs/api/cache-affinity.md`

## 10. 已知边界

- 本期只承诺单 Gateway 写节点和一个有效 CacheAffinity HMAC Secret；多实例 Secret 协调不在 PRD-050 范围。
- 本期实现路由/Key 亲和与预计复用，不实现语义缓存，不改写 Provider 实报 `cache_tokens` 或成本事实。
- OpenRouter、硅基流动仍是竞品和迁移兼容对象，不是默认上游 Provider。
- SQLite 是当前 POC 运行基线；PostgreSQL 方言与多节点并发语义需要后续独立验收。

## 11. 结论

PRD-050 已满足设计、两轮自审、实现、中文注释、租户隔离、后端权限、统一错误码与 request_id、0040、主/异常/权限测试、开发文档、真实 Runtime/Browser、日志隐私、清理和静止数据库验收要求，可以标记为“已完成”。总体 108 条 PRD 目标仍在进行中；本记录不预判或启动下一条 PRD。
