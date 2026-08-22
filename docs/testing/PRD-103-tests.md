# PRD-103 测试记录

状态：订单、支付回调、催收、退款审核、Console 专项及隔离浏览器验收均已完成。

## 已执行专项

| 测试组 | 当前结果 | 覆盖 |
| --- | --- | --- |
| Crypto/config | `3 passed` | 当前/上一 HMAC key、篡改/时间窗、AES-GCM/HKDF owner AAD、生产配置失败关闭 |
| Migration | `3 passed` | 0094->0095、fresh、pristine down/up、商业事实回滚阻断、完整性/FK |
| Payment API | `1 passed` | 100 重放、乱序、capture、dunning recovery、退款竞态、effect exactly-once、PCI canary |
| Compose/deploy | `9 passed` | 独立签名/加密密钥、对象根、Compose 透传、部署生成与生产失败关闭 |
| Console focused | `18 passed` | PRD-098/099/102/103 客户端边界、角色裁剪、退款审核及 token 即时清理 |
| Console full | `1025 passed` | 控制台全量 Node 回归 |
| Console lint | `329 files` | 控制台源码与测试静态检查 |

每次 pytest 调用均使用新的显式 `--basetemp`。专项测试和验收记录不输出本地密码、TOTP、Cookie、Session、
Webhook secret、client token、DSN、对象引用、对象路径、加密密钥或私有基础设施标识。

## 已验证边界

- Workspace 订单、支付与退款只允许 owner/finance_admin，Platform 退款列表与审核分别动态要求
  `platform.billing.read` 和 `platform.billing.adjust`；provider 回调拒绝所有浏览器与用户凭据。
- 当前和上一签名 key 均可在轮换窗口验签；错误 key version、签名篡改、陈旧 timestamp、重复 header、超大或长度
  漂移 body 全部在 JSON 业务解析前失败关闭。
- 验签成功先 durable persist receipt 再返回；同 provider/event 重放返回原 receipt，同 ID 不同 hash 标记冲突。
- worker 对乱序 event 使用单调 revision，100 次重放不会复制 capture、entitlement、dunning recovery 或 refund
  ledger effect。
- 回调 raw body 只以独立密钥加密写入隔离对象根。数据库、API、日志与 Console 对 PCI canary、client token、
  provider reference 和原始错误正文保持零命中。
- 退款在写锁内预留 captured 金额，超额与并发累计超额均被拒绝；客户申请不能直接执行，Platform 审核使用 CAS，
  最终结果只由 provider receipt 推进。
- Console 不把一次性 `client_token` 放入 DOM 或控制器状态；provider 消费成功和抛错路径均在 `finally` 中立即
  清空。developer 和只读 Platform operator 不获得写控件。

## 浏览器验收

- 在独立的 0095 SQLite 副本及独立 API/Console 端口完成 Workspace 登录、MFA 绑定与 Platform 登录；源验收库未修改。
- 从 Platform 创建并发布付费套餐，在 Workspace 创建订阅与支付意图；一次性 `client_token` 由页面内消费后立即清理，
  DOM 与控制器状态不保留该值。
- 使用当前本地测试 key 对原始 JSON bytes 签名，回调经无 Cookie provider 请求接受并由 receipt worker 处理；Workspace
  随后显示订单已履约、实收金额正确且订阅生效。
- Workspace 成功提交部分退款，Platform 待审核筛选显示一条申请并提供批准/拒绝控件；批准后待审核列表清空，处理中
  筛选显示该申请且不再暴露审核控件。
- Workspace 通过确认框取消订阅，状态变为已取消，取消命令随即移除。
- 390x844、1280x900、1440x900 三档视口均无页面级横向溢出或交互控件重叠；移动端退款队列、筛选器与套餐表单
  保持可读可操作，宽表只在自身容器内滚动。
- 验收输入和记录未显示本地密码、TOTP、Cookie、Session、Webhook secret、client token、对象引用、对象路径或受控
  基础设施标识。
