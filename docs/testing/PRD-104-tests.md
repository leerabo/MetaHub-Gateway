# PRD-104 测试记录

状态：不可变结算单、税额快照、发票产物、单次下载、五源冻结对账、双域审批冲红及 Console 验收均已完成。

## 已执行专项

| 测试组 | 当前结果 | 覆盖 |
| --- | --- | --- |
| 邻接 Migration | `12 passed` | PRD-101 至 PRD-104 迁移链、fresh、pristine down/up、完整性与回滚阻断 |
| PRD-104 Migration/API 最终回归 | `4 passed` | 0095->0096、结算单/税额、发票生成与下载、冻结对账、双审批和 provider worker |
| Console focused | `27 passed` | Identity 布局、Workspace 发票客户端、单次下载、Platform 对账/冲红和角色裁剪 |
| Compliance/navigation regression | `14 passed` | Billing 与 Compliance 独立导航、合规复核视图和控制权限 |
| Console full | `1030 passed` | 控制台全量 Node 回归 |
| Console lint | `331 files` | 控制台源码与测试静态检查 |
| Python static | 通过 | Ruff 与 compileall |

每次最终 pytest 调用均使用新的显式 `--basetemp`，未复用或删除已有测试目录。

## 已验证边界

- 结算单保存不可变版本、计费周期、币种、未税金额、税额、含税金额和资料修订；客户端不能提交服务端金额或税额。
- 发票申请只允许 Workspace owner/finance_admin；发票产物由 provider worker 生成 PDF 与 CSV，下载先签发短时单次令牌，再通过专用 Header 消费。
- 下载预检、令牌签发与 Header-only 消费均成功；令牌不进入 URL、DOM、控制器持久状态或浏览器存储。
- 对账创建时冻结订单、支付、Meter、结算单和发票五类事实水位；worker 只处理冻结范围并产出稳定差异分页。
- 冲红由账务域和合规域分别审批，请求人不能自批；只有两个域均批准后才进入 provider 队列，最终状态只由 worker 推进。
- Workspace、Platform billing 与 Platform compliance 使用各自凭据域；角色或权限动态撤销后不保留受保护数据或操作控件。

## 浏览器验收

- 在隔离的 0096 数据库副本完成 Workspace 登录与自动切换、结算单发布、发票申请、provider 处理及 PDF/CSV 下载。
- Platform 创建五源冻结对账并显式执行 worker，最终显示完成且无差异。
- 冲红流程验证请求人自批返回冲突；独立账务操作员与合规操作员分别批准后，provider worker 完成冲红。
- `Compliance -> 合规复核` 显示独立页面，不混入账单周期或退款审核内容。
- Identity 发票页和 Platform 合规页均在真实 Edge 的 `390x844`、`1280x900`、`1440x900` 三档完成复核：页面级横向溢出、控件重叠、控件裁切和视口外控件均为 0；移动端宽表只在自身容器内滚动。
- 验收输入、日志摘要、文档与提交内容均不包含本地密码、TOTP、Cookie、Session、下载令牌、DSN、私有对象标识、对象路径或受控基础设施信息。
