"""Frozen PRD-146 navigation, task-home, and advanced-tool contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContractSpec:
    source_id: str
    disposition: str | None
    placement: str
    route_code: str
    group_code: str | None
    permissions: tuple[str, ...]
    order: int
    source_kind: str = "excel"

    @property
    def label_key(self) -> str:
        return f"navigation.{self.source_id}.label"

    @property
    def description_key(self) -> str:
        return f"navigation.{self.source_id}.description"


@dataclass(frozen=True, slots=True)
class AdvancedToolSpec:
    tool_code: str
    route_code: str
    permissions: tuple[str, ...]
    order: int
    requires_deployment: bool = False


@dataclass(frozen=True, slots=True)
class RoleTaskSpec:
    task_code: str
    roles: tuple[str, ...]
    permissions: tuple[str, ...]
    action_route_code: str
    source_code: str
    priority: int
    writable: bool = True


GROUP_CODES = (
    "home",
    "workspace",
    "gateway",
    "finance",
    "governance",
    "applications",
    "developer-center",
    # 面向组织用户的支持与反馈入口放在导航末尾，避免与治理数据混在一起。
    "system",
)

HUMAN_ROLE_CODES = (
    "owner",
    "admin",
    "developer",
    "security_admin",
    "finance_admin",
    "legal_admin",
    "auditor",
    "viewer",
)

RETIRED_SOURCE_IDS = frozenset(
    {
        "growth-analytics",
        "customer-health",
        "partner-portal",
        "adapter-specs",
        "adapter-contract-runs",
        "competitor-comparison",
        "environments",
        "tenant-data-lifecycle",
    }
)

APPROVED_EXTRA_IDS = frozenset(
    {
        "migration-operations",
        "support-tickets",
        "workspace-subscription",
        "internal-finops",
    }
)

MISSING_SOURCE_IDS_0137 = frozenset(
    {
        "growth-analytics",
        "branding",
        "adapter-specs",
        "adapter-contract-runs",
        "error-maps",
    }
)

RESTORED_SOURCE_IDS = frozenset(
    {"providers", "local-models", "provider-health", "error-maps", "billing-disputes"}
)


def _spec(
    source_id: str,
    disposition: str | None,
    placement: str,
    route_code: str,
    group_code: str | None,
    permissions: tuple[str, ...],
    order: int,
    source_kind: str = "excel",
) -> ContractSpec:
    return ContractSpec(
        source_id,
        disposition,
        placement,
        route_code,
        group_code,
        permissions,
        order,
        source_kind,
    )


# The first 64 rows are the Excel disposition catalog in workbook order. The four
# final rows are approved source additions and cannot make up for a missing Excel row.
CONTRACT_SPECS: tuple[ContractSpec, ...] = (
    _spec("overview", "D01", "primary", "workspace.home.overview", "home", ("dashboard.read",), 10),
    _spec(
        "admin-assistant",
        "D01",
        "primary",
        "workspace.home.assistant",
        "home",
        ("admin_assistant.query",),
        11,
    ),
    _spec(
        "approvals", "D01", "primary", "workspace.home.approvals", "home", ("approval.read",), 12
    ),
    _spec(
        "growth-analytics",
        "D02",
        "retired_internal",
        "workspace.retired.growth-analytics",
        None,
        (),
        13,
    ),
    _spec(
        "customer-health",
        "D02",
        "retired_internal",
        "workspace.retired.customer-health",
        None,
        (),
        14,
    ),
    _spec(
        "partner-portal",
        "D02",
        "retired_internal",
        "workspace.retired.partner-portal",
        None,
        (),
        15,
    ),
    _spec(
        "branding",
        "D03",
        "detail",
        "workspace.deployment.branding",
        "workspace",
        ("deployment.branding.read",),
        19,
    ),
    _spec(
        "tenant",
        "D04",
        "primary",
        "workspace.settings.profile",
        "workspace",
        ("tenant.settings.read",),
        20,
    ),
    _spec(
        "users", "D04", "primary", "workspace.settings.members", "workspace", ("user.manage",), 30
    ),
    _spec(
        "role-permissions",
        "D05",
        "primary",
        "workspace.settings.roles",
        "workspace",
        ("rbac.matrix.manage",),
        32,
    ),
    _spec(
        "environments",
        "D06",
        # 租户运行环境由 SaaS 平台运维配置；Workspace 用户只消费已配置的路由和模型。
        # ``platform_internal`` is represented by the established internal-only
        # catalog disposition until the persisted contract enum is versioned.
        "retired_internal",
        "workspace.settings.deployments",
        "workspace",
        ("environment.read",),
        35,
    ),
    _spec(
        "providers",
        "D05",
        "primary",
        "workspace.gateway.providers",
        "gateway",
        ("provider.read",),
        40,
    ),
    _spec(
        "local-models",
        "D06",
        "primary",
        "workspace.gateway.private-endpoints",
        "gateway",
        ("local_endpoint.read",),
        41,
    ),
    _spec(
        "private-gateways",
        "D06",
        "primary",
        "workspace.gateway.private-deployments",
        "gateway",
        ("edge.deployment.read",),
        42,
    ),
    _spec(
        "adapter-specs", "D02", "retired_internal", "workspace.retired.adapter-specs", None, (), 43
    ),
    _spec(
        "adapter-contract-runs",
        "D02",
        "retired_internal",
        "workspace.retired.adapter-contract-runs",
        None,
        (),
        44,
    ),
    _spec(
        "provider-health",
        "D01",
        "primary",
        "workspace.gateway.provider-health",
        "gateway",
        ("provider.health.read", "request_log.read"),
        45,
    ),
    _spec(
        "error-maps",
        "D07",
        "advanced",
        "workspace.advanced.provider-errors",
        None,
        ("workspace.provider_error_override.read",),
        47,
    ),
    _spec("models", "D05", "primary", "workspace.gateway.models", "gateway", ("model.read",), 50),
    _spec(
        "model-aliases",
        "D05",
        "primary",
        "workspace.gateway.aliases",
        "gateway",
        ("routing.read",),
        60,
    ),
    _spec(
        "routing-policies",
        "D05",
        "detail",
        "workspace.gateway.routing-policies",
        "gateway",
        ("routing.read",),
        61,
    ),
    _spec(
        "model-quality-scores",
        "D05",
        "primary",
        "workspace.gateway.quality",
        "gateway",
        ("model_quality.read",),
        62,
    ),
    _spec(
        "experiments",
        "D05",
        "primary",
        "workspace.gateway.evaluations",
        "gateway",
        ("experiment.read",),
        63,
    ),
    _spec(
        "projects", "D05", "primary", "workspace.gateway.projects", "workspace", ("project.read",), 65
    ),
    _spec(
        "virtual-keys",
        "D05",
        "primary",
        "workspace.gateway.virtual-keys",
        "gateway",
        ("virtual_key.read",),
        70,
    ),
    _spec(
        "usage", "D08", "primary", "workspace.finance.usage", "finance", ("billing.usage.read",), 80
    ),
    _spec(
        "finops-ledger",
        "D09",
        "replaced",
        "workspace.finance.internal-finops",
        None,
        ("billing.finops.read",),
        81,
    ),
    _spec(
        "model-prices",
        "D01",
        "detail",
        "workspace.finance.provider-costs",
        "finance",
        ("workspace.provider_cost.read",),
        82,
    ),
    _spec(
        "response-cache",
        "D10",
        "advanced",
        "workspace.advanced.response-cache",
        None,
        ("response_cache.read",),
        83,
    ),
    _spec(
        "plans",
        "D11",
        "replaced",
        "workspace.finance.subscription",
        None,
        ("billing.plan.read",),
        84,
    ),
    _spec(
        "quota-buckets",
        "D12",
        "primary",
        "workspace.finance.quota",
        "finance",
        ("billing.quota.read",),
        85,
    ),
    _spec(
        "billing-disputes",
        "D01",
        "primary",
        "workspace.finance.disputes",
        "finance",
        ("workspace.commercial.subscription.read",),
        86,
    ),
    _spec(
        "budgets",
        "D05",
        "primary",
        "workspace.finance.budgets",
        "finance",
        ("billing.budget.read",),
        87,
    ),
    _spec(
        "cost-anomalies",
        "D13",
        "primary",
        "workspace.finance.anomalies",
        "finance",
        ("billing.anomaly.read",),
        88,
    ),
    _spec(
        "policies",
        "D05",
        "primary",
        "workspace.governance.policies",
        "governance",
        ("policy.read",),
        90,
    ),
    _spec(
        "policy-simulator",
        "D14",
        "advanced",
        "workspace.advanced.policy-simulator",
        None,
        ("policy.simulate",),
        91,
    ),
    _spec(
        "policy-snapshots",
        "D05",
        "primary",
        "workspace.governance.policy-releases",
        "governance",
        ("policy.read",),
        92,
    ),
    _spec(
        "policy-templates",
        "D01",
        "primary",
        "workspace.governance.policy-templates",
        "governance",
        ("policy_template.read",),
        93,
    ),
    _spec(
        "dlp-rules", "D05", "primary", "workspace.governance.dlp", "governance", ("dlp.read",), 94
    ),
    _spec(
        "intent-security",
        "D15",
        "advanced",
        "workspace.advanced.intent-security",
        None,
        ("intent_risk.classify",),
        95,
    ),
    _spec(
        "session-risks",
        "D05",
        "primary",
        "workspace.governance.session-risks",
        "governance",
        ("session_risk.read",),
        96,
    ),
    _spec(
        "output-safety",
        "D05",
        "primary",
        "workspace.governance.output-safety",
        "governance",
        ("request_log.read",),
        97,
    ),
    _spec(
        "webhooks",
        "D05",
        "primary",
        "workspace.governance.webhooks",
        "governance",
        ("webhook.manage",),
        98,
    ),
    _spec(
        "webhook-dead-letters",
        "D15",
        "advanced",
        "workspace.advanced.webhook-dead-letters",
        None,
        ("webhook.manage",),
        99,
    ),
    _spec(
        "audit-logs",
        "D16",
        "primary",
        "workspace.governance.audit",
        "governance",
        ("audit.read",),
        100,
    ),
    _spec(
        "request-logs",
        "D05",
        "primary",
        "workspace.observability.requests",
        "governance",
        ("request_log.read",),
        105,
    ),
    _spec(
        "request-explanations",
        "D17",
        "detail",
        "workspace.observability.request-explanation",
        "governance",
        ("request_explanation.read",),
        106,
    ),
    _spec(
        "feedback-summary",
        "D05",
        "primary",
        "workspace.observability.feedback",
        "system",
        ("feedback.use",),
        107,
    ),
    _spec(
        "quickstart",
        "D18",
        "task_home",
        "workspace.home.getting-started",
        "home",
        ("workspace.task_home.read",),
        108,
    ),
    _spec(
        "sdk-examples",
        "D19",
        "developer_center",
        "workspace.developer.sdk",
        "developer-center",
        ("developer_portal.read",),
        110,
    ),
    _spec(
        "migration-guide",
        "D19",
        "developer_center",
        "workspace.developer.migration",
        "developer-center",
        ("developer_portal.read",),
        111,
    ),
    _spec(
        "api-docs",
        "D19",
        "developer_center",
        "workspace.developer.api",
        "developer-center",
        ("console.basic.read",),
        112,
    ),
    _spec(
        "competitor-comparison",
        "D20",
        "retired_internal",
        "workspace.retired.competitor-comparison",
        None,
        (),
        113,
    ),
    _spec(
        "enterprise-chat",
        "D05",
        "primary",
        "workspace.applications.chat",
        "applications",
        ("chat.use",),
        115,
    ),
    _spec(
        "prompt-templates",
        "D05",
        "primary",
        "workspace.applications.prompts",
        "applications",
        ("prompt_template.read",),
        116,
    ),
    _spec(
        "files",
        "D05",
        "primary",
        "workspace.applications.files",
        "applications",
        ("file.read",),
        117,
    ),
    _spec(
        "file-security",
        "D21",
        "detail",
        "workspace.applications.file-security",
        "applications",
        ("file.read",),
        118,
    ),
    _spec(
        "batch-jobs",
        "D05",
        "primary",
        "workspace.applications.batch",
        "applications",
        ("batch.read",),
        119,
    ),
    _spec(
        "mcp-tools",
        "D19",
        "developer_center",
        "workspace.developer.mcp",
        "developer-center",
        ("mcp.read",),
        120,
    ),
    _spec(
        "tenant-data-lifecycle",
        "D04",
        # Persist platform-owned pages with the established internal-only
        # disposition; the database contract does not accept a new placement
        # enum value.
        "retired_internal",
        "workspace.settings.data-management",
        "workspace",
        # Tenant data lifecycle is operated by the SaaS platform. It remains
        # registered for backend ownership and migration compatibility, but is
        # excluded from the Workspace user navigation projection.
        ("tenant_data.read",),
        121,
    ),
    _spec(
        "debug-console",
        "D15",
        "advanced",
        "workspace.advanced.debug-console",
        None,
        ("debug_console.use",),
        122,
    ),
    _spec(
        "system-status",
        "D01",
        "primary",
        "workspace.observability.status",
        "system",
        ("console.basic.read",),
        130,
    ),
    _spec(
        "language-settings",
        "D22",
        "account",
        "identity.preferences",
        None,
        ("console.basic.read",),
        132,
    ),
    _spec(
        "config-summary",
        "D07",
        "advanced",
        "workspace.advanced.deployment-summary",
        None,
        ("workspace.deployment_diagnostic.read",),
        140,
    ),
    _spec(
        "migration-operations",
        None,
        "primary",
        "workspace.observability.migrations",
        "system",
        ("migration.read",),
        46,
        "extra",
    ),
    _spec(
        "support-tickets",
        None,
        "primary",
        "workspace.observability.support",
        "system",
        ("support.ticket.read",),
        48,
        "extra",
    ),
    _spec(
        "workspace-subscription",
        None,
        "primary",
        "workspace.finance.subscription",
        "finance",
        ("workspace.commercial.subscription.read",),
        79,
        "extra",
    ),
    _spec(
        "internal-finops",
        None,
        "primary",
        "workspace.finance.internal-finops",
        "finance",
        ("workspace.internal_finops.read", "workspace.provider_cost.read"),
        81,
        "extra",
    ),
)

ADVANCED_TOOL_SPECS: tuple[AdvancedToolSpec, ...] = (
    AdvancedToolSpec(
        "provider_error_overrides",
        "workspace.advanced.provider-errors",
        ("workspace.provider_error_override.read",),
        10,
    ),
    AdvancedToolSpec(
        "response_cache", "workspace.advanced.response-cache", ("response_cache.read",), 20
    ),
    AdvancedToolSpec(
        "policy_simulator", "workspace.advanced.policy-simulator", ("policy.simulate",), 30
    ),
    AdvancedToolSpec(
        "intent_security", "workspace.advanced.intent-security", ("intent_risk.classify",), 40
    ),
    AdvancedToolSpec(
        "webhook_dead_letters", "workspace.advanced.webhook-dead-letters", ("webhook.manage",), 50
    ),
    AdvancedToolSpec(
        "debug_console", "workspace.advanced.debug-console", ("debug_console.use",), 60
    ),
    AdvancedToolSpec(
        "deployment_summary",
        "workspace.advanced.deployment-summary",
        ("workspace.deployment_diagnostic.read",),
        70,
        True,
    ),
)

ROLE_TASK_SPECS: tuple[RoleTaskSpec, ...] = (
    RoleTaskSpec(
        "workspace_profile",
        ("owner", "admin"),
        ("tenant.settings.read",),
        "workspace.settings.profile",
        "workspace_profile",
        10,
    ),
    RoleTaskSpec(
        "member_responsibilities",
        ("owner", "admin"),
        ("user.manage",),
        "workspace.settings.members",
        "members",
        20,
    ),
    RoleTaskSpec(
        "subscription_ready",
        ("owner", "finance_admin"),
        ("workspace.commercial.subscription.read",),
        "workspace.finance.subscription",
        "subscription",
        25,
    ),
    RoleTaskSpec(
        "provider_connected",
        # Provider credentials are organization configuration. Developers use
        # the already-published Gateway and never complete this task.
        ("owner", "admin"),
        ("provider.read",),
        "workspace.gateway.providers",
        "providers",
        30,
    ),
    RoleTaskSpec(
        "model_catalog_ready",
        # Model publication and routing belong to the organization admin.
        ("owner", "admin"),
        ("model.read",),
        "workspace.gateway.models",
        "models",
        40,
    ),
    RoleTaskSpec(
        "project_key_ready",
        # Personal keys are created by the member who will use them; this is
        # not an organization owner/admin setup task.
        ("developer",),
        ("virtual_key.read",),
        "workspace.gateway.virtual-keys",
        "member_key_access",
        50,
    ),
    RoleTaskSpec(
        "first_gateway_call",
        ("owner", "admin", "developer"),
        ("developer_portal.read",),
        "workspace.developer.sdk",
        "usage",
        60,
    ),
    RoleTaskSpec(
        "security_policy_ready",
        ("owner", "admin", "security_admin"),
        ("policy.read",),
        "workspace.governance.policies",
        "policies",
        30,
    ),
    RoleTaskSpec(
        "dlp_ready", ("security_admin",), ("dlp.read",), "workspace.governance.dlp", "dlp", 40
    ),
    RoleTaskSpec(
        "audit_coverage",
        ("security_admin", "legal_admin", "auditor"),
        ("audit.read",),
        "workspace.governance.audit",
        "audit",
        50,
        False,
    ),
    RoleTaskSpec(
        "cost_controls",
        ("finance_admin",),
        ("billing.budget.read",),
        "workspace.finance.budgets",
        "budgets",
        40,
    ),
    RoleTaskSpec(
        "finops_ready",
        ("finance_admin",),
        ("workspace.internal_finops.read",),
        "workspace.finance.internal-finops",
        "finops",
        50,
    ),
    RoleTaskSpec(
        "service_visibility",
        ("viewer", "auditor"),
        ("console.basic.read",),
        "workspace.observability.status",
        "status",
        10,
        False,
    ),
)


def validate_catalog() -> None:
    source_ids = [item.source_id for item in CONTRACT_SPECS]
    if len(CONTRACT_SPECS) != 68 or len(source_ids) != len(set(source_ids)):
        raise RuntimeError("workspace navigation contract catalog is not unique")
    excel = [item for item in CONTRACT_SPECS if item.source_kind == "excel"]
    extras = {item.source_id for item in CONTRACT_SPECS if item.source_kind == "extra"}
    if len(excel) != 64 or extras != APPROVED_EXTRA_IDS:
        raise RuntimeError("workspace navigation disposition coverage is invalid")
    retired = {item.source_id for item in CONTRACT_SPECS if item.placement == "retired_internal"}
    if retired != RETIRED_SOURCE_IDS:
        raise RuntimeError("workspace navigation retirement contract is invalid")
    if any(item.group_code not in GROUP_CODES for item in CONTRACT_SPECS if item.group_code):
        raise RuntimeError("workspace navigation group is invalid")


LEGACY_SOURCE_IDS_0137 = tuple(
    item.source_id for item in CONTRACT_SPECS if item.source_id not in MISSING_SOURCE_IDS_0137
)


validate_catalog()

__all__ = (
    "ADVANCED_TOOL_SPECS",
    "APPROVED_EXTRA_IDS",
    "CONTRACT_SPECS",
    "GROUP_CODES",
    "HUMAN_ROLE_CODES",
    "LEGACY_SOURCE_IDS_0137",
    "MISSING_SOURCE_IDS_0137",
    "RESTORED_SOURCE_IDS",
    "RETIRED_SOURCE_IDS",
    "ROLE_TASK_SPECS",
    "AdvancedToolSpec",
    "ContractSpec",
    "RoleTaskSpec",
)
