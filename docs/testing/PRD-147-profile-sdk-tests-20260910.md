# PRD-147 资料与 SDK 迁接测试

## 已验证

- `.artifacts/s1-profile-sdk-batch-20260910/frontend.xml`：独立产品代码 328 项组合单测通过。资料与 SDK 用例覆盖 Cookie、精确字段、CSRF/CAS、同组织重入、失败重试、真实切组织、迟到响应、重复下载和权限拒绝。
- `.artifacts/s1-index-batch-hkabqabi/results.xml`：6 项 API 用例通过。包含 Workspace 元数据、两种 ZIP 的完整解压与白名单、租户隔离、混合凭据、成员撤权、生成制品期间撤权，以及 Python/JavaScript 旧 Bearer 兼容和分析事件故障降级。
- `.artifacts/s1-profile-sdk-batch-20260910/index-audit.json`：暂存代码秘密扫描与 JavaScript 语法检查通过；Python AST 与改动行 Ruff 检查无发现。旧文件既有长行不混入本次修复。

## 浏览器与发布边界

### 最终合并验收

- `.artifacts/s1-browser-_85vnzmx/acceptance.json`：1280x900 与 390x844 的 12 个阶段全部通过，289 个 HTTP 资产哈希核对通过，页面异常为 0。覆盖注册、组织创建、无登录使用者、独立 KEY 签发/轮换/停用、组织资料保存/重入/重载和八个向导目的页。
- 两个视口分别实际下载 Python 和 JavaScript ZIP，文件名、ZIP 签名及非空内容正确；详细 CRC 和精确文件清单由 API 用例验证。没有把示例下载当作真实首次调用。
- `.artifacts/s1-index-native-318508f5831a437b833a23ad4bb8c8a9/results.xml`：PostgreSQL 两项 SDK API 用例通过，0 跳过，107.701 秒。复用 SQLite 的业务断言，覆盖跨租户、混合凭据、权限变更、生成制品时撤权与下载事件；既有数据库保持不变，测试集群终态为 stopped。
- 最终独立导出树 `9cb3d70ea04615687d458db1534c180421870a2d`：资料、SDK 和向导 23 项相关单测通过；生产表面检查为 271 模块，构建为 289 资产。六项浏览器 harness 源目录/公开路由隔离测试通过。
- 合并试用包摘要 `3bc8a634902a52e2468d4bf72580af71b5b64c4e1bc5ee4f22e5109c4069f7ac`，相对上一在线包修改 9 个资产，其余 280 个保持不变。独立提交包摘要 `adffaa5e735da29fc5764c01f74a17feda676bf0f9f1feb2058d7d9a58b49fda`；两者区别是已在线但尚未归入本次提交的 S2 改动。

此处确认的是增量功能验收，发布事实另行记录。完整性能、跨浏览器/多 DPR/Linux 与后续业务域仍不完整，整个 Excel 目标未完成。

### 历史失败记录

独立产品浏览器曾依次发现 SDK 旧 Bearer/Cookie 冲突与后端仅接受 Bearer，均保留失败记录。合并包 `.tmp/s1-profile-release-20260910/source` 保留已发布 S2 页面，289 个资产中只变更 8 个，摘要 `84dbb9f0e925fea41489d39e336c0906004c080ad7a24a519541ebf5c7ab823a`。

`.artifacts/s1-browser-mu3pcmu3` 的组织资料保存、重入和重载已通过，但整体未通过：Windows Application Error 记录隔离 Python 进程 0xc0000005 崩溃，尚未证明原因。工具调整为故障/超时取堆栈，保留原证据，不放宽浏览器断言。

合并浏览器验收、发布、完整性能及跨浏览器结果尚待追加，不能以此记录声明整个 PRD-147 或 Excel 已完成。未调用收费模型。
