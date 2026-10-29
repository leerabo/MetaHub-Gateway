"""PRD-065 角色首页固定视图目录。

该目录是服务端产品上限，不是授权来源。角色配置只能从目录中选择候选视图，最终可见性还必须与每次请求从数据库恢复的
permission 快照求交，禁止按角色名或前端按钮状态补权。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RoleDashboardViewCode = Literal["executive", "finance", "security", "developer", "operations"]
RoleDashboardCardCode = Literal[
    "cost",
    "security",
    "stability",
    "adoption",
    "provider_health",
    "budget_risk",
]


@dataclass(frozen=True)
class RoleDashboardViewDefinition:
    """一项不可变角色首页视图定义。

    required_permissions 使用全部命中语义；card_order 同时承担稳定响应顺序，前端不得再依据角色重排卡片。
    """

    code: RoleDashboardViewCode
    label: str
    required_permissions: tuple[str, ...]
    card_order: tuple[RoleDashboardCardCode, ...]


# 五个视图的顺序同时是 admin 分段控件的稳定顺序，修改时必须同步迁移 canonical JSON 和专项测试。
ROLE_DASHBOARD_VIEWS: tuple[RoleDashboardViewDefinition, ...] = (
    RoleDashboardViewDefinition(
        code="executive",
        label="经营总览",
        required_permissions=("dashboard.read",),
        card_order=("cost", "stability", "adoption", "provider_health", "budget_risk", "security"),
    ),
    RoleDashboardViewDefinition(
        code="finance",
        label="财务视图",
        required_permissions=(
            "dashboard.read",
            "billing.usage.read",
            "billing.budget.read",
            "billing.anomaly.read",
        ),
        card_order=("cost", "budget_risk", "adoption", "stability", "provider_health", "security"),
    ),
    RoleDashboardViewDefinition(
        code="security",
        label="安全视图",
        required_permissions=("dashboard.read", "dlp.read", "policy.read", "audit.read"),
        card_order=("security", "stability", "provider_health", "budget_risk", "adoption", "cost"),
    ),
    RoleDashboardViewDefinition(
        code="developer",
        label="开发视图",
        required_permissions=("dashboard.read", "developer_portal.read", "request_log.read"),
        card_order=("adoption", "stability", "provider_health", "security", "cost", "budget_risk"),
    ),
    RoleDashboardViewDefinition(
        code="operations",
        label="运维视图",
        required_permissions=("dashboard.read", "provider.health.read", "request_log.read"),
        card_order=("provider_health", "stability", "budget_risk", "adoption", "security", "cost"),
    ),
)

# 通过精确映射查找定义，避免服务层接受大小写变体、别名或客户端自由文本。
ROLE_DASHBOARD_VIEW_BY_CODE = {definition.code: definition for definition in ROLE_DASHBOARD_VIEWS}
ROLE_DASHBOARD_VIEW_CODES = frozenset(ROLE_DASHBOARD_VIEW_BY_CODE)

# 卡片跳转需要额外能力；后端先按实时权限裁剪，前端还必须与本次后端导航快照再次求交。
CARD_NAVIGATION_REQUIREMENTS: dict[RoleDashboardCardCode, tuple[str, tuple[str, ...]]] = {
    "cost": ("usage", ("billing.usage.read",)),
    "security": ("dlp-rules", ("dlp.read",)),
    "stability": ("request-logs", ("request_log.read",)),
    "adoption": ("models", ("model.read",)),
    "provider_health": ("provider-health", ("provider.health.read", "request_log.read")),
    "budget_risk": ("budgets", ("billing.budget.read",)),
}


def available_view_codes(
    configured_codes: tuple[RoleDashboardViewCode, ...],
    permission_codes: tuple[str, ...],
) -> tuple[RoleDashboardViewCode, ...]:
    """按配置顺序返回当前请求真正可见的视图。

    参数：configured_codes 是数据库已严格校验的候选目录；permission_codes 是认证阶段读取的实时数据库快照。
    返回：只包含全部 required_permissions 均命中的视图，角色名本身不参与授权。
    """

    granted = frozenset(permission_codes)
    return tuple(
        code
        for code in configured_codes
        if all(
            permission in granted
            for permission in ROLE_DASHBOARD_VIEW_BY_CODE[code].required_permissions
        )
    )


def card_navigation_target(
    card_code: RoleDashboardCardCode,
    permission_codes: tuple[str, ...],
) -> str | None:
    """在实时权限满足目标能力时返回固定导航 ID。

    这里只完成后端权限门禁，不能替代前端对后端导航响应的 owner 复核；双门禁可避免权限撤销后的旧导航产生可点击入口。
    """

    target, requirements = CARD_NAVIGATION_REQUIREMENTS[card_code]
    granted = frozenset(permission_codes)
    # 卡片数字可见不代表明细页获权；只有目标能力全部命中才返回后端固定导航 ID。
    return target if all(permission in granted for permission in requirements) else None
