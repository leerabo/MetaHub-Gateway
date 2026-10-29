"""Immutable PRD-147 domain, reconciliation, route, and scheduler contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DomainDefinition:
    code: str
    source_migration: str
    target_code: str


@dataclass(frozen=True, slots=True)
class SchedulerDefinition:
    code: str
    installer_name: str | None
    owner_domain: str
    config_condition: str

    @property
    def execution_class(self) -> str:
        if self.owner_domain == "platform_global":
            return "cluster_singleton"
        return "cluster_partitioned"

    @property
    def manifest_digest(self) -> str:
        value = f"{self.code}|{self.owner_domain}|{self.config_condition}|v1"
        return hashlib.sha256(value.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class SchedulerBaselineMapping:
    baseline_installer: str
    final_scheduler_code: str
    mapping_kind: str

    @property
    def mapping_digest(self) -> str:
        value = (
            f"{self.baseline_installer}|{self.final_scheduler_code}|{self.mapping_kind}"
        )
        return hashlib.sha256(value.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class SchedulerQueueDefinition:
    scheduler_code: str
    code: str
    owner_domain: str
    partition_policy_code: str

    @property
    def manifest_digest(self) -> str:
        value = f"{self.code}|{self.owner_domain}|v1"
        return hashlib.sha256(value.encode("ascii")).hexdigest()

    @property
    def partition_code(self) -> str:
        suffix = "global" if self.partition_policy_code == "global" else "owner-hash-v1"
        return f"{self.code}/{suffix}"


DOMAIN_MANIFEST = (
    DomainDefinition("channel_attribution", "0131", "channel"),
    DomainDefinition("customer_success", "0132", "platform_customer_success"),
    DomainDefinition("growth_analytics", "0133", "platform_growth"),
    DomainDefinition("brand_distribution", "0134", "brand_domain"),
    DomainDefinition("provider_engineering", "0135", "platform_provider_engineering"),
    DomainDefinition("commercial_accounting", "0136", "commercial_accounting"),
    DomainDefinition("console_experience", "0137", "console_experience"),
    DomainDefinition("workspace_navigation", "0138", "workspace_navigation"),
)
DOMAIN_BY_CODE = {item.code: item for item in DOMAIN_MANIFEST}

INSTALLER_SCHEDULER_MANIFEST = (
    SchedulerDefinition(
        "status-publication",
        "install_status_publication_scheduler",
        "platform_global",
        "always",
    ),
    SchedulerDefinition(
        "gateway-snapshot",
        "install_gateway_snapshot_scheduler",
        "deployment",
        "gateway_placement_enabled",
    ),
    SchedulerDefinition(
        "admin-assistant",
        "install_admin_assistant_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "provider-health",
        "install_provider_health_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "usage-export",
        "install_usage_export_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "cost-anomaly",
        "install_cost_anomaly_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "platform-customer-success",
        "install_platform_customer_success_scheduler",
        "platform_global",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "customer-support",
        "install_customer_support_scheduler",
        "fixed_split",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "developer-contract",
        "install_developer_contract_scheduler",
        "platform_global",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "privacy",
        "install_privacy_scheduler",
        "global_account",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "compliance",
        "install_compliance_scheduler",
        "fixed_split",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "supply-chain",
        "install_supply_chain_scheduler",
        "platform_global",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "security-assurance",
        "install_security_assurance_scheduler",
        "fixed_split",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "launch-governance",
        "install_launch_governance_scheduler",
        "platform_global",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "request-explanation",
        "install_request_explanation_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "request-log-query-event",
        "install_request_log_query_event_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "platform-provider-engineering",
        "install_platform_provider_engineering_scheduler",
        "fixed_split",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "audit-export",
        "install_audit_export_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "webhook-replay",
        "install_webhook_replay_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "file-security-event",
        "install_file_security_event_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "experiment",
        "install_experiment_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "response-cache-outbox",
        "install_response_cache_outbox_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "platform-growth",
        "install_platform_growth_scheduler",
        "platform_global",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "approval",
        "install_approval_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "commercial-accounting",
        "install_commercial_accounting_scheduler",
        "fixed_split",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "notification",
        "install_notification_scheduler",
        "fixed_split",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "observability",
        "install_observability_scheduler",
        "fixed_split",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "risk",
        "install_risk_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "channel-attribution",
        "install_channel_scheduler",
        "channel",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "brand-domain",
        "install_brand_domain_scheduler",
        "fixed_split",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "security-event",
        "install_security_event_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "mcp",
        "install_mcp_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "tenant-data",
        "install_tenant_data_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "data-governance",
        "install_data_governance_scheduler",
        "workspace_composite",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "edge-support-bundle",
        "install_edge_support_bundle_scheduler",
        "deployment",
        "control_background_schedulers_enabled",
    ),
    SchedulerDefinition(
        "console-experience",
        "install_console_experience_scheduler",
        "fixed_split",
        "control_background_schedulers_enabled",
    ),
)
PLATFORM_JOB_SCHEDULER = SchedulerDefinition(
    "platform-job-runtime",
    None,
    "platform_global",
    "control_background_schedulers_enabled",
)
SCHEDULER_MANIFEST = (*INSTALLER_SCHEDULER_MANIFEST, PLATFORM_JOB_SCHEDULER)
SCHEDULER_BY_CODE = {item.code: item for item in SCHEDULER_MANIFEST}

PRIMARY_SCHEDULER_QUEUE_MANIFEST = tuple(
    SchedulerQueueDefinition(
        definition.code,
        f"{definition.code}.main",
        definition.owner_domain,
        "global"
        if definition.execution_class == "cluster_singleton"
        else "owner_hash_v1",
    )
    for definition in SCHEDULER_MANIFEST
)

CHILD_SCHEDULER_QUEUE_MANIFEST = tuple(
    SchedulerQueueDefinition(*item)
    for item in (
        (
            "platform-customer-success",
            "platform-customer-success.health",
            "platform_global",
            "global",
        ),
        (
            "platform-customer-success",
            "platform-customer-success.plan",
            "platform_global",
            "global",
        ),
        (
            "platform-customer-success",
            "platform-customer-success.renewal",
            "platform_global",
            "global",
        ),
        (
            "platform-provider-engineering",
            "platform-provider-engineering.contract",
            "platform_global",
            "global",
        ),
        (
            "platform-provider-engineering",
            "platform-provider-engineering.error-catalog",
            "platform_global",
            "global",
        ),
        (
            "platform-provider-engineering",
            "platform-provider-engineering.compatibility",
            "workspace_composite",
            "owner_hash_v1",
        ),
        (
            "commercial-accounting",
            "commercial-accounting.plan",
            "platform_global",
            "global",
        ),
        (
            "commercial-accounting",
            "commercial-accounting.subscription",
            "workspace_composite",
            "owner_hash_v1",
        ),
        (
            "commercial-accounting",
            "commercial-accounting.payment",
            "workspace_composite",
            "owner_hash_v1",
        ),
        (
            "commercial-accounting",
            "commercial-accounting.invoice",
            "workspace_composite",
            "owner_hash_v1",
        ),
        (
            "commercial-accounting",
            "commercial-accounting.dispute",
            "fixed_split",
            "owner_hash_v1",
        ),
        (
            "commercial-accounting",
            "commercial-accounting.reconciliation",
            "fixed_split",
            "owner_hash_v1",
        ),
        (
            "console-experience",
            "console-experience.platform-dashboard",
            "platform_global",
            "global",
        ),
        (
            "console-experience",
            "console-experience.workspace-dashboard",
            "workspace_composite",
            "owner_hash_v1",
        ),
        (
            "console-experience",
            "console-experience.platform-approval",
            "platform_global",
            "global",
        ),
        (
            "console-experience",
            "console-experience.workspace-approval",
            "workspace_composite",
            "owner_hash_v1",
        ),
        (
            "console-experience",
            "console-experience.account-preference",
            "global_account",
            "owner_hash_v1",
        ),
        (
            "console-experience",
            "console-experience.deployment-diagnostic",
            "deployment",
            "owner_hash_v1",
        ),
        (
            "console-experience",
            "console-experience.workspace-navigation",
            "workspace_composite",
            "owner_hash_v1",
        ),
        (
            "console-experience",
            "console-experience.role-task-home",
            "workspace_composite",
            "owner_hash_v1",
        ),
        (
            "console-experience",
            "console-experience.advanced-tool",
            "workspace_composite",
            "owner_hash_v1",
        ),
        (
            "console-experience",
            "console-experience.navigation-outbox",
            "workspace_composite",
            "owner_hash_v1",
        ),
        (
            "console-experience",
            "console-experience.navigation-reconciliation",
            "workspace_composite",
            "owner_hash_v1",
        ),
        (
            "platform-job-runtime",
            "platform-job-runtime.jobs",
            "platform_global",
            "global",
        ),
        (
            "channel-attribution",
            "channel-attribution.statements",
            "channel",
            "owner_hash_v1",
        ),
        (
            "brand-domain",
            "brand-domain.verification",
            "fixed_split",
            "owner_hash_v1",
        ),
        (
            "platform-growth",
            "platform-growth.events",
            "platform_global",
            "global",
        ),
    )
)
SCHEDULER_QUEUE_MANIFEST = (
    *PRIMARY_SCHEDULER_QUEUE_MANIFEST,
    *CHILD_SCHEDULER_QUEUE_MANIFEST,
)
SCHEDULER_QUEUES_BY_CODE = {
    definition.code: definition for definition in SCHEDULER_QUEUE_MANIFEST
}
SCHEDULER_QUEUES_BY_SCHEDULER = {
    scheduler.code: tuple(
        queue
        for queue in SCHEDULER_QUEUE_MANIFEST
        if queue.scheduler_code == scheduler.code
    )
    for scheduler in SCHEDULER_MANIFEST
}

BASELINE_SCHEDULER_MAPPINGS = (
    SchedulerBaselineMapping(
        "install_status_publication_scheduler", "status-publication", "same"
    ),
    SchedulerBaselineMapping(
        "install_gateway_snapshot_scheduler", "gateway-snapshot", "same"
    ),
    SchedulerBaselineMapping(
        "install_admin_assistant_scheduler", "admin-assistant", "same"
    ),
    SchedulerBaselineMapping(
        "install_provider_health_scheduler", "provider-health", "same"
    ),
    SchedulerBaselineMapping("install_usage_export_scheduler", "usage-export", "same"),
    SchedulerBaselineMapping("install_cost_anomaly_scheduler", "cost-anomaly", "same"),
    SchedulerBaselineMapping(
        "install_customer_health_scheduler", "platform-customer-success", "replacement"
    ),
    SchedulerBaselineMapping(
        "install_customer_support_scheduler", "customer-support", "same"
    ),
    SchedulerBaselineMapping(
        "install_developer_contract_scheduler", "developer-contract", "same"
    ),
    SchedulerBaselineMapping("install_privacy_scheduler", "privacy", "same"),
    SchedulerBaselineMapping("install_compliance_scheduler", "compliance", "same"),
    SchedulerBaselineMapping(
        "install_supply_chain_scheduler", "supply-chain", "same"
    ),
    SchedulerBaselineMapping(
        "install_security_assurance_scheduler", "security-assurance", "same"
    ),
    SchedulerBaselineMapping(
        "install_launch_governance_scheduler", "launch-governance", "same"
    ),
    SchedulerBaselineMapping(
        "install_request_explanation_scheduler", "request-explanation", "same"
    ),
    SchedulerBaselineMapping(
        "install_request_log_query_event_scheduler", "request-log-query-event", "same"
    ),
    SchedulerBaselineMapping(
        "install_provider_error_map_scheduler", "platform-provider-engineering", "merged"
    ),
    SchedulerBaselineMapping("install_audit_export_scheduler", "audit-export", "same"),
    SchedulerBaselineMapping("install_webhook_replay_scheduler", "webhook-replay", "same"),
    SchedulerBaselineMapping(
        "install_file_security_event_scheduler", "file-security-event", "same"
    ),
    SchedulerBaselineMapping(
        "install_adapter_contract_event_scheduler",
        "platform-provider-engineering",
        "merged",
    ),
    SchedulerBaselineMapping("install_experiment_scheduler", "experiment", "same"),
    SchedulerBaselineMapping(
        "install_response_cache_outbox_scheduler", "response-cache-outbox", "same"
    ),
    SchedulerBaselineMapping(
        "install_product_event_scheduler", "platform-growth", "replacement"
    ),
    SchedulerBaselineMapping("install_approval_scheduler", "approval", "same"),
    SchedulerBaselineMapping(
        "install_plan_billing_scheduler", "commercial-accounting", "merged"
    ),
    SchedulerBaselineMapping(
        "install_saas_billing_scheduler", "commercial-accounting", "merged"
    ),
    SchedulerBaselineMapping(
        "install_platform_billing_scheduler", "commercial-accounting", "merged"
    ),
    SchedulerBaselineMapping(
        "install_payment_commerce_scheduler", "commercial-accounting", "merged"
    ),
    SchedulerBaselineMapping(
        "install_invoice_reconciliation_scheduler", "commercial-accounting", "merged"
    ),
    SchedulerBaselineMapping(
        "install_notification_scheduler", "notification", "same"
    ),
    SchedulerBaselineMapping(
        "install_observability_scheduler", "observability", "same"
    ),
    SchedulerBaselineMapping("install_risk_scheduler", "risk", "same"),
    SchedulerBaselineMapping(
        "install_partner_portal_scheduler", "channel-attribution", "replacement"
    ),
    SchedulerBaselineMapping(
        "install_branding_scheduler", "brand-domain", "replacement"
    ),
    SchedulerBaselineMapping(
        "install_billing_dispute_scheduler", "commercial-accounting", "merged"
    ),
    SchedulerBaselineMapping(
        "install_security_event_scheduler", "security-event", "same"
    ),
    SchedulerBaselineMapping("install_mcp_scheduler", "mcp", "same"),
    SchedulerBaselineMapping("install_tenant_data_scheduler", "tenant-data", "same"),
    SchedulerBaselineMapping(
        "install_data_governance_scheduler", "data-governance", "same"
    ),
    SchedulerBaselineMapping(
        "install_edge_support_bundle_scheduler", "edge-support-bundle", "same"
    ),
)


def configured_installer_definitions(config: object) -> tuple[SchedulerDefinition, ...]:
    return tuple(
        definition
        for definition in INSTALLER_SCHEDULER_MANIFEST
        if definition.config_condition == "always"
        or bool(getattr(config, definition.config_condition, False))
    )

MANDATORY_RECONCILIATION_CHECKS = (
    "source_target_rows",
    "canonical_digest",
    "owner_coverage",
    "foreign_keys",
    "event_outbox",
    "audit_watermark",
    "scheduler_watermark",
    "legacy_write_fence",
    "route_registration_zero",
    "openapi_zero",
    "frontend_runtime_zero",
    "prefetch_zero",
    "permission_retirement",
    "navigation_64_plus_4",
    "cross_domain_negative",
    "target_smoke",
    "n_minus_one_fenced",
    "late_writer_fenced",
    "static_sensitive_zero",
)

# These are the external method/path contracts retired by PRD-147. The surviving
# GET /branding/public host-based endpoint and every /platform/* API are excluded.
LEGACY_ROUTE_MANIFEST = (
    ("POST", "/partners"),
    ("GET", "/partners/profile"),
    ("PATCH", "/partners/branding"),
    ("GET", "/partners/projects"),
    ("GET", "/partners/customers"),
    ("POST", "/partners/customers"),
    ("PATCH", "/partners/customers/{partner_customer_id}"),
    ("GET", "/partners/commission"),
    ("GET", "/partners/delivery-package"),
    ("GET", "/customer-health"),
    ("GET", "/product-analytics/funnel"),
    ("GET", "/branding"),
    ("PATCH", "/branding"),
    ("GET", "/branding/logo"),
    ("GET", "/branding/public/logo"),
    ("GET", "/adapter-specs"),
    ("GET", "/adapter-contract-runs"),
    ("GET", "/adapter-contract-runs/{adapter_contract_run_id}"),
    ("GET", "/error-maps"),
    ("POST", "/error-maps"),
    ("PATCH", "/error-maps/{error_map_id}"),
    ("GET", "/plans"),
    ("PATCH", "/plans"),
    ("GET", "/model-prices"),
    ("POST", "/model-prices"),
    ("GET", "/billing-disputes"),
    ("GET", "/billing-disputes/{billing_dispute_id}"),
    ("POST", "/billing-disputes"),
    ("PATCH", "/billing-disputes/{billing_dispute_id}"),
    ("GET", "/subscriptions/current"),
    ("POST", "/subscriptions/current/change-plan"),
    ("GET", "/dashboard/overview"),
    ("GET", "/dashboard/role-view"),
    ("POST", "/admin-assistant/query"),
    ("GET", "/approvals"),
    ("GET", "/approvals/{approval_id}"),
    ("POST", "/approvals/{approval_id}/step-up"),
    ("POST", "/approvals/{approval_id}/approve"),
    ("POST", "/approvals/{approval_id}/reject"),
    ("GET", "/user/preferences"),
    ("PATCH", "/user/preferences"),
    ("GET", "/internal/config/summary"),
    ("GET", "/console/navigation"),
    ("GET", "/console/preferences"),
    ("PATCH", "/console/preferences"),
    ("POST", "/console/events"),
)


__all__ = [
    "DOMAIN_BY_CODE",
    "DOMAIN_MANIFEST",
    "BASELINE_SCHEDULER_MAPPINGS",
    "CHILD_SCHEDULER_QUEUE_MANIFEST",
    "INSTALLER_SCHEDULER_MANIFEST",
    "LEGACY_ROUTE_MANIFEST",
    "MANDATORY_RECONCILIATION_CHECKS",
    "PLATFORM_JOB_SCHEDULER",
    "PRIMARY_SCHEDULER_QUEUE_MANIFEST",
    "SCHEDULER_BY_CODE",
    "SCHEDULER_MANIFEST",
    "SCHEDULER_QUEUE_MANIFEST",
    "SCHEDULER_QUEUES_BY_CODE",
    "SCHEDULER_QUEUES_BY_SCHEDULER",
    "DomainDefinition",
    "SchedulerBaselineMapping",
    "SchedulerDefinition",
    "SchedulerQueueDefinition",
    "configured_installer_definitions",
]
