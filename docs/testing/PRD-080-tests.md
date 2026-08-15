# PRD-080 账单争议与调整流程测试记录

状态：**已完成**（2026-08-15）。两轮编码前设计自审、实现、两轮实现后自审与修正、专项和必要回归、
0073 空库迁移往返、真实 Runtime、PAGE-112 Browser、文档与提交前安全门禁均已完成。本结论只关闭
PRD-080；`all_108_prds_complete=false`，未启动或混入 PRD-081。

## 1. 权威范围与设计门禁

- Excel `01_PRD总表` 第 81 行、`03_M2-M3扩展交付` 第 17 行、PAGE-112、账单争议 API、
  BillingDispute/AdjustmentRecord、`billing_dispute.created` 与 TC-0159/0160 共同确认本条为
  `PRD-080 | M2-企业增强 | P2 | 扩展层 | 账单争议 | 账单争议与调整流程`。
- 冻结设计见 `docs/prd/PRD-080-billing-dispute-adjustment-workflow.md`。第 16、17 节保存两轮编码前自审；
  第 19、20 节保存两轮实现后并发、公平、恢复、权限、隐私、兼容和证据复核及修正。
- 所有业务表均 tenant-qualified；四个 API 逐请求执行后端 Session/RBAC，写事务再次验证实时权限。
  成功和错误均返回稳定错误码、request_id/trace_id，并由最外层路径中间件设置 `no-store, private`。
- 原 Usage/FinOps charge append-only；批准只追加 reversal Adjustment/Entry，暂缓使用独立 hold overlay，
  不调用支付、不退款、不暗中返还 PRD-079 额度。

## 2. 实现后两轮自审结论

第一轮关闭了 scheduler 必须使用全表单调领取序列、terminal delivery 必须同轮收敛 source failed、真实
Control 崩溃必须精确终止 owner 树，以及 Runtime 必须直接证明稳定 `X-MetaHub-Delivery` 的问题。修正后
恢复留下精确一次失败 attempt，租约可接管，坏 source 不阻塞其他 tenant，接收端按稳定 delivery ID 去重。

第二轮关闭了 Chrome launcher close 与真实 browser stdio EOF 错配、已退出 PID fallback、登录网络失败被
误报为 DOM timeout，以及 PRD-078/079 fixture 把 0071/0072 错认成永久最新迁移的问题。提交前复核继续关闭
未捕获异常 no-store、零金额 scope 503、自由文本经 `User-Agent`/客户端链路 ID 泄漏、未知异常 traceback 和
多 item Webhook 汇总误限等边界。Browser 清理现只针对 CDP browser PID，登录先过网络门禁再等 Shell/PAGE-112；
旧业务在完整 0073/RBAC revision 16 上通过。
两轮复核后没有未关闭 P0/P1。

## 3. 已通过的自动化门禁

以下分组存在明确重叠，不能相加冒充一次根级全量测试。

| 验证项 | 最终结果 | 主要覆盖 |
| --- | ---: | --- |
| PRD-080 Python 专项 | `58 passed` | Shared、Control API、0073、outbox/scheduler、Runtime/Browser 合同 |
| Console 全量 | `815/815 passed` | 全 Console 单元、PAGE-112 exact wire、owner、Dialog、权限与响应式投影 |
| PRD-080 Browser 合同 | `13/13 passed` | 固定路径、CDP owner、双流排空、loopback、exact facts、隐私和清理；已包含在 Console 全量 |
| PRD-078/079 当前链兼容 | `30 passed` | 旧审批与套餐/额度 Control 业务在完整 0073、RBAC revision 16 上运行 |
| FinOps + PRD-071 read model | `9 passed` | reversal、active hold 单行投影、既有输出安全 marker 兼容 |
| Webhook 核心 / resume | `8 passed` / `10 passed` | delivery 终态、stored source、重启恢复与稳定投递坐标 |
| 共享链路隐私兼容 | `6 passed` | 邀请 path、access log 与意图分类继续拒绝客户端链路 ID/敏感值回显 |
| PRD-074 scheduler + PRD-075 cleanup | `8 passed` | 相邻公平调度与 lifespan/owned-process 清理 |
| 静态门禁 | 通过 | Console lint `253 files`；29 个非基线变更 Python 文件全规则 Ruff、i18n PRD-080 新增区间 E501=0、Python compileall/py_compile、Node syntax |
| Git whitespace | 通过 | `git diff --check` |

## 4. 迁移与 Runtime final

权威目录：`.artifacts/prd080-runtime/prd080-runtime-final-20260815-05/`。

| 阶段 | migration count/version | integrity | 外键违规 |
| --- | --- | --- | ---: |
| fresh | `73/0073` | `ok` | `0` |
| repeat | `73/0073`，applied 0 | `ok` | `0` |
| down | `72/0072` | `ok` | `0` |
| re-up | `73/0073` | `ok` | `0` |

- 真实 loopback Provider/Gateway/Control/Webhook 均启动；4 个成功调用生成 4 条 canonical Usage/charge，
  1 个上游失败请求保持可追踪。
- request-scope 与 time-range 各冻结 2 项；批准 1、拒绝 1，部分批准只生成 1 条 Adjustment/reversal，
  4 条 hold 全部释放，6 条 transition、7 条 Audit、2 条事件事实精确。
- Control 崩溃前首次投递失败已经提交；重启后两个 outbox 均 succeeded。网络投递 4、逻辑事件 2、重复 2，
  接收端去重通过，稳定 delivery ID/签名/exact payload 均通过，pending lease 0。
- developer 403、跨租户 404、幂等冲突 409、动态撤权 403；owned 进程和固定端口释放、SQLite sidecar 0、
  8 个受控文件敏感明文命中 0。
- `runtime-acceptance.json` SHA-256：
  `74174e1a88553644e08a9584e54e1e866206b943873630c86ecf09fe7619bfd7`。

## 5. Browser final

权威目录：`.artifacts/prd080-browser/prd080-browser-final-20260815-05/`。

- `status=passed`；桌面 `1280x720` 与移动 `390x844` 实际操作 PAGE-112。request/time 两类申请各 1，
  start review 两次、部分批准 1、拒绝 1，list/detail/create/transition 均走真实 Control API。
- 申请人自审由后端拒绝；finance 权限动态撤销后导航、DOM 和迟到响应清理；登出清除 Storage。原生 Dialog
  初始焦点、正反 Tab wrap、idle Escape、busy Escape、disabled controls 和焦点归还均通过。
- 四张 PNG 已人工复核为非空、内容完整、自由文本保持折叠，无控件/文字重叠；两个视口页面级横向溢出、
  文本溢出和重叠计数均为 0。
- 外部请求、禁止 query、Console error、Page error 和敏感明文命中均为 0；数据库为 `73/0073`、
  `integrity_check=ok`、外键违规 0；5173/8001/18883/19083 最终均无监听。
- 11 项源码 provenance 与当前工作树 SHA-256 全部匹配；manifest 中 JSON 与四张 PNG 的字节数/摘要全部匹配。
- `browser-acceptance.json` SHA-256：
  `a119250da461f04e966c3867f017084d6048eaf53d56877a0fcfef2a653e5193`。
- `sha256-manifest.json` SHA-256：
  `c0d53161ee0c1463c1cd98cd07d0fa58cd18f8a92b62faf42e71d2935bb12a99`。

早期 final 暴露 launcher/stdio 排空、已退出 PID fallback 和登录诊断顺序问题；`final-04` 随后又被最终
隐私、错误边界和聚合金额修正淘汰。修正和合同测试完成后只以 Runtime/Browser `final-05` 为提交白名单。

## 6. 安全、禁止文件与范围门禁

- Runtime、Browser work/staging/final 的 Provider Key、Token、Authorization、Prompt、Response、reason、
  decision reason、客户端链路 ID、`User-Agent`、幂等键和测试账号明文命中均为 0；公开证据只含安全状态、
  计数、服务端 ID 与 SHA-256。
- 提交白名单不包含数据库、WAL/SHM/journal、日志、Browser profile/cache、失败诊断目录、Office 锁文件、
  临时脚本、真实 `.env`、凭据或符号链接。
- 迁移、Runtime、Browser JSON、设计、API、测试、README、目录说明和 Excel 均保持
  `all_108_prds_complete=false`。

## 7. 下一项交叉确认

Excel `01_PRD总表` 第 82 行、`03_M2-M3扩展交付` 第 18 行、PAGE-113、Webhook format API、
SecurityEventPayload、`security.event_pushed` 与 TC-0161/0162 共同确认，下一条权威未完成项为
`PRD-081 | M2-企业增强 | P2 | 扩展层 | Webhook SIEM | SIEM/安全事件格式`。本次只确认，不实现。
