"""提供数据库权威 Gateway Cell 投影与 placement CAS 原子写入，边界是服务端恢复全部 owner。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from metahub_shared.db import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)

from .models import (
    GatewayCellListResponse,
    GatewayCellSummary,
    GatewayPlacementResponse,
    GatewayPlacementSummary,
    GatewayPlacementUpdateRequest,
)
from .snapshots import publish_gateway_snapshot_in_transaction

# Workspace 与幂等键格式在开启事务前校验，原始幂等值只用于派生摘要且不得落库。
_WORKSPACE_ID_RE = re.compile(r"^wsp_[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def list_gateway_cells(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> GatewayCellListResponse:
    """在事务内复核 PlatformSession 与读权限后构造固定安全投影。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require(actor, "platform.gateway.read")
        rows = connection.execute(
            """
            SELECT cell_code,region_code,capacity_class,status,health_status,
              capacity_limit,admitted_leases,release_watermark,config_watermark,
              revision,updated_at
            FROM gateway_cells ORDER BY region_code,cell_code
            """
        ).fetchall()
        connection.commit()
        return GatewayCellListResponse(
            cells=tuple(_cell_summary(row) for row in rows),
            request_id=request_id,
            trace_id=trace_id,
        )
    # 已知领域错误保持固定错误码，同时回滚本次权限触碰与读取事务。
    except ApiError:
        _rollback(connection)
        raise
    # 数据库或投影损坏统一折叠为不可用，不泄露 SQL、连接串或行内容。
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_gateway.unavailable") from None
    finally:
        connection.close()


def update_workspace_placement(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload: GatewayPlacementUpdateRequest,
    request_id: str,
    trace_id: str,
) -> GatewayPlacementResponse:
    """复核 owner/驻留/Cell/CAS 后原子写 placement、审计、事件、outbox 与签名制品。"""

    if (
        _WORKSPACE_ID_RE.fullmatch(workspace_id) is None
        or expected_revision < 0
        or _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None
    ):
        # 非规范 owner、revision 或幂等键不能进入写事务和审计链。
        raise ApiError("platform_gateway.request_invalid")
    canonical = {"workspace_id": workspace_id, **payload.model_dump(mode="json")}
    request_sha256 = _digest(canonical)
    idempotency_sha256 = _secret_digest(config, idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require(actor, "platform.gateway.manage")
        owner = connection.execute(
            """
            SELECT binding.tenant_id,state.status operational_status,
              workspace.status workspace_status,binding.status binding_status,
              region.region_code workspace_region,
              region.residency_policy_version workspace_policy_version
            FROM workspace_tenant_bindings binding
            JOIN workspaces workspace ON workspace.workspace_id=binding.workspace_id
            JOIN workspace_operational_states state
              ON state.workspace_id=binding.workspace_id AND state.tenant_id=binding.tenant_id
            JOIN workspace_regions region ON region.workspace_id=binding.workspace_id
            WHERE binding.workspace_id=?
            """,
            (workspace_id,),
        ).fetchone()
        # Workspace owner 必须由不可变绑定恢复，缺失时不区分具体表状态。
        if owner is None:
            raise ApiError("platform_gateway.workspace_not_found")
        # 生命周期、Workspace 与 tenant 绑定必须同时 active，任一关闭都禁止迁移。
        if (
            owner["operational_status"] != "active"
            or owner["workspace_status"] != "active"
            or owner["binding_status"] != "active"
        ):
            # 生命周期任一层失活都视为驻留冲突，避免暴露具体停用层级。
            raise ApiError("platform_gateway.residency_conflict")
        current = connection.execute(
            "SELECT * FROM workspace_gateway_placements WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        # 幂等命中只允许完全相同的规范请求，避免同一键覆盖另一 placement 意图。
        if current is not None and hmac.compare_digest(
            str(current["idempotency_sha256"]), idempotency_sha256
        ):
            # 相同幂等摘要若绑定不同请求摘要，固定返回冲突且不透露差异字段。
            if not hmac.compare_digest(str(current["request_sha256"]), request_sha256):
                raise ApiError("platform_gateway.idempotency_conflict")
            result = _placement_response(
                connection,
                workspace_id,
                request_id=request_id,
                trace_id=trace_id,
                idempotent_replay=True,
            )
            connection.commit()
            return result
        current_revision = int(current["revision"]) if current is not None else 0
        # If-Match 必须精确等于数据库当前 revision，禁止丢失更新。
        if current_revision != expected_revision:
            raise ApiError("platform_gateway.revision_conflict")
        primary = _active_cell(connection, payload.primary_cell_code)
        # Primary Cell 区域与驻留版本都必须等于 Workspace 权威事实，禁止客户端改写。
        if (
            primary["region_code"] != payload.primary_region_code
            or payload.primary_region_code != owner["workspace_region"]
            or payload.residency_policy_version != owner["workspace_policy_version"]
        ):
            # Cell、Workspace 或策略版本不一致时统一失败，禁止旧事实绕过驻留约束。
            raise ApiError("platform_gateway.residency_conflict")
        failover_id = None
        if payload.failover_policy == "explicit":
            failover = _active_cell(connection, payload.failover_cell_code or "")
            # 显式灾备也只能落入部署允许区域，不能因主区故障放宽驻留边界。
            if str(failover["region_code"]) not in set(config.workspace_allowed_regions):
                raise ApiError("platform_gateway.residency_conflict")
            failover_id = str(failover["gateway_cell_id"])
        now = _utcnow()
        revision = current_revision + 1
        if current is None:
            connection.execute(
                """
                INSERT INTO workspace_gateway_placements(
                  workspace_gateway_placement_id,workspace_id,tenant_id,
                  primary_region_code,primary_gateway_cell_id,failover_policy,
                  failover_gateway_cell_id,residency_policy_version,revision,
                  snapshot_generation,updated_by_operator_id,idempotency_sha256,
                  request_sha256,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    _id("wgp_"), workspace_id, owner["tenant_id"],
                    payload.primary_region_code, primary["gateway_cell_id"],
                    payload.failover_policy, failover_id,
                    payload.residency_policy_version, revision, revision,
                    actor.operator_id, idempotency_sha256, request_sha256,
                    _db_time(now), _db_time(now),
                ),
            )
        else:
            changed = connection.execute(
                """
                UPDATE workspace_gateway_placements
                SET primary_region_code=?,primary_gateway_cell_id=?,failover_policy=?,
                  failover_gateway_cell_id=?,residency_policy_version=?,revision=?,
                  snapshot_generation=?,updated_by_operator_id=?,idempotency_sha256=?,
                  request_sha256=?,updated_at=?
                WHERE workspace_id=? AND revision=?
                """,
                (
                    payload.primary_region_code, primary["gateway_cell_id"],
                    payload.failover_policy, failover_id,
                    payload.residency_policy_version, revision, revision,
                    actor.operator_id, idempotency_sha256, request_sha256,
                    _db_time(now), workspace_id, current_revision,
                ),
            ).rowcount
            # CAS UPDATE 未命中表示并发写入已获胜，本事务必须失败关闭并整体回滚。
            if changed != 1:
                raise ApiError("platform_gateway.revision_conflict")
        event_id = _id("gce_")
        connection.execute(
            """
            INSERT INTO gateway_cell_events(
              gateway_cell_event_id,event_type,gateway_cell_id,workspace_id,tenant_id,
              object_id,reason_code,payload_sha256,occurred_at)
            VALUES(?,'placement.changed',?,?,?,?,?,?,?)
            """,
            (
                event_id, primary["gateway_cell_id"], workspace_id, owner["tenant_id"],
                workspace_id, payload.reason_code, request_sha256, _db_time(now),
            ),
        )
        connection.execute(
            """
            UPDATE gateway_config_outbox SET snapshot_generation=?
            WHERE gateway_cell_event_id=?
            """,
            (revision, event_id),
        )
        connection.execute(
            """
            UPDATE gateway_cells SET config_watermark=config_watermark+1,
              revision=revision+1,updated_at=? WHERE gateway_cell_id=?
            """,
            (_db_time(now), primary["gateway_cell_id"]),
        )
        publish_gateway_snapshot_in_transaction(
            connection,
            config,
            gateway_cell_event_id=event_id,
            now=now,
        )
        connection.execute(
            """
            INSERT INTO platform_audit_logs(
              audit_id,actor_domain,actor_id,action_code,object_type,object_id,
              reason_code,request_id,trace_id,before_sha256,after_sha256,occurred_at)
            VALUES(?,'platform',?,'gateway.placement_changed','workspace',?,?,?,?,?,?,?)
            """,
            (
                _id("pau_"), actor.operator_id, workspace_id, payload.reason_code,
                request_id, trace_id,
                _digest({"revision": current_revision}) if current is not None else None,
                request_sha256, _db_time(now),
            ),
        )
        result = _placement_response(
            connection, workspace_id, request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return result
    # 领域错误必须回滚 placement、事件、outbox、签名制品与审计的整个原子集合。
    except ApiError:
        _rollback(connection)
        raise
    # 未知存储或类型错误统一返回固定不可用错误，原异常不得进入响应。
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_gateway.unavailable") from None
    finally:
        connection.close()


def _active_cell(connection, cell_code: str):
    """只返回 active 且 healthy 的精确 Cell；缺失与不可接流使用固定错误。"""

    row = connection.execute(
        "SELECT * FROM gateway_cells WHERE cell_code=?", (cell_code,)
    ).fetchone()
    # 不存在的 Cell 使用固定 404 语义，不回显候选 code 或内部拓扑。
    if row is None:
        raise ApiError("platform_gateway.cell_not_found")
    # draining、unavailable 或非健康 Cell 都不能成为新 placement 目标。
    if row["status"] != "active" or row["health_status"] != "healthy":
        raise ApiError("platform_gateway.residency_conflict")
    # 容量已满时拒绝新 placement，避免控制面写入一个无法承载请求的目标。
    if int(row["admitted_leases"]) >= int(row["capacity_limit"]):
        raise ApiError("platform_gateway.unavailable")
    return row


def _cell_summary(row) -> GatewayCellSummary:
    """把精确使用率压缩为有界容量带，禁止输出容量租户明细。"""

    ratio = int(row["admitted_leases"]) / int(row["capacity_limit"])
    band = "full" if ratio >= 1 else "busy" if ratio >= 0.8 else "available"
    return GatewayCellSummary(
        cell_code=row["cell_code"],
        region_code=row["region_code"],
        capacity_class=row["capacity_class"],
        capacity_band=band,
        status=row["status"],
        health_status=row["health_status"],
        release_watermark=row["release_watermark"],
        config_watermark=row["config_watermark"],
        revision=row["revision"],
        updated_at=_datetime(row["updated_at"]),
    )


def _placement_response(
    connection,
    workspace_id: str,
    *,
    request_id: str,
    trace_id: str,
    idempotent_replay: bool = False,
) -> GatewayPlacementResponse:
    """读取当前 placement 并只返回 Console 可展示的安全字段。"""

    row = connection.execute(
        """
        SELECT placement.*,primary_cell.cell_code primary_cell_code,
          failover_cell.cell_code failover_cell_code
        FROM workspace_gateway_placements placement
        JOIN gateway_cells primary_cell
          ON primary_cell.gateway_cell_id=placement.primary_gateway_cell_id
        LEFT JOIN gateway_cells failover_cell
          ON failover_cell.gateway_cell_id=placement.failover_gateway_cell_id
        WHERE placement.workspace_id=?
        """,
        (workspace_id,),
    ).fetchone()
    # 提交后若读不到当前记录说明事务事实损坏，使用固定 not-found 错误失败关闭。
    if row is None:
        raise ApiError("platform_gateway.workspace_not_found")
    return GatewayPlacementResponse(
        placement=GatewayPlacementSummary(
            workspace_id=row["workspace_id"],
            primary_region_code=row["primary_region_code"],
            primary_cell_code=row["primary_cell_code"],
            failover_policy=row["failover_policy"],
            failover_cell_code=row["failover_cell_code"],
            residency_policy_version=row["residency_policy_version"],
            revision=row["revision"],
            snapshot_generation=row["snapshot_generation"],
            updated_at=_datetime(row["updated_at"]),
        ),
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _require(context: PlatformContext, permission: str) -> None:
    """服务层再次复核实时事务身份的精确权限。"""

    # 服务层基于同一事务中的实时权限再次裁决，不能信任路由依赖的旧上下文。
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")


def _connect(config: AppConfig):
    """数据库连接错误统一映射为平台 Gateway 不可用。"""

    try:
        return connect_database(config.database_url)
    # 连接失败只映射固定错误码，禁止数据库地址或驱动信息离开服务边界。
    except DbError:
        raise ApiError("platform_gateway.unavailable") from None


def _rollback(connection) -> None:
    """异常收口时尽力回滚，不用回滚异常覆盖原领域错误。"""

    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        pass


def _secret_digest(config: AppConfig, value: str) -> str:
    """使用平台 Session 独立域派生幂等摘要，原始键不落库。"""

    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"prd101/placement-idempotency\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _digest(value: object) -> str:
    """对安全规范化对象计算审计摘要，不持久化原始请求正文。"""

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()


def _id(prefix: str) -> str:
    """生成服务端拥有且不可预测的领域标识。"""

    return prefix + uuid4().hex


def _utcnow() -> datetime:
    """集中生成带 UTC 时区的事务时间。"""

    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    """把事务时间规范化为毫秒精度 UTC 文本。"""

    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _datetime(value: object) -> datetime:
    """兼容 SQLite/PostgreSQL 行类型并返回带时区时间。"""

    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
