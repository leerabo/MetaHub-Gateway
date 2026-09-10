# 组织资料、初始化导航与 SDK 本地发布

日期：2026-09-10。范围为 PRD-093/094/097/147 的资料、成员隔离、导航及示例下载增量，不代表整个 Excel 已完成。

## 独立提交与组合验收

- `df36203`：PRD-094，组织成员停用不改变全局账号或其他组织。
- `d4a9f61`：PRD-093，资料使用权威 Workspace API。
- `97d4db5`：PRD-147，组织资料页和四个 SDK 接口接入 Workspace 会话，生成下载包后重新鉴权。
- `c2a02b8`：PRD-097，八步目的页接线及身份表单/向导字号。

`.artifacts/s1-browser-_85vnzmx/acceptance.json` 证明隔离生产包在 1280x900、390x844 完成 12 阶段：注册、组织、使用者、KEY、轮换/停用和八步导航。两个视口均完成资料保存/同组织重入/重载、Python/JavaScript 实际下载；无横向溢出，输入控件和向导主文字最小 16px，辅助文字最小 14px，页面异常为 0。截图已经目视复核。

PostgreSQL SDK 两项用例通过，0 跳过，107.701 秒，见 `.artifacts/s1-index-native-318508f5831a437b833a23ad4bb8c8a9/`。成员隔离原生测试和 SQLite API 的既有证据保持有效；未反复重跑无改动的大迁移套件。独立提交的敏感审计保存在 `.artifacts/s1-profile-sdk-batch-20260910/prd147-index-audit.json` 和 `prd097-index-audit.json`。

## 实际发布

入口：`http://localhost:5173/`。最终资产集摘要为 `3bc8a634902a52e2468d4bf72580af71b5b64c4e1bc5ee4f22e5109c4069f7ac`。相对之前的 `6b4ceb1d98733178c0676aaf1492b057534ce16886241bc5f3ae7bc79bf30ebe`，289 个资产只修改 9 个，其余 280 个原样保留。发布前逐文件确认与当前工作树的生产转换字节一致，不覆盖已在试用的 S2 用量/财务改动。

SQLite 0149 升级到 0150，只修改成员状态同步触发器及治理摘要。迁移前后 global_accounts、users、workspace_memberships、global_sessions、workspace_sessions 的全行摘要相同，外键检查通过。不恢复旧数据库、不重设账号、不撤销用户会话。

首次发布在备份 integrity_check 处因原生 sqlite3 连接缺少 metahub_sha256 函数停止，迁移和页面替换均未执行，控制面自动恢复。改用项目数据库连接后，原备份完整性和外键检查通过。第二次发布创建新的独立备份，不覆盖首次记录。

备份和过程记录位于 `.tmp/s1-profile-release-20260910/`：`previous-dist`、`database-before-0150.sqlite3`、`database-before-0150-retry.sqlite3`、`migration.json`、`publication.json` 与 `publication-retry.json`。这些是本地敏感运行资料，不纳入 Git，也没有删除。

Control 最终 PID 2780；Frontend 11248、Gateway 10120、Status 10552 保持原进程。通过原 launcher 启动，配置和密钥保持原样。

## 发布后核验

`.artifacts/s1-profile-sdk-batch-20260910/release-http.json` 为 passed：实际 HTTP 校验全部 289 个资源；首页、身份页、隐私页、状态页和三个后端 health 为 200；匿名使用者、KEY、平台注册策略和 SDK 接口为预期 401。新浏览器上下文验证桌面/手机注册页，页面异常为 0，无横向溢出，输入最小 16px。对应 `release-registration-1280.png`、`release-registration-390.png` 已目视复核。

发布后检查没有创建真实账号或读取用户浏览器会话；全部模型调用为 0。登录后的写操作来自隔离验收，不能描述成已在用户真实组织中逐项写入验证。

## 剩余工作

继续 S2 使用者到用量、内部财务与预算的真实浏览器业务链路及独立提交，再推进原 Excel 的支付/发票/通知、可靠性、企业身份、私有 Gateway 和迁移治理等批次。完整性能、多浏览器/多 DPR/Linux、真实首次调用和 72 小时长稳尚未完成，不等待长稳才交付本次已验证功能。

既有 `platform_metering.reconciliation_required` 告警仍存在，早于本次发布，原因及受控恢复合同见 S2 计量记录。本次未跳过旧用量事实、改账或自动认定历史计费方式。
