"""Gateway 数据面应用装配入口。

这里只组合模型调用路由和不会反压热路径的后台事件 worker；认证、租户、路由与
Provider Key 的业务判断仍在各自模块完成，避免应用入口持有敏感请求状态。
"""

from __future__ import annotations

from metahub_shared.runtime.fastapi_app import create_service_app

from metahub_gateway.api.internal_batch import router as internal_batch_router
from metahub_gateway.api.internal_chat import router as internal_chat_router
from metahub_gateway.api.openai_compatible import router as openai_compatible_router
from metahub_gateway.cache_affinity import install_cache_affinity_recovery
from metahub_gateway.error_maps import install_provider_error_map_cache
from metahub_gateway.events import install_runtime_event_dispatcher
from metahub_gateway.feedback.routes import router as feedback_router
from metahub_gateway.intent_security import install_intent_security_dispatcher
from metahub_gateway.policies import install_policy_snapshot_cache
from metahub_gateway.policies.redaction import install_policy_mask_runtime
from metahub_gateway.quota import install_quota_reconciliation
from metahub_gateway.response_cache.failure_spool import (
    install_response_cache_failure_recovery,
)
from metahub_gateway.usage.spool import install_usage_spool_recovery

# 数据面是模型调用热路径。本 PRD 只创建空壳和健康接口，后续鉴权、限流、预算、
# 路由、Provider Adapter 等能力会按 PRD 逐条接入到这个 app。
app = create_service_app(
    service_name="gateway-data-plane",
    title="MetaHub Gateway Data Plane",
    # 公开会话身份只服务 PRD-070 风险治理，不转发 Provider；浏览器调用需显式通过 CORS 预检。
    extra_cors_allow_headers=("X-MetaHub-Session-ID", "X-MetaHub-Turn-ID"),
)
app.include_router(openai_compatible_router)
app.include_router(internal_chat_router)
app.include_router(internal_batch_router)
app.include_router(feedback_router)
# mask 能力先做纯内存自检再进入 readiness；默认门禁关闭时不会改变旧部署健康状态。
install_policy_mask_runtime(app)
# 启动恢复必须作为最内层 lifespan 最先执行；失败时事件 worker、Policy cache 和端口均不启动。
install_cache_affinity_recovery(app)
# Fallback Webhook 通过有界队列异步投递，任何接收端故障都不能延迟主备模型切换。
install_runtime_event_dispatcher(app)
# Intent worker 必须包在 runtime event dispatcher 外层：启动时事件队列先就绪，退出时
# 先完成旁路分类和 source event 生产，再由内层 dispatcher 排空 Webhook 投递。
install_intent_security_dispatcher(app)
# Usage 恢复必须在事件队列创建后运行，才能只为本次新建分录入队；退出时先停恢复 worker，
# 再由事件 dispatcher 有界排空，避免 shutdown 期间继续产生新事件。
install_usage_spool_recovery(app)
# Response Cache 的 commit-unknown 恢复只读取无正文 owner 记录；必须在 Usage/事件基础设施
# 就绪后启动，退出时则先停止该 worker，避免 shutdown 期间继续形成失败终态。
install_response_cache_failure_recovery(app)
# Usage/Cache 恢复先补齐 canonical charge；额度 worker 随后只处理已经超过 24 小时的残留预留，
# 有正式 charge 时补结算，无 charge 时释放并保留稳定 reconciliation 终态。
install_quota_reconciliation(app)
# Policy 快照 scheduler 与事件 worker 都通过 lifespan 组合安装；后安装者必须包裹而非覆盖
# 前一生命周期，确保启动、退出和异常清理均有确定顺序。
install_policy_snapshot_cache(app)
# Provider 错误规则只轮询已认证请求见过的 tenant；安装为组合 lifespan，任何同步失败都
# 由 cache 内部降级到 builtin/LKG，不把控制面网络依赖带入模型请求热路径。
install_provider_error_map_cache(app)
