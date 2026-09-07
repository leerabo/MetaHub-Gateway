# Security Assurance 执行回执

当前状态：回执验证与失败关闭已实现，真实隔离 worker 尚未交付。设置公钥路径不会自动部署或启动
worker，也不能把编排测试的签名样本用于真实安全门禁。

## 部署信任

`METAHUB_ASSURANCE_RECEIPT_TRUST_BUNDLE_PATH` 指向部署方维护的 UTF-8 JSON 公钥文件。
文件顶层字段为 `schema_version="metahub.assurance-trust.v1"` 和 `keys`。每个 key 包含：

- `key_id`：小写标识，同一文件中唯一。
- `public_key`：32 字节 Ed25519 公钥的无 padding canonical base64url。
- `runner_image_sha256`、`isolation_policy_sha256`：经过独立部署核验的镜像和隔离策略摘要。
- `not_before`、`not_after`：带时区的有效期，执行开始至当前提交都必须位于有效区间。

文件只保存公钥，不接受请求方自带公钥。每次提交重新读取；删除公钥、修改受信镜像/策略或过期立即
拒绝旧回执。文件最多 64 KiB、32 个 key；缺失、不可读、重复键和未知字段均失败关闭。

## 执行合同

内部 runner callable 接收 `AssessmentExecutionRequest` 并返回签名 JSON 的 bytes，最多 256 KiB。
网络执行器只能使用 `execution_request_payload` 的固定字段；原 Workspace/tenant 只参与 owner
摘要绑定，不能作为生产客户目标选择器。执行请求绑定 profile、全部场景 registry、四类构建摘要、
assessment/generation、随机 lease challenge 和时间期限。

回执 JSON 顶层为 `key_id`、`receipt`、`signature`，receipt 的 schema 是
`metahub.assurance-execution.v1`。签名字节由 `receipt_signing_bytes` 生成：独立用途前缀加
canonical JSON，签名为 64 字节 Ed25519 的无 padding canonical base64url。
实现和严格 DTO 位于 `control-plane-api/src/metahub_control/security_assurance/receipts.py`。

必须包含 10 个顺序一致的场景，分别记录结果、实际起止时间、duration、source/target 摘要和 fixture
创建/清理清单。缓存、队列、对象、指标场景还必须提供对应资源清单。每种 fixture code 代表 exact
清单摘要，不是可递归删除的前缀；worker 必须先独立核验隔离环境和真实 owner marker。

服务验证签名、绑定、清单、时间和结果后，才持久化来源/创建/清理摘要和逐项 cleanup journal。
任一清理失败会生成 error/P0。无执行器、无信任或无有效签名时，assessment 为 failed，所有场景为
error/P0，environment receipt 为 NULL，且不创建虚假 cleanup journal。为兼容旧必填 DTO/数据库列，
未知镜像/subject/seed/cleanup 摘要暂以 64 个 `0` 表示“未取得”，绝不表示实际测量或有效回执。

## 未完成项

真实 worker 的无生产权限隔离部署、镜像/代码/迁移/合同度量、固定攻击场景、逐阶段短租约和撤权、
真实 fixture 创建与 exact cleanup、原始签名 envelope 的不可变归档仍待实现和实测。
只有这些证据齐全后，才可把签名结果用于真实安全验收；目前不能解除 G6/G8。
