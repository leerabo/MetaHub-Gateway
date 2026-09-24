# PRD-148 有数据业务页面审计

PRD-148 未完成。审计使用隔离数据库、真实账号登录及 Cookie API，不使用原生 computer-use，不作为生产商业验收或正式稳定性证据。

## 修改前合并清单

证据：`.artifacts/product-qa/20260920-populated-r1/prd148-populated-domains-chromium-scale-1.json`。运行退出码 1。完整报告及六张截图保留。

| 编号 | 分类 | 复现与影响 | 批次处理 |
| --- | --- | --- | --- |
| OBS-DOM-001 | 真实失败 | admin 使用真实会话经 API 创建工单，列表 200 且包含该工单，Support 页面却没有业务根节点或表单；developer/viewer 也只显示外壳 | Support 域迁移至当前 Workspace 授权上下文，并验收创建、回复、解决和权限边界 |
| OBS-DOM-002 | 真实失败 | admin/developer 的设备列表 API 200，但私有 Gateway 页面没有业务根节点或管理入口；viewer API 403 与回退符合边界 | Device 域单独迁移；仍缺有设备数据及真实设备交付证据 |
| OBS-DOM-003 | 已修复并通过边界复验 | 三角色保留正常登录 Cookie，从 Workspace 直接访问 Partner 都得到 400 partner.credential_conflict | `.artifacts/product-qa/20260920-populated-r6` 中三角色 handoff 均 200，随后 Partner 均得到预期 `403 partner.membership_required`；增加显式服务端 `POST /identity/partner-handoffs`，Partner 混合凭据拒绝仍保留 |
| OBS-QA-001 | 测试夹具问题 | human-r4 聚合 passed 漏算 expected=false、失败操作和禁止文本；HTML id 被当成可访问名称，通用点击可能自行离开当前页 | 原报告保留，撤回完整交互通过结论，补充严格业务断言 |
| OBS-EVD-001 | 证据绑定问题 | human-role-batch-r3 曾被错误重绑到后来源码；Excel r3 索引摘要与文件不符 | 已恢复 r10 批次来源，Partner r7 独立绑定 r12，并更正实际工作簿摘要 |

Support/Device 源码根因：当前 Console session.context 仅包含正式 Workspace authorization，而旧 PageOwner 仍要求 legacy user/tenant/session 和 authorization_fingerprint。不能通过伪造旧身份字段修复，应使用真实 Workspace owner、会话代次、授权指纹和导航边界。

## 已通过和限制

- Support/Device 集成批次 24/24，0 失败、0 错误、0 跳过；`outputs/prd148-support-device-current-20260920.xml`。r11/r12 与终态复核 r13 摘要一致。集成使用夹具，不是实际多角色业务验收。
- 严格 role-r4 的 180 个路由权限与外壳布局检查通过，不能证明业务内容及写入动作。
- human-r4 的完整交互结论不被采纳，详见 `outputs/prd148-evidence-review-current-20260920-r4.json`。
- 旧 PRD-087 的 0080 到 0155 迁移基线漂移仍未闭合，不能改称 Partner 页面产品失败。
- 真实付费、治理、私有设备、六方会签、发布复核、回滚、四类对账、scheduler-enabled 259200 秒和 HEAD/origin 一致证据仍不足。

## Support 批次验证

`supportTicketPageState.js` 现从已验证的 Workspace shell/session 创建页面 owner，以 workspace、真实会话代次、权限指纹、成员版本和导航路由隔离异步读写，没有构造 legacy tenant/user。相关单元测试 8/8，包括只读拒绝、失效会话、撤权和迟到响应。

`.artifacts/product-qa/20260920-populated-r2/prd148-populated-domains-chromium-scale-1.json` 中管理员与开发者各完成创建 201、回复 200、升级 201、解决 200、评价 200、重开 200，以及刷新后消息持久化核对；查看者无创建入口，实际写入返回 `403 support.permission_denied`。三个角色的 Support 页面测量无溢出、裁切和交互重叠。报告 `support_workflows_passed=true`，**整批仍为 failed、退出码 1**：Device 两角色空页面和 Partner 三角色入口冲突仍在。

浏览器批次源码绑定 r15（`75071449ad83d1474407ee0d35213c731f3bfa1905f772d4509bfc4e4dfa0c55`），封存时 `source_unchanged=true`。此后同步前后端生产资源摘要并修正 QA 脚本，后来的整体源码不能冒充该浏览器批次的原始来源。构建 294 assets，摘要 `5a5588c695f163d141e795889a8e094f94ae1e5750440f09d8cff8b0e04cd48b`；lint 838 files。第一次 lint 与构建并行触发 dist 文件删除/重建竞争的 ENOENT，属本次验证调度冲突，顺序重跑通过；不得据此指称产品运行时异常。

Support 尚未验收附件上传/扫描的完整浏览器流程、平台客服协作及每个权限组合。视觉检查另发现：英文账号下业务区仍为中文；创建表单位于工单详情下方；附件要求手填文件 ID，缺少直接选择文件的入口。以上作为后续体验批次合并项，不能将当前主流程通过扩展为完整 Support 或 GA 通过。

工作簿 r4 仅更正 `00_变更说明!B5` 的证据解释，`01_PRD总表!B60` 仍为“未启动”。18 个其他 ZIP 成员字节不变，目标表非目标 XML、样式及公式保持。原生表格渲染依赖 `skia.node` 不可加载，尚无 r4 新视觉预览，此限制保留在工作簿验证文件中。

QA 夹具修正后的局部复验 `.artifacts/product-qa/20260920-interaction-focused-r5` 已终态退出 0：三角色 × quickstart/support/private-gateways 共 9 页，授权断言、实际可访问名称、点击失败、禁止文本及几何检查均无记录问题，源码前后摘要相同。它只验证本次修正的判定逻辑和可逆控件，未重跑全部 180 页，也不改变 Device 缺业务控件的失败结论。

有数据域批次 r6 已在当前源码和真实隔离登录下通过：Support 主流程、三角色服务端 handoff 和 Partner 无会员边界均通过；Device 仍只有空态 Deployment，不能替代真实设备交付验收。证据为 `.artifacts/product-qa/20260920-populated-r6/prd148-populated-domains-chromium-scale-1.json`。

## 2026-09-22 当前源码复验

当前源码有数据域批次为 `.artifacts/product-qa/20260922-populated-domains-current` 和 `.artifacts/product-qa/20260922-positive-domains-classified`：Support admin/developer 的创建、回复、升级、解决、评价、重开和刷新持久化通过，viewer 写入为 403；Device 注册返回 201 且刷新后可见；Partner handoff 返回 200、归属投影返回 200，布局无溢出、裁切或交互重叠。正向报告中的 License 尚未签发 404、域切换 401 和导航卸载 `ERR_ABORTED` 已按接口合同和生命周期分类为预期事件，意外响应、请求失败和控制台错误为 0。该批绑定当前源码证据 `outputs/prd148-source-current-20260922-openrouter-static-i18n.json`，SHA256 `32c39158491e80b47ae38ca0cacb93b728b3948509fa51eaedce053b2bee4c1f`。

S1 注册到组织、Provider、员工 KEY、Gateway、计量和撤权的完整真实浏览器链路仍没有当前终态。最近历史批次在 `1280.onboarding-navigation.tenant` 截图等待字体阶段失败；当前脚本要求的固定迁移基线 SHA256 `062ae8e3a8024030efb712a5120793483203b86d552fcfd86a46e9bf804f4c2a` 在 `.tmp` 中不存在，现存基线候选均不匹配。因此本项分类为迁移基线缺失/证据不足，不放宽校验，也不把历史部分成功改写为完整通过。
