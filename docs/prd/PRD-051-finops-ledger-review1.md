# PRD-051 第一轮编码前自审修订稿

> 临时状态：由于当前沙箱补丁包装器可新增文件但无法更新已有文件，本稿临时覆盖
> `PRD-051-finops-ledger.md` 中同名主题。第二轮必须同时审查两份文件；编码前由主 Agent
> 合并回唯一 canonical 设计文档并删除本临时稿。本文件不是产品实现或完成标记。

## 1. 第一轮结论

第一轮不允许直接进入编码。原初稿存在四个阻断问题和三个重要边界缺口：

1. SQLite NUMERIC 不是 Python Decimal，初稿没有定义可证明的微元转换与历史反算规则。
2. 无快照和已有快照但无版本的 Usage 都无法满足“正式分录版本非空”。
3. 0040 的 `audit_logs` CHECK 不接受 FinOps action/object，0041 必须保真重建而非只改 Python 白名单。
4. `usage_ledger_id` 没有格式 CHECK，不能截取其后缀作为正式分录或事件 ID。
5. 稳定 event_id 不等于持久 Outbox，现有有界队列可能丢外部投递。
6. offset 分页在持续追加账本上会重复或漏行，Totals 与列表也需要同一读快照。
7. Gateway 当前 Usage 函数返回 None，必须冻结提交结果和三个 call site 的单次入队语义。

以下修订覆盖初稿中的冲突描述。

## 2. 金额与币种修订

### 2.1 新写入 CNY 微元

- Adjustment 仍仅接受 CNY，内部保存非负 `amount_micros`，一 CNY 等于一百万微元。
- API 金额必须是普通十进制 JSON string；Python 用 `Decimal` 校验后量化到六位，再以
  `int(decimal * 1_000_000)` 得到整数。该乘法发生在 Decimal 上，不允许 float。
- Gateway 已把 `normalized_usage.cost_cny` 写成 JSON string。0041 后的新 Usage trigger
  必须优先解析这个十进制文本，不能直接计算 `CAST(NEW.cost_cny*1000000 AS INTEGER)`。
- 文本语法只允许 ASCII 数字和最多一个小数点，整数部分 1..10 位，小数部分 0..6 位；
  禁止符号、指数、空白、Unicode 数字、NaN/Infinity。先右补零到六位，再分别把整数段
  和小数段转 INTEGER，最后执行整数运算。
- trigger 在插正式分录前同时核对：从微元反格式化出的固定六位文本，必须等于该 JSON
  成本经右补零后的固定六位文本；否则以固定 SQLite 错误拒绝整个 Usage 事务。

### 2.2 历史兼容转换

0041 在任何持久 DDL 前对每条历史 Usage 执行 preflight：

1. `cost_cny` 必须非 NULL、有限、非负且不超过 `1000000000.000000`。
2. 若 `normalized_usage.cost_cny` 是符合上述语法的 JSON string，以它为权威十进制文本，
   并要求其固定六位规范值与 `printf('%.6f', cost_cny)` 一致。
3. 只有历史 normalized JSON 缺该字段时，才允许一次性使用
   `printf('%.6f', cost_cny)` 形成 legacy canonical text；该 fallback 必须在迁移注释、
   PriceSnapshot source 和测试证据中披露，不能声称它恢复了已丢失的任意精度。
4. canonical text 使用与新 trigger 完全相同的“字符串拆分 + 右补零 + INTEGER 运算”生成
   micros，禁止 REAL 乘一百万后截断。
5. 回填后逐行把 micros 反格式化为六位文本，并与 canonical text 比较；任一不等则在提交前
   abort。另验证范围、计数、唯一来源、SUM 的 Python 大整数交叉核对。

列表 totals 不使用 SQLite `TOTAL()` 或可能溢出的无界 INTEGER SUM。服务层在同一只读事务
中流式读取筛选后的整数 micros，以 Python 任意精度 int 累计，再一次性格式化为 Decimal
string；不把全量行加载进内存。

### 2.3 多币种边界

正式账本必须保留来源 Usage 的原币事实，不能把 USD 成本静默记成零 CNY：

- Usage 分录新增 `billing_amount_micros` 与 `billing_currency IN ('CNY','USD')`，从
  `usage_ledger.cost_amount/currency` 的 normalized JSON 十进制文本精确转换。
- 同时保留 `cost_cny_micros`。CNY Usage 两者相同；USD Usage 在当前无 FX 时
  `cost_cny_micros=0`，并返回 `cny_normalization_status='unavailable'`，不得宣称已经折算。
- Adjustment 只允许 CNY；reversal 继承目标 billing currency，可冲正 CNY 或 USD，
  请求字段使用通用 `amount`，currency 从目标推导，客户端不能覆盖。
- GET totals 按 currency 分组返回 debit/credit/net；另返回 CNY normalized totals，并明确
  排除尚无 FX 的 USD。M1 不新增汇率表或复杂 FX。

若第二轮认为 Excel 的 `cost_cny` 验收要求必须覆盖 USD，则应阻断编码并把 FX 依赖显式
升级，而不是随意使用当前汇率。本修订选择“保留真实 USD + 明示未折算”作为不偷跑 FX 的
最小兼容方案。

## 3. PriceSnapshot 修订

正式分录的 `price_schema_version` 一律从它实际引用的 PriceSnapshot 冻结：

1. 已有 snapshot 且版本非空：验证 snapshot tenant/request 与 Usage 一致；Usage 自身版本
   非空时必须与 snapshot 版本相等，冲突在 DDL 前 abort。
2. 无 snapshot：为该 Usage 新建 ledger-only snapshot，source 为
   `finops_legacy_backfill`，version 为 `legacy-unversioned-v1`；billing dimensions 明确
   “金额来自历史 Usage canonical text，未重算单位价”。
3. snapshot 存在但 version 为空：不更新旧 snapshot，复制其可用单价/币种/映射到该 Usage
   独享的 ledger-only versioned snapshot；无法信任原字段时单位价为零，并在 billing
   dimensions 标明 `legacy_price_version_missing`。正式金额仍取不可变 Usage，不由零单价重算。
4. 新 Usage 无有效价格时，Gateway 在写 Usage 前创建 `source=finops_unpriced`、
   `version=unpriced-v1` 的零价 snapshot；数据库 trigger 对受控内部直写提供相同 fallback。
5. 正式 Usage 分录必须同时具备 source_request_id、snapshot ID 和非空 version。

ledger-only snapshot ID 不依赖 Usage ID 的格式；由 SQLite `lower(hex(randomblob(16)))`
生成并以主键/唯一约束防碰撞。

## 4. ID、来源唯一性与投影修订

- `finops_ledger_entry_id='fle_' || lower(hex(randomblob(16)))`。
- `event_id='flevt_' || lower(hex(randomblob(16)))`。
- 不从 `usage_ledger_id` 截取后缀，也不要求迁移拒绝历史自定义 Usage ID。
- 真正的 exactly-once 边界是 `UNIQUE(tenant_id, usage_ledger_id)` 与
  `UNIQUE(tenant_id, finops_adjustment_id)`。随机 ID 碰撞由唯一约束使事务失败，不能
  `INSERT OR IGNORE` 后伪装成功。
- 历史回填固定按 `created_at ASC, usage_ledger_id ASC` 分配只增 sequence；同一 0041
  事务失败会完整回滚，重跑不会留下半分配。
- 新 Usage 的 AFTER INSERT trigger 先补齐/核验 snapshot，再按精确文本算法创建分录；
  任一步失败都会回滚 Usage、PriceSnapshot、Request Log、Attempt 与预算结算。

## 5. Audit 迁移修订

0041 Up 必须在创建 Adjustment API 前保真重建 `audit_logs`：

1. 在任一 DDL 前断言 0040 applied，并记录 0040 audit 表列、复合 owner 关系、所有索引和
   immutable/owner/REPLACE guard trigger 的完整集合。
2. 新 CHECK 仅追加 `action='finops.adjustment_created'` 与
   `object_type='finops_adjustment'` 的合法组合，不放宽其他 action/object。
3. 全列复制现有行，保留 NULL、时间、actor、request/trace、摘要 JSON 和 rowid 语义。
4. 用双向 EXCEPT、逐列 NULL 等价、行数/关键索引/trigger 集合、foreign_key_check 和
   integrity_check 证明无漂移，再删除旧表。
5. 恢复 0040 的全部索引和不可变/owner guard；为 FinOps 审计增加必要查询索引时不能改变
   旧索引语义。
6. Python `ACTION_OBJECT_TYPES` 注册上述一对；`SUMMARY_FIELDS` 仅允许 type、六位金额
   string、currency、target entry ID、related request ID 和 `reason_sha256`。reason 正文、
   幂等原文、payload、密钥和请求正文永不进入 Audit。
7. Down 在业务事实 guard 通过后逆向保真恢复 0040 枚举与对象集合；存在 FinOps Adjustment、
   FinOps Audit、ledger Webhook 订阅/投递时，在任何 DDL 前原子拒绝。

## 6. 稳定分页修订

`GET /finops/ledger` 不使用 offset：

- 首次请求在一个 `BEGIN DEFERRED` 只读事务内读取当前租户最大
  `ledger_sequence` 作为 `snapshot_sequence`。
- totals、count 与第一页都限定 `ledger_sequence <= snapshot_sequence`，并在同一事务
  完成，避免列表和汇总来自不同数据库快照。
- 后续请求必须回传 snapshot_sequence 和上一页最后一条的 `before_sequence`，查询条件
  为 `ledger_sequence < before_sequence`，固定倒序，返回 next cursor。
- cursor 只控制当前租户内序列范围，不携带 tenant；非法、负数、超过 snapshot 的值返回
  `finops.query_invalid`。筛选变化必须重新获取 snapshot，不复用旧 cursor。
- 响应返回 `snapshot_sequence/next_before_sequence/has_more`，不再返回 offset。

## 7. Gateway 提交结果与事件修订

新增不可变 `FinOpsLedgerCommitted` 数据结构，字段仅包含 ledger entry ID、event ID、
tenant、safe payload、actor/object/trace 和 created_at：

1. `record_usage_ledger` 在原事务内插 Usage 后，按 tenant + usage ID 查询 trigger 创建的
   唯一正式分录，校验 snapshot/version/金额/source，然后构造 committed 对象。
2. 只有 connection.commit 成功后函数才返回该对象；commit 或构造失败完整 rollback 并抛出。
3. `safe_record_usage_ledger` 捕获既有受控数据库异常，记录不含 payload 的固定 warning，
   失败返回 None，成功原样返回 committed；不能在失败时生成随机事件。
4. non-stream 主调用、Fallback 成功、Stream finalizer 三个 call site 各自只调用一次 safe
   wrapper；返回非 None 时才把 committed 转为 `WebhookEvent` 并
   `enqueue_runtime_event(request.app.state, event)`。
5. enqueue 发生在 commit 后。队列缺失/满时记录 tenant、event ID、固定 reason，不回滚账本、
   不延迟或改写模型成功响应，也不二次 enqueue。
6. Control Adjustment 也在 commit 后投递同一 canonical 事件；幂等 replay 不重复 Audit/Event。

本期外部 Webhook 仍是现有 best-effort 语义：稳定 event ID 允许正常 worker 幂等，但没有持久
Outbox，进程在 enqueue 前退出或队列丢弃时可能永久没有 delivery。账本行是可审计领域事实，
测试和文档必须如实披露；不得把 event_id 描述为投递恢复保证。

## 8. 第一轮修订后的测试追加

- 用 `0.000001/0.100001/999999999.999999`、短小数、历史缺 normalized cost、REAL 尾差和
  非法指数覆盖 canonical text -> micros -> text；直接 float 乘法实现必须被合同测试阻止。
- 当前开发库“snapshot 有但 version NULL”的历史样本必须回填为新的 ledger-only snapshot，
  且旧 Usage/旧 snapshot 字节级不变。
- Audit Up/Down 对 0040 全列、NULL、索引、trigger、rowid/顺序执行双向保真检查。
- 历史非标准 Usage ID 仍可升级且只入账一次；随机 ID 格式和 source 唯一约束通过。
- 并发追加期间使用 snapshot + keyset 翻页，不重复、不漏掉快照内分录，新分录留待下次刷新。
- 三条 Gateway 成功链分别只 enqueue 一次；commit 失败无事件；queue full 有固定告警且响应不变。
- USD Usage 保留原币金额并标记 CNY 未折算；不得显示为免费或计入伪造 CNY totals。

## 9. 第一轮自审记录

- 审查结论：初稿存在 4 个 P0、3 个 P1，未获准直接编码。
- 已修订：金额 canonicalization、无版本快照、Audit 保真重建、ID 生成、事件可靠性披露、
  水位游标分页和 Gateway 返回/入队合同。
- 待第二轮重点：多币种是否满足 Excel 的 `cost_cny` 验收；legacy PriceSnapshot 的语义是否
  会被误解为真实单价；Audit 重建/Down guard 是否覆盖 0040 全部对象；best-effort 事件是否
  符合本 PRD 而未偷跑后续 Outbox。
- 当前状态：允许进入第二轮设计审查，不允许进入编码。
