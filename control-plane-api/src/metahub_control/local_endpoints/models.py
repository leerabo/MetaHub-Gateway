"""Closed request and response contracts for PAGE-116 local model Endpoints."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, SecretStr


class LocalInitialModelRequest(BaseModel):
    internal_model_id: str | None = None
    provider_model_id: str | None = None
    display_name: str | None = None
    capabilities: dict[str, object] | None = None
    context_window: int | None = None
    input_price_per_1k: Decimal | None = None
    output_price_per_1k: Decimal | None = None
    cache_price_per_1k: Decimal | None = None
    currency: str | None = None

    model_config = ConfigDict(extra="forbid")


class CreateLocalEndpointRequest(BaseModel):
    provider_code: str | None = None
    name: str | None = None
    engine_kind: str | None = None
    base_url: str | None = None
    auth_type: str | None = None
    bearer_token: SecretStr | None = None
    initial_model: LocalInitialModelRequest | None = None

    model_config = ConfigDict(extra="forbid")


class TestLocalEndpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalInitialModelInfo(BaseModel):
    model_id: str
    internal_model_id: str
    provider_model_id: str
    display_name: str
    capabilities: dict[str, bool]
    context_window: int
    input_price_per_1k: float
    output_price_per_1k: float
    cache_price_per_1k: float = 0.0
    currency: str
    status: str


class LocalEndpointInfo(BaseModel):
    local_endpoint_id: str
    tenant_id: str
    provider_id: str
    provider_code: str
    name: str
    engine_kind: str
    endpoint_hint: str
    auth_type: str
    status: str
    health_status: str
    last_tested_at: datetime | None
    revision: int
    catalog_revision: int
    catalog_status: str
    last_discovered_at: datetime | None
    discovered_model_count: int
    credential_configured: bool
    initial_model: LocalInitialModelInfo


class LocalEndpointListResponse(BaseModel):
    local_endpoints: list[LocalEndpointInfo]
    request_id: str
    trace_id: str


class LocalEndpointCreateResponse(BaseModel):
    local_endpoint: LocalEndpointInfo
    created: bool
    request_id: str
    trace_id: str


class LocalEndpointTestResult(BaseModel):
    status: str
    http_status: int | None
    latency_ms: int | None
    error_code: str | None
    discovered_model_count: int
    catalog_truncated: bool
    checked_at: datetime


class LocalEndpointTestResponse(BaseModel):
    local_endpoint: LocalEndpointInfo
    test_result: LocalEndpointTestResult
    request_id: str
    trace_id: str
