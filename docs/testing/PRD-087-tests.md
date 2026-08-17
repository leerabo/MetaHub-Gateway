# PRD-087 渠道伙伴与白标门户测试记录

日期：2026-08-17

结论：PRD-087 的 0080、RBAC revision 22、Partner/PartnerCustomer/CommissionRecord、两阶段分润游标、
EVT-099/source outbox、伙伴 API、CSV/交付包和 PAGE-119 已通过本记录所列门禁。
PRD-088 未启动；`all_108_prds_complete=false`。

## 1. 范围与自审

- Excel `01_PRD总表` 第 88 行、`03_M2-M3扩展交付` 第 24 行、PAGE-119、API-394..399、OBJ-118..120、
  EVT-099 与 TC-0173/0174 共同冻结本条，不按编号猜测范围。
- 编码前两轮自审已关闭幂等 payload、revision CAS、角色上限、终止 cutoff、账本 sequence、生效窗口、
  exact event、恢复 owner、租约续期、附件上限、CSV 注入、审计先于发送和 CSS 品牌泄漏问题，无未关闭 P0/P1。
- 所有 Partner、Customer、Commission、Cursor、Outbox、操作链和 API 均 tenant-qualified；后端逐请求校验
  Session tenant、RBAC 与 owner，前端隐藏入口不能替代授权。
- Provider Key、Token、Prompt、Response、Endpoint、raw Usage、客户展示名和品牌正文不进入事件、日志或正式证据。

## 2. 自动化门禁

| 分组 | 结果 | 覆盖 |
| --- | ---: | --- |
| PRD-087 Python 联合专项 | `53/53 passed` | Shared 26、Migration 15、Control 5、Browser harness 7 |
| Shared PRD-085/086 相邻 | `81/81 passed` | revision 22、权限增量、旧事件与错误合同兼容 |
| Control PRD-081/085/086 相邻 | `17/17 passed` | 0074 SIEM 6；当前 0080 Product Analytics/Customer Health 11 |
| Console PAGE-119 | `18/18 passed` | strict client、附件、owner/ticket/generation、生命周期、Browser 证据合同 |
| Console 全量回归 | `926/926 passed` | 主壳、权限、i18n、CSS 与所有既有页面单元合同 |
| Console lint | `286 files` | 当前 Console 源码和测试静态检查 |

历史 PRD-085/086 Control fixture 最初仍把“当前空库”固定在 0079，运行时 revision 22 因此按设计失败关闭。
fixture 已更新到 0080 后 `11/11` 通过；PRD-081 SIEM 状态机继续用只包含 0001..0074 的冻结迁移目录验证 `6/6`，
没有篡改产品数据库或弱化 RBAC 完整性检查。

## 3. Migration 0080

- 从空 SQLite 实际完成 `80/0080 -> 80/0080 -> 79/0079 -> 80/0080`。
- fresh、repeat、down、re-up 每阶段均为 `integrity_check=ok`、外键违规 0；迁移专项 `15/15`。
- 0080 创建 tenant-qualified Partner、PartnerCustomer、CommissionRecord、两阶段分润游标、EVT-099 source
  outbox 与伙伴安全操作链，安装复合 owner、append-only、幂等 payload hash、revision CAS、lease/generation、
  cutoff、exact payload、RBAC revision 22 和 down 原子阻断。
- seeded 往返恢复 revision 21 后再稳定升级 revision 22；非 admin 权限漂移、业务事实、outbox/operation/commission
  残留均会阻断回退。

## 4. Control/API 与并发

- 未配置 profile 返回 tenant-scoped 404；创建、同键重放、异 payload 冲突、branding CAS、可选 support_text 清空、
  Project 映射、终止 cutoff、历史用量、分润查询、CSV 和交付包均验证统一错误码、request_id 与 `no-store, private`。
- admin 配置六项；viewer 具备五项业务权限但不能首次配置；developer 动态升/降级立即生效。跨租户 customer 与不存在
  customer 保持同形 404，所有拒绝路径零业务写入。
- 分润按不可变 FinOps ledger sequence exactly-once 物化，覆盖 CNY/USD 分离、estimated、六位整数 rounding、
  claim 互斥、过期接管、旧 worker CAS 失败、失败退避、终止时 scan_upper/cutoff 竞争和 cutoff 后账本排除。
- EVT-099 exact 七字段 payload、稳定 event ID、原 actor/request/trace、同秒两次续租、空订阅终态、恢复与十次失败终态均
  可追踪；事件不公开 request_id、展示名、品牌、金额或附件正文。

## 5. Browser PAGE-119

权威批次：`.artifacts/prd087-browser/page119-final-20260817-r17/`

- 真实 HTTP Console/Control 与 Chromium 从空 SQLite 启动，在 `390x844`、`1280x720`、`1440x900` 生成 setup、
  ready、commission 和 tenant-isolation 四张 PNG；主 Agent 已逐图复核非空、无遮挡、无横向/文本溢出。
- 四次登录/注销均为 200；19 个 Partner 业务请求与 19 个响应一一对应，required route、Authorization、
  Idempotency-Key、If-Match、JSON body、query 和 `no-store` 合同全部通过，CORS OPTIONS 不冒充业务请求。
- viewer 操作入口、动态撤权根/导航/DOM/branding 清理、迟到响应丢弃、未授权导航/页面/预取为零、第二 tenant 独立、
  前一 tenant DOM 清理和注销 Storage 清理全部为 true；Console error、Page error、外部请求和禁止 query 均为 0。
- `browser-acceptance.json` 为 5568 bytes，SHA-256
  `b74797106b0f8859b9a676236c357ba10280d2d0fa8f82a88ed66abe48fb17dc`；manifest SHA-256
  `d7c91394e60d828f8990b2531cca9c34bb76180b2f9c89271ecb3098f90a6688`。

| 文件 | bytes | SHA-256 |
| --- | ---: | --- |
| `page-119-partner-setup-mobile-390x844.png` | 28151 | `cb3b4d6c4e1efe5085116d64d7dadab65bdda1c485d0f0f7236585a4ffc13902` |
| `page-119-partner-ready-desktop-1280x720.png` | 69097 | `1b607b22d1a614abecfb514bb7b304429ae5a8c328546e836fa65af5ae4fe82c` |
| `page-119-partner-commission-wide-1440x900.png` | 99312 | `f96fc8bb1a5d096d15c4147296e63e9269273083bfcf6d67e3d6c0bbd478d5bb` |
| `page-119-partner-tenant-isolation-mobile-390x844.png` | 32470 | `883548bf8be8fdc64794f49f6af821d22e0f7ad52547f5844981d86fc59c3e64` |

## 6. 隐私、禁止文件与资源审计

- r17 staging/work 分别扫描 5/6 个文件，已注册私密值明文命中均为 0；证据不捕获响应正文，PNG 仅保留已目视复核的
  安全聚合视图。浏览器、Console、Control 均由 harness 所有并停止，四个固定端口和正式 SQLite sidecar 为 0。
- 候选提交只允许源码、测试、迁移、文档和开发状态工作表；`.env`、SQLite/sidecar、日志、cache、coverage、build、
  Office 临时文件、私钥、失败诊断 artifact 和 `.tmp` 不得进入 Git。
- 工作簿仅更新 `12_开发状态`，提交前执行 ZIP CRC、openpyxl 重开、公式/样式/合并单元格/其他工作表语义校验，
  并确认每条状态仍为 `all_108_prds_complete=false`。

## 7. 下一项交叉确认

Excel `01_PRD总表` 第 89 行、`03_M2-M3扩展交付` 第 25 行、PAGE-120、API-400/401、OBJ-121、EVT-100 与
TC-0175/0176 共同确认下一条权威未完成项为：

`PRD-088 | M2-企业增强 | P2 | 扩展层 | 白标配置 | 客户/渠道白标配置`

本次只登记，不实现 PRD-088；`all_108_prds_complete=false`。

## 8. 中文注释范围门禁

全仓 `scripts/ci/check-comments.py` 会继续报告 PRD-080～086 的历史注释债务，因此 PRD-087 使用可审计的独立范围门禁：`scripts/ci/check-prd087-comment-scope.py` 固定检查 22 个交付文件的模块职责头、后端公开 API 文档字符串、前端导出 API JSDoc、迁移操作说明和安全常量边界；内部辅助分支不伪装成新增需求。当前结果为 `PRD-087 scoped Chinese comment gate passed (22 files)`。
