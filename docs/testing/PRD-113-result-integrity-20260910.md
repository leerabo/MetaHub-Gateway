# PRD-113 结果完整性与来源专项记录

日期：2026-09-10。状态：结果协议和防模拟验收修订通过，真实压测功能仍未完成。

## 要求与设计

只读核对原始增量计划 `01_PRD总表!A25:Y25` 与 `06_测试与验收计划!A25:L25`，未修改工作簿。
独立设计见 `docs/prd/PRD-113-capacity-performance-chaos-unit-economics.md` 第 10 节。
编码前两轮自审见 `docs/prd/S3-execution-evidence-correction-20260910.md`。

## 实现与测试

- 结果协议 v2 明确 measured/simulation，绑定对象、操作、manifest 与完整稳定执行参数摘要。
- 验证结果 checksum、固定 outcome、非空采样、百分位顺序和字段类型。
- HTTP 结果必须是 measured；旧格式、未知字段、篡改、跨对象或参数重放均拒绝。
- HTTP runner 未配置真实执行器时固定返回 503，不再制造成功指标。
- test_loopback 必须同时显式设置 environment=test，返回 simulation，不作为真实执行证据。
- 请求禁用环境代理及重定向；停止回执验证目标、accepted 与 kill generation。
- 拒绝结果不能进入基线或发布完成回调，保留固定失败状态。

最终专项：`.artifacts/s3-capacity-result-final-20260910.xml`，51 passed，零失败/跳过。
范围：`test_prd113_capacity_runner.py` 与 `test_capacity_result_integrity.py`。
完整性测试中的 measured 数据是明确标注的协议 fixture，不是压测测量，不计入性能基线。
相关 Ruff 与 git diff --check 通过，无模型或外部服务调用。

## 敏感审计

结果仅保留计数、百分位、资源聚合、摘要和固定状态，不新增 URL、header、Prompt/Response 或租户明细。
凭据只通过现有内部认证传递，不进入日志/报告；环境代理和重定向不能接收内部 Bearer/permit。
新增测试仅使用明确的合成凭据和对象，未读取试用 KEY 或用户数据库。
未修改迁移、已发布前端或当前 Control 进程，未提交临时数据库、日志、截图或证据原始文件。

## 尚未交付

实际隔离负载、持久 submit/poll/cancel、幂等执行、allowlist 合成目标、连续 stop 条件和恢复证明。
100+租户公平性、目标峰值 2 倍压力、真实资源曲线、72 小时长稳、PRD-107..112 全链路证据。
HTTP 503 只是移除假成功，不能记为完成真实压测。后续实现不阻止用户继续使用已发布的试用版本。
