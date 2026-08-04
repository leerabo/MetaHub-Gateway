"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/auth/dependencies.py
"""

from __future__ import annotations

import secrets
import time
from typing import Annotated

from fastapi import Header, Request
from metahub_shared.feedback import FEEDBACK_SUMMARY_ENDPOINT_DEADLINE_SECONDS
from metahub_shared.runtime.errors import ApiError

from .service import (
    AuthContext,
    authenticate_bearer,
    require_permission,
)


def _require_request_permissions(
    request: Request,
    authorization: str | None,
    permission_codes: tuple[str, ...],
) -> AuthContext:
    """认证当前请求并要求给定 permission code 全部命中。

    参数：请求对象提供可信服务配置，Bearer Header 只定位 Session，权限集合来自数据库快照。
    返回：带当前 tenant_id、revision 和授权指纹的上下文；任一权限缺失即统一 403。
    """

    context = authenticate_bearer(request.app.state.config, authorization)
    for permission_code in permission_codes:
        # 复合旧守卫采用全部命中语义，避免读写尚未拆路由时把已撤销的写权限重新放大。
        require_permission(context, permission_code)
    return context

def require_current_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """恢复当前登录用户。

    后续控制面管理 API 必须依赖这个函数拿租户上下文，不能只靠前端隐藏按钮。
    """

    # 认证请求是访问控制边界，调用方必须透传统一错误和 request_id。
    return authenticate_bearer(request.app.state.config, authorization)


def require_current_admin(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户为管理员角色。"""

    # 认证请求是访问控制边界，调用方必须透传统一错误和 request_id。
    return _require_request_permissions(
        request,
        authorization,
        (
            "user.manage",
        ),
    )


def require_tenant_settings_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取或维护当前租户的基础设置。

    PRD-059 v1 仅定义 ``tenant.settings.read``，且该权限只属于不可编辑权限矩阵的
    admin 角色；因此租户资料 GET/PATCH 共同复用这一精确权限点。后续若产品拆出写权限，
    必须先通过新迁移扩展权限目录，不能继续借用 ``user.manage`` 或角色名隐式放权。
    """

    # tenant_id 只能由 Bearer Session 恢复，客户端不得通过路径、查询或正文选择其他租户。
    return _require_request_permissions(request, authorization, ("tenant.settings.read",))


def require_virtual_key_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可写 Virtual Key。

    PRD-008 明确 viewer 不能创建 Virtual Key；后续 Virtual Key 正式接口必须复用
    这个依赖，而不是只在控制台隐藏“创建”按钮。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "virtual_key.write",
        ),
    )


def require_model_catalog_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取模型目录。

    模型目录对开发者开放，用于发现可调用的内部标准模型 ID；viewer 在 M0
    不开放，避免扩大企业内可用模型范围信息的暴露面。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "model.read",
        ),
    )


def require_request_log_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取调用日志。

    调用日志包含模型、状态、延迟、成本估算和错误码，面向管理员和开发者开放；
    viewer 在 M0 不开放，避免扩大企业内部模型与调用行为的可见范围。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "request_log.read",
        ),
    )


def require_request_explanation_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取请求解释中心。

    请求解释会聚合鉴权、预算、DLP、路由、成本与 Webhook 安全证据，因此只允许
    admin/developer。该依赖由 FastAPI 在业务 wire 解析前执行，viewer 不能借畸形
    path/query/header/body 探测请求或字段是否存在。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "request_explanation.read",
        ),
    )



def require_error_map_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取 Provider 错误归一规则。

    规则 pattern 和 Provider 范围属于租户治理配置，只向 admin/developer 开放。viewer 即使
    直接请求或提交畸形 query/body，也必须在业务 wire 解析前得到统一权限错误。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "provider_error_map.read",
        ),
    )


def require_error_map_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可修改 Provider 错误归一规则。

    规则会改变 Gateway 对上游失败的公开分类，因此写权限固定为 admin；前端隐藏编辑按钮
    不能替代该后端门禁，tenant_id 与 actor 仍只从已验证 Session 恢复。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "provider_error_map.write",
        ),
    )


def require_usage_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取用量与成本账本。
    用量 API 会暴露模型、Key、Token 和成本维度，M0 仅开放给管理员和开发者，避免 viewer 扩大企业内部调用可见范围。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "billing.usage.read",
        ),
    )


def require_dashboard_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取租户 Dashboard。

    PRD-026 旧总览和 PRD-065 角色首页都先经过本守卫；六个固定角色是否还能进入具体角色视图，随后继续以同一次请求的
    permission 快照与 tenant-scoped 配置求交。前端隐藏入口、角色名或历史 Session 均不能替代该后端权限校验。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "dashboard.read",
        ),
    )



def require_project_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取项目与应用归属信息。

    Project/Application 会暴露业务系统归属、Key 绑定数量和成本聚合入口，M0.5 只开放给管理员和开发者。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "project.read",
        ),
    )


def require_feedback_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前 Session 用户可以提交反馈并读取反馈汇总。

    参数 ``request`` 提供控制面配置，``authorization`` 是 Session Bearer Token；
    返回已经校验的用户、租户和会话上下文。逐请求反馈会暴露调用归属和模型满意度，
    因而仅向 admin/developer 开放，viewer 即使绕过前端直接调用也必须在读取正文或
    查询参数前由本依赖拒绝，避免利用校验差异探测反馈合同。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "feedback.use",
        ),
    )


def require_feedback_summary_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """在整个 250ms endpoint 预算内认证 Feedback Summary 读者。

    起点由共享中间件在任何 FastAPI dependency 前写入；本依赖把同一绝对 deadline
    继续传给 Session 查询/last_seen 锁等待，再保存给路由和 Summary worker。匿名、
    无效 Token 与 viewer 仍先得到稳定认证/RBAC 错误，不解析报表 query。
    """

    started = getattr(request.state, "request_started_monotonic", time.monotonic())
    deadline = started + FEEDBACK_SUMMARY_ENDPOINT_DEADLINE_SECONDS
    # deadline 只驻留当前请求，路由不得重新用当前时刻生成另一份 250ms 预算。
    request.state.feedback_summary_deadline = deadline
    context = authenticate_bearer(
        request.app.state.config,
        authorization,
        monotonic_deadline=deadline,
        deadline_error_code="feedback.unavailable",
    )
    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return require_permission(context, "feedback.use")


def require_usage_exporter(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可批量导出当前租户的用量与成本账本。

    PRD-043 的财务角色在当前 RBAC 尚未独立建模，因此批量导出仅允许 admin；
    developer 仍可使用分页 Usage API，但不能借导出绕过后续 PRD-059 的账单权限矩阵。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "billing.usage.export",
        ),
    )


def require_finops_admin(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取正式账本并创建财务调整。

    当前 RBAC 尚未建模独立 finance 角色，因此只允许 admin。返回统一认证产生的
    AuthContext；developer/viewer 即使手写请求也会在解析财务查询或正文前被拒绝。
    """

    # 复用统一管理员守卫，保持认证、租户恢复和固定权限错误完全一致。
    return _require_request_permissions(
        request,
        authorization,
        (
            "billing.finops.read",
            "billing.finops.write",
        ),
    )


def require_cost_anomaly_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取成本异常与检测器健康状态。

    PRD-053 尚未引入独立 finance/security 角色，因此读权限固定映射到 admin。
    该依赖必须在路由解析 query、header 和 body 之前执行，确保 developer/viewer
    无法借畸形输入探测治理接口合同。
    """

    # 统一管理员守卫同时恢复可信 tenant_id 和 actor，读接口不得自行解析客户端身份。
    return _require_request_permissions(
        request,
        authorization,
        (
            "billing.anomaly.read",
        ),
    )


def require_cost_anomaly_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可处置成本异常并创建或释放临时限流覆盖。

    返回的 tenant_id 与 actor_user_id 都来自已验证 Session，动作服务不得接受客户端
    覆盖这两个安全边界字段。当前写权限同样仅映射 admin。
    """

    # 写动作必须复用同一后端管理员守卫，前端隐藏按钮不能替代角色和租户校验。
    return _require_request_permissions(
        request,
        authorization,
        (
            "billing.anomaly.write",
        ),
    )


def require_provider_health_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户具备 Provider 健康状态读取权限。

    读取与主动探测必须拆分授权，否则只读运维角色会被无关的写操作权限阻断；
    探测接口另由 ``require_provider_health_prober`` 在后端独立校验。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("provider.health.read",))


def require_provider_status_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户同时可读 Provider 健康与请求级调用证据。

    状态页会展示逐 Provider 的真实调用错误和 request_id，因此只具备健康读取权限并不足够。
    两项权限必须在每次请求从数据库快照共同校验，任一项被撤销后旧 Session 也会立即失败关闭。
    """

    # 双权限必须作为同一次实时快照校验，不能让健康权限替代请求级故障证据权限。
    return _require_request_permissions(
        request,
        authorization,
        ("provider.health.read", "request_log.read"),
    )


def require_audit_log_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户具备读取管理审计日志的权限。

    M0.5 尚未提供独立 security 角色，因此安全负责人映射为 admin。developer/viewer 即使绕过
    控制台导航直接调用接口，也会在该后端依赖中返回统一 `auth.permission_denied`。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "audit.read",
        ),
    )


def require_audit_exporter(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可导出当前租户的不可变管理审计事实。

    PRD-059 前没有独立 SecurityAdmin 角色，因此只允许 admin。单独保留该依赖是为了
    后续权限矩阵演进时不扩大普通审计列表权限，也防止前端隐藏按钮被当成授权边界。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "audit.export",
        ),
    )


def require_model_price_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取模型价格时间线。

    价格会暴露企业模型成本口径，当前基础角色中仅 admin/developer 可读；viewer 手写请求也必须由后端拒绝。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "model_price.read",
        ),
    )


def require_model_price_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可创建不可变模型价格计划。

    当前 RBAC 尚未拆出 finance 角色，因此 M0.5 由 admin 承担财务写入职责，developer/viewer 均由后端拒绝。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "model_price.write",
        ),
    )


def require_model_quality_score_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取模型质量与延迟聚合。

    模型成功率、错误率和人工评分会暴露企业内部模型选型依据，因此只开放给
    admin/developer；viewer 直接调用时也必须由后端返回统一权限错误。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "model_quality.read",
        ),
    )


def require_model_quality_score_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可修改模型人工质量评分。

    当前 RBAC 没有独立产品负责人角色，PRD-048 将“产品/管理员”映射为 admin；
    developer 仍可查看统计，但不能通过手写请求改变线上路由质量先验。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "model_quality.write",
        ),
    )


def require_project_admin(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可管理项目、应用和 Virtual Key 归属绑定。

    写入归属会影响预算和审计解释，不能只依赖控制台隐藏按钮，必须在后端限制为 admin。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "project.write",
        ),
    )


def require_environment_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取租户 Environment。

    环境会暴露 Key、别名和预算的配置边界，M0.5 只开放给 admin/developer，viewer 不能通过手写请求绕过导航隐藏。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "environment.read",
        ),
    )


def require_environment_admin(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可创建、启停 Environment。

    环境状态会直接阻断数据面调用并改变预算边界，因此写权限必须在后端限制为 admin。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "environment.write",
        ),
    )


def require_budget_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取预算规则和预算状态。

    预算会暴露当前租户成本阈值和 Key 级治理策略，M0 暂开放给管理员和开发者；viewer 不能通过手写请求绕过页面隐藏。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "billing.budget.read",
        ),
    )


def require_budget_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可修改预算规则。

    PRD-023 的“财务”角色在 M0 尚未独立建模，因此写权限先限定为 admin，避免 developer 私自提高或关闭预算。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "billing.budget.write",
        ),
    )


def require_dlp_admin(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可以管理 DLP 规则。
    DLP 规则会直接影响用户输入是否进入模型上游，M0 尚未拆出 security role，因此只能由 admin
    在后端写入和读取，developer/viewer 即使手写 HTTP 请求也必须被拒绝。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "dlp.read",
            "dlp.write",
        ),
    )


def require_policy_admin(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可以读取、校验和修改租户策略。

    策略会影响后续路由、预算与安全决策。当前 RBAC 尚未拆出安全/财务角色，
    因此 PRD-044 委托既有 admin 守卫，避免复制角色判断后与认证主链路漂移。
    """

    # 直接委托统一管理员依赖，保证认证、租户恢复和固定权限错误完全复用现有边界。
    return _require_request_permissions(
        request,
        authorization,
        (
            "policy.read",
            "policy.write",
            "policy.simulate",
            "policy.publish",
        ),
    )


def require_virtual_key_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取脱敏 Virtual Key 元数据。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("virtual_key.read",))


def require_provider_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取当前租户 Provider 与 KeyPool 脱敏配置。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("provider.read",))


def require_provider_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可创建、更新和测试 Provider 或 KeyPool。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("provider.write",))


def require_model_catalog_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可维护当前租户模型目录。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("model.write",))


def require_routing_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取别名、回退和路由策略。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("routing.read",))


def require_routing_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可维护别名、回退和路由策略。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("routing.write",))


def require_finops_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取正式 FinOps 账本。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("billing.finops.read",))


def require_finops_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可创建财务调整和冲正。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("billing.finops.write",))


def require_provider_health_prober(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可主动触发 Provider 健康探测。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("provider.health.probe",))


def require_dlp_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取脱敏 DLP 规则。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("dlp.read",))


def require_dlp_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可创建和更新 DLP 规则。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("dlp.write",))


def require_intent_risk_classifier(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户具备一次性敏感意图分类权限。

    该入口会处理短生命周期企业正文并持久化安全摘要，因此只能校验独立
    ``intent_risk.classify`` 权限；不得复用 DLP 管理权限或按角色名称推导授权。
    """

    # 每次请求都从当前租户数据库矩阵恢复权限，角色降权无需重新登录即可生效。
    return _require_request_permissions(request, authorization, ("intent_risk.classify",))


def require_session_risk_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取本租户的高危会话报表。

    参数：请求与 Bearer Session Header。返回：数据库实时复核后的 tenant/actor 权限上下文。
    关键约束：本依赖先于 query 解析执行，客户端 tenant_id 和前端导航状态都不参与授权。
    """

    # 每次请求都读取当前租户的实时权限矩阵，避免撤权后旧 Session 继续看到高危会话。
    return _require_request_permissions(request, authorization, ("session_risk.read",))


def require_session_risk_manager(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户同时具备会话风险读取和处置权限。

    ``session_risk.manage`` 不隐含 read；自定义 manage-only 角色必须在正文和幂等 Header
    解析前得到统一 403。服务事务内还会再次复核两个权限，关闭路由通过后撤权竞态。
    """

    # manage 不隐含 read，两个权限必须同时由后端实时矩阵授予，不能依赖前端按钮可见性。
    return _require_request_permissions(
        request,
        authorization,
        ("session_risk.read", "session_risk.manage"),
    )


def require_policy_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取治理策略和版本摘要。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("policy.read",))


def require_policy_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可创建和更新治理策略。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("policy.write",))


def require_policy_simulator(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户具备执行无副作用策略模拟的独立后端权限。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("policy.simulate",))


def require_policy_publisher(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可发布或回滚策略快照。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("policy.publish",))


def require_policy_template_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取内置策略模板目录和租户内影响预览。

    该权限只开放模板目录与服务端规划结果，不能隐式获得 Policy、路由、预算或 DLP
    的原始读取权限；运行时始终使用当前数据库权限快照，避免角色名硬编码造成越权。
    """

    # 模板目录是独立治理资源，只校验专用读权限，不能因用户拥有其他 Policy 权限而旁路放行。
    return _require_request_permissions(request, authorization, ("policy_template.read",))


def require_policy_template_applier(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可提交一次服务端冻结的策略模板应用命令。

    本依赖只完成 HTTP 入口的粗粒度权限门禁。服务层仍必须在 ``BEGIN IMMEDIATE``
    内重新读取权限矩阵并按模板类别复核复合领域权限，防止预览后撤权或跨域提权。
    """

    # 入口先拒绝无模板应用权的请求；租户归属与复合领域权限仍由事务内服务校验负责。
    return _require_request_permissions(request, authorization, ("policy_template.apply",))


def require_permission_matrix_manager(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取和修改租户角色权限矩阵。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("rbac.matrix.manage",))


def require_system_config_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取脱敏运行配置摘要。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("system.config.read",))


def require_config_transfer_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可导出严格白名单租户配置。

    该独立权限不隐含 Provider、Model、路由或 Virtual Key 普通读取权限；后端仍以本次请求
    从数据库加载的矩阵快照为唯一依据，不能按 Admin/Developer/OpsAdmin 角色名推导放行。
    """

    # 导出会暴露租户治理结构，必须逐请求读取数据库权限快照，不能信任 Token 内旧角色声明。
    return _require_request_permissions(request, authorization, ("config.transfer.read",))


def require_config_transfer_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可执行 create-only 原子配置导入。

    写权限包含生成全新 Virtual Key 的高风险副作用，因此必须使用专用 critical 权限；普通
    Provider/Model/路由/Virtual Key 写权限的任意组合都不能替代该后端检查。
    """

    # 导入会原子写入多类租户资源，必须在解析正文前完成专用 critical 权限校验。
    return _require_request_permissions(request, authorization, ("config.transfer.write",))


def require_internal_service_token(
    request: Request,
    internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> None:
    """要求调用方提供控制面配置的内部服务 Token。

    与早期配置摘要接口不同，策略快照会暴露可执行 canonical expression，因此 dev/test
    也不得免认证。比较使用常量时间算法，错误响应和日志均不能泄露配置是否存在或 Token 内容。
    """

    configured = request.app.state.config.internal_config_token
    # 内部服务 Token 缺失时必须在读取任何租户快照前拒绝，且返回统一认证错误。
    if internal_token is None or not internal_token:
        raise ApiError("auth.internal_token_required")
    # 缺少服务端配置与 Token 不匹配共享固定错误，避免未授权调用者探测部署状态。
    expected = configured or ""
    if not expected or not secrets.compare_digest(internal_token, expected):
        raise ApiError("auth.internal_token_invalid")


def require_webhook_manager(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可以管理 Webhook Endpoint。

    Webhook 会把预算、DLP 和失败事件推送到企业系统；M0 尚未拆出 ops/security 角色，
    因此先开放给 admin/developer，viewer 即使手写 HTTP 请求也必须被后端拒绝。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "webhook.manage",
        ),
    )


def require_developer_portal_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取开发者门户内容。

    快速开始会暴露模型别名、Gateway base_url 和 Key 前缀等调用准备信息，M0 只开放给管理员和开发者。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "developer_portal.read",
        ),
    )


def require_debug_console_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可以使用内置 API 调试台。

    调试台会触发真实上游模型调用并写入用量账本，因此只开放给管理员和开发者；viewer 即使前端看不到入口，
    也必须由后端依赖再次拦截，避免通过手写 HTTP 请求绕过控制台按钮隐藏。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "debug_console.use",
        ),
    )


def require_enterprise_chat_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前登录 Session 具有企业 Chat 独立发送权限。

    参数：Control 请求和 Session Bearer Token。返回：从当前租户数据库实时复核的认证上下文。
    关键约束：``chat.use`` 不能由 project/model/debug 权限或角色名推导；撤权后的下一次
    options、建会话、发送和清空必须立即得到统一 403，前端隐藏入口不参与授权结论。
    """

    # 企业 Chat 会产生真实模型调用和成本，必须在正文解析前完成独立后端权限检查。
    return _require_request_permissions(request, authorization, ("chat.use",))


def require_prompt_template_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前登录 Session 具有模板定义和不可变历史读取权限。

    参数：Control 请求与 Bearer Session。返回：实时数据库矩阵复核后的当前租户上下文。
    关键约束：``prompt_template.use`` 只授权 Chat 最小投影和本次渲染结果，不能替代定义读取；
    Viewer 手写管理 API 也必须在解析 path/query/body 之前得到统一 403。
    """

    # 管理定义包含可解密 Prompt，必须逐请求读取实时矩阵，不能由 use 权限或前端入口可见性补权。
    return _require_request_permissions(request, authorization, ("prompt_template.read",))


def require_prompt_template_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前登录 Session 具有模板创建、启停和版本发布权限。

    参数：Control 请求与 Bearer Session。返回：实时权限上下文。关键约束：该 critical 权限
    必须独立于 read/use；业务服务仍需在 ``BEGIN IMMEDIATE`` 内再次复核，关闭鉴权后撤权竞态。
    """

    # 创建、启停和发布会产生不可变版本及审计事实，因此只接受数据库实时 write 授权。
    return _require_request_permissions(request, authorization, ("prompt_template.write",))


def require_prompt_template_catalog_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """允许具有模板 read 或 write 任一权限的用户读取表单最小候选目录。

    参数：Control 请求与 Bearer Session。返回：实时数据库矩阵上下文。该 any-of 只服务
    ``/prompt-templates/options``，不能授予 Project/路由管理权限，也不能按角色名或共享默认值补权。
    """

    context = authenticate_bearer(request.app.state.config, authorization)
    # options 只允许管理侧 read/write 用户，缺权时统一拒绝，避免借候选目录枚举项目或模型。
    if not {"prompt_template.read", "prompt_template.write"}.intersection(
        context.permission_codes
    ):
        # 候选目录也属于受保护资源投影，缺少两项管理权限时不能仅凭登录态访问。
        raise ApiError("auth.permission_denied")
    return context
