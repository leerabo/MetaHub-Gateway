# PRD-084 本地模型 Endpoint 接入测试记录

日期：2026-08-17

结论：PRD-084 的 Adapter v2、0077、Control、Gateway、EVT-096、PAGE-116、Runtime、Browser、迁移往返、
隐私与资源清理均已通过本记录所列门禁。诊断失败批次不作为通过证据。本条完成不代表 108 条 PRD 全部完成，
`all_108_prds_complete=false`。

## 1. 范围与完成依据

- Excel `01_PRD总表` 第 85 行、`03_M2-M3扩展交付` 第 21 行、PAGE-116、API-383..388、OBJ-115、
  EVT-096 与 TC-0167/0168 共同冻结本条，不按编号推测范围。
- 所有 Endpoint、Provider、Model、Credential、request、attempt、usage、event 与 outbox 事实均携带或由复合
  owner/FK 追溯 `tenant_id`；API 与 Gateway 每次请求重新执行后端 RBAC，前端 capability 只用于呈现。
- DLP、预算和权限阻断必须发生在上游网络前；实际越过 provider-call fence 的成功、失败、超时或取消请求必须
  形成可追踪 request/attempt/usage/cost 状态，只有 `called` 事实发布 `local_model.called`。
- Provider Key、Token、Prompt、Response、真实 Endpoint、DNS 地址和上游异常正文不得进入日志、Browser Storage、
  JSON、manifest、截图或 Office 状态表。OpenRouter、硅基流动只作为竞品/迁移兼容对象。

## 2. 设计与实现审查

- 编码前第一轮关闭通用 Provider/Import 旁路、父子漂移、重复目录探测、0077 activation/owner 迁移、持久
  EVT-096 恢复、routing attempt call fence 与原子创建顺序等问题。
- 编码前第二轮关闭 developer 角色不可达、旧 Provider 页 URL 暴露、token intent hash、父表反向漂移、
  call fence 与事件语义、started crash recovery、目录 ID 日志传播和不必要 processing receipt 等问题。
- 实现后第一轮关闭部署级私网 ACL、DNS 地址总量、Audit object_name 隐私、PAGE-116 权限闭环及 durable
  delivery pending/leased 恢复和终态 CAS。
- 实现后第二轮关闭 IPv4/IPv6 metadata 永久 deny、取消后的 usage 状态、0077 call/usage trigger 回归与
  Runtime/Browser provenance。最终 Browser 收口再修正证据汇总 SQL 对 Provider/catalog/audit 真实 schema 的
  关联，并增加 fresh 0077 实库查询合同和不含异常文本的安全失败坐标。最终无未关闭 P0/P1。

## 3. 自动化门禁

| 分组 | 最终结果 | 覆盖 |
| --- | ---: | --- |
| PRD-084 联合专项 | `56/56 passed` | Shared、0077、Control、Gateway、Console 与验收合同主路径 |
| 持久层/网络/取消专项 | `20/20 passed` | metadata deny、DNS/ACL、call fence、取消 usage 与 trigger |
| Shared PRD-063/064/084 | `116/116 passed` | Adapter/Event/Webhook exact wire 与相邻兼容 |
| Gateway PRD-063/064 | `74/74 passed` | provider contract、stream/non-stream、usage/error 兼容 |
| Adapter v2 matrix | `10 specs / 103 cases / 103 passed` | 国内默认上游、本地 Endpoint 与迁移兼容矩阵 |
| Control PRD-063/064 | `12/12 passed` | Provider Adapter/Health 相邻 API 回归 |
| Durable delivery | `2/2 passed` | pending/lease/source owner、恢复和终态 CAS |
| Console 全量 | `876/876 passed` | PAGE-116 与既有页面回归 |
| Console lint | `269 files` | Console 源码和测试静态门禁 |
| Runtime 合同 | `8/8 passed` | 空库、真实服务、traceability、隐私、provenance 与 cleanup |
| Browser harness | `7/7 passed` | owner、0077 往返、RBAC 漂移、实库汇总 SQL、隐私域与 evidence |
| Browser Node driver | `6/6 passed` | 双视口、四 PNG、网络 guard、动态撤权、失败目录回滚 |
| Ruff / compile / diff check | `passed` | PRD-084 新增和受影响 Python、语法、格式与补丁完整性 |

以上分组存在相邻覆盖，不相加冒充一次根级全量。开发期间只运行受影响专项，最终门禁扩展到 Adapter、
Provider Health、Webhook delivery、Console 全量和 PRD-063/064 相邻合同，没有反复运行无关全量测试。

## 4. Migration 0077

- 空 SQLite 自动化门禁实际完成 `77/0077 -> 77/0077 -> 76/0076 -> 77/0077`；各阶段均要求
  `integrity_check=ok`、外键违规 0。
- seeded 往返验证既有 Provider、Model、Credential、request、usage、routing attempt 和 Adapter activation
  的 rowid 与字段值不漂移；存在 Local Endpoint、catalog、receipt、event/outbox、subscription/delivery 或
  非法 activation 时，down migration 原子阻断。
- 0077 创建 tenant-qualified aggregate/catalog/receipt/call event/source outbox，扩展 Provider 和 routing
  attempt provider-call fence，并安装 owner、父子不可漂移、append-only、状态 CAS 与事件正交 trigger。

## 5. Runtime 正式验收

权威证据：`.artifacts/prd084-runtime/20260816T183036Z-c60eb988.json`

- JSON `status=passed`，SHA-256：
  `F226D7665714C6AF335C72AE64E33392976337CE7A1E8850319384C8FFDBFDDC`；配套 `.sha256` 与复算值一致。
- 从空 SQLite 到最终 `77/0077`，`integrity_check=ok`、外键违规 0，非终态 source/delivery 为 0。
- 真实 Control、Gateway、两个 OpenAI-compatible stub 和签名 Webhook receiver 通过；创建/探测、非流式、
  流式、上游失败、DLP 与预算前置阻断均完成。
- 共 5 条 request log（2 成功、3 失败）、3 条 called attempt、2 条 usage、20 tokens 且成本为正；3 条
  EVT-096、3 条签名 Webhook、3 条 sent source outbox 一一对应。DLP/预算阻断的上游调用增量均为 0。
- 数据库、日志和正式证据敏感 canary 命中 0；owned process 为 0、端口释放、runtime tree 已移除。
- provenance 已按最终 Runtime 五项源码复算一致；失败诊断批次不作为正式证据。

## 6. Browser 正式验收

权威批次：`.artifacts/prd084-browser/final-20260817-01/`

- `browser-acceptance.json`：`status=passed`，SHA-256
  `F1C6EAF61B496956326CD56974058A9250FAF8AF20C23816F1F9EFF9A949507E`。
- `sha256-manifest.json` SHA-256：
  `9A20078068521D2C3E1AC33C838E7EDBBA5060F112617C5BABBA28FC66290F13`；JSON 与 4 张 PNG 共 5/5 hash 相符。
- Chromium 在 `1280x720` 与 `390x844` 覆盖原生创建 Dialog、none/bearer、成功/失败探测、Endpoint 列表、
  developer read/test-only、后端 manage 拒绝、动态撤权/迟到响应丢弃和 secondary tenant 空状态。
- 主 Agent 已逐图复核：4 张 PNG 均非空，重叠与全局横向溢出为 0；移动表格使用边界内横向滚动。
  截图只显示固定 `http://[loopback]/v1` 占位，不含真实 Endpoint、Token、Prompt 或 Response。
- 最终数据库为 `77/0077`、`integrity_check=ok`、外键违规 0；2 个 Endpoint 覆盖 none/bearer 和
  healthy/unhealthy，secondary tenant 行 0，撤权后 developer read grant 0，创建 Audit 2 条。
- 外部请求、禁止 query、Console/page error、DOM/Storage/evidence 敏感命中、SQLite sidecar、owned 进程和
  端口残留均为 0；只保留正式批次目录。

正式文件复算值：

| 文件 | bytes | SHA-256 |
| --- | ---: | --- |
| `browser-acceptance.json` | 4927 | `f1c6eaf61b496956326cd56974058a9250faf8af20c23816f1f9eff9a949507e` |
| `page-116-create-dialog-desktop-1280x720.png` | 58118 | `24c85d7ecc672e106fd93f6411824f6e74b2528c1911689dc1cdaf6f71496873` |
| `page-116-local-endpoints-desktop-1280x720.png` | 80038 | `50282bfa2bedd7588b817becd9fcb0725f35868ca00dad809b92017f70b09ada` |
| `page-116-local-endpoints-mobile-390x844.png` | 34459 | `1daf596baf372b6a044c41d4261a8297c0bfbd8edbfb76ddca7a339129900987` |
| `page-116-tenant-isolation-mobile-390x844.png` | 32095 | `d8f4c46cd564b67a8243d23a4581376cea5b5f698a4bf0c63ddbd5e2ac7ba5c6` |
| `sha256-manifest.json` | 956 | `9a20078068521d2c3e1ac33c838e7edbba5060f112617c5babba28fc66290f13` |

## 7. 隐私、禁止文件与资源审计

- tracked/untracked、补丁、Runtime/Browser JSON、manifest 和 PNG 只允许固定 ID、枚举、计数、hash、状态、
  request_id 与脱敏占位；Provider Key、Token、Prompt、Response、真实 Endpoint 和上游正文命中必须为 0。
- 提交集合不得包含 `.env`、SQLite/sidecar、日志、cache、coverage、build、Office 临时文件、失败诊断 artifact
  或进程命令行证据；`.artifacts` 中只保留本记录引用的正式 Runtime 与 Browser 批次。
- 最终 owned 服务、固定端口、Browser profile、SQLite sidecar、work/staging 和临时凭据均须归零。

## 8. 下一项交叉确认

Excel `01_PRD总表` 第 86 行、`03_M2-M3扩展交付` 第 22 行、PAGE-117、API-389..391、OBJ-116、
EVT-097 与 TC-0169/0170 共同确认下一条权威未完成项为：

`PRD-085 | M2-企业增强 | P2 | 扩展层 | 用户行为埋点 | 产品使用埋点与激活漏斗`

`docs/prd` 与 `docs/testing` 均无 PRD-085 完成记录。本次只登记，不实现 PRD-085；
`all_108_prds_complete=false`。
