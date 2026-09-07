# Security Assurance 执行回执

当前状态：回执验证、失败关闭与数据库原文归档已实现，真实隔离 worker 尚未交付。设置公钥路径不会自动部署或启动
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

内部 runner callable 的合同为 `runner(request, lease) -> bytes`，两个参数分别是
`AssessmentExecutionRequest` 和协调器持有的 `AssuranceLease`；返回签名 JSON，最多 256 KiB。
网络执行器只能使用 `execution_request_payload` 的固定字段；原 Workspace/tenant 只参与 owner
摘要绑定，不能作为生产客户目标选择器。执行请求绑定 profile、全部场景 registry、四类构建摘要、
assessment/generation、随机 lease challenge 和时间期限。

`lease.check()` 同步复核数据库权限、owner 状态、绑定和到期时间；异步 HTTP 探针使用
`lease.guard()`，必须在每次请求与响应阶段检查。30 秒短租约只在剩余 15 秒内 CAS 续租，
不会超过评估整体 deadline。失败、撤权、取消或 close 后 capability 永久失效，不能恢复后复用。
它不得序列化或传给远程 worker；远程执行仍需要独立、受认证的协调传输，不能暴露数据库凭据。

排队启动上限为 30 秒。协调器的认领记录本身不构成执行授权；只有当前权限和短租约检查通过后
才调用 runner。排队过期或启动前撤权记录为 failed/error/P0，不启动攻击。调度器有界回收超时
queued 和过期 running，CAS 拒绝重复认领或旧 generation 落库；无实际执行回执时不写清理证明。

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

## 原文归档

部署前应用 SQLite/PostgreSQL 各自的 `0142_assurance_receipt_archive` 增量。验签后的原始 UTF-8
envelope、规范化请求 JSON、所选部署公钥快照、各自 SHA256、验签时间和 fencing generation
保存到 `assurance_receipt_archives`，与 run/finding/assessment 终态同一事务提交。原文上限
256 KiB；UTF-16/UTF-32、重复键、未知字段、无效签名和绑定不匹配均拒绝。

每个 assessment 仅一份归档，幂等重放读取原结果。验签有效但清理失败的回执仍归档，评估不得 passed。
已验签后、提交前才撤权的原文保留，但结果为 error 且不采信其清理事实；旧 generation 则全部拒绝。
任何归档或结果写入失败都会回滚本次事务，不能留下半份归档或 passed，运行任务由现有租约恢复收尾。

原文及公钥快照不进入公共 DTO、Console 或应用日志。公钥快照仅记录当时信任，不授权未来提交；
未来回执仍重新读取部署信任。历史未保存的 envelope 不回填。UPDATE/DELETE 被数据库触发器拒绝，
PostgreSQL 另以语句级触发器拒绝 TRUNCATE；非空归档拒绝 down 迁移。归档数据纳入 `archive_then_delete`
清单，但本增量不等于全库租户删除原生兼容性验收，也不等于独立 WORM 或能抵抗数据库管理员修改。

## 未完成项

已提供 `HttpFixtureBootstrap` 与 `DockerSandbox` 基础能力，尚未接入正式 app 的 runner。
初始化器只在独立 sandbox 内调用现有注册、验证码验证、登录、邀请、Workspace、项目和应用接口；
调用方须提供四个同 origin、无预置认证且 `trust_env=False` 的独占 HTTP 客户端。验证码适配器必须
交付本次实际生成且目标邮箱匹配的 challenge；本地集成使用 test_capture，不代表已有 SMTP 服务。

Docker SDK 仅安装到专用 supervisor：

```powershell
python -m pip install -r infra/docker/requirements.assurance-supervisor.txt
```

生产 Control 和 guest 不需要该依赖，也不能获得 Docker socket。部署配置固定专用 rootless socket
`unix:///run/user/<uid>/metahub-assurance/docker.sock`、daemon ID 和 `sha256:<64hex>` 镜像 ID。
daemon 须带 `metahub.assurance=dedicated-v1` 标记，启用 cgroup v2、资源限制能力与 builtin seccomp；
未标记容器混用、镜像隐式 VOLUME、网络/宿主挂载/权限配置不符均拒绝启动。

使用顺序为 create、lease_session（启动前核验并 attach）、start、await session.serve，最后无论成功
失败均 finally cleanup(handle)。lease_session 使用 exact handle 自动绑定环境、deadline 和新通道
nonce，不接受 guest 选择镜像、URL、命令或 owner；启动失败时仍须取已记录的 handle 清理。
cleanup 返回结构化观察，不是签名回执，也不是全部业务 fixture 的清理证明；close 仅关闭 SDK 连接。
未取得 exact handle 的创建失败必须报告未知残留，不能按名称扫描删除。已验证的成功清理可幂等读取。
该同步生命周期由独占 supervisor 串行调用；许可回调及 SDK 请求须保持有界，不把 supervisor 暴露为
可接受任意镜像、命令或 URL 的公共接口。SDK 的连接工厂只在 Linux 启用。

固定 guest 的 stdin 现用于专用许可管道，TTY 和 shell 仍禁用，不开放网络端口。管道使用有界长度帧
与严格 JSON，逐次检查执行绑定、序号、新挑战及最多一秒的消费窗口。现有初始化和五场景探针可直接
使用 `GuestLease.guard`；supervisor 每次询问权威许可，不能缓存一次批准覆盖整个评估。
取消、断流、拒绝或会话完成都会关闭管道并使容器管理实例失效；重放和迟到的许可不能重新启动它。

该协议只适用于经过 owner 核验的独占管道，不提供开放 TCP 上的认证。正式 Control 到独立 supervisor
的受认证远程许可通道尚未接入；测试中本地进程间调用的 AssuranceLease 不能作为该网络部署的替代。
`report_announced` 仅说明对端发来报告摘要，不说明已读到报告、执行通过、签名有效或清理完成。

固定入口现已实现于 `metahub_assurance_runner.guest`，构建配方为
`infra/docker/assurance-guest.Dockerfile`。它在镜像构建时生成独立合成基线，不复制工作站数据库；
运行时只在私有 tmpfs 副本上应用增量。完整 Control app 仅监听 guest 内 loopback 的 HTTPS，使用
本次临时证书和真实验证/登录；客户端必须验证该证书，禁止代理继承。此配置明确为 SQLite，验证码
使用非生产 test_capture，不能声称 PostgreSQL、SMTP、外部存储或全 owner 已验收。

guest 清除继承配置，在空的私有工作目录装配应用。stdout 只保留协议；阻塞启动期间仍复查许可，
取消线程任务不能当成底层线程退出。正常停止 API 后关闭本 guest 的空闲数据库池，再清理私有目录；
失败时不回传报告。supervisor 在所有失败路径仍须执行自己的 exact-ID 容器清理，不能信任 guest
自身的目录删除等同于宿主资源已释放。

`GuestLease.publish` 在退出前分块传回报告，避免依赖容器停止后仍存在的 tmpfs。报告上限 256 KiB，
每块最多 1 KiB 原文，仍使用 4 KiB 长度帧；每块和最终确认均复查许可。supervisor 检查连续块序号、
总长度、规范编码及最终 SHA256 后返回 `report_received` 与隐藏于 repr 的 report_bytes。
这只提供报告传输完整性，不等于内容验证、攻击执行、签名或清理证明，不可直接交给现有正式验签入口。

真实 worker 的无生产权限隔离部署、镜像/代码/迁移/合同度量、全部固定攻击场景、远程短租约传输、
全 owner fixture 创建与 exact cleanup 仍待完整接线和实测。当前仅已验证本地进程和真实 TLS 执行，
尚未构建或启动上述镜像，也未验证 Linux stdio 入口。数据库原文归档须随实际 runner 一并做部署验证。
只有这些证据齐全后，才可把签名结果用于真实安全验收；目前不能解除 G6/G8。

当前 SQLite Guest 报告升级为 `metahub.assurance-guest-report.v2`，增加 `resources` 闭集观察。
文件密钥每次独立随机生成，`file-storage` 与本次私有数据库位于同一专有目录。观察器仅只读打开
这份 SQLite，使用固定有界查询；实际上传、偏好更新和权限检查仍经真实 HTTPS 业务接口。
缓存验证覆盖持久导航投影的复合 owner、版本、实际内容及暖读；文件验证覆盖真实上传、磁盘
原文/抽取密文、合法解密、owner/字段交换、路径及覆盖拒绝。遥测经真实中间件/后台工作器落盘，
两个有界水位窗口逐项核对双租户伪名、计数、闭集标签、匿名输入和旧会话撤销。Guest 独立生成
用于伪名化的平台会话域密钥，观察器只读实际 bucket，报告增加两个 metric_bucket 摘要。
已实现的八类代表场景不表示全部响应缓存、队列、外部对象存储、Gateway 遥测或 owner registry
验收；queue_owner/late_response 仍返回 error。resources 只包含检查码/布尔/时间/标识与状态摘要，
不含真实 ID、对象键或正文，
也不是 fixture 删除证明。v2 原文依然不能直接作为正式签名回执，isolation/cleanup/full_owner
标记继续为 false，须由后续独立 supervisor 度量、完整执行与清理证据完成签名组装。
