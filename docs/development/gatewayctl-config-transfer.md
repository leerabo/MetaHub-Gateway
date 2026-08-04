# gatewayctl 配置迁移使用说明

`gatewayctl` 是 PRD-062 的配置导入导出客户端，只通过 Control API 工作。它不会直连数据库、扫描客户代码库、
读取 `.env` 或接受目标 tenant 参数。目标租户和最终权限始终由 Bearer Session 对应的后端认证上下文决定。

PRD-062 已完成专项、迁移和真实 source/target 运行验收；最终证据见
`docs/testing/PRD-062-tests.md`。

## 1. 启动方式

仓库源码环境使用 PowerShell 包装：

```powershell
$env:METAHUB_PYTHON = 'C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
powershell -ExecutionPolicy Bypass -File scripts\gatewayctl.ps1 --help
```

包装脚本只按自身位置解析 `shared-sdk/python`，不扫描当前目录；真实回归会从仓库外工作目录清空
`PYTHONPATH` 后启动该脚本，避免调用方环境掩盖源码根解析问题。也可在已经正确设置 `PYTHONPATH` 的环境中运行：

```powershell
python -m metahub_shared.gatewayctl.cli --help
```

命令合同：

```text
gatewayctl config export [--output FILE|-] [--force] [--control-url URL] [--token-stdin]
gatewayctl config import --input FILE [--generated-keys-output FILE] [--dry-run]
                         [--control-url URL] [--token-stdin]
```

默认 Control URL 是 `http://localhost:8001`。

## 2. Session Token

CLI 只从以下两个入口读取 Control Plane Session Token：

1. 默认读取当前进程的 `METAHUB_CONTROL_TOKEN`。
2. 显式传入 `--token-stdin` 时只读取标准输入第一行，并优先于环境变量。

CLI 不提供 `--token` 参数，也不接受 `Bearer ` 前缀、空白、控制字符或超过 4096 字符的值，避免 Token 进入
shell history、进程列表或自由文本错误。下面的 PowerShell 方式让实际值不出现在命令行历史中：

```powershell
$secure = Read-Host -AsSecureString 'Control Session Token'
$token = [System.Net.NetworkCredential]::new('', $secure).Password
$token | powershell -ExecutionPolicy Bypass -File scripts\gatewayctl.ps1 config export --token-stdin
$token = $null
$secure = $null
```

也可以由受控进程注入 `METAHUB_CONTROL_TOKEN`。不要把真实 Token 写入脚本、README、终端录屏、测试证据或
命令行参数；命令结束后应清理长生命周期父进程中的环境变量。

为避免在每段示例重复 Token 读取与清理，后续命令假定受控启动进程已经注入
`METAHUB_CONTROL_TOKEN`；需要标准输入方式时，把该命令按上例追加 `--token-stdin` 并由私密输入管道提供一行
Token。

后端对 export 校验 `config.transfer.read`，对 validate/apply 校验 `config.transfer.write`。本地参数和角色名称
不能替代后端权限，401/403 会稳定映射为退出码 3。

## 3. 导出

### 3.1 导出到文件

先创建一个由交付人员控制的普通本地目录，目标文件本身必须不存在：

```powershell
New-Item -ItemType Directory -Path .\transfer -ErrorAction SilentlyContinue | Out-Null
powershell -ExecutionPolicy Bypass -File scripts\gatewayctl.ps1 config export `
  --output .\transfer\tenant-config.json `
  --control-url http://localhost:8001
```

CLI 在目标同目录写临时文件，完成 UTF-8 JSON、flush 和 fsync 后原子发布。目标已存在时在发网前返回
`gatewayctl.output_exists`；只有明确使用 `--force` 才允许覆盖一个仍被复核为普通文件的目标：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\gatewayctl.ps1 config export `
  --output .\transfer\tenant-config.json --force
```

成功写文件时 stdout 是一行安全摘要：

```json
{
  "status": "exported",
  "config_export_id": "<server-generated-id>",
  "manifest_sha256": "<64-lowercase-hex>",
  "request_id": "req_gatewayctl_example",
  "trace_id": "trc_example"
}
```

目标文件只包含可直接作为 import 输入的 ConfigExport v1，不包含 HTTP envelope、Provider Credential、
Virtual Key 明文/哈希/前缀或 Session Token。

### 3.2 导出到 stdout

`--output -` 是默认值，会把 ConfigExport 本体作为单行 JSON 写入 stdout：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\gatewayctl.ps1 config export --output -
```

该输出刻意不包含事实 ID 和追踪 envelope，以便直接成为 import 文件内容。`--output -` 不能与 `--force`
同用。正式交付优先使用 `--output FILE` 的原子写入，不应依赖 shell 重定向覆盖重要文件。

### 3.3 导出失败边界

服务端只有在当前租户四类配置可由 v1 完整表达、规范 JSON 不超过 2 MiB/200000 节点且能按同一合同回导时
才返回成功。历史脏值、不可表达状态或过大清单返回 `config_transfer.export_state_invalid`。服务端不保存导出
正文，同一 tenant、actor 和 request ID 不能重放旧文件；request ID 复用返回
`config_transfer.export_request_conflict`。CLI 每次命令自行生成新的安全 request ID，不提供覆盖参数。

## 4. 导入前检查

输入必须是一个已存在的普通非链接文件。CLI 逐项 `lstat` 路径组件，打开前后核对同一文件身份，并在发网前执行：

- 最大 2 MiB、严格 UTF-8 JSON、无重复 key；
- 深度 8、最多 200000 节点、单字符串 4096 字符；
- 顶层精确 ConfigExport v1、四集合和总行数上限；
- NaN/Infinity、控制字符、未配对 surrogate 和敏感字段名拒绝；
- `source_tenant_id/exported_at` 基础类型与格式检查。

本地门禁只减少无效网络调用，不能替代服务端领域校验、租户隔离和 apply 锁内重检。

使用 `--dry-run` 执行本地检查和服务端 `/config/import/validate`，不写业务数据、不生成 Key：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\gatewayctl.ps1 config import `
  --input .\transfer\tenant-config.json `
  --dry-run
```

`--dry-run` 不能与 `--generated-keys-output` 同用。校验成功退出 0；行级业务错误输出完整 ImportReport 摘要并
退出 4，不会继续调用 apply。报告最多携带 200 条安全行错误，`error_count/errors_truncated` 给出完整数量关系。

## 5. 正式导入与一次性 Key

只要输入中存在状态为 `active` 的 Virtual Key，或省略 `status` 而按默认 active 解释，正式 import 就必须提供
一个尚不存在的 `--generated-keys-output`。即使 validate 判断该 Key 当前为 `unchanged`，也不能省略：validate
与 apply 是不同快照，并发删除可能让 apply 重新创建 Key；CLI 必须在提交前已有安全句柄接收唯一一次明文。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\gatewayctl.ps1 config import `
  --input .\transfer\tenant-config.json `
  --generated-keys-output .\private-delivery\generated-virtual-keys.json `
  --control-url http://localhost:8001
```

`private-delivery` 必须预先存在并由交付人员控制，目标密钥文件必须不存在。CLI 在调用 apply 前：

- 拒绝 symlink、Windows reparse point、UNC 路径、非普通目标和链接父路径；
- 以 exclusive create 打开文件并持续持有句柄；
- POSIX 收紧到当前用户所有且精确 0600；
- Windows 设置受保护 DACL，仅允许当前用户、SYSTEM 和 Administrators 完全控制；无法证明安全即失败关闭。

apply 成功且确实产生新 Key 后，CLI 才把以下内容写入已预留句柄，并完成 flush/fsync：

```json
{
  "config_import_id": "<server-generated-id>",
  "request_id": "req_gatewayctl_example",
  "generated_virtual_keys": [
    {
      "virtual_key_id": "<server-generated-id>",
      "name": "delivery-key",
      "environment": "prod",
      "key_prefix": "<short-prefix>",
      "plain_value": "<returned-once>"
    }
  ]
}
```

明文只允许存在于该私有文件和当前调用内存。stdout 只给 `generated_virtual_key_count`，stderr、普通报告、
AuditLog、Webhook 和日志均不含 `plain_value`。如果 apply 最终为 `unchanged`、`replayed` 或 `invalid`，CLI 会
关闭并删除未提交的空预留文件。

如果服务端已提交，但本地密钥文件写入或 fsync 失败，CLI 删除可能的部分文件，返回退出码 6、
`gatewayctl.generated_keys_commit_failed` 和可用的 `config_import_id`。此时业务配置可能已经生效，而明文无法
从服务端恢复；不要把该结果误判为事务回滚，也不要盲目重复导入。应凭 `config_import_id/request_id` 核对审计
与导入事实，并按既有 Key 管理流程显式创建或轮换新的可交付 Key。

## 6. 两阶段与重放语义

正式 import 固定执行：

```text
本地严格门禁
  -> POST /config/import/validate
  -> 对潜在 active Key 预留私有文件
  -> POST /config/import（服务端写锁内完整重检）
  -> 有新 Key 时提交私有文件，否则删除空预留
  -> stdout 输出安全摘要
```

validate 与 apply 使用不同 request ID，最终摘要同时包含 `validation_request_id` 和 apply 的
`request_id/trace_id`。服务端对相同 actor/request/hash 的精确重试返回 `replayed`，不会再次返回明文；CLI 能
验证并安全处理该状态，但不提供用户自定义 request ID 的参数。HTTP replay 报告使用本次 trace，Webhook 补投
保留首次提交事实的原 trace。

## 7. 网络安全

- 默认 `http://localhost:8001`；HTTP 只允许字面 `localhost` 或回环 IP，不执行 DNS 猜测。
- 非回环 Control URL 必须使用 HTTPS。
- URL 拒绝 userinfo、query、fragment、反斜线、非法端口和非 HTTP(S) scheme。
- Authorization 只发送到初始化时冻结的 scheme、host 和有效端口。
- 只允许同 origin 的 307/308，并完整保留 POST 方法与正文；301/302/303、跨协议/主机/端口和带
  userinfo/query/fragment 的重定向全部拒绝。
- 请求与响应都受 2 MiB 边界；成功响应必须是明确 UTF-8 JSON 且包含 `Cache-Control: no-store`。
- CLI 对响应执行精确字段、状态、链路 ID、计数、错误截断和一次性 Key 数量校验，异常服务端响应不会落盘。

## 8. 输出和退出码

除 `export --output -` 的 ConfigExport 本体外，stdout 始终是一行紧凑安全 JSON。服务端返回业务
`invalid` 时，stdout 保存安全 ImportReport 摘要、stderr 为空且进程退出 4；参数、本地文件、权限、网络或
协议异常时 stdout 为空，stderr 是一行固定字段 JSON：

```json
{
  "status": "error",
  "code": "gatewayctl.input_invalid",
  "request_id": "req_gatewayctl_example",
  "trace_id": null,
  "config_import_id": null
}
```

错误输出不包含 message、路径、URL、HTTP 正文、Token、配置值、异常或 traceback。

| 退出码 | 语义 |
| ---: | --- |
| 0 | 导出、dry-run 或 apply 成功；`replayed` 也视为安全成功 |
| 2 | 参数、Token、Control URL、本地输入/输出、JSON 或敏感字段门禁失败 |
| 3 | 后端 401/403 认证或权限失败 |
| 4 | 后端 400/409/413/422，或 ImportReport 为 `invalid` |
| 5 | 网络、超时、5xx、重定向、响应协议或未知运行时错误 |
| 6 | generated keys 文件无法安全预留或已提交后无法完整落盘 |

常见本地固定码包括：

| code | 说明 |
| --- | --- |
| `gatewayctl.argument_invalid` | 参数组合或未知参数无效 |
| `gatewayctl.token_unavailable` | Token 缺失或格式不安全 |
| `gatewayctl.control_url_invalid` | Control URL 不满足 origin/TLS 边界 |
| `gatewayctl.input_invalid` / `gatewayctl.input_too_large` | 输入文件或清单资源边界失败 |
| `gatewayctl.sensitive_input_rejected` | 输入含凭据语义字段 |
| `gatewayctl.output_invalid` / `gatewayctl.output_exists` | 导出目标不安全或已存在 |
| `gatewayctl.generated_keys_output_required` | 潜在 active Key 没有显式私有文件 |
| `gatewayctl.generated_keys_open_failed` | 无法在 apply 前安全预留密钥文件 |
| `gatewayctl.generated_keys_commit_failed` | 服务端提交后密钥文件无法完整持久化 |
| `gatewayctl.redirect_rejected` | 重定向改变 origin 或 POST 语义 |
| `gatewayctl.network_unavailable` | 连接、TLS 或超时失败 |
| `gatewayctl.response_invalid` | HTTP 成功/错误响应不符合冻结协议 |
| `gatewayctl.failed` | 未知异常在最外层被固定脱敏折叠 |

服务端合法错误码会原样保留在固定 `code` 字段，但自由文本 message 不会输出。

## 9. 交付检查

在把导出文件或一次性 Key 交给客户环境前，至少复核：

1. 当前 Session 属于正确目标租户，后端权限来自预期数据库 revision。
2. 导出文件只含 ConfigExport v1 白名单，未出现 credential/key/token/secret/plain_value 等字段。
3. 先完成 `--dry-run`，保存安全报告中的 request/trace ID；不要保存原始 Token 或 Key 到测试证据。
4. active Key 导入使用尚不存在的私有目标，交付后按企业密钥渠道传递并及时删除非必要副本。
5. 根据退出码和 `report.status` 判断结果；退出 6 时优先核对 `config_import_id`，不能假定数据库已回滚。
6. 不把 `--force` 用于一次性 Key 文件；该参数只适用于已明确复核的脱敏导出文件。
