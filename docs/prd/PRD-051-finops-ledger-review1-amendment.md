# PRD-051 第一轮修订勘误：USD Usage

> 本勘误覆盖 `PRD-051-finops-ledger-review1.md` 2.3 中“USD 时
> `cost_cny_micros=0`”的描述。两轮自审合并回 canonical 文档时只保留本勘误口径。

- 正式分录以 `amount_micros + currency` 保存来源原币金额，Usage 直接复制
  `cost_amount + currency` 的规范十进制事实。
- 另设可空 `normalized_cny_micros`。原币为 CNY 时等于 amount；USD 在没有受信汇率快照
  时必须为 NULL，不能写 0 冒充“已经归一化为零成本”。
- API totals 按原币分别返回 debit/credit/net；`net_cost_cny` 只累计
  `normalized_cny_micros IS NOT NULL`，并返回 `unnormalized_entry_count`。
- Adjustment 本期仍限定 CNY，不引入 FX。reversal 继承目标原币；PriceSnapshot 继续提供
  原币价格证据。
- 任何页面、事件和导出都必须把 NULL 展示为“未折算”，不能展示为 `0 CNY` 或免费。

该问题属于第一轮 P0，采用上述口径后允许交第二轮继续审查。
