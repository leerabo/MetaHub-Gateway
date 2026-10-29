"""Private deployment files, never caller-selected URLs or credentials."""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Literal, Self

from metahub_shared.db import database_backend
from metahub_shared.infrastructure.restore_dispatch import RestoreDispatchModel
from metahub_shared.runtime.config import AppConfig
from pydantic import Field, model_validator


def _read(path: Path, limit: int) -> bytes:
    try:
        if not path.is_absolute():
            raise ValueError
        with path.open("rb") as source:
            value = source.read(limit + 1)
        if not value or len(value) > limit:
            raise ValueError
        return value
    except (OSError, ValueError):
        raise ValueError("restore.configuration_invalid") from None


def read_credential(path: Path, *, minimum: int = 32, maximum: int = 512) -> str:
    try:
        value = _read(path, maximum + 2).decode("ascii").strip()
        if not minimum <= len(value) <= maximum or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError
        return value
    except (UnicodeError, ValueError):
        raise ValueError("restore.credentials_invalid") from None


class RestoreRunnerSettings(RestoreDispatchModel):
    state_directory: Path = Field(repr=False)
    target_code: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,79}$")
    region_code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,39}$")
    environment_class: Literal["dev", "staging", "production"]
    runtime_file: Path = Field(repr=False)
    internal_token_file: Path = Field(repr=False)
    permit_secret_file: Path = Field(repr=False)
    postgres_admin_url_file: Path | None = Field(default=None, repr=False)
    max_pending: int = Field(default=16, ge=1, le=100)

    @model_validator(mode="after")
    def private_paths(self) -> Self:
        paths = [self.state_directory, self.runtime_file, self.internal_token_file, self.permit_secret_file]
        if self.postgres_admin_url_file is not None:
            paths.append(self.postgres_admin_url_file)
        if any(not path.is_absolute() for path in paths):
            raise ValueError("restore.configuration_invalid")
        return self

    def load_runtime(self) -> tuple[AppConfig, str | None]:
        try:
            config = AppConfig.model_validate_json(_read(self.runtime_file, 1024 * 1024))
            expected = {"dev": "dev", "test": "dev", "staging": "staging", "prod": "production"}
            if expected.get(config.environment) != self.environment_class:
                raise ValueError
            if config.environment_info.name != config.environment:
                raise ValueError
            backend = database_backend(config.database_url)
            if backend not in {"sqlite", "postgresql"}:
                raise ValueError
            if backend == "postgresql" and self.postgres_admin_url_file is None:
                raise ValueError
            if self.environment_class == "production" and (backend != "postgresql" or config.kms_provider != "aws"):
                raise ValueError
            admin = (
                read_credential(self.postgres_admin_url_file, minimum=16, maximum=4096)
                if self.postgres_admin_url_file is not None else None
            )
            return config, admin
        except (TypeError, ValueError):
            raise ValueError("restore.configuration_invalid") from None

    def credentials(self) -> tuple[str, str]:
        token, secret = read_credential(self.internal_token_file), read_credential(self.permit_secret_file)
        if hmac.compare_digest(token, secret):
            raise ValueError("restore.credentials_invalid")
        return token, secret


def load_settings(path: Path) -> RestoreRunnerSettings:
    try:
        return RestoreRunnerSettings.model_validate_json(_read(path, 65536))
    except (TypeError, ValueError):
        raise ValueError("restore.configuration_invalid") from None
