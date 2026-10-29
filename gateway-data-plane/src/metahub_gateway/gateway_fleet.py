"""把签名 placement 制品失败关闭地同步到 Gateway 进程内存。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from metahub_shared.db import connect_database
from metahub_shared.gateway_fleet import (
    decode_placement_snapshot,
    install_placement_snapshot,
    remove_placement_snapshot,
    verify_placement_snapshot,
)
from metahub_shared.runtime.config import AppConfig

logger = logging.getLogger("metahub.gateway.gateway_fleet")


def sync_gateway_placements_once(
    config: AppConfig,
    app_state: Any,
    *,
    now: datetime | None = None,
) -> int:
    """验证每个 Workspace 的全局最新制品，并为当前 Cell 安装或撤销本地版本。"""

    if not config.gateway_placement_enabled:
        return 0
    # 启用态必须同时拥有显式本地 Cell 与独立签名密钥，否则不安装任何制品。
    if not config.gateway_cell_code or not config.gateway_placement_signing_secret:
        return 0
    current = (now or datetime.now(UTC)).astimezone(UTC)
    connection = connect_database(config.database_url)
    try:
        rows = connection.execute(
            """
            SELECT ranked.gateway_cell_id,ranked.cell_code,ranked.workspace_id,ranked.tenant_id,
              ranked.snapshot_payload,ranked.snapshot_payload_sha256,
              ranked.signing_key_version
            FROM (
              SELECT outbox.gateway_cell_id,cell.cell_code,event.workspace_id,event.tenant_id,
                outbox.snapshot_payload,outbox.snapshot_payload_sha256,
                outbox.signing_key_version,outbox.snapshot_generation,outbox.published_at,
                ROW_NUMBER() OVER(
                  PARTITION BY event.workspace_id
                  ORDER BY outbox.snapshot_generation DESC,outbox.published_at DESC
                ) AS placement_rank
              FROM gateway_config_outbox outbox
              JOIN gateway_cell_events event
                ON event.gateway_cell_event_id=outbox.gateway_cell_event_id
              JOIN gateway_cells cell ON cell.gateway_cell_id=outbox.gateway_cell_id
              WHERE outbox.status='completed' AND outbox.expires_at>?
            ) ranked
            WHERE ranked.placement_rank=1
            ORDER BY ranked.workspace_id
            """,
            (_db_time(current),),
        ).fetchall()
    finally:
        connection.close()
    installed = 0
    for row in rows:
        try:
            payload = str(row["snapshot_payload"])
            payload_hash = hashlib.sha256(payload.encode("ascii")).hexdigest()
            # 持久载荷哈希必须先于解析和验签匹配，损坏行不能替换内存 LKG。
            if not hmac.compare_digest(payload_hash, str(row["snapshot_payload_sha256"])):
                raise ValueError("gateway placement snapshot hash mismatch")
            snapshot = decode_placement_snapshot(payload)
            # 解码后的 owner、Cell 与 key version 必须和数据库权威连接行完全一致。
            if (
                snapshot.gateway_cell_id != str(row["gateway_cell_id"])
                or snapshot.workspace_id != str(row["workspace_id"])
                or snapshot.tenant_id != str(row["tenant_id"])
                or snapshot.cell_code != str(row["cell_code"])
                or snapshot.signing_key_version != int(row["signing_key_version"])
            ):
                # 任一持久 owner 字段不匹配都拒绝安装，避免跨租户或跨 Cell 快照串用。
                raise ValueError("gateway placement snapshot durable owner mismatch")
            verify_placement_snapshot(
                snapshot,
                signing_secret=config.gateway_placement_signing_secret,
                signing_key_version=config.gateway_placement_signing_version,
                tenant_id=snapshot.tenant_id,
                workspace_id=snapshot.workspace_id,
                now=current,
            )
            if snapshot.cell_code == config.gateway_cell_code:
                install_placement_snapshot(app_state, snapshot)
                installed += 1
            elif remove_placement_snapshot(
                app_state,
                tenant_id=snapshot.tenant_id,
                workspace_id=snapshot.workspace_id,
                generation=snapshot.generation,
            ):
                installed += 1
        # 过期续签、错误 owner 或损坏制品均被隔离，绝不能替换内存中的最后已知良好版本。
        except ValueError:
            continue
    return installed


def install_gateway_placement_sync(app: FastAPI) -> None:
    """把本地制品轮询器组合进 Gateway lifespan，关闭开关时不创建任务。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """原生命周期就绪后启动同步，停机时等待当前只读数据库轮次结束。"""

        async with original_lifespan(current_app):
            stop_event = asyncio.Event()
            task: asyncio.Task[None] | None = None
            if current_app.state.config.gateway_placement_enabled:
                task = asyncio.create_task(
                    _sync_loop(current_app, stop_event),
                    name="gateway-placement-snapshot-sync",
                )
            try:
                yield
            finally:
                stop_event.set()
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _sync_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    """按有界间隔验签同步；异常只记录固定事件和错误码并保留 LKG。"""

    config = app.state.config
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(sync_gateway_placements_once, config, app.state)
        except Exception:
            logger.warning(
                "gateway placement synchronization failed",
                extra={
                    "event": "gateway_config.sync_failed",
                    "error_code": "gateway.placement_unavailable",
                },
            )
        delay = min(2.0, max(0.2, config.gateway_placement_snapshot_ttl_seconds / 3))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        # 正常轮询超时进入下一次验签同步，不记录为错误或暴露制品信息。
        except TimeoutError:
            continue


def _db_time(value: datetime) -> str:
    """把 Gateway 查询水位时间规范化为毫秒精度 UTC 文本，避免本地时区参与比较。"""

    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = ["install_gateway_placement_sync", "sync_gateway_placements_once"]
