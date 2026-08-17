# PRD-086 客户健康评分测试记录

日期：2026-08-17

结论：PRD-086 的 0079、RBAC revision 21、`GET /customer-health`、每小时 Scheduler、EVT-098、
PAGE-118、迁移往返、真实 API 与双视口 Browser 均已通过本记录所列门禁。PRD-087 未启动；
`all_108_prds_complete=false`。

## 1. 范围与自审

- Excel `01_PRD总表` 第 87 行、`03_M2-M3扩展交付` 第 23 行、PAGE-118、API-392/393、OBJ-117、
  EVT-098 与 TC-0171/0172 共同冻结本条，不按编号猜测范围。
- 编码前两轮自审已关闭 GET 写入、账单争议冒充工单、changed-only 事件、整数最大余数、风险下限、两阶段租约、
  stable ID、strict DTO、迟到响应和证据可判定性问题，无未关闭 P0/P1。
- 所有评分、Scheduler、outbox、Webhook 和 API 均 tenant-qualified；权限由后端逐请求校验，前端导航只负责呈现。
- Provider Key、Token、Prompt、Response、Endpoint、用户明细和异常正文不进入评分、事件、日志或正式证据。

## 2. 自动化门禁

| 分组 | 结果 | 覆盖 |
| --- | ---: | --- |
| Shared PRD-086 | `36/36 passed` | RBAC revision/hash、错误目录、EVT-098 exact envelope 与敏感字段拒绝 |
| Control PRD-086 | `7/7 passed` | 公式、风险下限、纯读 GET、租户/RBAC、Scheduler/Outbox、idle writer 保护 |
| Migration 0079 | `12/12 passed` | fresh/repeat/down/re-up、schema、RBAC、公式、租约、down guard |
| Console PAGE-118 | `14/14 passed` | strict client、owner/ticket/generation、迟到响应、390/1280/1440 布局 |
| PRD-085/086 Python 相邻回归 | `92/92 passed` | Shared、Control、迁移及相邻 Product Analytics 合同 |
| Console 受影响回归 | `61/61 passed` | PAGE-117/118、权限、Shell、i18n 与 Browser 合同 |
| Console lint | `279 files` | 当前 Console 源码和测试静态检查 |

最终 Ruff 精确改动范围、Python compileall、`git diff --check` 与 Excel ZIP/openpyxl 校验均通过；
敏感信息、禁止文件和 staged diff 审计在提交前门禁中复核。不把未执行的根级全量写成全绿。

## 3. Migration 0079

- 从空 SQLite 实际完成 `79/0079 -> 79/0079 -> 78/0078 -> 79/0079`。
- fresh、repeat、down、re-up 每阶段均为 `integrity_check=ok`、外键违规 0。
- 0079 创建 tenant-qualified append-only `customer_health_scores`、source outbox 和 tenant Scheduler 游标，安装
  owner、safe-count、公式/hash、唯一来源、CAS/lease、changed-only 事件与 down 原子阻断。
- seeded 往返恢复 RBAC revision 20 后再稳定升级 revision 21；新旧 tenant、自定义角色和矩阵 hash 均精确验证。

## 4. 真实 Scheduler 与 API

权威批次：`.artifacts/prd086-runtime/prd086-live-20260817-05/`

- 使用空 SQLite 完成迁移、repeat、0079 down/re-up 和 seed，真实启动 Control/Console 与全部 lifespan Scheduler。
- 三个 tenant 的首次评分 source outbox 最终均为 `sent`；PRD-086 空轮询优化后启动日志中
  `customer_health.scheduler_failed=0`，并新增回归断言 idle Scheduler/Outbox 不执行 `BEGIN IMMEDIATE`。
- 真实 HTTP 验收覆盖 200/401/403/422；隔离数据库上的真实 Control HTTP 覆盖
  `503 customer_health.unavailable`，所有路径均验证 `no-store, private` 和头体 request_id 关联。
- healthy、high-risk、no-samples 三个身份只读取自己的 tenant；三次 GET 前后评分/outbox/scheduler 计数均为
  `3/3/3`，证明 GET 纯读。
- `runtime-acceptance.json` SHA-256 为
  `56fd2262be07a3b8f76a3cdc71675091f033f10576ad38c13fa96e8393ff9756`；
  `api-acceptance.json` SHA-256 为
  `80bb42199b38d095ea65b8117a10bc091c9315e263046d53d623cd43ef662df9`。

真实全 lifespan 同时暴露出多个历史 Scheduler 的 SQLite 空轮询写锁竞争；PRD-086 本身已修为 idle 只读并验证为
0 次 scheduler failure。为避免把历史模块竞争混入页面视觉证据，Browser 批次关闭 uvicorn lifespan，但保留完整
Control 路由、认证、RBAC、中间件、数据库和 Console；Scheduler 的真实运行证据由本节独立承担。

## 5. Browser PAGE-118

权威批次：`.artifacts/prd086-runtime/prd086-live-20260817-06/`

- 真实 HTTP Console/Control 在 `1280x720`、`1440x900`、`390x844` 生成 healthy、high-risk、no-samples 和
  tenant-isolation 四张 PNG，主 Agent 已逐图复核非空、无遮挡和无横向溢出。
- healthy 为 `83.89%`，high-risk 为 `39.44%/at_risk`，no-samples 为 `33.89%` 并显示冻结规则提示；
  工单信号始终为 unavailable。
- viewer 无 `customer_health.read`：导航计数 0、面板计数 0；稳定观测窗口服务端客户健康请求行保持 `18 -> 18`，
  证明未授权身份零预取。浏览器 Console error、Control warning 与 PRD-086 Scheduler failure 均为 0。

| 文件 | bytes | SHA-256 |
| --- | ---: | --- |
| `browser-acceptance.json` | 1211 | `215c73b2ab40064f06094d8bfdda86a8878e6a6a050d08c92d0dd475f8bac74f` |
| `healthy-1280x720.png` | 70212 | `6ded26bde4a6035e27c08c78cf898e461ce53db5436588dd92df6643a958fafc` |
| `high-risk-1440x900.png` | 96377 | `6df5e8d9f342a77aeadb44f8d0e010903a76d7640244e5dd2d5c922cf3d5f422` |
| `no-samples-390x844.png` | 32194 | `373824dec96d875d71c3c77f5033650fc81a0401f10867c5e11a75edf875074d` |
| `tenant-isolation-390x844.png` | 33966 | `640ef0d2fec161da736de1dc637965d35303673188baffc01cf63f2e90e90c27` |

## 6. 隐私、禁止文件与资源审计

- 正式 Runtime/Browser 批次 4 个 JSON 中，Provider Key、Token、Prompt、Response、Endpoint、认证头、密码、
  Secret 等敏感键和已知测试凭据命中为 0；PNG 只保留已目视复核的安全聚合视图。
- 候选提交 31 个路径中 `.env`、SQLite/sidecar、日志、cache、coverage、build、Office 临时文件、私钥和失败诊断
  artifact 命中为 0；工作簿 ZIP CRC、openpyxl 重开及 `all_108_prds_complete=false` 均通过。
- 最终 owned 服务、8001/43187 监听、PRD-086 launcher、pytest basetemp、SQLite 与 sidecar 已归零；
  `.artifacts` 只保留本记录引用的正式 Runtime 与 Browser 批次。

## 7. 下一项交叉确认

Excel `01_PRD总表` 第 88 行、`03_M2-M3扩展交付` 第 24 行、PAGE-119、API-394..399、OBJ-118..120、
EVT-099 与 TC-0173/0174 共同确认下一条权威未完成项为：

`PRD-087 | M2-企业增强 | P2 | 扩展层 | 伙伴门户 | 渠道伙伴与白标门户`

本次只登记，不实现 PRD-087；`all_108_prds_complete=false`。
