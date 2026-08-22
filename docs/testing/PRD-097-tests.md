# PRD-097 测试记录

状态：已完成。PRD-097 的 0089 迁移、八步事实求值、WorkspaceSession 资源 allowlist、初始化中心和真实
浏览器验收均已通过；权威下一项为 `PRD-098 - SaaS套餐、试用、订阅与Entitlement`。
`saas_incremental_roadmap_complete=false`，`all_108_prds_complete=false`。

## 已执行门禁

| 测试组 | 实际结果 | 覆盖 |
| --- | --- | --- |
| DB | migration `3 passed` | 0088->0089 回填、fresh install、pristine down/up、非 pristine 原子回滚阻断、完整性和 FK |
| API/SEC/GR/FLOW | onboarding API `2 passed` | 八步状态、seed/placeholder 排除、confirm/defer/reopen、CAS/幂等、权限、WorkspaceSession 与 CSRF/Origin 负矩阵、真实资源链激活和证据失效 |
| Compatibility | legacy/new onboarding 兼容组 `5 passed` | 旧摘要、新 Workspace 初始化、0089 缺失时 N-1 运行、资源认证适配和撤权 |
| Resource compatibility | allowlist 资源路由 `39 passed` | Environment、Provider/Key、local Endpoint、健康探测、Model/Alias、Project/Application、Virtual Key 原有 Bearer 合同 |
| WEB/UI/UCD/COMP | PRD-094/097 focused `12 passed`；Console full `1001 passed`；lint `317 files` | 严格 DTO、Workspace Cookie/CSRF/CAS、未知结果幂等重试、角色裁剪、迟到响应隔离、八步呈现和焦点恢复 |
| Static | Console lint、定向 Ruff、Python compile、JS syntax 和 `git diff --check` 通过 | PRD-097 变更集；环境未安装 mypy，因此不把 mypy 记为通过项 |

## 迁移与事实验收

- 0089 创建 Progress、StepFact、Acknowledgement、ActivationCheckpoint、Event 与 EventOutbox，并为每个
  active Workspace 安装严格有序的八个步骤；最新迁移为 `0089`。
- pristine 数据可回退到 0088 并重新 apply；存在 acknowledgement、checkpoint、event、activation 或
  已变化投影时 down 原子失败，不留下部分回滚。
- 空 Workspace 为 `0/8`。system-created dev Environment、placeholder Credential、seed Model/VirtualKey
  和来源不可证的旧 active 行均不能形成完成状态。
- 测试中的可解密 Credential、成功 probe、同 owner Model/Alias/Project/Application/VirtualKey 和真实
  request+Usage/ledger 链在一次 recheck 后完成八步并激活；事实失效后回到 incomplete 并保留历史事件。
- fresh 与升级库均通过 `PRAGMA integrity_check=ok`，foreign key violation 为 0。

## 授权与并发结论

- onboarding 只由 active WorkspaceSession 授权；Bearer、Platform、Support 和混合可解释凭据均被拒绝。
  GlobalSession 可为账号壳层继续存在，但不会被 onboarding 解释或授予 Workspace 权限。资源 allowlist
  恰好解释 WorkspaceSession 或 Legacy Bearer，未扩展其他 tenant 路由。
- organization owner/admin 可操作全部步骤，developer 不能确认组织 profile；所有请求重验 Membership、
  permission 与 revision，Workspace suspend 或角色降权后立即失败。
- PATCH/recheck 使用 progress revision、带引号的 If-Match 与 Idempotency-Key。相同请求可重放，异 body
  冲突；CAS 竞争让客户端刷新投影，不会覆盖较新的事实。
- 服务端不接受 complete、workspace/tenant/actor/source/evidence 等权威字段。GET 只读，recheck 才追加
  checkpoint、step/activation event 和 outbox。

## 浏览器验收

- 应用内 Browser 连接 `http://localhost:5173/identity.html` 和本地 Control API，直接使用本地测试身份完成
  GlobalSession 登录与 WorkspaceSession 恢复；凭据输入在提交后清空，未进入截图或验收输出。
- 初始真实投影为 `0/8`，第一步 current，其余七步按依赖 blocked。页面实际完成 profile confirm、
  environment defer/confirm/reopen/reconfirm 和 recheck，最终投影为 `2/8`。
- 每次操作后焦点回到步骤状态面板；步骤按钮保持原生 button 键盘语义。自动化驱动的合成 Enter/Space
  只验证了聚焦、未触发 click，因此没有把这项工具限制误记为完整键盘激活证据。
- 390、1280、1440 三档横向溢出、裁剪文本和越出视口控件均为 0；浏览器 console error/warning 为 0，
  密码输入残留长度均为 0。

## 敏感审计

源码、测试、迁移、文档和 Excel 候选执行敏感模式检查；Git 候选不包含 SQLite、WAL、SHM、浏览器 profile、
Cookie、TOTP、Provider/Virtual Key 明文、Prompt/Response 或原始网络正文。测试凭据只用于本地受控输入，
不进入回复、日志、截图、文档或测试证据。

本记录关闭 PRD-097；下一权威需求为 `PRD-098 - SaaS套餐、试用、订阅与Entitlement`。
