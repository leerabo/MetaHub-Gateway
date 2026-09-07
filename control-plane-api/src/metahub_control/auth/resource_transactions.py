"""Opt-in transaction ownership for migrated Console resource writes."""

import sqlite3
from collections.abc import Callable
from types import TracebackType
from typing import Self

from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .service import AuthContext, require_permission, require_transaction_permission

RESOURCE_PERMISSIONS = frozenset(
    {
        "project.write",
        "environment.write",
        "provider.write",
        "model.write",
        "virtual_key.write",
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
    ) -> None:
        self.connection = connection
        self.config = config
        self.context = context
        self.permission = permission

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
            self.connection.execute("BEGIN IMMEDIATE")
        self.context = self._authenticate()

    def commit(self, committer: Callable[[sqlite3.Connection], None] | None = None) -> None:
        self.context = self._authenticate()
        if committer is not None:
            committer(self.connection)
        else:
            self.connection.commit()

    def _authenticate(self) -> AuthContext:
        if not self.connection.in_transaction or self.permission not in RESOURCE_PERMISSIONS:
            raise ApiError("infra.service_unavailable")
        context = self.context
        if not isinstance(context, AuthContext):
            raise ApiError("auth.permission_denied")
        if not context.session.session_id.startswith("wss_"):
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
