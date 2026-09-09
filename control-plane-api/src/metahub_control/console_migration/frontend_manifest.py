"""Versioned contract for the Console sources shipped after PRD-147."""

from __future__ import annotations

import hashlib
import json

FRONTEND_PRODUCTION_SURFACE_MANIFEST = {
    "version": 1,
    "html_entrypoints": (
        "index.html",
        "console.html",
        "identity.html",
        "privacy.html",
        "partner.html",
        "platform.html",
        "status.html",
    ),
    "entrypoints": (
        "src/app/main.js",
        "src/features/public-home/main.js",
        "src/features/identity/main.js",
        "src/features/partner/main.js",
        "src/features/platform/main.js",
        "src/features/public-status/main.js",
    ),
    "style_entrypoints": (
        "src/styles/brand-domains.css",
        "src/styles/console-experience.css",
        "src/styles/console-migration.css",
        "src/styles/edge-operations.css",
        "src/styles/identity.css",
        "src/styles/identity-access.css",
        "src/styles/public-home.css",
        "src/styles/main.css",
        "src/styles/partner.css",
        "src/styles/platform.css",
        "src/styles/status.css",
    ),
    "replacement_modules": (
        "src/features/workspace-commercial-accounting/",
        "src/features/workspace-console-experience/",
        "src/features/workspace-navigation/",
        "src/features/workspace-provider-engineering/",
        "src/features/workspace-quota/",
        "src/lib/workspaceCommercialAccounting.js",
        "src/lib/workspaceConsoleExperience.js",
        "src/lib/workspaceNavigation.js",
        "src/lib/workspaceQuota.js",
        "src/lib/providerEngineering.js",
    ),
    "retired_source_ids": (
        "adapter-contract-runs",
        "adapter-specs",
        "competitor-comparison",
        "customer-health",
        "growth-analytics",
        "partner-portal",
    ),
    "retired_runtime_modules": (
        "src/features/adapter-contract-runs/",
        "src/features/adapter-specs/",
        "src/features/admin-assistant/",
        "src/features/approvals/",
        "src/features/billing-disputes/",
        "src/features/competitor-comparison/",
        "src/features/customer-health/",
        "src/features/dashboard/",
        "src/features/error-maps/",
        "src/features/model-prices/",
        "src/features/partner-portal/",
        "src/features/plan-billing/",
        "src/features/product-analytics/",
        "src/lib/adapterContractRuns.js",
        "src/lib/adapterSpecs.js",
        "src/lib/adminAssistant.js",
        "src/lib/approvals.js",
        "src/lib/billingDisputes.js",
        "src/lib/configSummary.js",
        "src/lib/customerHealth.js",
        "src/lib/dashboard.js",
        "src/lib/errorMaps.js",
        "src/lib/modelPrices.js",
        "src/lib/partnerPortal.js",
        "src/lib/planBilling.js",
        "src/lib/productAnalytics.js",
        "src/lib/saasBilling.js",
        "src/lib/userPreferences.js",
    ),
    "retired_runtime_literals": (
        "data-plan-management-root",
        "data-saas-subscription",
        "identity-subscription",
        "identity-payment-commerce",
        "data-payment-intent-form",
        "data-refund-request-form",
        "refresh-payment-commerce",
        "loadAdminAssistantState",
        "loadApprovalsState",
        "loadBillingDisputesState",
        "loadModelPricesState",
        "loadPartnerPortalState",
        "loadPlanManagementState",
        "plan-billing-",
        "plans-panel",
        "product-analytics-",
    ),
    "retired_external_routes": (
        "/adapter-contract-runs",
        "/adapter-specs",
        "/admin-assistant/query",
        "/approvals",
        "/billing-disputes",
        "/branding",
        "/branding/logo",
        "/branding/public/logo",
        "/console/events",
        "/console/navigation",
        "/console/preferences",
        "/customer-health",
        "/dashboard",
        "/error-maps",
        "/internal/config/summary",
        "/model-prices",
        "/partners",
        "/plans",
        "/product-analytics/funnel",
        "/subscriptions/current",
        "/user/preferences",
    ),
    "production_dockerfile": "../infra/docker/console.Dockerfile",
    "production_asset_set_sha256": (
        "964db3aa0dc56dd10f1fc79ec3be41362c5fa0f092b99f41f4317159c9938143"
    ),
    "output_directory": "dist",
}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


FRONTEND_PRODUCTION_SURFACE_MANIFEST_DIGEST = hashlib.sha256(
    _canonical_json(FRONTEND_PRODUCTION_SURFACE_MANIFEST).encode("ascii")
).hexdigest()
FRONTEND_PRODUCTION_ASSET_SET_DIGEST = str(
    FRONTEND_PRODUCTION_SURFACE_MANIFEST["production_asset_set_sha256"]
)


__all__ = [
    "FRONTEND_PRODUCTION_SURFACE_MANIFEST",
    "FRONTEND_PRODUCTION_SURFACE_MANIFEST_DIGEST",
    "FRONTEND_PRODUCTION_ASSET_SET_DIGEST",
]
