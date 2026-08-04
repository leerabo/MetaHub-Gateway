# PRD-047 成本优先与可用性优先路由测试记录

测试日期：2026-07-19

当前状态：已完成。需求设计、两轮独立设计自审、实现期安全复审、代码实现、0036 正反迁移、专项与根级全量回归、真实三服务/API/Webhook/Usage、停止后数据库和日志复核、以及真实 Chrome 三档验收均已通过。本记录只把 `artifacts/prd047/20260719T145100` 作为最终成功批次；其他批次仅用于故障复盘，不替代最终证据。

运行环境：Windows PowerShell；Python 使用 `C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。pytest 的 `TEMP/TMP` 固定为 `D:/MetaHub/.tmp`，每轮使用唯一 `--basetemp`。真实验收只操作开发库的一致性副本，不修改 `D:/MetaHub/.data/metahub-dev-prd031.db`。

## 1. 验收口径

- Excel `TC-0093`：`cost_first` 在健康、KeyPool 可用且价格可比较的候选中选择本请求估算成本最低者；调用日志、RoutingDecision、RoutingCandidate、Usage 和签名 Webhook 可解释同一选择。
- Excel `TC-0094`：`availability_first` 避开新鲜 unhealthy Provider 和不可用 KeyPool；同时覆盖缺价、混币、无 KeyPool、依赖失败、权限、跨租户、统一错误、request_id/trace_id 和敏感信息边界。
- 自动化、迁移、真实 API、真实浏览器和停止后终验是五层独立证据，不能互相替代。
- 只有当前代码状态下退出码为 0 的最终结果计为通过；失败批次保留用于说明修复过程，不参与通过统计。

## 2. 设计与安全复审

- 第一轮设计自审关闭 RoutingPolicy 版本覆盖、implicit fixed、强制 CAS、Decimal 精度、依赖分类、Token 估算、事件 actor 和 mode-aware 最小读取等问题。
- 第二轮设计自审关闭 Audit/Webhook down、SQLite 版本触发器、复合 FK、持久事件、动态 fallback、严格 JSON、前端异步状态和 Decision/Candidate owner 图等问题。
- 真实开发库迁移前置复核补充兼容 PRD-030 的 `virtual_keys.environment_id=NULL` 历史 Key，并保留 0035 Policy 为空的重复 Decision 历史。
- 实现期安全复审补充 CNY Budget、`outstanding_cost_cny`、Provider 环境 Key 边界、Provider URL 审计清洗、Browser owner-safe cleanup、API/UI Token 分账、Gateway 健康端点网络白名单和 `httpx/httpcore` fail-closed 日志过滤。

完整设计与自审记录见 [PRD-047 设计文档](../prd/PRD-047-cost-first-availability-first-routing.md)。

## 3. 专项与全量自动化

最终门禁结果：

| 门禁 | 结果 |
| --- | --- |
| Python 根级全量 | 收集并通过 `574/574`，退出码 0 |
| JavaScript/Node 示例 | `33/33 passed` |
| Console 全量 | `228/228 passed` |
| Console lint | `106 files passed` |
| Runtime 验收脚本专项 | `6/6 passed` |
| Browser owner/安全专项 | `16/16 passed` |
| 中文注释检查 | `comment coverage ok` |
| Python compile / Browser `node --check` | 退出码 0 |
| 最近改动 Python 文件 Ruff `--select F` | `59 files passed` |

根级 Python 全量已经包含以下 PRD-047 重点组，不重复累计数量：

- shared 两阶段路由、Token 估算、Decimal 成本、稳定原因码和严格错误/诊断合同；
- ModelAlias RoutingPolicy wire、CAS、幂等、admin/developer/viewer RBAC、租户隔离和跨租户防枚举；
- Gateway Health/KeyPool/价格最小读取矩阵、Decision/Candidate 原子事实、Fallback/流式兼容和失败请求追踪；
- Policy Dry-Run 与 Gateway 共享排序语义；
- CNY Budget、极端价格、主备最坏预留和未结算分项；
- Provider 环境 Key 的租户/code/Endpoint 边界、Provider URL 日志与 Audit 清洗；
- 0036 空库、升级、down/up、历史数据保真、复合外键、trigger 和 DML 反例；
- Browser 网络门禁、API/UI Token 分账、Storage/DOM/URL 隐私、PNG 像素和 finally cleanup。

## 4. 0036 迁移验收

最终批次从未改动的开发库使用 SQLite Backup API 创建：

| 证据 | 值 |
| --- | --- |
| 源库 SHA-256 | `06632c12ab305507213c34756c815618b58092373f386bd103245ad6d943fabd` |
| Backup 初始 SHA-256 | `023bb873a952f72f12b93957df75326dcb7ac4830a354b837e57399990615289` |
| 首次迁移 | 仅应用 `0035`、`0036` |
| 二次迁移 | `applied=[]`，36 条全部 skipped |
| 0036 checksum | `a67af782a037c77b40ffc1722095645ad07ca3d36ce34424b64eff97a008f785` |
| 共同业务表 | 55 张逐表共同字段与行完全一致 |
| 规范化 manifest SHA-256 | `ee07db90a8fa77d7fe2b67c05a63b93e38937dca397fcb17dd4f4ca1d42718cd` |
| 升级后完整性 | 36 migrations、最高 `0036`、`integrity_check=ok`、FK 违规 0 |

规范化比较以只读 immutable 连接打开两库；每个值携带 SQLite 类型标签，行和表均按稳定顺序编码并逐表比较。新增表、0036 新列和 `schema_migrations` 不伪装成历史业务事实。

迁移专项还验证：

- `model_alias_routing_policies`、`routing_policy_events`、`routing_candidates` 和 `budget_reservations.outstanding_cost_cny` 的正向与回滚；
- Alias/Environment/Model/Provider/Policy/Decision/Candidate 的 tenant-qualified owner 图；
- Policy 版本、事件、候选与决策不可变约束；
- 0035 legacy 重复 Decision 和 NULL Environment Key 兼容；
- Provider Audit 历史 `base_url` 不可逆清理是唯一明确的数据内容例外，其他审计行与链路字段保真；
- down 清理 PRD-047 Webhook/事件/trigger/index，且不恢复已删除的敏感 URL。

## 5. 真实 Runtime 验收

最终成功批次：`artifacts/prd047/20260719T145100`。三服务使用同一高熵 Auth Secret 和 Provider-Key Encryption Secret，仅通过子进程环境传递；Provider Health、Policy Snapshot Sync 和 Usage Export 后台任务在隔离验收中关闭。Gateway、Control API 和 Console 启动后健康检查均为 HTTP 200。

Runtime 输出：

- `status=passed`，suffix 为 `20260719t065500b28ca8`，stderr 为 0 字节；
- `cost_first` 保存 2 条候选，选择低成本合格模型，Usage 按实际模型归因，`route.policy_selected` HMAC 验证通过；
- `availability_first` 排除新鲜 unhealthy 候选，未执行价格比较；
- 无 KeyPool 路径 Provider 调用为 0，返回 `gateway.no_route_available`，失败 RequestLog 与 request_id 可追踪；
- admin/developer/viewer 与第二租户覆盖读写权限、跨租户 404、防枚举和 Budget 越权；
- CNY Budget 预留、Usage 和 `outstanding_cost_cny=0` 精确结算；
- 数据库 Prompt sentinel 明文命中 0，Provider 与 Webhook receiver 只保留安全摘要。

## 6. 真实 Chrome 验收

系统 Chrome 对 `routing-policies` 执行 admin、developer、viewer 三角色和 1280/900/390 三档验收：

- admin 的模式单选不自动保存，显式 PATCH 恰好 1 次，保存 `cost_first v4`；请求体不含 tenant_id；
- developer 页面可见但只读，写请求为 0；viewer 回退 overview，无导航、无页面 DOM，ModelAlias 预取为 0；
- developer/viewer 后端 PATCH 均返回 `auth.permission_denied`，前端隐藏不替代后端授权；
- 三角色 browser error、console error、HTTP 5xx 和非回环网络请求均为 0；
- DOM、URL、localStorage、sessionStorage 未泄露密码、Prompt、Virtual Key、Provider Key 或跨角色 Token；
- Browser 仅允许 Console/Control origin，以及 Gateway `/health`、`/health/live`、`/health/ready`、`/version` 的无凭据 GET/最小 CORS 预检。

最终截图均通过 PNG 解码、尺寸、非空/非单色像素检查和人工目视检查：

| 截图 | 像素 | bytes | SHA-256 |
| --- | ---: | ---: | --- |
| `browser-desktop-1280x720.png` | 1280x2442 | 189,063 | `a343a22d1e75ed25eabac7171cbd8181ff1b57446d82171333fdebe3a669889e` |
| `browser-intermediate-900x800.png` | 900x2442 | 180,619 | `f44965debcda21cd63e4aa1e74d422eaacbb2806d9588f441241e17bd0e3a0bf` |
| `browser-mobile-390x844.png` | 390x4738 | 215,293 | `ab952615a95feef32d18b2ca77fdae7187c936d84a3f2d73e4d9570e8a36a3c8` |

## 7. 清理与停止后数据库

Browser 在写锁内重新验证完整 owner 图后执行 tenant-qualified 精确清理：

| 资源 | Browser cleanup 影响行数 |
| --- | ---: |
| 剩余 Session | 7 |
| 临时用户 | 5 |
| Alias | 2 |
| Model | 4 |
| Provider | 4 |
| Virtual Key | 1 |
| Webhook | 0 |
| 第二租户 Environment | 1 |
| 第二租户 | 1 |

3 个 UI Session 已由真实 logout 撤销，因此 cleanup 再撤销 7 个；最终 10 个验收 Session 全部 revoked。Webhook 已由 Runtime 先禁用，所以 Browser 影响 0 行，但终态仍精确为 1 个 disabled Endpoint。2 条 enabled RoutingPolicy 因历史 Decision 引用而保留，其关联 Alias 均 disabled，`activeResidueCount=0`、`namedActiveResidueCount=0`、`callablePolicyCount=0`。

服务停止后执行 `wal_checkpoint(TRUNCATE)`：

- 最终数据库：`artifacts/prd047/20260719T145100/metahub-prd047.db`；
- 大小 `3,751,936` bytes，SHA-256 `daf0acfc9d88aa79681cbcbc83d763738ee280f23059abca0d78e1f9e9972039`；
- 36 条迁移、最高 `0036`、checksum 匹配、`integrity_check=ok`、FK 违规 0；
- `-wal`/`-shm` 不存在，8000/8001/5173 无监听；
- active/named 临时资源和可调用临时 RoutingPolicy 均为 0；预算预留 1 条且 outstanding 为 0，Usage 1 条。

## 8. 日志与敏感信息终验

服务使用原生子进程句柄保存日志。停止后 Gateway `32/32`、Control `205/205` 条 MetaHub JSON 均可逐行解析；每个文件另外只有 4 条预期 Uvicorn 生命周期/访问行，无 PowerShell 包装或硬换行。

最终扫描 12 份日志/JSON 和业务库：

- `logger=httpx/httpcore.*`、`HTTP Request:`、`/upstream-a|b`、`/hook`、完整第三方 URL 均为 0；
- Prompt、管理员/临时密码、验收 Auth/Provider Secret、Authorization/Bearer/JWT、完整 Virtual/Provider Key、Webhook Secret 均为 0；
- 唯一 Virtual Key/Provider/Webhook 展示只到总长 12 的安全 prefix；哈希和密文字段不含明文前缀；
- Audit JSON 无 `base_url`、`endpoint_url`、`plain_value`、`signing_secret`、Token 或 Provider Key 字段；
- Traceback、应用 `ERROR`、Uvicorn ERROR、`NativeCommandError` 均为 0；
- 结构化 503 精确为无 KeyPool 同一 request_id 的 `request.failed`、`error.diagnosed`、`request.completed` 三条，其他请求 5xx 为 0。

## 9. 作废批次

- `20260719T120059`：Playwright 依赖路径缺少 `playwright-core`。
- `20260719T132828`：旧 Browser 脚本错误要求 API Token 与 UI Token 相等。
- `20260719T134044`：旧网络门禁错误阻断 Gateway 健康请求。
- `20260719T135329`：旧敏感规则把 12 位 Virtual Key prefix 误判为完整 Key。
- `20260719T140847`：行为验收通过，但 `httpx` 自由文本泄露 Provider/Webhook 完整 URL；随后增加 fail-closed Filter 和真实 MockTransport 测试。
- `20260719T143031`：Runtime/Browser 和 owner-safe cleanup 均通过，但 PowerShell 5.1 把 native stderr 包装为 `NativeCommandError` 并硬换行，无法证明 JSONL 合同，因此不得封账。

## 10. 已知边界

- 候选仍限于 ModelAlias 的 default/fallback 两项；不做任意候选池、部门/用户 override 或自动学习。
- `cost_first` 缺价/混币按稳定 fixed 顺序回退，不伪造跨币种汇率；active CNY Budget 的价格依赖更严格，缺失时失败关闭。
- 流式调用不在中途切换模型；非流式仍最多一次受策略约束的 fallback。
- Webhook 持久 outbox、崩溃接管和 dead-letter 仍属于 PRD-057。
- OpenRouter、硅基流动仍只是竞品/迁移兼容对象，不是默认上游 Provider。

## 11. 结论

PRD-047 的设计、两轮自审、实现、安全复审、迁移、专项/全量、真实三服务/API/Webhook/Usage、资源清理、停止后数据库/日志复核和真实浏览器均已通过，可以标记完成。按 Excel 里程碑和优先级重新识别出的下一条未完成项是 `PRD-048 - 效果-成本路由基础`（M1-企业POC/P1）；总体 108 条 PRD 目标仍在进行中。
