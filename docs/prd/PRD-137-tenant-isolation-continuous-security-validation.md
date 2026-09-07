# PRD-137 租户隔离持续验证与安全攻防

状态：补充修复中。依赖 PRD-090..136；范围以 Excel PRD-137 为准。0129、
finding/remediation/risk acceptance 编排与 Platform Console 已实现，但 2026-09-07 源码复核确认
正式 app 未接入真实隔离 runner，创建/清理回执也未来自实际执行，不能视为完整安全验证已交付。

## Excel 权威合同

- API：`POST /platform/security/assessments`、
  `GET /platform/security/assessments/{id}`、
  `POST /platform/security/findings/{id}/remediations`。
- 对象：`SecurityAssessment`、`IsolationTestRun`、`PenTestFinding`、`Remediation`、
  `RiskAcceptance`。
- 事件：`security.assessment_failed`、`security.finding_created`、`security.finding_closed`。
- SLO：每次发布增量隔离门禁 < 30 分钟；季度完整攻防；P0 修复 < 24 小时。

## 1. 产品闭环

platform_security 选择签名、版本化 assessment profile，系统在隔离环境创建专用 tenant/workspace/account、Platform
和 Edge 测试主体，按 owner registry 验证 IDOR、写越权、缓存污染、队列 owner、对象前缀、指标标签、动态撤权、
重放、迟到响应和模糊输入。runner 只执行固定 scenario code 和 target kind，不能接收 URL、脚本、SQL、shell、
任意 header 或生产客户 selector。

失败生成不可变 finding，绑定受影响 owner/contract、最小复现 code、severity、evidence digest 和 remediation SLA；
修复通过同一 profile 新 generation 复验后关闭。与租户有关的通知只含本空间安全摘要；风险接受需要 Workspace
owner + security_admin 双批及 platform_security review，P0/P1 默认不能用于 G6 放行。

## 2. 0129 评估、发现与隔离执行

- SecurityAssessment、IsolationTestRun、PenTestFinding、Remediation 和 RiskAcceptance 全部带隔离目标的
  `workspace_id/tenant_id` 复合 owner。Assessment 保存 profile/version/signature、code/migration/contract/artifact
  digest、scope、status、requester、environment receipt 和 summary digest；平台是发起主体而不是业务 owner，
  禁止无 Workspace/tenant 的全局扫描。
- IsolationTestRun 绑定 assessment、scenario code、source/target subject matrix、owner registry version、runner image、
  seed/cleanup receipt、fixed outcome、duration 和 evidence digest；不保存凭据、请求正文或原始响应。
- PenTestFinding 绑定 assessment/run、owner kind、contract code、severity、fixed weakness code、status、SLA deadline、
  first/last seen 和 immutable evidence reference。
- Remediation 追加保存 change digest、verification assessment、outcome 和 approver；RiskAcceptance 绑定 finding、scope、
  双 Workspace approver、platform reviewer、compensating controls 和 expiry。
- runner lease、scenario receipt、cleanup journal 和 event/outbox 持久化；清理测试 fixture 不删除 assessment/finding。

Down 在任一 assessment、run、finding、remediation、acceptance、runner 水位或事件存在时拒绝。

## 3. 运行隔离、权限与 Console

platform_security 只能从 registry 发起；runner machine identity 只访问隔离环境 public contracts 和受控 fault injector，
无生产数据库、对象存储、KMS、Provider 或迁移管理权限。测试 tenant 使用显式 marker 和唯一 owner，创建前后验证目标环境，
cleanup 只删除 exact manifest 内 fixture。外部审计员只读经裁剪证据，不获得执行 token。

Platform Console 展示 profile、owner coverage、run、finding、SLA、remediation 和清理 receipt；Workspace 只展示本空间
通知与风险接受动作。POST 使用 step-up、Idempotency-Key、If-Match、no-store；动态撤权后 runner 不获得新 lease，
已租约在短 deadline 后 fencing。页面不渲染 exploit payload、凭据、内部 endpoint 或客户标识。

## 4. 第一轮自审：测试平台本身的攻击面

- P0：允许任意脚本/URL 会把安全评估变成远程执行与 SSRF 平台。修正为签名 scenario registry 和固定参数上限。
- P0：runner 使用生产平台角色会读真实客户数据。修正为隔离 machine identity、test owner manifest 和网络/资源边界。
- P0：只测列表 API 会漏队列、缓存、对象和指标侧信道。修正为版本化 owner registry 覆盖同步/异步/存储/观测面。
- P1：finding 保存原始 exploit/response 会泄漏 Secret。修正为 weakness code、hash、固定摘要和受控外部证据。

## 5. 第二轮自审：撤权、清理与证据保留

- P0：权限撤销后已有长 lease 继续攻击。修正为短 lease、每阶段恢复 permission revision、deadline 和 fencing token。
- P0：cleanup 按前缀删除可能触碰生产/其他测试数据。修正为 exact fixture manifest、owner marker、环境证明和逐项删除 receipt。
- P1：清理 fixture 同时删除 finding 会抹去失败证据。修正为运行数据和持久 assurance facts 分库/owner 生命周期。
- P1：修复者直接关闭 finding 会跳过复验。修正为只有新 assessment generation 的同 profile 通过才能关闭。

两轮均为修正后通过，无未关闭 P0/P1 设计问题。

## 6. 专项验证计划与批次边界

0128/0129 往返/down guard；scenario 签名、任意输入/生产 selector 拒绝；全 owner registry IDOR/写越权/缓存/队列/
对象/指标/动态撤权/重放/迟到响应；runner 故障/接管/fencing；finding/remediation/复验/双批接受；exact cleanup 和
静止终态；30 分钟增量门禁与 P0 SLA；Platform/Workspace 三浏览器三缩放；敏感证据审计。完整 G6 留到 PRD-138 后。

## 7. 2026-09-07 合批修复设计与未完成项

与 PRD-134..136 合批修复 schema 查询和 CAS，保留所有 lease、generation、当前权限及双人审批
判断。现有业务测试在原生数据库运行时仍使用受控 runner，验证的是编排行为，不是攻击执行。
第一轮自审：真实执行器必须使用固定场景和隔离主体，绑定待测 artifact/code/migration/contract，
不能接收请求提供的 URL、SQL、命令或生产客户 selector。缺失、失败或不可信结果仍失败关闭。
第二轮自审：当前 `_persist_execution` 自行生成 environment/seed/cleanup 摘要并写 deleted，不能
证明创建或清理确实发生。必须补齐来自受信执行器的实际回执和验证，不能仅让十个 outcome 恒真，
也不能把自动生成的 SHA256 当作签名、环境隔离或清理证明。
该缺口与原生数据库兼容均属于本批必须完成的工作。兼容测试通过后仍不得把本条标记全部完成，
更不得用它解除 G6/G8。历史受控合同测试保留，但原文“固定隔离 runner 已交付”结论已撤回。

## 8. 受信执行回执修复

服务只接受 `metahub.assurance-execution.v1` 签名 envelope。部署配置
`METAHUB_ASSURANCE_RECEIPT_TRUST_BUNDLE_PATH` 提供公钥、有效期、runner 镜像和隔离策略摘要，
每次结果提交重新读取；请求不能自带公钥。签名用途独立，绑定 assessment/owner 摘要、profile、
code/migration/contract/artifact、generation、随机租约 challenge、执行期限和完整场景 registry。
传给执行器的 payload 不包含生产 Workspace/tenant 标识，不允许以报告 owner 选择客户目标。

第一轮复核：裸 outcome、签名篡改、不同制品/owner/租约重放、已撤销公钥、未知字段、重复 JSON、
超大内容、场景乱序/缺项均拒绝。镜像与隔离策略必须同时命中部署信任，不接受响应自授信任。
第二轮复核：时间区间和 duration 一致；source/target 不同，fixture manifest 与场景资源类别逐项
一致，创建不晚于执行、清理不早于执行结束。清理失败生成 error/P0；未验签的结果不写 cleanup
journal，environment receipt 为 NULL，旧 DTO 必填摘要使用全零表示“未取得”，绝不代表实际回执。

仍未完成：真正的固定场景隔离 worker、制品与运行环境的实际度量、完整原始签名回执持久化、每阶段
短租约/撤权检查、真实 fixture 创建与 exact cleanup。当前测试里的签名和 fixture 是明确标注的
编排测试数据，不构成实际安全通过或独立审批。因此本条仍为补充修复中，G6/G8 未解除。

## 9. 真实接口探针实施设计

固定探针执行真实 GlobalAccount/WorkspaceSession 认证的 HTTP 合同。第一组接入 projects/applications
跨空间读取与写入、Membership 动态降权、旧 WorkspaceSession 重放和严格输入拒绝。每个拒绝用例
都先验证同一资源的合法访问，防止目标整体故障被误判为隔离成功；外部目标和资源标识不来自评估请求。
租约在每次请求前后检查，撤权后立即停止发送新请求；输出只包含固定检查码、HTTP 状态和摘要。

第一轮自审：不得用认证替身替代真实注册、验证码消费、登录和切换；隔离邮箱的本地投递只负责测试
账号开通，不能冒充外部身份认证或审批。固定路径中的 fixture ID 必须验证，禁止 URL/SQL/shell 输入。
第二轮自审：未实现的 cache/queue/object/metrics/late-response 场景必须返回 error；合法读取对照、
异空间写后原值验证、旧会话明确重放、降权后重新签发 WorkspaceSession 仍无写权限都必须有实际响应证明。
本阶段验证探针功能，不代表 OS/网络隔离部署、镜像度量或完整 10 场景已交付，后续仍接入同一执行链路。

2026-09-07 实施复核：探针拒绝重定向、不同来源客户端、Authorization 混用及非法 fixture ID。
每次请求与响应分块前后检查租约，整体 deadline 和单请求 10 秒墙钟上限约束挂起调用；撤销、超时或
取消后实例永久 fencing。响应上限 256 KiB，拒绝压缩后再读正文，缺失/重复资源及无效 JSON 不得穿透为
成功结果。检查记录只保留固定代码、状态和布尔结论，证据摘要不包含凭据或原始响应。

第二轮实施复核补入可观察的合法改名及回读，避免空操作 200 被当作真实可写证明；跨空间拒绝和成员
降权拒绝后均验证完整项目投影未变。真实权限中 developer 不具备 project.write，因此使用临时 admin
降为 viewer；严格输入按已存在的 422/identity.request_invalid 合同判断，不为探针修改服务端权限。
当前真实注册/验证码/登录/邀请/空间创建与切换由集成测试提供，未做鉴权替换；ASGI 进程内运行不证明
TLS、容器/VM 或网络隔离。租约回调的持久化协调、剩余五场景及全 owner registry 仍待实现。

## 10. 持久化短租约设计与两轮自审

单次执行许可为 30 秒，评估的 1800/14400 秒只作为整体 deadline。每个探针请求前后经协调器读取
assurance_runner_leases、当前 requester 权限与 owner 状态，剩余 15 秒内用原 token、fencing generation、
permission revision 和到期时间做 CAS 续租。新许可不得跨越评估 deadline，也不能复活过期租约。
运行器回调只接收 lease capability，序列化请求不携带数据库配置；真实远程安全传输仍单独接入。

第一轮自审：绑定 assessment/owner/profile/build/generation/challenge/起止时间，复核场景 registry。
operator 禁用或版本变化、角色撤销、全局账号禁用及 Workspace/tenant 停用都停止许可；失败和取消后的
capability 永久拒绝后续调用。不得用延长 profile deadline 或自动后台保活代替逐阶段检查。
第二轮自审：所有续租按当前 fencing generation 比较，最终结果也保存并比较实际 generation；
接管后旧实例不能落库。执行器失联后调度器将过期 running 评估落为 error/P0，保留失败事实，不伪造
环境、fixture 或 cleanup。调度与结果提交之间必须重查绑定，竞态只允许一个终态，不允许重复事实。

### 排队撤权与崩溃恢复补充复核

2026-09-07 内存基线复现：入队事务提交后、创建租约前撤销 operator，原执行入口直接抛错，留下
没有 lease 的 queued 评估；现有调度只处理 running，因此无法收尾。部署文档还保留了旧单参数
runner 合同，需与本批接口一起更新。磁盘耗尽的原生测试未完成，不计为通过。

第一轮自审：协调器先以 CAS 认领已有 queued 事实，再检查当前权限；失败只记录 error/P0，绝不
调用 runner 或签发可用执行许可。排队启动上限固定为 30 秒，过期任务不得迟到启动攻击。认领记录
不是环境或执行证明，所有未知度量仍为空或明确的未取得值，不生成 cleanup journal。
第二轮自审：调度器每轮有界扫描过期 queued，使用同一认领和失败落库路径；与原请求竞争时只允许
一个认领成功。认领后进程再次退出则由 running 短租约回收，恢复不依赖原请求或原账号继续有效。
验证覆盖撤权发生在入队后、排队超时、迟到启动、重复调度、原 token 不变但 fencing generation
已变化、Workspace/tenant 停用和禁用后恢复仍不能复活旧 capability。

## 11. 原始回执归档设计与两轮自审

执行链后续批次保持真实隔离 worker、精确 fixture 生命周期、远程许可和原始回执同一目标。
当前先完成被服务丢弃的验签原文归档：新增 0142 双数据库增量，不修改 0129 或 0139 已发布校验和。
归档表以 assessment 为唯一键，带复合 owner、原始 UTF-8 envelope 及其 SHA256、规范请求 JSON 与
摘要、所选部署公钥快照与摘要、验签时间和最终 fencing generation。只收已验证的闭集 DTO；不存
凭据、攻击正文或原始 API 响应，不把 archive 原文加入公共 API、Console 或日志。

第一轮自审：归档与 run/finding/assessment 终态同事务，归档失败不能产生 passed。插入必须绑定
running assessment 和当前 lease generation；UPDATE/DELETE 被数据库约束拒绝。相同请求重放
读取原事实而不追加新归档；旧 worker 或异 owner 无法注入原文。无效签名不归档，清理失败但验签
有效的原文保留，防止失败证据消失。历史已丢弃的原文不能从摘要恢复，因此不伪造历史回填。
第二轮自审：保留收到的原始字节而非重新序列化 envelope；JSON 明确为 UTF-8，拒绝重复键与未知
字段。公钥快照只证明当时部署信任，不能替代未来提交时的当前信任检查。回滚在归档非空时拒绝，
并保留 tenant 删除清单的 archive_then_delete 分类。SQLite/原生 PostgreSQL 均验证迁移重放、空表
往返、跨 owner/篡改/更新删除拒绝与事务回滚。数据库追加保护不声称抵抗 DBA 或替代外部 WORM。

实施复核：原生 PostgreSQL 的 TRUNCATE 不经过行级 DELETE，因此需增加语句级拒绝触发器。
公钥快照保存经校验 DTO 的规范 JSON，时间可规范为 Z；只有 envelope 要求保留收到的逐字节原文。
最终提交时才发生撤权的，保留先前已验签原文，但结果一律 error，且不再采信其 cleanup；若 generation
已被接管，则原文和结果都拒绝写入。归档或后续结果写入失败均回滚整个事务，保留 running 事实供恢复。

清单登记修正为实际 source_table/tenant_key_column/source_prd/created_at 四列，并恢复全部不可变
保护。SQLite 使用 sqlite_schema/pragma_table_info；原生 PostgreSQL 使用 pg_attribute、约束 DDL、
触发器 DDL 及函数体计算本表摘要，不复制冻结 SQLite 哈希。本增量不宣称已改造 PRD-089 的全库原生
删除验证器；全库历史清单迁移和实际租户删除仍须由所属业务范围独立验证。

## 12. 隔离执行环境与真实初始化设计

继续同一安全执行链，先将真实账号/Workspace/项目/应用/邀请的初始化从测试搬入 runner 能力，
并实现专用 rootless Docker 的资源生命周期。初始化只调用现有 HTTP 业务接口；验证码经隔离邮箱
适配器取得，再走真实验证和登录，不能直接写账号或伪造会话。初始化步骤同样逐请求检查许可、
限制时间和响应大小；异常仅返回固定错误码，不暴露密码、Cookie、邮箱或接口响应。

容器管理器使用 Docker SDK，不接受评估请求提供的镜像、命令、环境变量、路径或 URL。部署方固定
不可变镜像 ID；daemon 必须为 Linux/rootless，带专用 assurance 标记且不得混用未标记容器。
每个容器生成唯一 environment/owner marker，禁用网络、宿主挂载、额外权限及日志驱动，使用只读
根文件系统、非 root UID、受限 tmpfs/CPU/内存/PID。创建后从实际 inspect 复核约束，不能把请求参数
当成生效证明。清理只使用当前进程持有的 exact ID，并复核 marker 与镜像，不按名称或前缀扫描删除。

第一轮自审：Docker 访问权仅属于独立隔离 supervisor，不授予生产 Control，也不传入 guest。
初始化客户端必须同 HTTPS origin、无预置 Cookie/Authorization/代理信任，邮箱只能使用固定隔离
域和本次唯一标识。响应中的账号/资源/成员 ID、角色与 owner 必须核对；部分初始化失败不能丢掉
已有资源记录，更不能把未创建对象写成已清理。镜像声明的隐式 VOLUME 也须拒绝。

第二轮自审：短许可在每次请求及响应阶段检查，失败后实例永久失效；秘密不进入 repr/异常/返回摘要。
容器创建后启动失败仍保留 exact handle 供清理；owner marker 变化时拒绝删除并报告残留。删除后必须
确认 exact ID 不存在，Docker 连接异常不能视为已清理。专用 daemon 标记是部署前置约束，不是独立
OS/网络安全认证；本轮 API 初始化集成和 SDK 合同不冒充真实容器运行。

该基础能力之后仍需 guest 打包、逐阶段远程许可、镜像/code/migration/contract 实测绑定、全部十场景
及全 owner registry、真实部署与完整生命周期回执。未接齐前正式 app 继续失败关闭，G6 不放行。

实施复核：HTTP 根地址在当前 httpx 中存在显式尾斜杠的比较差异，因此比较双方统一根路径，发送的
Origin 统一不带路径；不能只规范化一方。Workspace slug 使用 asr 前缀，保留完整随机环境标识并满足
既有 48 字符上限。成员 revision 拒绝 bool，邀请角色、资源状态、租户与项目归属均在下一步前核对。
初始化成功产生 13 条哈希观察记录；记录只证明已收到相应创建响应，不表示这些对象已被清理。

容器补充自审：rootless 本身不保证资源限额有效，daemon 还须报告 cgroup v2、内存/交换区/PID/CPU
限制能力及 builtin seccomp。SDK 禁用用户 Docker 配置的代理注入；只读 inspect 复核实际容器配置。
创建响应丢失、先前已缺失而本次未观察到删除、删除后的查询异常一律不产生清理成功；不扫描前缀补删。
成功删除并确认 exact ID 不存在后保留原观察时间，幂等重试返回同一条记录，不生成新的删除事实。

当前固定 guest 入口仅为待交付协议，guest 模块、镜像构建及许可传输未接入，不提供伪造通过结果的
占位进程。Docker SDK 契约覆盖参数转换和失败路径，不能证明主机上的实际 cgroup、网络或容器隔离。

## 13. Guest 许可管道设计与两轮自审

固定 guest 与独立 supervisor 通过 Docker attach 的 stdin/stdout 通信；只为固定入口开放 stdin，
TTY、shell、宿主挂载和网络仍禁止。supervisor 必须在启动前核对 exact container ID、owner marker、
镜像及有效配置，再独占 attach。生产 Control 不持有该通道；其到 supervisor 的受认证远程连接仍为
独立后续步骤，不能把当前进程内租约对象序列化或给 guest 数据库配置。

管道协议使用四字节长度和严格 UTF-8 JSON，每帧最多 4 KiB，拒绝重复键、未知字段、非标准数值及
类型强转。绑定包含执行请求摘要、评估 generation、环境 ID、整体 deadline 和每通道随机 nonce。
每次请求另有递增序号及新随机挑战，许可只能回应本次请求、在最多一秒内消费，不能缓存后重复使用。
每次请求前后仍调用权威租约检查，管道许可不代替 30 秒数据库租约，也不延长评估整体期限。

第一轮自审：握手不能由 guest 选择执行 owner、镜像、URL 或命令；绑定由 supervisor 持有的执行请求
和 exact handle 构造。当前阶段的传输真实性来自专用 Docker 管道及其 owner 核验，不冒充可在开放
TCP 上使用的认证协议。没有签名结果、测量或清理事实时不能生成成功回执。完成通知只携带待读取报告
的摘要，不能视作报告已验签或执行通过。

第二轮自审：取消、断流、过期、重放、乱序、拒绝或解析失败使双方通道永久失效；每次只允许一个
未完成许可请求。SDK 负责 Docker 流多路复用解析，但单次底层读取也须限制大小；stderr 不返回调用方，
且累计输出有上限。超时必须 shutdown 并关闭自己持有的 socket，解除后台读取，不按名称影响其他容器。
通道失败后的容器精确清理由 supervisor 的 finally 负责，权限撤销不能阻止清理。进程间/SDK 测试只
证明通信合同，不证明 Linux 管道、实际 Docker、全部探针或真实端到端回执已交付。

实施复核：管道关闭失败不能跳过 exact-ID 容器清理；即使容器已删除，该关闭失败也必须保持未验证，
不能在下一次 close 因“已经关闭”分支而变为成功。标准 SocketIO 包装和底层 socket 均须关闭。
会话关闭回调同时使容器管理实例永久失效；同步权威回调返回后再次检查失效标记，拒绝取消后迟到的
许可。会话完成通知只宣布一个报告摘要；当前尚无实际 guest 报告读取、测量核对和签名回执组装。

## 14. 固定 Guest 执行与报告原文设计

继续同一执行链，guest 入口固定，不接受请求中的命令、模块、URL、数据库或路径。镜像内包含独立
合成数据库基线，运行时只复制到本容器的私有 tmpfs；生产配置、宿主 `.env`、凭据和 Docker socket
不得继承。guest 使用完整 Control 应用装配及真实鉴权，通过仅绑定 loopback 的 HTTPS socket 访问，
证书每次生成且客户端必须验证该证书。验证码只用非生产隔离交付适配器，不替换验证或登录函数。
先使用明确标识的 SQLite 执行配置完成真实进程链，不能将它声称为 PostgreSQL/外部存储/全部 owner 验收。

第一轮自审：启动、初始化、每个探针请求和报告传输都必须持续检查已有许可；取消或撤权后禁止报告
成功。启动过程可能超过单次管道空闲时间，因此阻塞初始化期间仍要检查权威许可，不能缓存许可。
stdout 专用于协议，应用日志不得混入帧或异常输出。TLS 验证、代理禁用及绑定地址均不可由评估输入
覆盖。基线永远只读，guest 对副本应用增量；文件清理仅属于本次创建的精确私有目录。

第二轮自审：Docker tmpfs 可能在进程退出后消失，不能依赖退出后再读取其中的报告文件。报告通过
严格有界分块传输，每块继续绑定请求摘要、序号、挑战、报告摘要及总长度；supervisor 验证顺序、
长度和最终摘要后才返回原文字节。收到原文不等于可信执行，仍需后续独立镜像度量、完整场景/owner
覆盖、fixture 清理、容器删除观察和签名回执组装。guest 只输出闭集结果、观察时间和标识摘要，
不输出账号、密码、验证码、Cookie、原始 API 响应或路径。当前五个缺失场景保持 error，G6 不放行。

实施复核：完整 Control 会保留 SQLite idle pool，停止 API 后还必须关闭本 guest 进程持有的空闲
连接，再删除精确私有目录。清理失败不回传报告，不把删除异常忽略为成功。阻塞初始化使用线程时，
取消 await 不等于线程停止，必须等该线程结束再清理目录；硬超时仍由 supervisor 清理容器。
本轮实际进程/TLS 结果仅覆盖 SQLite 配置；Linux 入口和镜像配方尚无原生构建/部署证据。

## 15. 真实资源隔离场景批量设计与两轮自审

2026-09-07 继续同一安全业务批次，剩余五类场景保持完整范围，不以 HTTP 列表结果代替实际资源。
共用既有真实注册、Workspace、权限、短许可和私有 Guest 生命周期。初始化 DTO 增加从创建响应
核对的 source/target tenant ID；HTTP 请求不能传入观察器、SQL、路径、密钥或资源后端配置。

| 场景 | 实际执行与必要证据 |
| --- | --- |
| cache_owner | 经真实导航接口创建并反复读取持久化投影；观察 head/version/items/sources 的复合 owner、版本和内容；合法偏好变更与跨 owner 注入后分别复读，不能只比较空列表或要求不同 ETag |
| queue_owner | 使用实际异步工作创建、领取、处理与完成路径，验证租户和 lease 绑定、外租户读取及迟到提交拒绝；同步空导出和队列列表不构成执行证明 |
| object_prefix | 双租户经真实 multipart 接口上传同名不同正文；检查数据库 owner、磁盘原文/抽取密文、合法认证解密、跨租户/文件/字段交换拒绝、非法路径及覆盖拒绝；对象键按现有随机 file ID 合同，不虚构租户目录 |
| metrics_labels | 经真实业务请求进入 telemetry buffer，再检查实际 flush 的计数与标签；验证两个租户独立、伪名化、未知标签输入和权限撤销，不以单独 hash 函数测试替代指标链 |
| late_response | 使用真实 Console 和浏览器，对已认证请求施加受控延迟，完成空间切换/撤权后释放旧响应；检查 DOM、缓存与后续请求均不回填旧 owner，Python mock 不构成浏览器证据 |

第一轮自审：新观察器只用于固定 SQLite/本地密文存储配置，数据库以只读模式打开，限定查询和有界
结果，不开测试后门、不替换鉴权。文件密钥单独随机生成，存储根明确位于本次私有目录；合法认证
解密必须先成功，随后才测试替换 owner 的拒绝，避免把缺失文件/错误密钥误当成隔离成功。缓存必须
存在真实非空投影项与源记录，且 HTTP 返回的 snapshot/generation 对应实际持久化记录。

第二轮自审：所有 HTTP、观察及有限本地存储攻击前后检查当前许可，失败后永久失效；观察器缺失
返回 error，不降级成 passed。只返回闭集检查码、布尔值、时间和标识/状态摘要，不返回正文、密钥、
Cookie、对象键、原始数据库行或接口响应。对上传响应丢失、检查失败及取消，仍由已拥有的精确
Guest 私有目录收尾；观察到创建不等于观察到删除，报告不得自行宣布完整清理或 full owner coverage。
缓存和文件的当前实现不覆盖响应缓存、外部对象存储及全部 owner registry；后续 queue/metrics/
browser、实际镜像、远程认证、度量、签名及归档验收仍是同一目标的必需工作，G6 不因此放行。

实施复核：导航非法请求使用既有 422 合同。拒绝响应会清除发起客户端 Cookie，攻击请求改用
独立客户端携带真实已验证会话，不污染合法对照的 cookie jar，不改变产品的清 Cookie 策略。
实际文件上传暴露旧 Bearer 路由与旧 Session 写事务的迁移缺口，按 PRD-147 单独修复和审计，
不在探针内签发 Legacy Token 或直调 create_file 代替 HTTP。正常 Guest 当前包含七场景/247
检查点及 10 条资源观察；上传原文不输出，磁盘篡改只能产生 failed，资源创建后撤权禁止回传报告。
