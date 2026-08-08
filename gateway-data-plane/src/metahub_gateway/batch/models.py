"""Strict in-process identities for trusted Enterprise Chat and Batch calls."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from metahub_shared.batch import BatchContractError, canonical_batch_price
from metahub_shared.runtime.routing_policy import TokenEstimate

from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.session_risk import SessionRiskRequestIdentity

_OWNER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MONEY_QUANT = Decimal("0.000001")


def _owner_id(value: object) -> str:
    if type(value) is not str or _OWNER_ID.fullmatch(value) is None:
        raise ValueError("trusted_gateway_invocation_invalid")
    return value


def _money(value: object) -> str:
    if type(value) is not str:
        raise ValueError("trusted_gateway_invocation_invalid")
    try:
        amount = Decimal(value)
        normalized = amount.quantize(_MONEY_QUANT)
    except (InvalidOperation, ValueError):
        raise ValueError("trusted_gateway_invocation_invalid") from None
    if not amount.is_finite() or amount < 0 or amount != normalized:
        raise ValueError("trusted_gateway_invocation_invalid")
    return format(normalized, ".6f")


@dataclass(frozen=True, slots=True)
class BatchRouteCandidateCoverage:
    """One frozen candidate and its versioned CNY prices."""

    role: Literal["default", "fallback"]
    model_id: str
    provider_id: str
    input_price_per_1k: str
    output_price_per_1k: str
    price_source: Literal["price_plan", "model_catalog"]
    price_version: str

    def __post_init__(self) -> None:
        if self.role not in {"default", "fallback"} or self.price_source not in {
            "price_plan",
            "model_catalog",
        }:
            raise ValueError("trusted_gateway_invocation_invalid")
        _owner_id(self.model_id)
        _owner_id(self.provider_id)
        _owner_id(self.price_version)
        try:
            input_price = canonical_batch_price(self.input_price_per_1k)
            output_price = canonical_batch_price(self.output_price_per_1k)
        except BatchContractError:
            raise ValueError("trusted_gateway_invocation_invalid") from None
        object.__setattr__(self, "input_price_per_1k", input_price)
        object.__setattr__(self, "output_price_per_1k", output_price)


@dataclass(frozen=True, slots=True)
class BatchRouteCoverage:
    """Frozen ordered default/fallback candidates covered by one Batch attempt."""

    candidates: tuple[BatchRouteCandidateCoverage, ...]

    def __post_init__(self) -> None:
        if (
            type(self.candidates) is not tuple
            or not 1 <= len(self.candidates) <= 2
            or any(not isinstance(item, BatchRouteCandidateCoverage) for item in self.candidates)
            or self.candidates[0].role != "default"
            or (len(self.candidates) == 2 and self.candidates[1].role != "fallback")
            or len({item.role for item in self.candidates}) != len(self.candidates)
            or len({item.model_id for item in self.candidates}) != len(self.candidates)
        ):
            raise ValueError("trusted_gateway_invocation_invalid")

    @property
    def default(self) -> BatchRouteCandidateCoverage:
        return self.candidates[0]

    @property
    def fallback(self) -> BatchRouteCandidateCoverage | None:
        return self.candidates[1] if len(self.candidates) == 2 else None


@dataclass(frozen=True, slots=True)
class BatchAllocationCoverage:
    """One tenant-qualified budget allocation recovered from the consumed grant."""

    batch_budget_allocation_id: str
    budget_rule_id: str
    budget_date: str
    reserved_cost_cny: str

    def __post_init__(self) -> None:
        _owner_id(self.batch_budget_allocation_id)
        _owner_id(self.budget_rule_id)
        if (
            type(self.budget_date) is not str
            or len(self.budget_date) != 10
            or self.budget_date[4] != "-"
            or self.budget_date[7] != "-"
        ):
            raise ValueError("trusted_gateway_invocation_invalid")
        object.__setattr__(self, "reserved_cost_cny", _money(self.reserved_cost_cny))


@dataclass(frozen=True, slots=True)
class TrustedBatchContext:
    """Batch-only owner graph, route coverage, and settlement instructions."""

    tenant_id: str
    batch_job_id: str
    batch_task_id: str
    batch_attempt_id: str
    request_id: str
    trace_id: str
    route_plan_hash: str
    route: BatchRouteCoverage
    token_estimate: TokenEstimate
    allocations: tuple[BatchAllocationCoverage, ...]

    def __post_init__(self) -> None:
        for value in (
            self.tenant_id,
            self.batch_job_id,
            self.batch_task_id,
            self.batch_attempt_id,
            self.request_id,
            self.trace_id,
        ):
            _owner_id(value)
        if type(self.route_plan_hash) is not str or _SHA256.fullmatch(self.route_plan_hash) is None:
            raise ValueError("trusted_gateway_invocation_invalid")
        if (
            not isinstance(self.route, BatchRouteCoverage)
            or not isinstance(self.token_estimate, TokenEstimate)
            or type(self.token_estimate.input_tokens) is not int
            or self.token_estimate.input_tokens < 1
            or type(self.token_estimate.output_tokens) is not int
            or self.token_estimate.output_tokens < 0
            or self.token_estimate.truncated is not False
            or type(self.allocations) is not tuple
        ):
            raise ValueError("trusted_gateway_invocation_invalid")
        if any(not isinstance(item, BatchAllocationCoverage) for item in self.allocations):
            raise ValueError("trusted_gateway_invocation_invalid")
        allocation_ids = [item.batch_budget_allocation_id for item in self.allocations]
        rule_ids = [item.budget_rule_id for item in self.allocations]
        if len(allocation_ids) != len(set(allocation_ids)) or len(rule_ids) != len(set(rule_ids)):
            raise ValueError("trusted_gateway_invocation_invalid")


@dataclass(frozen=True, slots=True, init=False)
class TrustedGatewayInvocation:
    """A complete trusted call; sources cannot mix identity, payload, or Batch facts."""

    source: Literal["enterprise_chat", "batch"]
    auth_context: GatewayAuthContext
    _payload: dict[str, Any] = field(repr=False)
    session_risk_candidates: tuple[SessionRiskRequestIdentity, ...] = ()
    batch: TrustedBatchContext | None = None

    def __init__(
        self,
        *,
        source: Literal["enterprise_chat", "batch"],
        auth_context: GatewayAuthContext,
        payload: dict[str, Any],
        session_risk_candidates: tuple[SessionRiskRequestIdentity, ...] = (),
        batch: TrustedBatchContext | None = None,
    ) -> None:
        if type(payload) is not dict:
            raise ValueError("trusted_gateway_invocation_invalid")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "auth_context", auth_context)
        object.__setattr__(self, "_payload", copy.deepcopy(payload))
        object.__setattr__(self, "session_risk_candidates", session_risk_candidates)
        object.__setattr__(self, "batch", batch)
        self.__post_init__()

    @property
    def payload(self) -> dict[str, Any]:
        """Return an isolated copy of the validated request payload."""

        return copy.deepcopy(self._payload)

    def __post_init__(self) -> None:
        if self.source not in {"enterprise_chat", "batch"}:
            raise ValueError("trusted_gateway_invocation_invalid")
        if not isinstance(self.auth_context, GatewayAuthContext):
            raise ValueError("trusted_gateway_invocation_invalid")
        if type(self.session_risk_candidates) is not tuple or any(
            not isinstance(item, SessionRiskRequestIdentity)
            for item in self.session_risk_candidates
        ):
            raise ValueError("trusted_gateway_invocation_invalid")
        if self.source == "enterprise_chat":
            if self.batch is not None:
                raise ValueError("trusted_gateway_invocation_invalid")
        else:
            if (
                not isinstance(self.batch, TrustedBatchContext)
                or self.session_risk_candidates
                or self.batch.tenant_id != self.auth_context.tenant_id
                or self._payload.get("stream") is not False
            ):
                raise ValueError("trusted_gateway_invocation_invalid")
