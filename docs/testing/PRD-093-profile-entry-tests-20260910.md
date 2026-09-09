# PRD-093 资料合同测试记录

本记录仅属于资料入口增量，不声明整个 Excel 已完成。

- SQLite：`.artifacts/s1-index-batch-5emvyjwh/results.xml` 中资料 API 用例通过。验证名称权威来源、精确 DTO、CSRF、CAS、旧组织会话失效、重新进入、另一组织不变、事件不包含名称。
- 前端：`.artifacts/s1-profile-sdk-batch-20260910/frontend.xml` 的 328 项组合单测通过，含资料 renderer、保存/重入、迟到响应和真实跨组织状态替换用例。
- 浏览器：`.artifacts/s1-browser-rsw8452q` 中桌面资料保存、重入、重载与 16px 输入检查通过；整体运行因 SDK 后端 401 失败，不作为完整流程通过证据。

本批发布与完整浏览器结果另行追加，当前不得以测试代码存在代替交付事实。
