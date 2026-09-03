# PRD-145 测试记录

状态：单 PRD 聚焦测试通过。S7 批次 G7 依照开发计划在 PRD-147 完成后统一执行。

## 专项范围

| 测试组 | 覆盖 | 结果 |
| --- | --- | --- |
| Control API / 状态机 | Platform/Workspace 总览与助手隔离、审批短 cursor、GlobalAccount preference CAS/outbox、部署摘要/scheduler fence、revision 回退、poison outbox | 6/6 通过 |
| 0137 migration | fresh matrix、pristine roundtrip、legacy locale backfill、fact-loss rollback guard | 2/2 通过 |
| Console PRD-145 单测 | Cookie Session/域 CSRF、严格 DTO、store/AbortController/cursor 隔离、迟到/回退响应、偏好幂等与活动 dispatcher | 6/6 通过 |
| 受影响前端回归 | 角色首页、旧助手、旧审批、语言偏好及 PRD-145 新组件 | 57/57 通过 |
| 静态检查 | console lint 472 文件；Python Ruff 聚焦路径 | 通过 |
| 敏感审计 | 高置信密钥/JWT、Bearer、浏览器持久化，只报文件名扫描 | 零命中 |

受影响前端回归的 57 项包含 6 项 PRD-145 新单测，因此本条不重复计数的聚焦测试总数为 65。

## 自动化结果

后端：

    pytest control-plane-api/tests/integration/test_prd145_console_experience.py
    6 passed

    pytest control-plane-api/tests/integration/test_prd145_db_migrations.py
    2 passed

    ruff check <PRD-145 focused paths>
    All checks passed

API 测试覆盖：

- Platform 与 Workspace DTO、权限、projection generation 和 credential isolation。
- 聚合审批只读、分页 cursor owner 绑定及来源 revision 回退拒绝。
- GlobalAccount preference ETag/CAS、幂等 replay、event/outbox。
- deployment diagnostic 白名单与 scheduler lease fence。
- poison outbox 的持久失败状态。

迁移测试覆盖全新数据库和 legacy preference backfill；存在不可逆新事实时 down migration 明确拒绝。

前端：

    node --test tests/unit/prd145ConsoleExperience.test.mjs
    6 passed

    node --test dashboard + assistant + approvals + preferences + role-dashboard + prd145
    57 passed

    node scripts/lint.mjs
    console lint ok (472 files)

专项断言包括：

- 三套 client 均为 credentials include、cache no-store，不发送 Authorization。
- Platform/Workspace/GlobalAccount POST/PATCH 使用各自 CSRF Cookie。
- preference PATCH 使用 If-Match 和稳定幂等键，5xx 重试复用 intent。
- 所有公开 normalizer 拒绝 extra field。
- Platform/Workspace cursor 和受保护 intent 不进入 snapshot。
- 迟到响应、navigation epoch/revision 回退和 fingerprint 漂移不能覆盖新 owner。
- Workspace 活动 dispatcher 不调用旧 overview、assistant 或 approval loader。
- renderer 对新权限执行二次 fail-closed 交集。

## 测试过程修正

首次最终 pytest 命令的 --basetemp 父目录不存在，测试在 fixture 初始化前退出；创建可再生父目录后，
使用可追踪 session 重跑并取得 6/6 与 2/2 明确通过结果。该问题不涉及产品代码。

Ruff 首次发现 Workspace permission import 排序，机械修复后聚焦路径全部通过。

历史 UI 回归最初有 6 项仍断言旧组件标记。迁移为新组件断言后，其中一项发现真实问题：缺少新 Workspace
审批权限时仍可渲染空筛选器。实现已增加 renderer 权限交集；相同门禁覆盖总览、助手、系统状态和部署摘要。

## 两轮实施自审

### 第一轮：owner、并发与事实

- 三个 store 的 owner key 字段互不兼容，无法跨域接受 ticket。
- cursor、幂等键和问题正文只在 operation/DOM 内存中存在。
- navigation epoch、permission revision 与 fingerprint 只能单调推进。
- preference revision 回退、projection revision 回退均清理 busy 并进入 conflict。
- migration roundtrip 和 fact-loss guard 均有真实 SQLite 覆盖。

### 第二轮：活动调用、权限与体验

- performRefresh 到活动 dispatcher 之前不存在旧页面 loader 调用。
- dispatcher 五个新入口只加载当前页面 controller。
- hash/pagehide/logout/Session 失效会 abort 并清空受保护状态。
- Workspace 新页面 renderer 不单独信任导航字符串，必须通过权限交集。
- 英文语言迁移面板使用英文账号设置入口；旧租户级保存按钮不再出现。

## 敏感审计与批次门禁

- 对 PRD-145 migration、Control、Console、测试路径执行高置信 private key、云密钥、OpenAI/GitHub key 和
  JWT 模式的只报文件名扫描：零命中。
- 新 client/store/controller 执行 Authorization/Bearer 扫描：零命中。
- 新 client/store/controller/renderer 执行 localStorage、sessionStorage、IndexedDB、Cache API 和
  console 输出扫描：零命中。
- 测试只使用固定长度占位 Cookie、保留域名和临时 SQLite；正式证据不保存密码、TOTP、Cookie、Token、
  Provider Key、真实 Endpoint 或客户正文。
- .tmp 为可再生测试材料；正式证据只进入 .artifacts、docs/testing 和权威 Excel。
- 本提交必须使用显式路径暂存，不使用 git add .，不纳入未跟踪 PRD-101..105 文档和无关 outputs/。
- 数据库、跨域集成/FLOW、三浏览器三缩放、性能、长稳、安全与必要回归在 PRD-147 后统一执行 G7。

## 权威计划回写

- 权威 Excel 的 01_PRD总表!B57 已从“未启动”更新为“已完成”；PRD-146 保持“未启动”。
- 权威文件和 outputs/prd145-roadmap-closeout 副本均由 artifact-tool 重新导入验证：12 个 sheet，
  公式错误扫描 0 命中。
- B56:B58 的 styleId、字体、边框、换行与对齐保持一致；仅 B57 单元格值发生变化。
- 12 张工作表顶部和 PRD-145 聚焦范围均成功渲染；13 张 PNG 非空，采样颜色数为 7..24。
- 本地图像查看接口无响应，computer-use 状态读取连续两次超时，因此不声称完成了人工视觉审图；
  自动渲染、像素非空、尺寸与样式一致性检查通过。S7 G7 将再次执行真实浏览器和人工页面复核。
