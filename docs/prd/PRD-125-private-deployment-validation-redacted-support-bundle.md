# PRD-125 私有部署验证与脱敏支持包

状态：设计冻结，尚未编码；计划迁移 0117。依赖 PRD-120..124，范围以 Excel PRD-125 为准。

## Excel 权威合同

- API：`POST /edge/deployments/{id}/validations`、`GET /edge/deployments/{id}/validations/{run_id}`、`POST /local/support-bundles`、`GET /edge/support-bundles/{id}`。
- 对象：`DeploymentValidation`、`ValidationCheck`、`SupportBundle`、`DeliveryAcceptance`。
- 事件：`edge.validation_completed`、`edge.support_bundle_ready`、`edge.delivery_accepted`。
- SLO：全套验证 < 15 分钟（不含上游长超时）；支持包 < 100MB 且生成 < 5 分钟。

## 1. 产品闭环

tenant admin 启动版本化验证计划，Agent 依次检查设备证书、签名配置/LKG、Provider 固定连通 outcome、真实最小请求、Usage/成本/失败同步、备份 readiness、容量和基准。每个 check 保存输入合同版本、固定 outcome、耗时和证据摘要，失败给出产品化恢复动作并可仅重试失败项。全部必需检查通过后，客户显式接受交付。

本地支持包生成器从编译期白名单采集版本、水位、固定健康/错误码和有界日志摘要，执行递归敏感扫描并让客户预览文件清单/分类/大小；客户同意后才加密上传。平台下载需要有效 SupportGrant，不能发起任意路径采集。

## 2. 0117 数据与制品

- Validation/Check 使用 plan version、deployment revision、release/config generation 和不可变结果，跨版本比较只比较同 check code。
- SupportBundle 云端只存 manifest hash、size、scan version/result、customer approval、object reference、TTL 和 download audit；本地原始收集区生成后清理。
- bundle manifest 拒绝符号链接、绝对路径、路径穿越、设备文件和超过预算的文件；压缩前后都有大小/条目上限。
- DeliveryAcceptance 绑定完整 validation generation、客户 actor/step-up、accepted time；后续配置变化不会改写历史接受事实。

## 3. 权限与页面

tenant admin 执行验证和接受；security_admin 预览/批准支持包；platform_support 只有有效的客户授权、双审批、短 TTL、固定 scope 才可下载，且每次恢复当前 grant revision。页面以检查列表、固定状态、恢复动作、支持包预览和交付签署为主，不显示 raw log、Secret 名称或内部实现说明。

## 4. 第一轮自审：验收真实性与支持边界

- P0：仅检查健康接口会产生虚假验收。修正为通过公开 Gateway 路径执行最小真实调用并核对 Usage/失败事实。
- P0：支持包允许任意路径会成为远程文件读取。修正为编译期 collector 白名单，无用户路径参数。
- P0：平台支持直接触发/下载会绕过客户。修正为本地生成、客户预览批准、SupportGrant 和逐次下载审计。
- P1：修复建议包含内部命令或路径会泄漏环境。修正为固定产品动作码，由 UI 映射安全文案。

## 5. 第二轮自审：可重复证据与清理

- P0：扫描只查常见键名会漏掉值型 Secret。修正为键名、已知本地 Secret HMAC、token/private-key 格式和熵规则组合，命中即阻断上传。
- P1：重复验证覆盖旧结果会失去交付证据。修正为 run 不可变，重试生成新 attempt 并关联原 run。
- P1：支持包过期后对象仍可下载。修正为元数据 TTL、对象存储短时授权、下载时 grant/TTL 双重检查和删除作业。
- P1：临时采集目录残留违反静止终态。修正为成功/失败/取消均补偿清理，并在门禁中扫描。

两轮均为修正后通过，无未关闭 P0/P1。

## 6. 专项验证计划

0117 迁移；真实调用到 Usage/失败闭环、失败项重试、跨版本比较；路径穿越/符号链接/压缩炸弹/大小预算；Secret 值/格式/熵扫描；SupportGrant 缺失/过期/撤销/字段裁剪；三浏览器预览/接受；验证/支持包 SLO；对象 TTL、临时目录、日志和静止终态。
