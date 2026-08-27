# PRD-124 测试记录

状态：已完成。云管私有 Gateway 安装制品、纯读预检、可恢复安装/接入、本地 CLI、双人发布、Workspace 向导和 0116 已交付；本条保留独立设计、测试记录、敏感审计和提交。下一权威项为 `PRD-125 - 私有部署验证与脱敏支持包`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Installer SDK/CLI | canonical manifest、Ed25519 用途域、全 artifact/SBOM/hash、在线/离线复现、纯读预检、root 精确确认、staging/journal/audit、崩溃恢复、补偿、身份复用、卸载保留/清除和凭据零回显 |
| Migration + Control | `0115 -> 0116 -> 0115 -> 0116`、fresh head、seed catalog、不可变 release/artifact/audit、权限防漂移、业务事实 down guard、复合 owner、双人发布、Workspace 隔离、幂等/CAS 和合法状态机 |
| Console | exact catalog/preflight/installation DTO、Cookie-only client、canonical 下载、并发加载、失败恢复、owner 权限、跨标签页 Workspace 切换、响应迟到与三视口布局 |
| Runtime/API | seed 管理员登录、Workspace list/switch、真实 catalog、五个 artifact 下载、长度/SHA-256、no-store、失败预检、恢复后安装/注册健康闭环 |
| Security | tenant-neutral 制品、可信下载根、符号链接/越界拒绝、固定错误码、stdin/安全文件凭据、无 DOM/URL/Storage/argv 明文、敏感值和禁止文件暂存扫描 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| Installer SDK/CLI | `12/12 passed` |
| Control API + Console navigation | `5/5 passed` |
| Migration 0116 fresh/up/down guard | `3/3 passed` |
| PRD-124 Console | `4/4 passed` |
| Python static | PRD-124 源码/测试 Ruff 全规则通过 |
| Runtime/API | 登录、Workspace 切换、catalog、五个 artifact 下载均通过；内容长度和 SHA-256 一致，响应为 `Cache-Control: no-store, private` |
| Browser | Chromium 390/1280/1440 均无页面级横向溢出、文本裁切或控件重叠；Console 日志 0，成功登录后 55 条服务请求无 4xx/5xx |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮按供应链和文件系统边界复核。下载服务统一解析可信 release 根并逐项验证 regular-file、resolved path、长度和 SHA-256；符号链接、路径越界、未知 artifact 和内容篡改均稳定映射 `edge.installer_integrity_failed`。manifest 覆盖 Compose、容器清单、profile、SBOM 和 CLI，preflight 只读取有界系统事实，不创建 staging、journal 或云端 Installation。
- 第二轮按生命周期和凭据边界复核。数据库与服务共同拒绝非法状态跳跃并返回 409 `edge.state_conflict`；只有 `active/uninstalled` 写完成事件，中间步骤写 started。root 下 install/connect 的精确确认下沉 runtime，凭据只在确认后从 stdin/安全文件读取；崩溃恢复按 generation、step receipt 和 hash-chain audit 幂等继续，卸载默认保留三类客户数据，audit purge 在终态 journal 前后两个崩溃窗口均可重入收敛。Console 订阅 `identity.html` 的同源 Workspace 失效广播，严格拒绝附带 Workspace/租户字段的信封，并在重新加载前推进全局代次、清空 DOM、取消全部 Edge lane。无未关闭 P0/P1。

## Browser 与真实流程

- 本地 Chromium 直接使用 seed 管理员凭据登录，并在真实开发库中选择 Workspace 和私有 Gateway。390、1280、1440 三视口均完成导航收缩/展开、可见 Tab 焦点、表格局部横向滚动和页面级溢出检查；临时 viewport override 已恢复，Console warning/error 为 0。
- 真实流程先提交 generation 1 的 `dns_unavailable` 失败预检，页面保持不可安装；generation 2 预检通过后，Installation 按 `installing -> installed -> connecting -> active` 合法序列推进，最终展示“接入完成”“预检 已通过”“安装 运行中”“注册 健康”“无需补偿”以及配置/审计/Secret 保留状态。
- API 实际下载 v1.0.0 的五个 artifact，逐一校验响应长度和 SHA-256；下载不缓存、不接受越界 release 根。页面没有展示或保存注册 token、Secret、Cookie 或 shell 凭据。

## 安全与静止终态

- 安装制品不包含 tenant/Workspace/Deployment 凭据；preflight 只上传固定 check/outcome/reason/duration，不上传主机名、IP、路径、环境变量、代理、CA 或证书正文。CLI 不在 argv、stdout/stderr、journal 或 audit 中保存注册材料。
- 提交前敏感审计覆盖显式暂存的新增加行、Excel ZIP 条目和本地 seed/`.env` 已知敏感值的内存精确比对；扫描只输出计数，不输出密码、Token、Cookie、TOTP、私钥或其他材料。历史 `.pytest-*`、`.tmp-*`、用户文件、`__pycache__` 和未跟踪 PRD-101..105 文档不纳入提交。
- Control API 和 Console 开发服务保持运行供后续 PRD 复用；PRD-124 测试产生的数据库事实是本地不可变开发验收证据，不伪装为生产数据。监听、进程、临时目录和禁止文件在独立提交前再次审计。

## 批次门禁

PRD-124 独立提交后直接进入 PRD-125。G5 的 0111..0122 fresh/逐级/down guard、全新 Linux 容器、在线/离线全介质、代理/断网/N/N-1、Chromium/Edge/Firefox 三缩放、长稳与完整静止终态仍保留到 PRD-130 独立提交后统一执行，不用本条 Windows 开发环境和单浏览器证据替代。
