# PRD-148 多角色走查问题汇总（2026-09-16）

PRD-148 未完成。本轮先采集并合并问题，再按业务域修复。原生 computer-use 初始化和重置重试均失败，错误为 `failed to write kernel assets` / `os error 3`；浏览器工具同样失败。以下使用 Playwright，不能写作原生 computer-use 验收。

## 证据范围

- 本机实际注册的组织所有者通过登录表单进入 Workspace，在当前开发服务走查 59 个路由。见 `outputs/prd148-local-all-pages-20260916/report.json` 及其来源清单。该轮使用真实本机 Cookie，但不是外部身份、商业付款或独立签署验收。
- 初轮 17 页面采集过早读到了加载态，保留为 `outputs/prd148-local-governance-walkthrough-20260916`；增加等待并记录实际接口状态的复查为 `outputs/prd148-local-governance-steady-20260916`。标题出现、零溢出和 HTTP 200 都不能单独证明完整功能可用。
- 隔离 Platform owner 的导航审计为 `.artifacts/prd148-platform-pages-20260916`。它使用测试身份和 MFA 初始化，没有执行真实会签或发布。
- 现有全浏览器 QA `.artifacts/product-qa/20260916-012939` 已结束：14/15 任务通过，Chromium 1× 登出失败；批次来源变化，不能认证最终当前源码。

## 合并后的问题

| 业务域 | 观察与源码证据 | 分类 / 处理 |
| --- | --- | --- |
| Workspace 共用顶栏 | 虚拟 KEY 列表响应在鼠标按下后到达，`renderConsoleContentPreservingSidebar` 替换顶栏；松开时没有 click、没有 logout 请求，15 秒后仍在控制台 | 真实产品缺陷，已修复候选并做前后对照；仍需当前浏览器回归 |
| Workspace 治理 | DLP GET 401，`dlp.js` 仍发送旧 Bearer，路由仍使用旧守卫；角色权限、策略发布依赖旧 AuthContext 身份，未发起有效数据读取 | 真实产品缺陷，待按治理域迁移 Cookie、CSRF、事务内再鉴权与页面 owner |
| Workspace 通知 | Webhook GET 401；客户端与路由仍保留旧身份传输 | 真实产品缺陷，待通知域修复及跨组织/撤权回归 |
| Workspace 生命周期与私有部署 | 数据管理、私有 Gateway、私有模型等只有标题；页面 owner 仍要求旧 `user/session/authorization_fingerprint` | 真实产品缺陷，待以当前 Workspace 事实建立 owner；禁止伪造旧身份填充 |
| 开发者中心 | `/developer/api-versions`、changelog、schema、两个 SDK 列表 401；接入信息在不同采集轮次有加载态与正常结果 | 真实接口失败；接入加载问题继续定位，不把短采样写作永久失败 |
| 财务与使用 | 新组织无订阅；历史 Gateway 请求没有结算用量；额度页仅标题，部分其他财务页面加载未收敛 | 商业证据不足加页面问题；需要真实套餐/权益、计量与额度闭环，不能以模拟付款替代 |
| Platform | overview 503；导航到后续页面出现点击超时 | 已观察失败，需区分隔离库未准备投影和实际导航/布局问题，尚未定因 |
| 反馈摘要 | 本机 `/feedback/summary` 503 | 真实接口失败，待核对当前服务源码和数据库投影，不能擅自归为夹具 |

### 登出复现与修复边界

`outputs/prd148-logout-hydration-before-20260916/report.json`：按下后释放真实列表响应，旧按钮被移除，没有登出 HTTP 请求，15 秒超时。

`outputs/prd148-logout-hydration-after-20260916/report.json`：同样操作保留同一按钮，登出 HTTP 200 并回首页。候选仅保留内容相同的顶栏，并用 WeakSet 保证事件只绑定一次；身份或品牌内容变化仍替换顶栏。未延长登出门槛。

首次无定向延迟的隔离复查 `.artifacts/prd148-logout-diagnostic-20260916` 为三角色通过，不能抹掉原始偶发失败。已向正式浏览器旅程加入按下期间完成真实数据加载的回归。

## 测试夹具与迁移基线

- 原生工具 kernel assets 路径故障属于工具环境问题。
- 首轮页面采集把 networkidle 当作业务渲染完成；其加载态/标题不计功能通过。
- 59 路由审计的 `authorizedRoutes` 尚未并入高级工具目录。高级入口的 `allowed=false` 不能据此判为越权，需按 `/workspace/advanced-tools` 再核对。
- Platform 审计使用 scheduler-disabled 隔离库；overview 缺投影是否为夹具问题仍须验证。其他导航失败保留，不抹成通过。
- 既有完整迁移链 0155、PRD-132 `WorkspaceSessionContext` 夹具修正继续保留，本轮不改历史基线要求。

## 尚不能证明

以上不证明所有页面的创建/修改/撤权等动作通过，也不证明真实付款、企业治理全流程、私有 Gateway 交付、六方会签、发布复核、实际回滚及财务/隐私/安全/迁移对账。正式 scheduler-enabled 连续 72 小时尚未具备启动条件，旧健康检查观察不计入 GA。HEAD/origin 仍待一致。
