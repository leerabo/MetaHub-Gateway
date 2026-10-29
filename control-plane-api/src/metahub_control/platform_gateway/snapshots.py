"""从数据库权威 placement 与 config outbox 发布短期签名制品。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db import connect_database
from metahub_shared.gateway_fleet import (
    create_placement_snapshot,
    encode_placement_snapshot,
)
from metahub_shared.runtime.config import AppConfig


def publish_gateway_snapshot_in_transaction(
    connection,
    config: AppConfig,
    *,
    gateway_cell_event_id: str,
    now: datetime | None = None,
) -> bool:
    """在调用方事务内发布或续签一个当前 placement，保持审计与制品原子可见。"""

    secret = config.gateway_placement_signing_secret
    # 缺少独立签名密钥时不能生成降级制品，调用方仍可按失败关闭策略处理。
    if not secret:
        return False
    row = connection.execute(
        """
        SELECT outbox.gateway_config_outbox_id,outbox.snapshot_generation,
          event.workspace_id,event.tenant_id,cell.gateway_cell_id,cell.cell_code,
          cell.region_code,cell.status cell_status,placement.revision placement_revision,
          placement.residency_policy_version
        FROM gateway_config_outbox outbox
        JOIN gateway_cell_events event
          ON event.gateway_cell_event_id=outbox.gateway_cell_event_id
        JOIN workspace_gateway_placements placement
          ON placement.workspace_id=event.workspace_id
         AND placement.tenant_id=event.tenant_id
         AND placement.primary_gateway_cell_id=outbox.gateway_cell_id
         AND placement.snapshot_generation=outbox.snapshot_generation
        JOIN gateway_cells cell ON cell.gateway_cell_id=outbox.gateway_cell_id
        WHERE outbox.gateway_cell_event_id=?
        """,
        (gateway_cell_event_id,),
    ).fetchone()
    if row is None:
        return False
    current = (now or datetime.now(UTC)).astimezone(UTC)
    snapshot = create_placement_snapshot(
        signing_secret=secret,
        signing_key_version=config.gateway_placement_signing_version,
        tenant_id=str(row["tenant_id"]),
        workspace_id=str(row["workspace_id"]),
        gateway_cell_id=str(row["gateway_cell_id"]),
        cell_code=str(row["cell_code"]),
        region_code=str(row["region_code"]),
        cell_status=str(row["cell_status"]),
        placement_revision=int(row["placement_revision"]),
        residency_policy_version=str(row["residency_policy_version"]),
        generation=int(row["snapshot_generation"]),
        ttl_seconds=config.gateway_placement_snapshot_ttl_seconds,
        now=current,
    )
    payload = encode_placement_snapshot(snapshot)
    payload_sha256 = hashlib.sha256(payload.encode("ascii")).hexdigest()
    changed = connection.execute(
        """
        UPDATE gateway_config_outbox
        SET snapshot_id=?,snapshot_payload=?,snapshot_payload_sha256=?,
          signing_key_version=?,status='completed',attempt_count=attempt_count+1,
          published_at=?,expires_at=?,updated_at=?
        WHERE gateway_config_outbox_id=? AND snapshot_generation=?
        """,
        (
            "gss_" + uuid4().hex,
            payload,
            payload_sha256,
            snapshot.signing_key_version,
            _db_time(snapshot.issued_at),
            _db_time(snapshot.expires_at),
            _db_time(current),
            row["gateway_config_outbox_id"],
            row["snapshot_generation"],
        ),
    ).rowcount
    return changed == 1


def refresh_gateway_snapshot_once(
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> bool:
    """领取一个 pending 或临近过期的当前 placement，日志不暴露制品或 Secret。"""

    # 续签与首次发布使用同一独立密钥边界，缺失时不触碰 outbox 状态。
    if not config.gateway_placement_signing_secret:
        return False
    current = (now or datetime.now(UTC)).astimezone(UTC)
    refresh_before = current + timedelta(
        seconds=max(1, config.gateway_placement_snapshot_ttl_seconds // 2)
    )
    connection = connect_database(config.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT outbox.gateway_cell_event_id
            FROM gateway_config_outbox outbox
            JOIN gateway_cell_events event
              ON event.gateway_cell_event_id=outbox.gateway_cell_event_id
            JOIN workspace_gateway_placements placement
              ON placement.workspace_id=event.workspace_id
             AND placement.tenant_id=event.tenant_id
             AND placement.primary_gateway_cell_id=outbox.gateway_cell_id
             AND placement.snapshot_generation=outbox.snapshot_generation
            WHERE (outbox.status='pending' AND outbox.next_attempt_at<=?)
               OR (outbox.status='completed' AND outbox.expires_at<=?)
            ORDER BY CASE outbox.status WHEN 'pending' THEN 0 ELSE 1 END,
              outbox.updated_at,outbox.gateway_config_outbox_id
            LIMIT 1
            """,
            (_db_time(current), _db_time(refresh_before)),
        ).fetchone()
        if row is None:
            connection.commit()
            return False
        worked = publish_gateway_snapshot_in_transaction(
            connection,
            config,
            gateway_cell_event_id=str(row["gateway_cell_event_id"]),
            now=current,
        )
        connection.commit()
        return worked
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _db_time(value: datetime) -> str:
    """把签名制品事务时间规范化为毫秒精度 UTC 文本，确保跨进程排序一致。"""

    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
