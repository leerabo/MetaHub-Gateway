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
