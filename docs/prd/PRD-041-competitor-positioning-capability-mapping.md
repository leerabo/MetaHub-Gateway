# PRD-041 - 竞品定位与能力映射

状态：已完成设计、两轮自审、编码、专项/全量测试、运行库迁移和桌面/移动浏览器验收。

## 需求理解

PRD-041 属于 `M0.5-W3/4`、`P0` 核心能力，面向售前和产品角色，在控制台“帮助中心-竞品对比”提供统一、克制、可核验的产品定位说明。权威 Excel 要求：

- 明确 OpenRouter 和硅基流动是对标平台、竞品平台和迁移兼容对象，不是默认模型厂家。
- 明确 MetaHub 的产品本体是面向中国境内企业的 AI Gateway + 治理控制面。
- 说明 MetaHub 当前实现如何承接企业治理、成本、审计和调试，而不是用攻击性语言评价竞品。
- Provider 分类不能出现 `openrouter`、`siliconflow` 等把平台名称直接当成模型厂商类型的枚举。
- 页面访问记录 `help.competitor_viewed`，事件必须带认证上下文恢复的 `tenant_id`、用户和 `request_id`。
- 不新增竞品业务 API，不把任一竞品设置为默认上游，不做实时竞品抓取、价格排名、性能跑分或采购建议。

架构文档把 OpenRouter 类平台概括为统一 API、模型聚合和开发者体验，把硅基流动类平台概括为国产模型推理服务、模型生态和可获得性，同时要求 MetaHub 聚焦中国企业的租户、预算、审计、内部成本、调试与本地交付。外部事实会变化，因此页面只展示经官方资料支持的高层定位，不展示易过期的模型数量、价格、吞吐或“有/没有某功能”断言。

截至 2026-07-15 的官方资料核验结论：

- OpenRouter 官方文档将其描述为跨模型和 Provider 的统一 API，并提供 OpenAI-compatible 请求、模型路由、用量/成本信息和数据策略；页面不得声称 OpenRouter “没有治理”。
- 硅基流动官网将其描述为大模型 API、模型推理服务，并公开企业网关、流量/配额、观测和私有化等能力；页面不得声称硅基流动 “只有推理、没有企业能力”。
- New API 和 Sub2API 可作为界面信息组织与术语覆盖参考，但不复制其源码、品牌视觉或业务模式，也不把这两个开源项目列为本 PRD 的主要竞品结论。

本 PRD 的定位页是“对标维度与 MetaHub 当前实现导航”，不是市场宣传落地页、完整功能矩阵、法律结论或采购评估报告。

## 涉及模块

- `docs/prd/PRD-041-competitor-positioning-capability-mapping.md`
  - 保存本设计、自审结论、实现与验收证据。
- `console-web/src/features/competitor-comparison/`
  - 新增版本化 `CompetitorMapping` 展示模型、术语常量、来源信息和页面渲染器。
  - 展示关系定义、三方定位、共同能力模式、MetaHub 当前实现映射、边界说明和官方资料。
- `console-web/src/features/developer-start/renderDeveloperStart.js`
  - 在后端确认 active item 后渲染竞品对比页；不得仅依靠前端角色判断授权。
- `console-web/src/app/main.js`
  - 绑定竞品帮助页访问状态机；渲染完成后每个 visit 最多尝试一次 `help.competitor_viewed`。
- `console-web/src/lib/consoleVisitTracker.js`
  - 抽取 PRD-040 已验证的 generation/attempted/inFlight 通用状态机，API 固定为 `createUniquePageVisitTracker({ targetItemId, recordVisit })`。
  - 迁移指南和竞品帮助页复用；`main.js` 保留 `createMigrationGuideVisitTracker(recordVisit)` 兼容导出和现有测试。
- `control-plane-api/src/metahub_control/console/models.py`
  - 扩展事件类型枚举，允许 `help.competitor_viewed`。
- `control-plane-api/src/metahub_control/console/service.py`
  - 增加“竞品对比”导航定义和 event/navigation 配对；后端继续校验规范 path/hash/source/viewport。
- `control-plane-api/src/metahub_control/providers/service.py`
  - 不新增竞品 Provider 类型；保持现有 Provider 类型白名单，显式测试平台名称作为类型时被拒绝。
- `console-web/src/features/providers/renderProviderPanel.js`
  - 收紧 `migration_compatible` 中文标签和帮助文案，明确它是客户主动配置的兼容入口分类，不是模型厂家分类或默认上游。
- `infra/migrations/0030_competitor_help_event.sql`
  - 原子扩展 `console_page_events.event_type` 约束以支持 `help.competitor_viewed`。
- `infra/migrations/0030_competitor_help_event.down.sql`
  - 回滚时把竞品帮助事件降级为 `console.page_viewed`，保留租户、用户、路径和请求链路。
- `control-plane-api/tests/integration/`、`console-web/tests/unit/`
  - 增加事件、权限、迁移、Provider 分类、术语、来源、交互和响应式测试。

本 PRD 不修改 Gateway 数据面、路由、Provider Adapter、用量成本计算或调用日志热路径。

## 数据表/字段设计

### CompetitorMapping 展示对象

`CompetitorMapping` 是版本化静态展示对象，不新增业务表：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 稳定标识，例如 `openrouter`、`siliconflow`、`metahub` |
| `display_name` | string | 页面名称 |
| `object_kind` | enum | `benchmark_platform`、`model_service_platform`、`enterprise_gateway` |
| `relationship_to_metahub` | enum | `benchmark_and_migration_compatibility` 或 `product_itself` |
| `focus_summary` | string | 中立的高层定位，不写排他性优劣判断 |
| `common_patterns` | string[] | 可学习的 API、模型接入、路由或开发者体验模式 |
| `metahub_mapping` | string[] | MetaHub 当前已实现模块的导航映射 |
| `source_label` | string | 官方资料名称 |
| `source_url` | HTTPS URL | 官方资料链接，不由租户输入 |
| `source_verified_on` | date | 最近人工核验日期 |
| `status` | enum | 固定为 `active`，为后续版本化预留 |

Excel 建议业务对象默认带 `tenant_id/created_at/updated_at/status`。本对象不落库、不由租户创建，也不表达租户业务状态，因此不伪造 `tenant_id` 和时间字段；租户归属由访问事件记录。若后续允许租户自定义竞品评估，必须新建独立 PRD 和带 `tenant_id` 的业务表。

### console_page_events 约束变更

不新增字段，仅把允许事件从：

```text
console.page_viewed, dashboard.viewed, migration.guide_viewed
```

扩展为：

```text
console.page_viewed, dashboard.viewed, migration.guide_viewed, help.competitor_viewed
```

事件继续保存：`event_id`、`tenant_id`、`user_id`、`session_id`、`event_type`、`navigation_item_id`、`target_section`、`path`、`request_id`、`trace_id`、`payload_json`、`created_at`。

`0030` 正向和回滚脚本都使用 `BEGIN IMMEDIATE`，重建表时复制全部历史并恢复三个现有索引。回滚仅把 `help.competitor_viewed` 改为 `console.page_viewed`，不得删除事件。

## API设计

### 不新增业务 API

Excel 的“API/动作=无”按字面执行：

- 不新增 `/competitors`、`/comparison` 或远程抓取接口。
- 不在控制面保存竞品价格、能力快照、账号或密钥。
- 不向 OpenRouter、硅基流动、New API、Sub2API 发起运行时请求。

### 复用 GET /console/navigation

新增导航项：

```json
{
  "id": "competitor-comparison",
  "label": "竞品对比",
  "group": "帮助中心",
  "target_section": "competitor-comparison",
  "required_roles": ["admin", "developer", "viewer"],
  "order": 115
}
```

帮助内容不包含租户配置或敏感信息，因此三种已登录角色都可读。匿名用户仍由认证依赖返回 401。后端必须拒绝未知 active item，前端隐藏不作为权限依据。

### 复用 POST /console/events

请求：

```json
{
  "event_type": "help.competitor_viewed",
  "navigation_item_id": "competitor-comparison",
  "target_section": "competitor-comparison",
  "path": "/#competitor-comparison",
  "payload": {
    "source": "sidebar",
    "hash": "#competitor-comparison",
    "viewport_class": "desktop"
  }
}
```

后端固定配对规则：

- `competitor-comparison` 只能写 `help.competitor_viewed`。
- `help.competitor_viewed` 不能配其他导航、target、path 或 hash。
- path 固定 `/#competitor-comparison`，禁止 query string。
- `source` 只允许 `sidebar/direct_hash`，`viewport_class` 只允许 `mobile/desktop`。
- `tenant_id/user_id/session_id` 只从认证上下文恢复，额外请求字段由 Pydantic 拒绝。
- 事件连接、INSERT 或 commit 失败继续返回现有 503 `console.event_unavailable` 并 rollback。

## 前端页面/交互设计

### 页面结构

页面是工作型帮助视图，不做营销 Hero，不使用竞品 Logo 拼贴或攻击性视觉：

1. **定位结论**：首屏直接说明三条不可混淆的分类。
2. **对象关系**：用小型定义表区分模型厂家、云端模型 Endpoint、聚合/推理平台、MetaHub 产品本体。
3. **能力映射**：按“统一接入、模型与路由、用量成本、租户权限、策略治理、审计调试、部署交付”七个维度展示各平台公开定位和 MetaHub 当前实现入口。
4. **MetaHub 当前实现**：提供可点击的控制台导航命令，例如模型别名、用量与成本、预算、审计日志、调试台；不存在的后续能力不得展示为已完成。
5. **售前表述规范**：展示推荐说法、禁止说法和需要单独核验的采购问题。
6. **资料与时效**：列出官方来源、核验日期和“资料会变化”的提示。

### 文案规则

必须使用：

- “对标平台 / 竞品平台 / 迁移兼容对象”。
- “默认上游是模型厂商、云端模型 Endpoint 或客户本地 OpenAI-compatible Endpoint”。
- “MetaHub 当前产品定位是面向中国境内企业的 AI Gateway + 治理控制面”。
- “能力映射基于公开资料和 MetaHub 当前实现，不构成采购、法律或安全结论”。

禁止使用：

- “OpenRouter/硅基流动是模型厂家”。
- “竞品没有治理、审计、私有化或企业能力”。
- “全面领先、完全替代、最便宜、最快、100% 兼容”等无法由本 PRD 证明的表述。
- 竞品账号、价格、模型数或性能的实时排名。

### 交互

- 页面默认展示“定位与映射”，不要求用户输入任何内容。
- 使用三个固定原生 tabs：“定位关系”“能力映射”“售前规范”。模块级 `activeComparisonTab` 保留 15 秒刷新前的选择，登录边界变化时重置为“定位关系”；键盘支持 ArrowLeft/ArrowRight/Home/End/Enter/Space，ARIA 关系完整。
- `CompetitorMapping.metahub_mapping` 保存 `{ label, navigation_id, implementation_status }`。首版只允许 `implementation_status=current`，渲染时再与后端 `navigation_items` 求交集；“查看 MetaHub 能力”按钮只跳转当前角色可见入口，viewer 不显示无权限入口，也不把不可见入口改写成可点击 hash。
- 官方资料链接在新标签页打开，使用 `target=_blank`、`rel="noopener noreferrer"`，链接文本和 URL 来自受控静态常量。
- `switchConsoleNavigation` 对 `migration-guide` 和 `competitor-comparison` 都禁止写通用 `console.page_viewed`；两页各自由渲染后 tracker 成为唯一事件 owner，避免一次点击同时写两种事件。
- 首次 direct hash 和侧栏进入都在页面渲染后由唯一 tracker 写一次事件；15 秒刷新不重复，并发渲染共享同一个 in-flight Promise，离开后重入允许新事件。
- 事件失败不阻止阅读；401 清理 Session，非 401 显示一次带 request_id 的非阻断提示且本 visit 不重试。

### 响应式

- 桌面使用受约束的维度表和两列对照；移动端转为单列，宽表只在自身容器横向滚动。
- 目标视口为 1280x720 和 390x844；根节点必须满足 `scrollWidth <= clientWidth`。
- 标题、来源、按钮和长英文标识允许换行，不允许遮挡后续内容。

## 错误码设计

本 PRD 不新增错误码，复用现有统一错误：

| 场景 | HTTP | 错误码 | 页面处理 |
| --- | --- | --- | --- |
| 未登录读取导航或写事件 | 401 | `auth.token_missing/token_invalid` | 清理 Session 并显示登录页 |
| active item 不存在 | 422 | `console.navigation_invalid` | 回退概览并保留 request_id |
| event/nav/path/hash/payload 不匹配 | 422 | `console.event_invalid` | 不落库，页面继续可读 |
| 事件数据库不可用 | 503 | `console.event_unavailable` | 一次非阻断告警，本 visit 不重试 |
| `provider_type=openrouter/siliconflow` | 422 | `provider.type_invalid` | 引导选择真实厂商类型、OpenAI-compatible、custom 或显式迁移兼容分类 |

所有 API 错误继续携带 `code/message/request_id/trace_id/suggestion`。页面不得显示 SQL、数据库路径、Token、Provider Key、Virtual Key、Prompt 或上游原始响应。

## 安全与权限约束

- 三种已登录角色可读帮助页；匿名访问由后端认证拒绝。帮助页不得读取租户 Provider、Key、调用内容或其他业务敏感数据。
- 后端导航定义是入口授权事实源；前端只能根据返回的 visible items 渲染和跳转。
- 事件身份来自 Session，前端不得提交 `tenant_id/user_id/session_id`。
- `CompetitorMapping` 是静态只读对象，不接受表单、URL query、远程 JSON 或租户覆盖。
- 所有展示文本和属性统一 HTML 转义；官方链接只允许代码内固定 HTTPS URL。
- 不加载竞品脚本、图片、像素或 iframe，避免向第三方泄露控制台访问、租户或浏览历史。
- Provider 类型白名单不增加 `openrouter/siliconflow`；显式桥接只可使用现有 `migration_compatible`，且不能成为 seed、默认配置或自动推荐。
- 页面不展示真实 Provider Key、Virtual Key、Authorization、Prompt、Response、租户资源 ID 或调用日志正文。
- 页面不攻击竞品，不推断未公开能力，不把来源资料转化为法律、合规、安全或采购结论。

## 测试用例

### 数据迁移

1. 全新数据库应用 `0001..0030`，`schema_migrations` 为 30 条。
2. 从 `0029` 升级后保留三类历史事件，并允许 `help.competitor_viewed`。
3. `0030` 回滚把帮助事件降级为 `console.page_viewed`，保留 tenant/user/session/path/request/trace/payload/time。
4. 回滚后重新升级，三个索引存在，`foreign_key_check` 为空，`integrity_check=ok`。
5. 注入中途失败时迁移 runner rollback，不留下唯一临时副本或丢失历史。
6. 回滚顺序严格验证 `0030 -> 0029`：回滚 0030 后仍允许 `migration.guide_viewed`，再回滚 0029 才移除该类型。

### 后端主流程、异常与权限

1. admin/developer/viewer 导航均包含 `competitor-comparison`，分组为“帮助中心”。
2. 匿名导航和事件返回统一 401；未知 active item 返回 `console.navigation_invalid`。
3. 三种已登录角色都可写合法 `help.competitor_viewed`，事件 tenant/user/session 来自认证上下文。
4. 伪造 event/nav/target/path/hash/query/source/viewport 或额外 tenant_id 均返回 422 且不落库。
5. 连接、INSERT、commit 失败 rollback 并返回 `console.event_unavailable` 与 request_id。
6. Provider 类型白名单不包含 `openrouter/siliconflow/silicon_flow`；创建这些类型返回 `provider.type_invalid`。
7. seed 和运行库不存在把竞品平台作为默认 Provider 类型或默认上游的记录。

### 内容与术语

1. 页面同时出现 OpenRouter、硅基流动和 MetaHub，且两类竞品关系固定为 `benchmark_and_migration_compatibility`。
2. 页面出现“不是默认模型厂家”和默认上游正确定义。
3. 页面不出现攻击性、绝对化或已知错误表述，例如“竞品没有治理”“全面领先”“100% 兼容”。
4. 能力映射只描述公开定位和 MetaHub 当前实现，不把未来能力展示为已完成。
5. 官方来源均为受控 HTTPS 地址，带核验日期和非采购结论声明。
6. 全仓术语扫描不出现把 `openrouter/siliconflow` 加入 Provider 类型枚举、seed 或厂商下拉项。

### 前端交互

1. viewer 可渲染帮助内容，但只显示其后端可见的 MetaHub 能力跳转。
2. 能力维度 tabs 的 ARIA、Arrow/Home/End 键盘操作正确。
3. 点击 MetaHub 能力入口只导航到后端 visible item，不接受任意 hash。
4. direct hash、侧栏点击、15 秒刷新、并发刷新、失败刷新和离开重入验证每个 visit 最多一次尝试；侧栏点击不得额外写 `console.page_viewed`。
5. 事件 401 清 Session；非 401 显示一次 request_id，不阻塞内容。
6. 动态导航标签、来源和错误文本全部转义。
7. 1280x720 与 390x844 无根节点横向溢出、文本重叠或按钮越界。

### 验收用例映射

- `TC-0081`：控制台和 README 的表述一致；页面把两者定位为对标/迁移兼容对象，MetaHub 映射到当前治理、成本、审计、调试入口；Provider 类型无竞品平台枚举。
- `TC-0082`：匿名、伪造事件、非法 Provider 类型、事件存储故障和敏感字段均走统一错误；页面无攻击性或默认上游误导。

## 代码修改计划

1. 完成本设计的两轮独立自审，先修正事实、权限、事件和术语风险。
2. 新增 `0030` 正反迁移，扩展事件 DTO、导航定义和 event/nav 配对测试。
3. 保持 Provider 类型白名单不变，补 OpenRouter/硅基流动非法类型和 UI 文案回归测试。
4. 新增静态 `CompetitorMapping` 模块、帮助页、受控官方来源、能力映射和售前表述规范。
5. 抽取通用 visit tracker，接入竞品帮助事件，并保持 PRD-040 状态机行为不变。
6. 增加后端、迁移、前端、术语扫描、响应式和中文注释测试。
7. 更新 README、目录说明和 `docs/testing/PRD-041-tests.md`。
8. 运行专项测试、Ruff、前端 lint、中文注释门禁和全量 CI。
9. 升级运行库、重启服务，在桌面/移动浏览器验证内容、键盘、导航、事件去重和统一错误。

## 外部事实来源

以下来源仅用于核验高层定位，页面运行时不会请求这些站点：

- OpenRouter API Overview：<https://openrouter.ai/docs/api/reference/overview>
- OpenRouter Models：<https://openrouter.ai/docs/guides/overview/models>
- SiliconFlow 官网：<https://siliconflow.cn/>
- SiliconFlow 大模型推理服务网关：<https://siliconflow.cn/ai-gateway>
- New API：<https://github.com/Calcium-Ion/new-api>
- Sub2API：<https://github.com/Wei-Shaw/sub2api>

## 设计自审 1

第一轮只审查产品事实、中立表述、展示模型和前端信息架构，不评估迁移实现。初稿存在以下问题：

- **阻断：竞品能力缺失推断风险。** OpenRouter 当前公开数据策略，硅基流动当前公开企业网关、配额和观测能力；若沿用架构早期的“客户还需要我们”句式，页面容易被理解为竞品没有治理。设计已改为“公开定位 + MetaHub 当前实现映射”，禁止使用“竞品没有治理/审计/私有化”等断言。
- **重要：页面 tabs 语义不确定。** “能力维度使用 tabs 或分段控制”无法形成稳定 DOM 和测试契约。已固定为“定位关系 / 能力映射 / 售前规范”三个 tabs，并定义模块级状态、登录重置和完整键盘操作。
- **重要：静态能力跳转可能越权或误导。** 初稿只说按当前角色可见，却未定义数据结构和求交方式。已把映射收紧为 `{label,navigation_id,implementation_status=current}`，渲染时必须与后端导航求交，不能为 viewer 构造任意 hash。
- **重要：开源参考来源不准确。** 初稿写成“New API/One API”，容易把不同项目混为一体。已改为直接引用 New API 当前仓库，Sub2API 单独列出；两者只作为信息组织参考，不进入主要竞品结论。
- **建议：静态事实需要维护约束。** 已要求版本、核验日期、官方来源和文案同时更新，页面不展示模型数、价格、性能等高漂移数据。

结论：产品事实与页面契约修订后可编码，不需要实时抓取或新增竞品数据库。

## 设计自审 2

第二轮只审查数据库、事件、权限、Provider 分类和可验证性，不重复评估市场事实。初稿存在以下问题：

- **阻断：特殊页面可能双写事件。** 现有侧栏点击默认写 `console.page_viewed`，仅对迁移指南例外。若只新增 help tracker，一次点击会同时写通用事件和 `help.competitor_viewed`。设计已要求两个特殊入口都跳过通用事件，由各自 post-render tracker 单一负责。
- **阻断：0030 回滚不能误删 PRD-040 枚举。** 0030 的回滚目标必须保留 `migration.guide_viewed`，只把 `help.competitor_viewed` 降级；测试明确按 `0030 -> 0029` 连续回滚验证边界。
- **重要：通用 tracker 抽象范围不清。** 已固定新 helper 的参数和状态机语义，并要求保留旧 `createMigrationGuideVisitTracker` 导出，现有 PRD-040 并发、失败和 Session 变化测试必须继续通过。
- **重要：TC-0082 的“无权限”含义。** 帮助内容对三种已登录角色开放，因此异常权限用匿名 401、未知 active item、额外身份字段和伪造事件证明；不能虚构一个已登录但无帮助页权限的角色。后端仍是唯一入口事实源。
- **重要：Provider 分类证据不足。** 只检查页面文案不能证明后端。测试必须同时断言后端白名单、创建 `openrouter/siliconflow/silicon_flow` 返回 `provider.type_invalid`、前端下拉无这些厂商类型、seed 无竞品默认 Provider；`migration_compatible` 继续允许客户显式桥接但不能默认推荐。
- **重要：迁移失败与索引保留。** 0030 正反脚本沿用 `BEGIN IMMEDIATE`，测试必须覆盖历史计数、三个索引、外键和 integrity check，并复用 runner 失败 rollback 证据。

结论：两项阻断和四项重要问题已进入明确实现与测试契约，数据库与权限方向可以编码。
