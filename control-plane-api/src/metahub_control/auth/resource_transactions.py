"""Opt-in transaction ownership for migrated Console resource writes."""

import sqlite3
from collections.abc import Callable
from dataclasses import replace
from types import TracebackType
from typing import Self

from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .service import (
    AuthContext,
    refresh_transaction_authentication,
    require_permission,
    require_transaction_permission,
)

RESOURCE_PERMISSIONS = frozenset(
    {
        "project.write",
        "environment.write",
        "provider.write",
        "provider.health.read",
        "provider.health.probe",
        "provider_catalog.read",
        "model.write",
        "virtual_key.write",
        "virtual_key.read",
        "project.read",
        "environment.read",
        "model.read",
        "routing.read",
        "routing.write",
        "model_quality.read",
        "model_quality.write",
        "experiment.read",
        "experiment.write",
        "request_log.read",
        "feedback.use",
        "request_explanation.read",
        "audit.read",
        "audit.export",
    }
)


class ResourceWriteTransaction:
    """Own one connection; reauthenticate before reads, each retry and commit."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        config: AppConfig,
        context: AuthContext,
        permission: str,
        *,
        additional_permissions: tuple[str, ...] = (),
        read_only: bool = False,
    ) -> None:
        self.connection = connection
        self.config = config
        self.context = context
        self.permission = permission
        self.additional_permissions = additional_permissions
        self.read_only = read_only

    def __enter__(self) -> Self:
        try:
            self.begin()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            if self.connection.in_transaction:
                self.connection.rollback()
        finally:
            self.connection.close()

    def begin(self) -> None:
        if not self.connection.in_transaction:
            self.connection.execute("BEGIN" if self.read_only else "BEGIN IMMEDIATE")
        self.verify()

    def verify(self) -> AuthContext:
        """Recheck read/replay/no-op exits without committing business facts."""
        self.context = self._authenticate()
        for permission in self.additional_permissions:
            require_permission(self.context, permission)
        return self.context

    def commit(self, committer: Callable[[sqlite3.Connection], None] | None = None) -> None:
        self.verify()
        if committer is not None:
            committer(self.connection)
        else:
            self.connection.commit()

    def _authenticate(self) -> AuthContext:
        if (
            not self.connection.in_transaction
            or self.permission not in RESOURCE_PERMISSIONS
            or any(code not in RESOURCE_PERMISSIONS for code in self.additional_permissions)
        ):
            raise ApiError("infra.service_unavailable")
        context = self.context
        if not isinstance(context, AuthContext):
            raise ApiError("auth.permission_denied")
        if not context.session.session_id.startswith("wss_"):
            if self.permission == "provider_catalog.read":
                from metahub_control.workspace_access.provider_catalog_permissions import (
                    load_verified_provider_catalog_permissions,
                )

                current = refresh_transaction_authentication(
                    self.connection, context, unavailable_error_code="infra.service_unavailable"
                )
                permissions = load_verified_provider_catalog_permissions(
                    self.connection,
                    role_code=current.user.role,
                    unavailable_error_code="infra.service_unavailable",
                )
                return require_permission(
                    replace(current, extension_permission_codes=permissions), self.permission
                )
            return require_transaction_permission(
                self.connection,
                context,
                self.permission,
                unavailable_error_code="infra.service_unavailable",
            )
        from metahub_control.workspace_access.service import (
            authenticate_workspace_session_in_transaction,
        )

        current = authenticate_workspace_session_in_transaction(
            self.connection,
            self.config,
            session_token=context.token,
            read_only=self.read_only,
        ).as_auth_context()
        if (
            current.user.tenant_id != context.user.tenant_id
            or current.tenant.tenant_id != context.tenant.tenant_id
            or current.user.user_id != context.user.user_id
            or current.session.session_id != context.session.session_id
            or current.token_jti != context.token_jti
        ):
            raise ApiError("auth.permission_denied")
        return require_permission(current, self.permission)
