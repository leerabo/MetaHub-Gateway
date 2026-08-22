# PRD-099 测试记录

状态：实现、自动化门禁、真实浏览器验收和最终候选审计已完成。权威下一项为
`PRD-100 - 生产基础设施`。`saas_incremental_roadmap_complete=false`，`all_108_prds_complete=false`。

## 已执行门禁

| 测试组 | 实际结果 | 覆盖 |
| --- | --- | --- |
| DB | migration `3 passed` | 0090->0091、fresh install、pristine down/up、非 pristine 回退阻断、索引、触发器、完整性与 FK |
| API | platform billing `1 passed` | 真实 Workspace 登录与订阅、Usage outbox、exactly-once、周期关闭、迟到 Usage、CSV、Platform 调整 CAS/幂等与凭据负矩阵 |
| WEB | PRD-094..099 affected `35 passed` | Workspace/Platform Cookie 边界、CSV Blob 清理、响应 allowlist、角色裁剪、调整 CSRF/幂等/CAS、响应式 DOM |
| Browser | Identity/Platform `390x844`、`1280x900`、`1440x900` 通过 | 真实 Cookie/Session/TOTP、账单摘要、筛选、调整表单、响应式布局与敏感输入清理 |
| Static | Console lint `321 files`、Ruff、compileall、`git diff --check` 与候选敏感扫描通过 | PRD-099 当前变更集与 PRD-094..098 页面兼容 |

## 迁移与领域结论

- 0091 独立创建 meter definitions/readings、billing cycles、quota allocations、summaries、adjustments 与
  outbox/checkpoint，不复用 PRD-051 FinOps 或 PRD-079 quota ledger。
- 迁移 checkpoint 固定到上线时 `usage_export_ledger_sequences` 最大序列，不回填上线前历史 Usage；之后由
  Usage INSERT trigger 追加独立 outbox。worker 使用 canonical sequence 与 source 唯一键保证 exactly-once。
- 每条 Usage 只投影四个整数数量和 owner hash，不复制 `raw_usage`、Prompt、Response 或 Secret。请求 meter
  使用套餐月请求权益；Token meter 明确为 informational，缺失价格不解释为免费。
- open 周期读取 live aggregate；closed summary 不可变。迟到 Usage 追加 `late_usage` adjustment 并标记
  `reconciliation_required`。手工调整要求平台权限、summary hash CAS 与幂等键。
- pristine 数据可回退并重新 apply；存在 billing/readings/outbox 事实时 down 原子失败。fresh 与升级库均通过
  完整性和 FK 检查。

## 授权与客户端结论

- Workspace finance 和 Platform billing 是两个独立 Cookie/Session 解释域，混合 Bearer、Workspace、
  Platform 或 Support 凭据在业务查询前失败关闭。
- Workspace Console 提供周期、meter、配额、超额和 CSV；仅 owner/finance_admin 有 DOM。Platform Billing
  提供状态筛选、主体绑定游标、reconciliation 列表和 adjustment 表单；调整权限撤销后零 DOM。
- CSV 客户端在读取 Blob 前验证冻结 Content-Type、文件名、`no-store` 与 `nosniff`，点击完成后立即删除
  临时节点并 revoke Blob URL。平台调整正文只从已加载摘要派生 owner、cycle、meter 和 CAS hash。

## 真实浏览器验收

- 使用隔离 SQLite 数据库完成真实 GlobalSession、WorkspaceSession、PlatformSession/TOTP 流程；Workspace
  账单摘要显示 4 个 meter，Platform Billing 显示 1 个 `reconciliation_required` 周期并可按该状态筛选。
- 390x844、1280x900、1440x900 六个页面/视口组合的 root 横向溢出均为 0，未发现未受控元素越界或
  交互控件重叠；Identity 与 Platform console error/warning 均为 0。
- Identity 的 3 个密码输入均为空；Platform 验证成功后 TOTP 输入已从 DOM 移除。密码、TOTP、Cookie、
  Session 与 Secret 均未进入截图、测试记录或提交候选。
- 截图保存在 `.tmp/prd099-browser-evidence/`：
  - `identity-390.png` `4d7c3ba7aa5faea230e30be390fc89c7b1dddd102fcd7ac3445a02257c2758d1`
  - `identity-1280.png` `21a569eca41ab0326bb72a87361b9197c79d26e6b291bd8874a2a1ad32151206`
  - `identity-1440.png` `ca35b80322234c66c389d7b71f993f8f4b357320377181f53c97132b36d83a57`
  - `platform-billing-390.png` `b9a7be7c0ff7153998e02b2fe307021173fcae5442a8e653c71c41011cfb272b`
  - `platform-billing-1280.png` `c35146ee5354343b1ba955e453145d58685349a145bf27d517543c9456f1fb1e`
  - `platform-billing-1440.png` `f3904c7db8443037a11f4565c5ca890e82eaeea3e2f89f2b980dc14adb55e538`

## 定向回归说明

- PRD-051/079/080 组合回归未全绿：历史 fixture 仍把最新 migration 硬编码为 `0073`/`0083`，在当前
  `0091` 仓库会于 setup 失败。
- 其余 4 个失败已用独立 basetemp 单独复现：旧 quota outbox 投递预期、旧 webhook mock 缺少
  `payload_format`，以及 2 个 Windows spool 重试/目录可见性预期。失败所在模块和测试不在 PRD-099
  候选 diff 中；PRD-099 的 DB/API/Console 专项均通过，未用这些历史失败替代或放宽专项门禁。
