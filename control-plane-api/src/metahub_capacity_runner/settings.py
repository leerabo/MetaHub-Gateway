"""Deployment-owned, closed target allowlist. No request can supply a target or credential."""

from __future__ import annotations

import hashlib
import ipaddress
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .contracts import CapacityPermitClaims, RunnerModel
from .fault_contracts import FaultCode


def private_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        if isinstance(address, ipaddress.IPv6Address):
            if address.scope_id is not None:
                raise ValueError
            address = address.ipv4_mapped or address
        if (
            any(ord(character) < 33 or ord(character) == 127 for character in value)
            or parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port is None
            or not 1 <= parsed.port <= 65535
            or not (address.is_private or address.is_loopback)
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ValueError
    except ValueError:
        raise ValueError("runner.target_invalid") from None
    return value.rstrip("/")


def read_secret(path: Path) -> str:
    try:
        with path.open("rb") as source:
            value = source.read(1025).decode("ascii").strip()
        if not 16 <= len(value) <= 512 or any(character.isspace() for character in value):
            raise ValueError
        if any(ord(character) < 33 or ord(character) > 126 for character in value):
            raise ValueError
        return value
    except (OSError, ValueError, UnicodeError):
        raise ValueError("runner.credentials_unavailable") from None


class SyntheticIdentity(RunnerModel):
    subject_code: str = Field(pattern=r"^synthetic-[a-z0-9][a-z0-9-]{2,63}$")
    credential_file: Path | None = Field(default=None, repr=False)


class CapacityTarget(RunnerModel):
    service_code: Literal["gateway", "control", "status"]
    environment_class: Literal["dev", "staging", "production"]
    region_code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,39}$")
    origin: str = Field(repr=False)
    resource_origin: str = Field(repr=False)
    resource_credential_file: Path = Field(repr=False)
    identities: list[SyntheticIdentity] = Field(min_length=1, max_length=500, repr=False)
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    max_rps: int = Field(default=100, ge=1, le=100_000)
    max_concurrency: int = Field(default=16, ge=1, le=256)
    request_timeout_seconds: int = Field(default=10, ge=1, le=30)

    _origins = field_validator("origin", "resource_origin")(private_origin)

    @model_validator(mode="after")
    def validate_scope(self):
        if len({item.subject_code for item in self.identities}) != len(self.identities):
            raise ValueError("runner.identities_invalid")
        if self.service_code == "gateway" and self.model_name is None:
            raise ValueError("runner.model_required")
        if self.service_code != "status" and any(
            item.credential_file is None for item in self.identities
        ):
            raise ValueError("runner.credentials_unavailable")
        paths = [self.resource_credential_file] + [
            item.credential_file for item in self.identities if item.credential_file is not None
        ]
        if any(not path.is_absolute() for path in paths):
            raise ValueError("runner.credential_path_invalid")
        return self

    def accepts(self, claims: CapacityPermitClaims) -> bool:
        return (self.service_code, self.environment_class, self.region_code) == (
            claims.service_code,
            claims.environment_class,
            claims.region_code,
        )

    def credentials(self, count: int) -> tuple[list[str | None], str]:
        if count > len(self.identities):
            raise ValueError("runner.identities_unavailable")
        keys = [
            read_secret(item.credential_file) if item.credential_file else None
            for item in self.identities[:count]
        ]
        distinct = [key for key in keys if key is not None]
        resource_key = read_secret(self.resource_credential_file)
        if len(set(distinct)) != len(distinct) or resource_key in distinct:
            raise ValueError("runner.identities_invalid")
        return keys, resource_key

    def digest(self) -> str:
        digest = hashlib.sha256(self.model_dump_json().encode())
        keys, resource_key = self.credentials(len(self.identities))
        for key in [*keys, resource_key]:
            digest.update(hashlib.sha256((key or "").encode("ascii")).digest())
        return digest.hexdigest()


class CapacityFaultBinding(RunnerModel):
    service_code: Literal["gateway", "control", "status", "shared"]
    environment_class: Literal["dev", "staging", "production"]
    region_code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,39}$")
    fault_code: FaultCode
    binding_code: str = Field(pattern=r"^synthetic-[a-z0-9][a-z0-9-]{2,63}$")
    origin: str = Field(repr=False)
    internal_token_file: Path = Field(repr=False)
    permit_secret_file: Path = Field(repr=False)
    probe_service_code: Literal["gateway", "control", "status"]
    probe_rps: int = Field(default=5, ge=1, le=100)
    recovery_timeout_seconds: int = Field(default=60, ge=10, le=120)

    _origin = field_validator("origin")(private_origin)

    @model_validator(mode="after")
    def validate_paths(self):
        if not self.internal_token_file.is_absolute() or not self.permit_secret_file.is_absolute():
            raise ValueError("runner.credential_path_invalid")
        return self

    def credentials(self):
        token, secret = read_secret(self.internal_token_file), read_secret(self.permit_secret_file)
        if token == secret:
            raise ValueError("runner.identities_invalid")
        return token, secret

    def accepts(self, claims):
        return (self.service_code, self.environment_class, self.region_code, self.fault_code) == (
            claims.service_code,
            claims.environment_class,
            claims.region_code,
            claims.fault_code,
        )

    def digest(self):
        digest = hashlib.sha256(self.model_dump_json().encode("ascii"))
        for secret in self.credentials():
            digest.update(hashlib.sha256(secret.encode("ascii")).digest())
        return digest.hexdigest()


class CapacityRunnerSettings(RunnerModel):
    state_directory: Path
    targets: list[CapacityTarget] = Field(min_length=1, max_length=100)
    max_queued_runs: int = Field(default=20, ge=1, le=100)
    fault_bindings: list[CapacityFaultBinding] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_targets(self):
        scopes = [
            (item.service_code, item.environment_class, item.region_code) for item in self.targets
        ]
        if len(set(scopes)) != len(scopes) or not self.state_directory.is_absolute():
            raise ValueError("runner.configuration_invalid")
        faults = [
            (item.service_code, item.environment_class, item.region_code, item.fault_code)
            for item in self.fault_bindings
        ]
        if len(set(faults)) != len(faults):
            raise ValueError("runner.configuration_invalid")
        for fault in self.fault_bindings:
            if (fault.probe_service_code, fault.environment_class, fault.region_code) not in scopes:
                raise ValueError("runner.configuration_invalid")
        return self

    def target(self, claims: CapacityPermitClaims) -> CapacityTarget:
        fault = self.fault(claims) if claims.operation == "chaos" else None
        for target in self.targets:
            if (target.service_code, target.environment_class, target.region_code) == (
                fault.probe_service_code if fault else claims.service_code,
                claims.environment_class,
                claims.region_code,
            ):
                if (fault.probe_rps if fault else claims.target_rps) > target.max_rps:
                    raise ValueError("runner.target_capacity_exceeded")
                if fault and (
                    claims.synthetic_tenant_count > fault.probe_rps * claims.duration_seconds
                    or claims.synthetic_tenant_count / fault.probe_rps
                    + target.request_timeout_seconds
                    > 60
                    or claims.synthetic_tenant_count * 3 / fault.probe_rps
                    + target.request_timeout_seconds * 3
                    > fault.recovery_timeout_seconds
                ):
                    raise ValueError("runner.target_capacity_exceeded")
                keys, resource_key = target.credentials(claims.synthetic_tenant_count)
                if fault and any(secret in [*keys, resource_key] for secret in fault.credentials()):
                    raise ValueError("runner.identities_invalid")
                return target
        raise ValueError("runner.target_unavailable")

    def fault(self, claims):
        for binding in self.fault_bindings:
            if binding.accepts(claims):
                return binding
        raise ValueError("runner.fault_adapter_unavailable")

    def execution_target_digest(self, claims):
        target = self.target(claims)
        if claims.operation == "load_test":
            return target.digest()
        return hashlib.sha256(
            (target.digest() + self.fault(claims).digest()).encode("ascii")
        ).hexdigest()


def load_settings(path: Path) -> CapacityRunnerSettings:
    try:
        with path.open("rb") as source:
            payload = source.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError
        return CapacityRunnerSettings.model_validate_json(payload)
    except (OSError, ValueError):
        raise ValueError("runner.configuration_invalid") from None
