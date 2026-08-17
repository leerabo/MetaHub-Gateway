# PRD-085 产品使用埋点与激活漏斗测试记录

日期：2026-08-17

结论：PRD-085 的 0078、RBAC revision 20、七类 ProductEvent 来源、EVT-097、Control API、PAGE-117、
Runtime、Browser、迁移往返、隐私与资源清理均已通过本记录所列门禁。诊断失败批次不作为通过证据。本条完成不代表
108 条 PRD 全部完成，`all_108_prds_complete=false`。

## 1. 范围与完成依据

- Excel `01_PRD总表` 第 86 行、`03_M2-M3扩展交付` 第 22 行、PAGE-117、API-389..391、OBJ-116、
  EVT-097 与 TC-0169/0170 共同冻结本条，不按编号推测范围。
- ProductEvent、source outbox、subscription/delivery、来源 request/audit/seed 事实均携带或由复合 owner/FK
  追溯 `tenant_id`；API 每次请求重新执行后端 RBAC，前端 capability 只用于呈现。
- 失败请求、Usage 与成本继续由既有 RequestLog/Usage/FinOps 链路追踪；失败行为不推进漏斗。
- Provider Key、Token、Prompt、Response、上游异常正文不得进入 ProductEvent、日志、JSON、manifest 或截图。

## 2. 设计与实现审查

- 编码前第一轮关闭整数基点舍入、错误幂等、配置导入来源、未知 query 和首次成功语义问题。
- 编码前第二轮关闭注册来源真实性、订阅快照、down 外部依赖、事件计数语义和后端/前端本地化耦合问题。
- 实现后第一轮关闭混合 UTC 排序、迁移 dead-letter/阻断覆盖、PRD-074 陈旧权限计数及 Gateway 连接代理问题。
- 实现后第二轮关闭 Runtime 路径/锁/限流/RBAC/WAL 清理和 Browser Fetch/Network 响应竞态、隐私坐标问题。
  最终无未关闭 P0/P1。

## 3. 自动化门禁

| 分组 | 最终结果 | 覆盖 |
| --- | ---: | --- |
| PRD-085 Python 联合专项 | `104/104 passed` | Shared 45、0078 13、Control 4、Browser harness 8、Runtime 34 |
| Shared PRD-074 相邻完整文件 | `9/9 passed` | revision 20 增量权限与 batch 权限边界 |
| Control 相邻回归 | `18/18 passed` | RequestLog、Provider、Virtual Key、预算、邀请、Webhook、导航 |
| Gateway RequestLog 相邻完整文件 | `11/11 passed` | 成功/失败、Usage/成本、部分写入回滚与连接状态 |
| Console 全量 | `894/894 passed` | PAGE-117 与既有 Console 页面回归 |
| Console lint | `274 files` | Console 源码和测试静态门禁 |
| Browser Node driver | `9/9 passed` | 双视口、四 PNG、网络 guard、401/403/迟到响应、隔离 Chrome 与 owner 回滚 |
| PRD-085 定向中文注释 | `21 files + composite additions passed` | 独立文件全量及合并文件本条新增定义/关键分支 |
| Ruff（按设计排除既有 E501） / compileall / Node check / diff check | `passed` | PRD-085 新增和受影响源码、语法、格式及补丁完整性 |

以上分组存在重叠，不相加冒充一次根级全量。开发阶段只运行受影响专项，完成门禁才扩展到 Console 全量和
Shared/Control/Gateway 相邻回归。并行 pytest 的系统临时目录争用和一次子目录启动造成的迁移相对路径失败均为
非权威环境诊断；正式结果使用仓库根目录和工作区独立 `--basetemp` 复跑。

全仓 `scripts/ci/check-comments.py` 仍会报告 PRD-080～084 合并大文件中的既有注释债务；本条没有借 PRD-085
大范围改写旧 PRD，也不把定向通过虚写为全仓通过。PRD-085 的 21 个独立源码/测试/迁移文件及合并文件本条新增
定义、tenant/RBAC/owner/错误分支均已按同一检查器定向通过。

## 4. Migration 0078

- 空 SQLite 正式验收实际完成 `78/0078 -> 78/0078 -> 77/0077 -> 78/0078`；各阶段均要求
  `integrity_check=ok`、外键违规 0，checksum 一致。
- 0078 创建 tenant-qualified ProductEvent 与 source outbox，安装来源投影、owner、append-only、唯一来源、
  快照成对、租约/终态 CAS 与 down guard；seeded 往返恢复 revision 19 后再稳定升级 revision 20。
- subscription、delivery、attempt、dead-letter、RBAC drift 或 live 事实存在时，down migration 在首个 DDL 前
  原子阻断，不留下部分 mutation。

## 5. Runtime 正式验收

权威证据：`.artifacts/prd085-runtime/prd085-runtime-20260817T055917Z-f41d66e4.json`

- JSON `status=passed`，bytes `2920`，SHA-256
  `9bc26bcd9dc04a18b9628be5a689fe9492ee2e4ed99f6a623dffe36397ee61d6`；配套 `.sha256` 与复算值一致。
- 真实 Control、Gateway、本地 OpenAI-compatible stub、Webhook receiver 和 Console HTTP 通过；七阶段全部完成。
- Gateway 成功 1、失败 1；Usage 1、12 tokens、成本为正；source outbox sent 6、Webhook success 6。
- 服务日志、数据库和正式证据敏感 canary 命中 0；sidecar、端口、进程和工作目录清理完成。

## 6. Browser 正式验收

权威批次：`.artifacts/prd085-browser/prd085-browser-20260817T073524Z-final-r2/`

- `browser-acceptance.json`：`status=passed`，七阶段、授权 PAGE-117、动态撤权、未授权零预取、第二租户隔离、
  九次 query-free GET/no-store 和零外部请求、零 Console/page error 全部通过；状态序列固定为
  `200,200,200,403,200,401,200,401,200`，实际覆盖当前 403、当前 401、迟到 200 和迟到 401。
- Chromium 在 `1280x720` 与 `390x844` 生成四张 PNG。主 Agent 已逐图复核：均非空，无文本重叠、无全局横向
  溢出；移动图的纵向滚动位置用于聚焦漏斗阶段，不是布局裁切。
- 最终数据库为 `78/0078`、`integrity_check=ok`、外键违规 0；primary/secondary tenant 各自仅见自己的事实，
  主租户 `2 events / 2 stages`、次租户 `1/1`，两个租户的权限目录均精确为 revision 20；撤权角色 read grant
  为 0。验收内完成撤权 3 次、恢复 2 次、Session 失效 2 次，最终权限保持撤销。
- 进程、固定端口、Browser profile、SQLite sidecar、work/staging 和临时凭据均已清理。
- Chromium 151 首个诊断批次在业务、权限与截图完成后因 `OsUpdateHandlerWin` 造成 owner tree 清理超时，故不作为
  通过证据；由于该版本仍可能忽略 feature 禁用项，最终驱动改用排除更新器和 `debug.log` 的 owner-qualified Chrome
  硬链接运行树，并加固 marker-last 与 Harness/Node 分项 best-effort 清理。最终批次完成扩展流程与静止终态。
- 正式来源证明覆盖 24 个执行文件，包括产品分析、认证依赖、Session 服务、Token、RBAC、统一错误、导航、Console、
  迁移和验收合同；源码任一漂移都会拒绝发布。

正式文件复算值：

| 文件 | bytes | SHA-256 |
| --- | ---: | --- |
| `browser-acceptance.json` | 5927 | `2ba160c3b7351ff1b9771ac1d2bd1bba9bcfb06b90e94caa3463484b92a881f9` |
| `page-117-growth-analytics-desktop-1280x720.png` | 79864 | `b60fa73ce18e7b52ca11112592eead7c00face69bd32cde0ad9e26e15d22202b` |
| `page-117-growth-analytics-mobile-390x844.png` | 31202 | `192df9333678d23be7d96bafcefc1149bf6b6d92cf2c4efb3b31609775f46c3c` |
| `page-117-initial-state-desktop-1280x720.png` | 84146 | `96dd4b8a60db5e9e48418a29be3a5c8c170bc29ebdcca4e5ad1f0025a843704b` |
| `page-117-tenant-isolation-mobile-390x844.png` | 33289 | `5217f0a05b905adb46f16c252165d3392f6614d0858b7895bd3e3769336369d3` |
| `sha256-manifest.json` | 958 | `bc51513b7ed3b20af628121c1d49adcc7a5fedb6d4a04156875d113591be49a1` |

## 7. 隐私、禁止文件与资源审计

- tracked/untracked、补丁、Runtime/Browser JSON、manifest 和 PNG 只允许固定 ID、枚举、计数、hash、状态和
  request_id；Provider Key、Token、Prompt、Response 与上游正文命中必须为 0。
- 提交集合不得包含 `.env`、SQLite/sidecar、日志、cache、coverage、build、Office 临时文件、失败诊断 artifact
  或进程命令行证据；`.artifacts` 只保留本记录引用的正式 Runtime 与 Browser 批次。
- 最终 owned 服务、固定端口、Browser profile、SQLite sidecar 和 PRD-085 临时目录均须归零。

## 8. 下一项交叉确认

Excel `01_PRD总表` 第 87 行、`03_M2-M3扩展交付` 第 23 行、PAGE-118、API-392/393、OBJ-117、
EVT-098 与 TC-0171/0172 共同确认下一条权威未完成项为：

`PRD-086 | M2-企业增强 | P2 | 扩展层 | 客户健康 | 客户健康评分`

本次只登记、不实现 PRD-086；`all_108_prds_complete=false`。
