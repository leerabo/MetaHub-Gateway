# PRD-137 测试记录

状态：补充修复中。0129、assessment/finding/remediation/risk acceptance 编排和 Platform Console
已有实现。2026-09-07 复核撤回“固定场景隔离 runner 已交付”的旧结论；历史受控 runner 测试只证明
编排合同，未证明真实隔离、攻击执行或 exact cleanup。下面历史结果不覆盖本次发现的产品缺口。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0129 Migration | `0128 -> 0129 -> 0128 -> 0129`、fresh/current head、重复执行、Platform/Workspace 权限、assessment/run/finding/remediation/risk/cleanup/event/outbox/action、复合 owner、完整性与业务事实 down guard |
| Security Assurance API | 精确三路由闭集、Cookie/CSRF/Origin/step-up/CAS/幂等、签名 profile、10 个固定场景、任意 URL/脚本/selector 拒绝、owner 漂移同形拒绝、缺失 runner 失败关闭、finding/remediation/独立复验、风险接受双批与过期 |
| Runner 与 scheduler | 短 lease、permission revision 恢复、接管 fencing、迟到结果拒绝、exact fixture cleanup、事件/outbox 消费和静止终态 |
| Console | strict DTO、assessment/finding owner 校验、profile/digest 绑定、run/finding/SLA/remediation/cleanup 裁剪投影、Abort/epoch/revision、动态撤权、无敏感持久化和响应式布局 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-137 Migration | `4/4 passed`；0129 fresh/往返、跨 owner 子事实拒绝和业务事实 down guard |
| PRD-137 API/服务 | `7/7 passed`；缺失 runner 失败关闭、固定 runner/幂等/任意输入拒绝、remediation 新 generation 独立复验、风险接受双批/重复申请拒绝/过期、permission revision、lease takeover fencing 和三路由闭集 |
| PRD-137 Console | `5/5 passed`；固定路由与写 header、strict DTO/owner、裁剪渲染、step-up/幂等重试/撤权和禁止持久化 |
| JavaScript/static | Console lint `console lint ok (411 files)` |
| Python static/format | PRD-137 后端、受影响权限服务、运行时错误目录和聚焦测试 Ruff 全规则、compileall 通过 |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮复核 runner 信任来源、owner registry、路由闭集、finding 证据与复验。未配置 runner 不再伪造 passed，
  而是 10 个场景全部生成 `error/P0` 并使 gate 失败关闭；0129 为所有子事实补齐复合 owner 外键或 owner trigger；
  风险接受保持内部服务，不扩张 Excel 外部 HTTP 合同。修正后无未关闭 P0/P1。
- 第二轮复核 lease、动态撤权、重复审批、前端 owner 漂移和可观测摘要。runner lease 使用 profile 的
  `1800/14400` 秒上限；closed finding 或已有 pending/active 接受时拒绝重复申请；Platform client 严格校验
  assessment Workspace/profile/digest、GET ID、finding/run/remediation owner；cleanup 指标按数值计数解析。
  修正后无未关闭 P0/P1。

## 敏感审计与临时证据

- 正式结论保存在本文和 Git，原始测试结果按运行独立保留。`.tmp` 还包含冻结源码、基础设施和持久
  VM 磁盘，不能整体删除；只可单独核验已停止、无依赖的可重建测试目录后处理。
- 审计不得输出密码、Token、Cookie、client secret、TOTP、step-up grant、签名值、私钥、Provider Key、
  exploit 正文、客户正文、对象 URL 或本地管理员凭据；本地测试凭据只在进程内使用，不打印或持久化。
- Console DTO/DOM/Storage 只保留固定 code、digest、裁剪摘要和计数，不投影 runner subject、原始请求/响应、
  内部 endpoint、生产 selector 或 fixture 内容。
- 未跟踪的 PRD-101..105 用户文档不进入 PRD-137 暂存集；最终暂存只包含本条实现、测试、文档和路线图事实。

## 批次门禁

本条按批次规则只运行 0129、API/服务、Console 和静态专项。`0123 -> 0130` 全数据库矩阵、集成、FLOW、
真实三浏览器三缩放、性能、安全、代理/断网、Windows/Linux、SQLite/PostgreSQL、N/N-1、四波试点、灾备、
支付/Edge 演练与 72 小时长稳统一留到 PRD-138 独立提交后执行。G6 通过也只能形成
`release_candidate`，不能公开 GA。

## 2026-09-07 受信回执修复复验

schema/CAS 后端兼容已修复，风险动作测试按同一对象的 result_revision 排序，不依赖 rowid。
服务新增部署公钥信任与 Ed25519 执行 envelope；全部构建摘要、owner、profile、registry、generation
和随机 lease challenge 都进入签名绑定。缺失/无效回执不再编造 environment/seed/cleanup 或 deleted
journal；全零必填摘要只表示未知。已验签但清理失败也只能生成 error/P0。

- PostgreSQL：`g6-security-998994776de84a6d8b67961bdb3638ea-junit.xml` 12/12 通过，另有 8 项
  未选。包括签名编排、缺失执行器、裸结果/篡改/异制品/撤销信任拒绝、清理失败、整改、双批和 fencing。
- SQLite：`sqlite-r2-junit.xml` 中本条服务/API 13/13、当时加密合同 42/42；整次 61/61。
- 最终单元：`receipt-config-unit-r2-junit.xml` 86/86，其中签名合同增加用途隔离负例后为 43 项，
  另有 PRD-134/135 时间/水位 13 项与共享账号/Workspace 配置回归 30 项。
- Console：`console-unit-junit.xml` 中本条 5/5。改动 Python 的 Ruff 和 `git diff --check` 通过。

证据位于 `.artifacts/g6-security-20260907/`，独立敏感审计为 `prd-137-sensitive-audit.json`。
首轮原生 9 通过/6 失败和定向修复后 7 通过/8 未选分别保留；不合并成虚构的一次完整通过。
测试用公钥和签名来自 `prd137_receipt_support.py`，明确是合成编排数据，不能作为实际安全证据。

两轮设计与复核见本条 PRD 第 8 节，部署合同见 `docs/deployment/security-assurance-receipts.md`。
真实固定场景 worker、运行环境/制品度量、原始签名回执归档、逐阶段短租约与撤权、真实创建和 exact
cleanup 仍未交付。本次只关闭“无可信回执也可声称通过和已清理”的路径，不标记 PRD-137 或 G6/G8 完成。

## 2026-09-07 真实认证接口探针

新增 `metahub_assurance_runner/probes.py`，固定执行 projects/applications 的跨空间读取与写入、
Membership admin 降为 viewer、旧 WorkspaceSession 重放及严格输入拒绝。合法写入必须实际改名并
回读；非法写入拒绝后确认项目投影未变。当前范围不等于全 owner registry 的隔离覆盖。

集成测试新建三个账号、两个 organization Workspace、两个项目、一个应用和一个邀请成员，全程经过
真实注册、验证码消费、密码登录、CSRF、邀请与切换接口，不替换认证依赖或直接写入业务主体。
仅验证码投递使用非生产 test_capture 适配器。SQLite 从只读使用的迁移基线复制，测试仅修改副本。

- `http-probes-r1-junit.xml`：1 项集成测试失败，内部场景 3 通过、2 失败；确认 developer 无项目
  写权限、严格输入错误返回 422。属于探针配置/断言修正，未放宽产品权限或错误合同，保留原始失败。
- `http-probes-r2-junit.xml`：修正后 1 项集成测试通过，5 个场景全部通过。
- `probe-unit-r1-junit.xml`：首轮边界单元 28/28 通过。
- `http-probes-r3-junit.xml`：第二轮增加合法写入真实生效、拒绝后无副作用、深层 JSON 和重复 Cookie
  检查后 33/33 通过。其中 1 项集成测试的 5 场景共 59 个检查点，其余 32 项是受控传输边界测试。
- 本轮 Python Ruff 和 diff whitespace 检查通过。敏感审计见 `prd-137-http-sensitive-audit.json`。

以上证据位于 `.artifacts/g6-security-20260907/`。测试报告排除密码、邀请 token 和会话 Cookie，
传输负例只验证失败行为。集成使用 ASGI 进程内 HTTP 合同，不能作为真实 HTTPS、隔离部署、环境度量、
独立安全验收或清理证明。cache/queue/object/metrics/late-response 明确返回 scenario_not_implemented；
实际 worker、全 owner registry、持久化短租约、fixture 创建/清理和原始签名回执归档仍未完成。

## 2026-09-07 持久化租约与排队恢复

已实现协调器 30 秒短租约、剩余 15 秒内 CAS 续租、当前权限和 owner 复核、整体 deadline 上限、
永久 fencing、最终 generation 比较与保存、过期 running 回收。补充复现并修复了入队后撤权留下
永久 queued 的问题；启动上限 30 秒，超时 queued 经同一认领路径记录失败，不调用 runner。
部署 callable 已更新为 `runner(request, lease) -> bytes`。设计与两轮自审见本条 PRD 第 10 节。

- `lease-r1-junit.xml`：早期租约单元和服务定向 15/15，另有 9 项未选。
- `lease-r2-junit.xml`：当时持久化协调 1 项、capability 单元 11 项、探针边界 32 项和签名 43 项，
  共 87/87。受控 runner 内部检查均有完成标记，不能以预期失败吞掉的断言冒充成功。
- `g6-lease-d137fab14ec6497f840df2062897322d`：原生尝试出现 13 个 setup error，随后磁盘耗尽。
  JUnit 只有 38 字节 XML 声明，报告不完整，不能计为通过，也不能从它推断完整失败原因分布。
- `queue-red-r1-junit.xml`：新增排队撤权、中断、迟到启动用例 3/3 先失败，确认产品缺陷。
- `queue-green-r1-junit.xml`：修复后 2 通过、2 失败；后两项是首次 outbox 投递数错误地断言为零，
  已修正为先完成失败事件投递，再验证重复调度为零，未跳过持久化终态和 runner 零调用检查。
- `queue-green-r2-junit.xml`：排队恢复、Workspace/tenant/账号登录停用、同 token 的 generation
  接管 7/7。停用后恢复也不能复活原 capability；旧结果不落库，超时后只生成一次失败事实。
- `lease-final-r1-junit.xml`：最终 SQLite/API/回执/HTTP 探针/capability 定向 109/109；新增双调度器
  同时认领同一超时 queued，最终只有一个 lease、10 个 error run、10 个 finding，且没有伪造 cleanup。
- `g6-lease-e1664b43329741e4a1cb85ac4ec68f12-junit.xml`：原生 PostgreSQL 21/21，370.17 秒。
  使用独立 SCRAM 测试实例、真实迁移及 seed 模板，覆盖原编排 12 项、持久化短租约 1 项、排队恢复
  3 项、owner/登录撤权 3 项、同 token 接管 1 项与双调度器竞争 1 项。实例已正常停止。
- 本次相关 Python Ruff 和 `git diff --check` 通过。独立敏感审计为
  `prd-137-lease-sensitive-audit.json`，精确限定本条暂存文件；原始记录均保留在同一证据目录。

租约和撤权测试使用受控平台认证及合成签名，属于持久化协调合同；同次 HTTP 探针仍使用真实注册和
会话认证，但为进程内 ASGI。上述结果不证明隔离 worker、完整 owner 矩阵、真实外部网络或实际清理。
远程许可传输、剩余五种场景、真实制品/环境度量、fixture 创建/清理与原始签名不可变归档仍未完成。

## 2026-09-07 原始签名回执归档

设计与两轮自审见本条 PRD 第 11 节。新增 0142 双数据库增量，原文、公钥验证上下文、请求摘要和
fencing generation 与最终结果原子提交；无效签名不归档，有效失败回执保留。已验签后撤权时不接受
成功或清理声明，接管后的旧 generation 不落库。公共 DTO 不暴露原文和公钥快照。

- `archive-smoke-r1-junit.xml`：5 通过、1 失败、54 未选。失败为公钥 DTO 时间规范化 Z 与输入
  +00:00 的断言差异；改为与校验后的 TrustKey DTO 比较，不重写 envelope。
- `archive-focused-r1-junit.xml`：66 通过、1 失败。接管测试未递增 updated_at，被既有租约触发器
  拒绝；修正测试，使接管事务有效后再验证旧 generation 无法归档，未放宽产品约束。
- `archive-final-r1-junit.xml`：测试路径拼写错误，0 项执行，不计通过。
- `archive-final-r2-junit.xml`：SQLite/签名/服务/API/短租约/探针定向 120/120，215.65 秒。
  其中归档 7 项、签名 47 项、服务 13 项、持久化租约 9 项、capability 11 项、边界 32 项和
  真实认证 ASGI 探针 1 项。归档覆盖精确字节、重放、不可变、非空 down 拒绝、空表往返、登记摘要、
  异 owner/损坏摘要/旧 generation 拒绝、归档与后续结果写入失败原子回滚及提交时撤权。
- `archive-guards-r1-junit.xml`：增强真实列摘要检查后 SQLite 归档 7/7，36.22 秒。
- `g6-archive-4516d8c7f8d945ffa767985452f09773`：原生首轮基础设施中断，只有部分进度字符，
  没有完整 JUnit，不能认证整次测试数量或通过。运行栈停在下一临时数据库的 psycopg.connect；
  专用服务仍有进程但 pg_isready 无响应，正常/立即停止都失败。按已核对 PID 和数据目录终止测试
  进程及专用实例、启动器，exec 已退出。事实记录为 `archive-native-interruption.json`。
  未以观测超时自行重启，也未修改产品鉴权；后续本地测试设置 PGCONNECT_TIMEOUT=10、autovacuum=off。
- `g6-archive-guards-9bf165cea0824cd3b807bb94b7f9b283-junit.xml`：原生归档专项 7/7，另 21 项
  未选，174.20 秒。真实迁移/seed/克隆数据库上验证原文、重放、UPDATE/DELETE/TRUNCATE 拒绝、
  归档失败和后续写入失败回滚、撤权保留与 generation 接管拒绝、表/列/触发器函数体摘要和 down/up。
  新实例正常停止，postmaster.pid 已移除，不将专项结果扩张成首次原生整次通过。

原中断实例 PID 3416/14856 经 taskkill 报告无运行实例，但 CIM/TCP 表仍显示旧记录及 9442 端口；
未认证其 OS 资源已完全清理，因此保留旧数据目录、不复用该端口。该残留不作为仍在运行的测试通过
证据，也不将本次新实例正常退出扩张为旧实例已完整清理。
相关 Python Ruff 和 diff 检查通过。本条独立范围的敏感扫描记录为
`prd-137-archive-sensitive-audit.json`，原文与公钥快照未进入公共 DTO 或应用日志。

基线只在模块副本上应用增量；原始 `prd134-api-base0/base.sqlite3` 的 SHA256 仍为
`572173fbf10027815ea1e1ecbdd201d3b7084f6335a86e436a762a68945f3db6`。
归档测试使用受控认证和合成签名，不作为真实隔离或清理证据；G6/G8 未解除。

## 2026-09-07 初始化与隔离生命周期

设计及两轮自审见本条第 12 节。共享有界 HTTP transport 供初始化和五种已实现探针复用，真实
注册/验证/登录/邀请/空间及资源创建已由 `HttpFixtureBootstrap` 执行，不再由集成测试重写业务流程。
新建对象仅保存步骤、标识 SHA256 和观察时间。SDK 仅用于独立 supervisor，未注入正式 Control。

- `bootstrap-smoke-r1-junit.xml`：33 失败，初始化前地址边界误拒绝；原因是仅规范化比较的一方。
- `bootstrap-focused-r1-junit.xml`：75 通过、1 失败；实际 Workspace 接口拒绝超过 48 字符的 slug。
  修正 runner 生成规则，未放宽服务端约束。
- `bootstrap-api-r2-junit.xml`：真实认证 ASGI 集成 1/1，32.15 秒。
- `sandbox-sdk-r1-junit.xml`：71 通过、1 失败；SDK 集合对象测试替身未命中实际调用，未当成部署失败。
- `sandbox-sdk-r2-junit.xml`：78 通过、1 失败；SDK 序列化省略默认 Privileged=false，断言需区分
  请求编码与 daemon inspect。实际 inspect 仍要求 Privileged=false。
- `bootstrap-sandbox-unit-r3-junit.xml`：156 通过、1 失败；测试直接把含 name 的调用参数传入仅接收
  容器配置的 SDK 方法。改用真实高层到低层转换链，仅替换最终传输，拒绝未替换的网络 I/O。
- `sandbox-serialization-r4-junit.xml`：1 失败、79 未选；SDK 会补齐未使用的健康检查可选字段为 None。
  改为验证禁用命令及其他字段未启用，不修改 guest 的禁用健康检查策略。
- `bootstrap-final-r1-junit.xml`：158/158，25.85 秒；后续补充等价根地址互操作测试。
- `bootstrap-final-r2-junit.xml`：最终定向覆盖 46 初始化边界、80 Docker SDK 合同、32 探针边界和
  1 条真实鉴权集成，159/159 通过、0 跳过，23.96 秒。

真实鉴权集成覆盖三账号、二 Workspace、二项目、一应用、一次邀请、空间切换和五场景共 59 检查点；
初始化另有 13 条哈希观察。测试直接检查运行时生成的密码、邀请码、验证码、邮箱及 Cookie 不进入
结果、观察记录、repr 或日志。ASGI 测试没有替换认证，但不证明 TLS/容器/网络隔离。

Docker SDK 合同覆盖专用 daemon、rootless/seccomp/cgroup 能力、不可变镜像、无隐式卷、实际 inspect
配置、撤权后不启动、启动失败保留 handle、owner 变化拒绝删除、API 异常和响应丢失不冒充已清理，以及
确认删除后的原记录幂等。包含真实 SDK 参数转换，但没有连接真实 Docker daemon，不能计作部署验收。
Windows 当前无 Docker/Podman 命令，完整 guest 模块/镜像、许可通道、余下五种场景和全 owner 覆盖
仍待完成。未启动 72 小时等待，也未将本轮结果用于 G6/G8 放行。

本轮不修改迁移、公共业务 DTO 或前端；因此只运行受影响的上述单元与 API 集成，不重复整批数据库、
浏览器、性能套件。独立敏感审计为 `prd-137-bootstrap-sandbox-sensitive-audit.json`，包含精确暂存
源码和所有成功/失败测试证据摘要。共享基线仍要求 SHA256 与前节一致。

## 2026-09-07 Guest 与 Supervisor 许可管道

设计及两轮自审见本条第 13 节。新增严格 4 KiB 帧、执行绑定、单次挑战/序号、短消费窗口、永久
fencing 和独占 Docker attach。SDK 解复用限制底层单次读取并丢弃有总量上限的 stderr；未添加外网
入口、任意命令、宿主挂载或 Docker 权限到生产 Control。固定 guest 的 stdin 只作协议管道。

- `lease-channel-r1-junit.xml`：41 通过、1 失败。测试通过 model_copy 注入 bool 后又经模型序列化，
  bool 被测试序列化器输出为 1，未实际测试 JSON true。改为直接用标准 JSON API 构造非法请求；
  产品解析器仍拒绝 bool/字符串序号、未知字段、重复键、UTF-16、非标准数值及超长帧。
- `lease-channel-r2-junit.xml`：135/135，7.62 秒，覆盖协议、SDK 流、生命周期及本地子进程管道。
- `lease-channel-final-r1-junit.xml`：155/155，8.68 秒，加入真实进程重放、关闭失败及 capability 回归。
- `lease-channel-final-r2-junit.xml`：234/234、0 跳过，31.82 秒。包含 45 协议边界、8 SDK 流、86
  生命周期、11 capability、46 初始化边界、32 探针边界、5 子进程管道和 1 条真实鉴权集成。
- `lease-channel-fencing-r3-junit.xml`：157/157，8.55 秒。会话关闭后同时 fence 容器管理实例，并
  在迟到的同步权威回调返回后复查；只复验协议/SDK/生命周期/capability/子进程受影响范围。
- `lease-channel-close-r4-junit.xml`：97/97，2.04 秒。关闭失败状态不因重试消失；同时关闭 SocketIO
  和底层 socket，关闭异常不跳过容器精确清理。此次只复验 9 个流合同和 88 个生命周期用例。

真实本地子进程通过 OS stdin/stdout 管道调用 GuestLease，覆盖连续许可、撤权、断流、非法请求及
重放，使用真实 AssuranceLease capability 的受控续租回调。它不是完整 guest，也未执行租户攻击。
注册/邀请/空间/资源的真实鉴权 ASGI 集成已改经 socketpair 许可协议，五场景的 59 检查点继续通过，
并检查运行时凭据不会出现在结果、日志或观察记录；该集成仍不证明 TLS 或 Docker 部署。

所有失败记录保留。Python Ruff、diff 检查及独立敏感扫描记录在本轮提交前核验，扫描记录为
`prd-137-lease-channel-sensitive-audit.json`，只覆盖本条精确暂存文件与上述六份原始测试证据。
共享 SQLite 基线未变。当前完整 guest 模块/镜像、真实构建度量、Control 与独立 supervisor 的认证
通道、剩余五场景、全 owner registry 及真实签名回执仍待接齐，不能将本轮管道合同用于 G6/G8 放行。
