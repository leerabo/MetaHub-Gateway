# S2 用量、预算与内部账本本地发布

日期：2026-09-10。入口 `http://localhost:5173/`。本批已发布，不代表整个增量开发 Excel 已完成。

## 独立提交与验证

- `b818260`：PRD-147，浏览器同源 GET 导出来源校验，保留 CSRF 和单凭据权限边界。
- `ebdf71d`：PRD-146，六条导航服务在工作线程执行，避免同步数据库阻塞事件循环。
- `043f24e`：PRD-099，组织会话用量/导出/预算，使用者聚合 DTO 及共享财务交易权限范围。
- `6b4bda9`：PRD-144，组织会话内部账本、微元调整、幂等、冲正与原生数据库金额校验。
- `3fb330e`：PRD-094，稳定使用者归属的独立测试和敏感记录，引用用量消费方的公共代码，不重复提交。
- `ebcdeda`：PRD-147，正式用量/预算/internal-finops 页面、组织生命周期、字号及真实浏览器验收。
- `efccb19`：PRD-146，消除偏好测试对先前 scheduler 测试事件数的依赖，保持既有纯 UI 偏好不发导航权限变更事件的合同。

设计和两轮自审、独立测试与敏感审计均在对应 PRD 记录；索引见 `S2-browser-finance-batch-20260910.md`。本轮是连续完成关联业务后做联合验证，没有为每次小改动重跑整套迁移。

实际 index 候选前端 473 项通过，用量/财务 Cookie API 18 项通过；导航及金额/cache 定向组最初 55 通过、1 个旧测试顺序依赖失败，修订后导航四项独立通过，未更改生产事件逻辑。双数据库金额、权限、撤权和审计回滚的既有证据按未改动业务复用。

## 最终生产包

精确 index 候选资产摘要 `3f00f96672fa8bee828a964614aea7fc29497a846a48f935c87b3e6de33a81e5` 已通过 `.artifacts/s1-browser-36cbe1xa/acceptance.json`。试用站另有已经发布的首页和服务状态页改动，本批保留它们，不用 index 候选覆盖退回旧页面。

最终合并发布候选在 `.tmp/s2-finance-release-20260910/source`，摘要 `be068acf1c3c2cbe96ce665ab805151d335c42a13b9d2cc7abc34298aaf199f4`。289 个资产仅变更 `src/lib/resourceAuthentication.js` 和 `src/styles/main.css`，其余 287 个与旧试用站逐字节相同；全体资产经生产转换后与工作区源码一致。

最终 `.artifacts/s1-browser-ju8_gec2/acceptance.json` 为 production_build=true、passed：390/1280/1440 三视口，27 个业务阶段、21 次布局检查，0 API 5xx、0 页面异常、0 资产失败、0 外部浏览器请求、0 导航数据库诊断。用量筛选按钮和文字输入至少 16px，页面没有横向溢出。注册、创建组织、六名免登录使用者、独立 KEY、预算创建/更新/模式、调整/冲正、实际 CSV 下载及历史归属保持均通过。

用量由隔离测试夹具显式产生，不是模型推理。每组织三条合成用量原分录保持，新增微元借记和冲正后共五条分录，新增调整净额零，不把原成本说成零；三名 Owner 才增加三组账号/用户/成员，六名使用者不增加登录身份。所有隔离浏览器服务已停止。

## 发布与数据保护

Control 从 PID 2780 切换到 PID 8132，启动时间 2026-09-10 10:31:16（本地）。复用原 launcher 和配置，Frontend 11248、Gateway 10120、Status 10552 未重启。本次无迁移，SQLite 仍为 0150，不改账号密码、Provider 密钥或平台注册策略。

备份 `.tmp/s2-finance-release-20260910/database-before.sqlite3` 完整性与外键检查通过。发布前后 global_accounts、users、workspace_memberships、global_sessions、workspace_sessions、workspace_consumers、virtual_keys、virtual_key_consumers、usage_ledger、finops_ledger_entries 的全行摘要相同，见 `database-backup.json` 与 `database-after.json`。数据库以只读源在线备份，不回写或恢复旧数据。

原 dist、元数据及发布过程保留于同目录 `previous-dist`、`metadata-0.before`、`metadata-1.before`、`release.json`、`preflight.json`、`publication.json`，均不纳入 Git。没有删除旧试用数据或早前失败证据。

初次脚本健康观察使用 localhost 和 2 秒超时，记录 FrontendPublished=true、ControlReady=false；不覆盖该记录、不重跑发布或重复重启。后续实际 HTTP/浏览器验证通过；再测 127.0.0.1 健康返回 200、约 420ms，localhost 返回 200、约 2074ms。当前服务可用，首次观察窗口未确认不等于进程启动失败。

## 发布后复核

`.artifacts/s2-finance-release-20260910/release-http.json` 为 passed：通过实际 HTTP 核对 289 个资产及摘要；首页、身份页、隐私页、状态页和三个服务 health 均为 200；匿名使用者、KEY、用量、预算、账本、平台注册策略与 SDK 接口为预期 401。桌面/手机新浏览器上下文中注册页无页面异常、无横向溢出、输入最小 16px，没有创建真实用户或读取用户的浏览器会话。

后续就绪确认见同目录 `publication-verified.json`；敏感范围扫描与精确发布记录暂存审计见 `release-audit.json`。扫描仅覆盖明确文件和规则，不代表全仓库、所有日志或完整安全会签。本轮未重新验收保留的反馈连接池等其他工作区改动。

## 继续推进

旧 `platform_metering.reconciliation_required` 告警仍存在，日志未见新的 traceback；未跳过历史用量、改账或用当前价格/权益追认旧事实。真实 Provider 到 Gateway 再到计量、历史计量受控恢复、规模性能、多浏览器/多 DPR/Linux、支付/发票/通知和后续运行时/企业身份/私有 Gateway/迁移治理仍需按原 Excel 批次完成。72 小时长稳不阻塞本次已验证功能供用户试用，完整目标保持进行中。
