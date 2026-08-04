# PRD-062 配置导入导出 CLI 测试记录

状态：**通过，PRD-062 已达到完成门禁**。记录日期：2026-08-01。

本记录只陈述实际执行的 PRD-062 专项、直接受影响回归、0054 迁移和真实运行证据；没有因局部缺口
重复无关根级全量。全部 108 条 PRD 仍在进行，`all_108_prds_complete=false`。

## 1. 验收范围与环境

- 工作区：`D:/MetaHub`。
- Python：`C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。
- `PYTHONPATH`：`shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src`。
- pytest 固定 `-p no:cacheprovider`，`TEMP/TMP` 和 `--basetemp` 只使用
  `D:/MetaHub/.artifacts/prd062-*`。
- 不读取、枚举、写入或删除 `D:/MetaHub/.tmp`；不使用 `C:/tmp`、不提权、不访问公网。
- 权威设计：`docs/prd/PRD-062-config-import-export-cli.md`；HTTP 和 CLI 合同分别见
  `docs/api/config-transfer.md`、`docs/development/gatewayctl-config-transfer.md`。

## 2. 设计与两轮自审

两轮编码前自审均为“修正后通过”。第一轮冻结 manifest、create-only、一次性 Key、RBAC、租户引用和
0054 事实边界；第二轮补齐两快照 Key 文件预留、请求重放双 trace、导出不可重放、RBAC 自定义矩阵保真、
迁移 down guard、wire 资源限制和 `.tmp` 禁区。实现期又同步以下合同：

- JSON 上限为 2 MiB、深度 8、200000 节点、5000 总行；重复 key、非有限数、控制字符、未配对
  surrogate 和敏感字段失败关闭。
- `exported_at` 不参与 manifest hash；导出结果必须先通过回导 wire 门禁。
- 导出 request ID 冲突返回 `config_transfer.export_request_conflict`；精确导入 replay 不重放 Key 明文。
- HTTP replay 使用本次 trace；Webhook 补投保留首次提交的 event ID、时间和 trace。
- active Virtual Key 在 apply 前独占预留私有文件；没有生成 Key 时删除空预留。

## 3. 自动化结果

| 分组 | 最终结果 | 覆盖结论 |
| --- | ---: | --- |
| Config Transfer API 专项 | `6 passed` | 四类对象、回导、严格 reader、权限优先、replay、路由事件异常、no-store |
| CLI 完整专项 | `33 passed` | export/import、文件身份/DACL、退出码、origin/redirect、协议、模块启动和独立 PowerShell 包装 |
| AuditLog 动作级摘要 | `13 passed` | imported/exported exact keys、类型、hash、计数和存量解码失败关闭 |
| 0054 迁移专项 | `3 passed` | fresh、重复执行、0053 up、空库 down/re-up、有事实 down 阻断、完整性 |
| Audit/Webhook/RBAC 直接回归 | `12 passed` | Audit 写入/导出、Webhook、六角色权限快照和职责分离 |
| Provider/Model/Alias/Virtual Key 回归 | `9 passed` | 四类对象主流程、权限、Fallback 与 RoutingPolicy |
| 中文注释门禁 | `comment coverage ok` | 正式源码根通过 |
| 关键 Python 语法编译 | 退出码 `0` | Config Transfer、CLI、共享错误响应和 Webhook 共 8 个模块 |

API 全文件最初有 2 条失败，均由统一异常响应重建后丢失 `Cache-Control` 引起；共享运行时增加 `/config`
路径 no-store 后，失败节点 `2 passed`，完整专项 `6 passed`。实际 CLI 又发现包级提前导入导致 `python -m`
输出 runpy warning；改为惰性导出并增加启动测试后，模块 stderr 无警告。随后真实独立启动又发现 Windows
PowerShell 5.1 会把无 BOM UTF-8 中文注释结尾误解析到下一行，导致源码根变量没有赋值；包装注释改为 ASCII
字符收尾，并新增“仓库外 cwd、清空 `PYTHONPATH`、仅指定 `METAHUB_PYTHON`”子进程回归后，完整 CLI 为
`33 passed`，真实 `gatewayctl.ps1 --help` 退出码为 0。

## 4. 0054 实际迁移

`D:/MetaHub/.artifacts/prd062-runtime/source.db` 和 `target.db` 分别从空库实际迁移并 seed：

- 两库均为 `54` 条连续 applied，最高 `0054`，全部 checksum 与迁移文件一致。
- 两库 `integrity_check=ok`、`foreign_key_check=0`，9 个 Config Transfer owner/immutable trigger 齐全。
- source：1 个 import、4 个 import row、1 个 export，AuditLog 含一次 `config.imported/exported`。
- target：2 个 import、14 个 import row，AuditLog 含两次 `config.imported`；第二次为新 request ID 下的
  create-only 幂等执行，全部对象 unchanged。
- source/target 一次性 Key 明文均不出现在 SQLite 文件中。

自动化迁移专项另证明：空事实可 down/re-up 并精确恢复 RBAC revision 1；已有导入/导出事实时 down 在
DDL 前阻断，不会删除审计或自定义权限证据。

## 5. 真实 CLI 与 Control API

实际运行使用 source Control `127.0.0.1:18001`、target Control `127.0.0.1:18002`，Token 仅放当前进程
`METAHUB_CONTROL_TOKEN`，不进入参数或输出。

- source 正式 import 创建 Provider/Model/Alias/Virtual Key 各 1 条，并把 1 个新 Key 只写入私有文件。
- source export 成功，manifest hash 为
  `b5dd01d1d6eb7572c629beec80d64fc63f79194d71cce143b0f11a46b7b2e7b1`。
- target dry-run：Provider `1 create/1 unchanged`、Model `1 create/1 unchanged`、Alias `1 create`、
  Virtual Key `1 create/1 unchanged`，错误数 0。
- target apply 生成 1 个新 Key；Control API 回读四类对象全部可见。
- 同 request ID 精确 replay 返回 HTTP 200、`status=replayed`、本次 trace、0 个 Key；同文件新 request ID
  再执行时所有对象 unchanged、0 个 Key，CLI 删除空预留文件。
- Viewer 的 export/import 均返回 `auth.permission_denied`，CLI 退出码 3，拒绝流程没有留下导出或 Key 文件。
- 第二租户创建唯一 Provider 后，默认租户列表不可见；默认租户使用其 UUID PATCH 返回
  `404 provider.not_found`，并携带 request_id/trace_id。

## 6. Gateway、追踪与隐私

目标 Gateway `127.0.0.1:18000` 连接 target.db，本机无日志 OpenAI-compatible stub 位于 18080；Console
位于 5173。Gateway、source/target Control、Console 健康检查均为 HTTP 200。

- 第一次模型请求因 Key 限流 active 而隔离环境无 Redis，按安全合同返回
  `503 gateway.rate_limit_unavailable`；RequestLog 保存 failed、错误码、模型和 Provider，Token 为 0。
- 复用该 request ID 返回 `409 gateway.request_id_reused`，没有覆盖第一次失败事实。
- 通过正式 Control API 将验收 Key 的限流状态设为 disabled 后，使用新 request ID 调用成功：HTTP 200，
  上游返回 `runtime-ok`，用量为 `3 input / 2 output / 5 total`。
- 成功 RequestLog 为 `status=success`；UsageLedger 为 `status=recorded`、`usage_source=upstream`、5 Token、
  CNY 价格快照链路完整。
- Control/Gateway 日志只含安全元数据；usage 数字在普通日志中为 `***MASKED***`，未出现 Token、Virtual Key、
  Provider Key、Prompt 或 Response 正文。stub 完全关闭访问日志。

## 7. 结论与下一项

PRD-062 的设计、两轮自审、实现、详细中文注释、后端权限、租户隔离、统一错误码/request_id、0054、
专项与直接回归、开发文档和真实运行验收全部通过，可以标记完成。

Excel `01_PRD总表!A64:R64` 与 `02_M0-M1核心开发!A64:R64` 交叉确认，下一条未完成项为
`PRD-063 | M1-企业POC | P1 | 核心层 | 国产Provider适配1 | 国内模型Endpoint适配规范`；
`all_108_prds_complete=false`。
