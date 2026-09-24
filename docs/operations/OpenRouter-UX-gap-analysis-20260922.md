# OpenRouter UX 差距收口（2026-09-22）

这份记录把 OpenRouter 的简洁体验与 MetaHub 的企业职责分开比较。OpenRouter 的主路径更短，主要围绕模型选择、密钥和请求使用；MetaHub 还必须表达组织边界、Provider/模型/路由、员工 KEY、预算、审计、治理、Support、Device、Partner 和私有 Gateway。差距不在于删除这些企业能力，而在于首屏是否只呈现当前角色的下一步。

## 已落地的收敛

- Workspace 组织管理员首屏收敛到 Workspace、Gateway、Finance、Governance 四个工作域；完整高级入口仍按授权深链进入。
- 员工只看到一个统一 Gateway 地址和一把个人 MetaHub KEY；`/v1`、`/responses`、`/messages` 是同一网关协议路径，模型名称只在客户端强制要求时填写。
- Provider → 模型 → 员工 KEY → 首次调用形成单一下一步链，预算和治理显示例外状态，详情按需展开。
- 管理员工作台去掉包住四张操作卡的外层卡；Webhook 事件选择器修正了滚动裁切和操作行重叠。
- 中文用户是当前发布优先对象；英文静态投影继续修复混合文案，Support 与 Private Gateway 的完整短语已通过统一 locale 出口处理。

## 身份域边界

SaaS 平台运营管理员不是 Workspace 组织管理员。平台运营管理员使用 Platform session，负责平台 bootstrap、平台账务、发布门禁和控制面迁移；组织管理员、Developer 和员工使用 Workspace session，负责组织 Provider、模型、路由、员工 KEY、预算、Support、Device、Partner 和私有 Gateway。两类 session、CSRF 和浏览器证据必须隔离，不能把 Platform 页面通过算成组织管理能力，也不能把组织配置结果算成 SaaS 平台运营通过。

## 当前证据与边界

当前源码证据为 `outputs/prd148-source-current-20260922-support-locale-final.json`，SHA256 `d0c39d15adf763d2ceea482cbd2ee11f4b9a4515d9cc8c429ad2c3a25a678cee`。前端全量单测 2234/2234、lint 841 files、生产构建 295 assets/277 modules；前端和控制面 manifest 已统一到资源摘要 `be93bbfd03b244a16c7483c7caf8f428ec9f59733efdea074e87a81004884069`。

平台与 Workspace 分角色浏览器证据仍引用各自原始源码快照，不能因为本批 locale 修复就改写成当前完整浏览器 GA 证明。历史 Chromium 登出超时和 Workspace page-view 并发失败继续保留；真实注册到付费/额度、企业治理与私有 Gateway 生产链、Partner/Device 生产数据、发布复核/回滚、财务/隐私/安全/迁移对账以及 HEAD/origin 仍没有完整当前证据。六方会签与连续 259200 秒稳定性观察按用户指示不执行，因此 `prd148_complete=false`、`ga_approval=false`。
