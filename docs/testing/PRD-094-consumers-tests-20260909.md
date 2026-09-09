# PRD-094 使用者增量验证

日期：2026-09-09。范围为免登录使用者与 KEY，不声称原 PRD-094 或整套 SaaS 完成。

## 自动化证据

- SQLite：`.tmp/pytest-consumers-3o7kusiy/results.xml`，6 passed，0 failures。覆盖无登录身份增量、同名独立身份、一人多 KEY、轮换旧凭据失效、停用阻断、恢复、严格 DTO、越权、CSRF、审计失败回滚、并发轮换 200/409、审计行/导出序号/触发器保留、pristine down/up。
- PostgreSQL：`.artifacts/g6-security-20260907/local-workspace-native-c2af598a0ad64f7d9bfeb31e73b409b0.xml`，2 passed。覆盖真实 PostgreSQL 的创建/切换/使用者/KEY/轮换/停用、不可变归属、带事实禁止回滚、pristine down/up。回滚异常断言收紧为 DbError 和固定代码，并复查安全事实未改变。中间失败 bcb729 的原因是断言要求对外返回已被运行时隐藏的 SQL 文本，不用该失败记录冒充通过。
- 前端：workspaceConsumers、virtualKeys、identity、prd095PlatformAccess、prd112PublicStatus 共 51 passed。覆盖输出转义、无邮箱密码字段、管理员可见性、API DTO、轮换版本。Console lint 811 文件通过；消费者 Python Ruff 和 `git diff --check` 通过。
- 生产构建 289 个资产，271 个可达 JS 模块；前后端资产摘要 `1654c36c05c2380dbc06e142b673ab62a3f6be8ef7fb03df5b94c8640b4fc36b` 一致。

## 浏览器与本地 HTTP

- 独立渲染夹具使用真实 renderer/CSS；桌面 1280 与移动端 390x844 截图均已查看，无页面/表单横向溢出。姓名/部门标签 15px、输入 16px。夹具不代表真实表单提交验收。
- 发布后实际 `/console.html#virtual-keys` 已读取到管理员目录、姓名/部门、员工选择器与轮换按钮，无 API 404。实际页截图已查看；没有借用用户会话写入数据。独立标签关闭，用户原标签保留，视口复原。
- `.tmp/smoke_local_consumers.py` 通过开发服务建立独立测试账号，再走真实 HTTP：创建组织 201、切换 200、Owner 确认、员工创建 201、KEY 创建 201、轮换 200、停用 200。创建使用者前后 GlobalAccount/User/Membership 数量不变。
- Gateway 使用无效 `/feedback` 正文验证鉴权：有效 KEY 为 422，旧 KEY/停用员工为 401；员工 KEY 访问管理接口为 401。重新启用单 KEY 后停用员工仍为 401。未调用上游模型，不把该检查称为成功模型调用。
- 测试组织 `wsp_b061b4d4d215448491029da9bc8e35de` 留作可追溯验收事实；测试使用者与 KEY 均已停用，测试会话退出。不物理删除审计或组织。

## 本地发布

- `.data/metahub-dev-prd117.sqlite3` 从 0147 升级到 0148，quick_check 正常，foreign_key_check 无违规。备份 `.data/consumers-trial-backup-20260909T082445Z.sqlite3`。
- 比对保留 3 条审计、3 条导出序号、18 个审计触发器、4 个账号、2 个空间、3 个成员的有序内容哈希，迁移前后完全一致。
- 预检首次因脚本写错 `workspace_membership_id` 列名而在迁移前退出；修复后成功执行。没有恢复旧库或撤销用户会话。
- Control PID 3868、Gateway PID 6956；两者 `/health` 为 200。前端 5173 PID 336、状态 8002 PID 6892 未重启。前端 identity/privacy/consumer 模块均为 200。
- 后续 PRD-093 性能修复已将 Control 更新为 PID 18888，Gateway 保持 6956。两者健康检查 200，匿名消费者/KEY 读请求为 401；未修改用户会话或再次迁移数据库。

## 尚待验证

最终 S1 合并回归 `.tmp/pytest-s1-consumer-regression-mi28dr3a/results.xml` 已通过 34 项，包含本文件的 6 个消费者用例、注册/Workspace/Membership 与原性能门禁。详情见 `S1-organization-consumer-batch-20260909.md`。

按使用者的用量财务筛选、完整模型与路由调用、正式生产发布、72 小时任务均不在上述通过结论内。独立代码提交仍待依赖顺序的分层暂存，不把未提交的工作树当作提交证据。

## 后续真实浏览器证据

`.artifacts/s1-browser-6y8nim3f/acceptance.json`：1280x900 和 390x844 各自通过真实注册、新建组织、两位员工使用者、独立 KEY、轮换和停用。10 个阶段通过，0 页面异常、0 外部 Origin 请求。数据库中仅管理员增加 2 个 GlobalAccount/User/Membership；4 位使用者和 4 条 KEY 关联不增加登录身份。员工 KEY 访问管理接口为 401，轮换后的旧 KEY 和停用员工均被 Gateway 拒绝。

同一次运行发现并验证了 KEY 字号修复，六个采样最小文字输入字号 16px，无页面横向溢出。已查看桌面和手机终态截图；用例由 `scripts/acceptance/s1-organization-browser.py` 启动隔离数据库、真实 Control/Gateway 和浏览器，成功副本已删除、服务已退出。报告只证明鉴权，不包含模型推理。

上述“按人用量仍待验证”是较早状态；后续实现及限定证据见 S2 两份批次记录，不能据此推断所有财务、生产或长稳门禁完成。最新本地发布见 `S1-browser-local-release-20260909.md`；核心消费者代码的独立暂存仍需后续整理。
