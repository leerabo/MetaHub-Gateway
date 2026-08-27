# PRD-127 测试记录

状态：已完成。私有 Gateway release、确定性分批 rollout、维护窗口/冻结、失败自动暂停、可恢复 Agent 命令、单节点回滚、Workspace/Platform Console 和 0119 已交付；本条保留独立设计、测试记录、敏感审计和提交。下一权威项为 `PRD-128 - 私有Gateway License、容量与离线宽限`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0119 Migration | `0118 -> 0119 -> 0118 -> 0119`、权限与角色矩阵、复合 owner、不可变 release/event、状态 CHECK、tenant deletion manifest、运行中事实/down guard、完整性与外键 |
| Release/Rollout Control | 双人发布、签名/兼容矩阵、确定性 cohort、维护窗口/冻结/策略 revision、幂等 replay、pause watermark、Agent lease/start/report、短租约重启恢复、总 deadline 收敛和 rollback |
| Shared/Gateway | Ed25519 用途分域签名、exact manifest/command、owner/hash/generation/兼容验证、有界 drain、staging/commit/LKG、取消水位、崩溃恢复及 rollback receipt |
| Console | Workspace/Platform Cookie/CSRF/DTO/store 隔离、权限扩展白名单、release/channel/window/freeze/rollout/rollback 控件、空态/错误态、迟到响应防护和有界表格滚动 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| Control API/Migration/Navigation | `5/5 passed`；0119 roundtrip/down guard、完整 release-rollout-Agent-report-pause-rollback 闭环和双域导航权限 |
| Shared SDK | `7/7 passed`；签名、窗口、一次应用、drain timeout、commit 取消复核、重启恢复和 rollback/LKG |
| Gateway synchronization | `2/2 passed`；machine control path、201 report 与 204 idle |
| PRD-127 Console | `4/4 passed` |
| PRD-120..127 Console 必要回归 | `41/41 passed` |
| Python static | PRD-127 变更文件 Ruff 通过 |
| Console lint | `console lint ok (382 files)` |
| Browser 390x844 | Workspace `#private-gateways` 与 Platform `#edge-upgrades` 均无页面横向溢出，升级区和空态/控件可见，浏览器日志 `[]` |
| Roadmap | generator 自校验通过；validator tests `4/4`、CLI `169/169` 通过，Excel/ZIP 完整且下一项 `PRD-128` |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮按 owner、审批和跨域权限复核。修正 Console 后端扩展权限白名单遗漏 PRD-125..127 权限、导致合法登录失败的问题，并加入完整验证权限集测试。release 发布坚持双人审批和用途分域签名，平台 rollout 不得绕过客户维护窗口/冻结，Agent 只领取自身 Deployment 命令；Workspace/Platform 的 Session、DTO、request client 与状态缓存独立。
- 第二轮按租约与终态复核。发现命令总 deadline 之外的短 lease 到期没有恢复路径，Agent 崩溃会让 target 永久卡在 `leased`；修正为事务内清除旧 nonce/expiry、命令恢复 `issued`、target 恢复 `selected`，并验证重新领取、开始和上报。rollout 总 deadline 自动暂停，rollback deadline 将 attempt 标记失败，迟到上报只保留审计。移动端宽表改为局部有界滚动，390x844 页面宽度保持在 viewport 内。无未关闭 P0/P1。

## API、恢复与浏览器证据

- 0119 从 0118 基线回填兼容 head，验证权限防漂移、不变 release/artifact/event、状态机、业务 down guard、`integrity_check=ok` 与外键违规 0；旧 PRD 截断 migration fixture 继续保留其历史版本断言。
- API 闭环覆盖 release 创建/审批、rollout 预检与开始、Agent lease/start/report、失败阈值 pause、Workspace 策略 CAS 和 rollback。幂等键重放保持同一事实；owner、签名、hash、generation、policy revision 或 compatibility 任一漂移均失败关闭。
- 短 lease 到期可以由新 EdgeSession 恢复；总 deadline 到期进入静态终态，命令不再重新领取。Agent 在 commit 前复核取消水位，drain 超时不 staging、不切换；切换成功后 receipt 与 LKG 原子持久，重启不会重复执行。
- 本地已认证 Chromium 会话在真实 390x844 viewport 验收 Workspace 和 Platform 两个入口。Workspace 六个升级区、维护策略和回滚控件可见；Platform release/rollout 空态与创建控件可见；两页 `document.scrollWidth=375`、viewport 宽 `390`，浏览器日志为空。默认 1280x720 viewport 已恢复。

## 敏感审计与静止终态

- 提交前审计扫描 `42` 个变更文件（`41` 个文本、`1` 个 Excel）及 Excel 的 `20` 个 ZIP 条目，并对本地 `.env` 中 `4` 个实际敏感值逐值比对；已知值、私钥标记和生产 Token 模式命中均为 `0`。签名模型和异常使用固定字段/错误码，私钥、artifact 正文、verification code、WebAuthn credential、step-up grant 不进入 DTO、DOM、URL、Storage 或日志。
- Agent 命令只包含签名 manifest、固定路径标识和水位，不接受自由 shell、credential、Secret、Token、正文、stdout/stderr 或 traceback。Platform 投影不返回 Workspace 显示名或客户正文；tenant 与 platform Cookie/CSRF/client 保持分离。
- `.tmp`、pytest basetemp、cache、生成数据库和 Excel 锁文件均为可复现产物；正式结果写入本文、测试代码和 Git 后直接删除。未跟踪 PRD-101..105 文档保持不动。

## 批次门禁

本条只运行受影响单元/API 冒烟、必要 Console 回归和两页移动视口验收。真实 Linux/代理/断网、请求排空、N/N-1 升级回滚、Canary 30 分钟、单节点 10 分钟、60 秒自动暂停、Chromium/Edge/Firefox 多缩放、长稳、全介质敏感扫描、性能、安全以及 0111..0122 fresh/逐级/down guard 留到 PRD-130 独立提交后统一执行 G5，不能用当前 Windows/SQLite/进程内证据替代。
