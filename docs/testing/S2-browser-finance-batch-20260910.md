# S2 使用者用量与内部财务浏览器验收

设计及两轮自审：`../prd/S2-browser-finance-batch-20260910.md`。本记录覆盖使用者用量、预算、内部账本和共享导出，不代表完整商业计费或整个 Excel 已完成。

## 源码联合结果

`.artifacts/s1-browser-zvjj5lze/acceptance.json` 的业务结果为 passed：1280、390、1440 三视口共 27 阶段、21 布局检查。该次 `source_root=C:\MetaHub`、`production_build=false`，不能替代独立候选生产包验收。

- 每个视口从注册、创建组织开始，建立两名免登录使用者和独立 KEY。数据库仅增加三个 Owner 对应的 global_accounts、users、workspace_memberships，各 3 行；六名使用者不创建登录账号。
- 每个组织注入三条明确标识的合成用量，并形成三条既有 legacy-amount 账本分录。不是 Provider 推理，外部模型调用为 0。
- 预算以 `12.345678` 创建，block 改为 alert_only，再修改为 `23.456789`，最终恰好一条规则。
- 调整草稿取消并重开后原因清空。提交 `0.000001` 借记、查看详情、提交同额冲正后共五条账本分录；原三条分录逐项保持，新增调整净额为 `0.000000`，不是删除原账或重算费用。
- 按使用者筛选并通过浏览器真实下载 CSV，核对恰好两条记录、精确 request_id 和 KEY 归属。KEY 轮换、使用者停用后，历史归属和原币成本 CNY `0.500000`、USD `0.250000` 保持，不混币换算。
- 可见文字输入、选择和文本域最小 16px，无文档横向溢出；页面异常、外部请求、资产失败均为 0。截图仍显示用量筛选动作按钮偏小；随后补为 16px 和最小 40px 高度，需候选包浏览器验证。预算保存后表单清空，截图中的 1000 经源码核对是 placeholder，不是默认金额；规则列表仍显示实际保存值，不能声称整个界面已完成打磨。
- 页面访问统计 `/workspace/navigation/page-views` 有两次 503，错误码 `workspace_session.unavailable`。报告中的业务 passed 和 `navigation_database_failures=[]` 不证明零网络错误，问题尚未定位。

隔离服务已停止，临时数据库由既有验收清理逻辑移除；未修改用户的试用数据库或服务。已目视复核 `390-finance-adjustment.png`、`390-finance-budget.png`、`1440-finance-detail.png`、`1280-finance-usage.png`；1440 账本详情在首屏以下，截图本身不能证明详情全部布局。

## 开发中测试

- `.artifacts/s2-origin-fixture-unit-20260910.xml`：56 项，含来源负矩阵和测试夹具路径、鉴权、回滚及并发去重。
- `.artifacts/s2-finance-frontend-origin-20260910.xml`：473 项，含财务、用量和共享认证回归。
- `.artifacts/s2-api-8wu9yy9g/results.xml`：真实 Cookie 用量/导出 API 定向 1 项。runner 清除 METAHUB 环境、使用隔离 cwd 和显式源码种子路径，不读取用户 .env。
- 独立来源修复暂存构建及 33 后端、95 前端测试，见 `PRD-147-resource-export-origin-tests-20260910.md`。

既有双数据库、权限、动态撤权、审计回滚及短时 HTTP 证据参照 `S2-internal-finance-budget-batch-20260909.md` 和 `S2-consumer-usage-accounting-batch-20260909.md`。不为无改动业务重复运行整套迁移。

## 保留失败与修订

保留各次原始报告，不覆盖失败：`s1-browser-ksoij49j` 为夹具误查不存在的 slug；`7bkqr5ci` 为金额定位器歧义；`tc5xi5hu` 为财务输入 13.3333px；`fsuie9br` 为未解释导航 503；`580gfa72` 为 GET 导出无 Origin；`rt0_5hkj` 为 no-referrer 导致缺少 Referer。夹具、定位器、输入字号和导出来源问题分别修订后通过上述源码流程，导航故障未证明修复。

API runner 隔离 cwd 后首次种子相对路径失败位于 `.artifacts/s2-api-i2ww51lw/`，显式种子路径修订后通过。早期 `.artifacts/s2-api-tmhutp0x/` 的运行 cwd 为仓库，不能作为配置隔离证据。

## 后续验收边界

首次实际 index 候选 `e7e37a979c187b2c566f8857869b965191d8fe51` 构建通过 289 资产，前端 `.artifacts/s2-finance-index-frontend-20260910.xml` 为 473 项、0 失败。API `s2-api-dr7tqybw` 和浏览器 `s1-browser-dp65s1v6` 在启动阶段发现测试引用未提交 helper，未进入业务验收。两者临时目录保留；将撤权模拟改用既有独立请求作用域后需要重新导出候选。此修订不改变任何生产财务或资产字节。

## 独立候选生产包结果

实际 index `22cccf8fc31a27637368d5bfea2d64d99ba1bf9e` 导出至 `.tmp/s2-finance-index-r2-20260910`，289 资产摘要 `3f00f96672fa8bee828a964614aea7fc29497a846a48f935c87b3e6de33a81e5`，与前一候选生产资产字节相同。前端 473 项证据可复用；两处测试支持文件移除未提交 helper 依赖。

- `.artifacts/s2-api-dpenng9g/results.xml`：18 项、0 失败/跳过，完整本批用量与财务 Cookie API，包含动态撤权、权限矩阵、审计回滚、CSRF、冲正及预算阻断释放。
- `.artifacts/s1-browser-36cbe1xa/acceptance.json`：production_build=true，三视口 27 阶段、21 布局检查；用量按钮最小 16px，资产失败、页面异常、外部请求、API 5xx、导航数据库诊断均为 0。三个最后历史归属步骤约 1 秒，不再出现前次两个 34 秒等待。只证明本次没有复现 503，不保证全部故障根因已消除。
- 生产包截图已目视复核 `390-finance-usage.png`、`1440-finance-detail.png`。输入/按钮可读，无水平溢出；仍不以单一截图证明首屏以外的全部详情布局。
- `.artifacts/s2-finance-index-navigation-20260910.xml`：56 项中 55 项通过、1 项旧偏好测试失败。失败断言假定此前 scheduler 测试已产生一条事件；既有 PRD-146 合同明确纯 UI 偏好不得产生 navigation changed，需改为操作前后事件数相等，再独立复核。不能将这份报告记为整组通过。

上述浏览器服务已正常停止，隔离 DB 已由既有逻辑清理。正式试用站仍未替换；其首页/状态页有已发布而未纳入本批的改动，发布时必须保留并核验。

独立 PRD 提交后需要基于实际完整候选源码构建并运行 production-dist 联合流程。真实 Provider 到 Gateway 再到用量、规模性能、三浏览器/多 DPR/Linux、历史平台计量受控恢复及后续业务域仍未完成。已有历史计量告警不得靠跳过旧用量或套用当前权益和价格消除；本批不等待 72 小时才发布已验证增量。
