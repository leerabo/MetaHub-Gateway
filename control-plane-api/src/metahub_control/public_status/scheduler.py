"""Keep the signed public status projection available across quiet periods."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .bundle import current_status_bundle_is_fresh, publish_status_bundle

logger = logging.getLogger("metahub.control.public_status.scheduler")


def install_status_publication_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop_event = asyncio.Event()
            task: asyncio.Task[None] | None = None
            config = current_app.state.config
            if _publication_configured(config):
                task = asyncio.create_task(
                    _publication_loop(config, stop_event),
                    name="public-status-publication-scheduler",
                )
            try:
                yield
            finally:
                stop_event.set()
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


def refresh_status_bundle_once(config: AppConfig, *, now: datetime | None = None) -> bool:
    """Publish the initial or expiring bundle; concurrent iterations remain idempotent."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    connection = connect_database(config.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if current_status_bundle_is_fresh(connection, config, now=current):
            connection.rollback()
            return False
        publish_status_bundle(connection, config, operator_id=None, now=current)
        connection.commit()
        return True
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        connection.rollback()
        raise ApiError("status.publication_unavailable") from None
    finally:
        connection.close()


async def _publication_loop(config: AppConfig, stop_event: asyncio.Event) -> None:
    steady_delay = min(300.0, max(30.0, config.status_bundle_ttl_seconds / 6))
    failure_delay = 1.0
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(refresh_status_bundle_once, config)
            delay = steady_delay
            failure_delay = 1.0
        except Exception:
            logger.warning(
                "public status publication failed",
                extra={
                    "event": "status.publication_failed",
                    "error_code": "status.publication_unavailable",
                },
            )
            delay = failure_delay
            failure_delay = min(30.0, failure_delay * 2)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            continue


def _publication_configured(config: AppConfig) -> bool:
    if not config.database_url:
        return False
    if config.kms_provider == "test":
        return bool(config.kms_test_master_key)
    return config.kms_provider == "aws" and bool(config.config_distribution_signing_key_id)


__all__ = ["install_status_publication_scheduler", "refresh_status_bundle_once"]
