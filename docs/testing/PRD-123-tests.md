# PRD-123 测试记录

状态：已完成。本地 Secret 独立托管、KeyStore/KMS 信封加密、原子轮换与回退、固定 Provider 测试、
Agent 引用同步、Control 零明文安全投影和 Workspace Console 已交付；本条保留独立设计、测试记录、
敏感审计和提交。下一权威项为 `PRD-124 - 云管私有Gateway安装包、预检与接入向导`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration + Control | `0114 -> 0115 -> 0114 -> 0115`、fresh head、复合 owner/不可变事实、权限扩展、down guard、Agent mTLS/EdgeSession/capability、幂等/异 hash 和 Workspace 安全读取 |
| Shared + Gateway | exact DTO、AES-256-GCM/AAD、错误 key/version、DPAPI/KMS KeyStore、schema v2、重启恢复、并发轮换、失败回退、drain/lease、引用阻断删除和 outbox 重传 |
| Local API | loopback/CIDR、结构化 Origin、forwarded-header 拒绝、handoff 摘要/限流、Secure Cookie、CSRF、step-up、owner、CAS、no-store 和 Provider connection ID |
| Console | 云端 exact 安全投影、`security_admin` 独占本地写控件、credentialed CORS、响应上限、Secret/handoff 双层清零、无 Storage/URL、迟到响应和离页/登出清理 |
| Performance | Windows 本地 schema/DPAPI 环境；120 次 active version acquire/unseal 的 p95 必须 `<10ms` |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-123 Migration + Control API | `5/5 passed`；PRD-122 相邻迁移/遥测回归合并为 `11/11 passed` |
| Shared + Gateway core | `39/39 passed`，包含隔离复跑的 `<10ms` 解封门禁 |
| Local API/runtime/CORS | `19/19 passed`；含 handoff、Cookie/CSRF、Origin/CIDR、Provider ID 和 owner 负矩阵 |
| Runtime config | `43/43 passed`；完整配置文件在不读取开发 `.env` 的干净 cwd 下执行 |
| PRD-120..123 Console 专项 | `20/20 passed` |
| Console lint | `374 files` 通过 |
| Python static | PRD-123 源码/测试 Ruff 全规则通过 |
| Roadmap | generator 可重复重建；validator tests `4/4`、CLI `149/149` 通过，下一项 `PRD-124` |
| Diff hygiene | `git diff --check` 通过 |

完整 Console 测试额外执行时，未修改且当前 HEAD 已包含的
`platformPageState.js:2925` 使用 `location/history`，触发 PRD-096 的旧源码扫描断言；PRD-123 文件不在该
失败链路，聚焦与相邻回归均通过。该历史门禁不混入 PRD-123 提交，G5 必要回归会继续报告其状态。

## 两轮实施自审

- 第一轮按 key 所有权、零明文和本地管理边界复核。确认云端 schema/DTO 只有随机 reference、kind、状态、
  revision、固定 outcome/capability 和时间；修正 handoff 从隐式测试注入为部署独立凭据、摘要常量时间校验、
  十分钟 owner-bound Session、Origin/CIDR/forwarded-header/CSRF/step-up 门禁，配置和 `repr/model_dump` 不暴露
  handoff、路径或 KeyStore 材料。
- 第二轮按轮换、迟到响应和策略类防御纵深复核。确认 prepare -> test -> activate -> drain -> retire、旧 lease
  释放前不可删、失败候选不可激活、sync 同 hash 幂等/异 hash 冲突；修正 local Provider tester 参数为
  `provider_connection_id`，并把 Origin 校验从字符串前缀改为结构化 URL 解析，拒绝伪 loopback hostname、
  userinfo、path 和非法端口。无未关闭 P0/P1。

## Browser 与真实边界

- 本地 Chromium 复用 seed 管理员会话加载了本次页面代码，标题文案已包含“本地 Secret 引用”，Console
  warning/error 为 0。当前账号角色为 `admin`，按合同不得显示本地 Secret 写控件。
- 刷新后 Workspace HttpOnly Cookie 已过期，页面返回固定 `workspace_session.required` 并清除旧业务 DOM，
  没有尝试通过 Storage、URL 或伪造 owner 绕过。`security_admin` 写表单、提交后 DOM/字节清零、迟到响应和
  logout 清理由真实渲染/绑定单元测试覆盖；三浏览器真实写链路保留到 PRD-130 的 G5 批次门禁。

## 安全与静止终态

- Secret/handoff 只经二进制 request body 进入本地 Gateway；不进入 query、argv、浏览器 Storage、页面 state、
  云端数据库、事件、日志或错误文本。云端 projection schema 没有 value/ciphertext/token/endpoint 字段，
  Provider 响应 body 不读取，测试结果只保留 fixed outcome/capability/latency bucket。
- 提交前敏感扫描覆盖 `50` 个 staged 文件的 `8,960` 条新增行和 Excel 的 `20` 个压缩条目，并在内存中
  加载 `6` 个本地 seed/`.env` 敏感值做精确比对。已知值、PEM 私钥、长 Bearer/JWT、`sk/mhv/whsec`
  token、session/handoff 实值和禁止暂存路径命中均为 `0`；扫描只输出计数，不输出敏感材料。
- 历史 `.pytest-*`、`.tmp-*`、用户文件和未跟踪的 PRD-101..105 文档不纳入提交。本条测试目录已逐项解析到
  `C:\MetaHub` 并预览，递归清理由本地 safety policy 拒绝，因此保持未跟踪且未暂存；未通过跨 shell 或
  放宽路径范围绕过策略。

## 批次门禁

PRD-123 独立提交后直接进入 PRD-124。G5 的 0111..0122 全迁移、真实客户 KMS/Provider、长稳轮换、
三浏览器三缩放、全介质敏感扫描和完整静止终态仍保留到 PRD-130 独立提交后统一执行，不用本条聚焦、
Windows DPAPI 和单浏览器 fail-closed 结果替代。
