"""Reuse closed SQLite handles without sharing Gateway request transactions."""

from metahub_shared.db.connection import database_connection_pool_scope
from starlette.types import ASGIApp, Receive, Scope, Send


class GatewayDatabasePoolMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Scope spans streamed bodies and background callbacks; each close still ends ownership.
        with database_connection_pool_scope():
            await self.app(scope, receive, send)
