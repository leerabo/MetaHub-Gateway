"""Closed deployment bindings and short-lived commands for dependency fault proxies."""

from __future__ import annotations

import hashlib
import ipaddress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import Field, field_validator, model_validator

from .contracts import CapacityPermitClaims, RunnerModel
from .engine import validate_capacity_scope

FaultCode = Literal[
    "dependency-timeout",
    "redis-unavailable",
    "kms-unavailable",
    "object-store-unavailable",
    "cell-isolation",
    "region-isolation",
]


def private_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address):
            if address.scope_id is not None:
                raise ValueError
            address = address.ipv4_mapped or address
        if (
            not (address.is_private or address.is_loopback)
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ValueError
        return str(address)
    except ValueError:
        raise ValueError("fault.address_invalid") from None


class FaultBinding(RunnerModel):
    binding_code: str = Field(pattern=r"^synthetic-[a-z0-9][a-z0-9-]{2,63}$")
    service_code: Literal["gateway", "control", "status", "shared"]
    environment_class: Literal["dev", "staging", "production"]
    region_code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,39}$")
    fault_codes: list[FaultCode] = Field(min_length=1, max_length=6)
    listen_host: str = Field(repr=False)
    listen_port: int = Field(ge=1, le=65535)
    upstream_host: str = Field(repr=False)
    upstream_port: int = Field(ge=1, le=65535)
    max_connections: int = Field(default=64, ge=1, le=512)

    _hosts = field_validator("listen_host", "upstream_host")(private_host)

    def accepts(self, execution: CapacityPermitClaims) -> bool:
        return (
            execution.operation == "chaos"
            and execution.fault_code in self.fault_codes
            and (execution.service_code, execution.environment_class, execution.region_code)
            == (self.service_code, self.environment_class, self.region_code)
        )

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode("ascii")).hexdigest()


class FaultProxySettings(RunnerModel):
    state_directory: Path
    internal_token_file: Path = Field(repr=False)
    permit_secret_file: Path = Field(repr=False)
    bindings: list[FaultBinding] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_bindings(self):
        listeners = {(item.listen_host, item.listen_port) for item in self.bindings}
        if (
            len(listeners) != len(self.bindings)
            or len({item.binding_code for item in self.bindings}) != len(self.bindings)
            or any((item.upstream_host, item.upstream_port) in listeners for item in self.bindings)
            or any(
                not path.is_absolute()
                for path in (
                    self.state_directory,
                    self.internal_token_file,
                    self.permit_secret_file,
                )
            )
        ):
            raise ValueError("fault.configuration_invalid")
        return self

    def binding(self, code: str, execution: CapacityPermitClaims) -> FaultBinding:
        for binding in self.bindings:
            if binding.binding_code == code and binding.accepts(execution):
                return binding
        raise ValueError("fault.binding_unavailable")


class FaultLeaseCommand(RunnerModel):
    execution: CapacityPermitClaims
    binding_code: str = Field(pattern=r"^synthetic-[a-z0-9][a-z0-9-]{2,63}$")
    action: Literal["apply", "renew", "release", "status"]
    sequence: int = Field(ge=1, le=2_147_483_647)
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_lease(self):
        if (
            self.execution.operation != "chaos"
            or self.issued_at.utcoffset() is None
            or self.expires_at.utcoffset() is None
            or not timedelta(0) < self.expires_at - self.issued_at <= timedelta(seconds=30)
        ):
            raise ValueError("fault.command_invalid")
        try:
            validate_capacity_scope(self.execution)
        except HTTPException:
            raise ValueError("fault.manifest_invalid") from None
        return self


class FaultLeaseInfo(RunnerModel):
    protocol_version: Literal[3]
    object_id: str = Field(pattern=r"^che_[0-9a-f]{32}$")
    execution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_code: str
    state: Literal["active", "released", "expired", "interrupted"]
    sequence: int = Field(ge=1, le=2_147_483_647)
    lease_until: datetime
    end_at: datetime
    reset_connections: int = Field(ge=0)
    withheld_bytes: int = Field(ge=0)
    active_connections: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_times(self):
        if (
            self.lease_until.utcoffset() is None
            or self.end_at.utcoffset() is None
            or self.lease_until > self.end_at
        ):
            raise ValueError("fault.lease_time_invalid")
        return self
