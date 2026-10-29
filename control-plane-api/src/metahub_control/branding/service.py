"""PRD-088 BrandingConfig 事务服务、租户隔离和 EVT-100 source 事实。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import BRANDING_UPDATED_EVENT_TYPE, build_webhook_event

from metahub_control.audit import AuditRequestMeta, commit_audited_transaction, record_audit_log
from metahub_control.auth import AuthContext, require_transaction_permission

from .models import (
    BrandingInfo,
    BrandingPatchRequest,
    BrandingResponse,
    PublicBrandingInfo,
    PublicBrandingResponse,
    validate_custom_domain,
)
from .png import MAX_LOGO_BYTES, validate_png

_BRANDING_ID = re.compile(r"^brc_[0-9a-f]{32}$")
_EVENT_ID = re.compile(r"^evt_brd_[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT = {
    "product_name": "MetaHub",
    "primary_color": "#176B87",
    "secondary_color": "#F59E0B",
    "login_title": "企业 AI Gateway",
    "login_message": "安全、可追踪的企业模型网关",
}


@dataclass(frozen=True, slots=True)
class BrandingLogo:
    """受保护 Logo 下载结果；bytes 不参与 repr、日志或通用响应。"""

    body: bytes
    etag: str
    request_id: str
    trace_id: str


def get_branding(
    config: AppConfig, context: AuthContext, *, request_id: str, trace_id: str
) -> BrandingResponse:
    """按认证 tenant 读取唯一 active BrandingConfig。"""

    connection = _connect(config)
    try:
        row = _load_row(connection, context.user.tenant_id)
        return _response(row, request_id, trace_id)
    except ApiError:
        raise
    except (sqlite3.Error, TypeError, ValueError):
        raise ApiError("branding.unavailable") from None
    finally:
        connection.close()


def update_branding(
    config: AppConfig,
    context: AuthContext,
    payload: BrandingPatchRequest,
    *,
    revision: int,
    request_id: str,
    trace_id: str,
    audit_request_meta: AuditRequestMeta,
    now: datetime | None = None,
) -> BrandingResponse:
    """在单一事务内 CAS 更新配置、审计、EVT-100 source 和 outbox。"""

    connection = _connect(config)
    tenant_id = context.user.tenant_id
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 依赖层校验后仍在写事务内复核，防止动态撤权与排队写入之间形成窗口。
        require_transaction_permission(
            connection,
            context,
            "branding.read",
            unavailable_error_code="branding.unavailable",
        )
        require_transaction_permission(
            connection,
            context,
            "branding.manage",
            unavailable_error_code="branding.unavailable",
        )
        before = _load_row(connection, tenant_id)
        current_revision = _integer(before["revision"], minimum=1)
        if current_revision != revision:
            raise ApiError("branding.revision_conflict")
        updates = _parse_updates(payload, before)
        if not updates:
            raise ApiError("branding.request_invalid")
        if updates.get("custom_domain") is not None:
            conflict = connection.execute(
                "SELECT 1 FROM branding_configs WHERE custom_domain=? AND tenant_id!=? LIMIT 1",
                (updates["custom_domain"], tenant_id),
            ).fetchone()
            if conflict is not None:
                raise ApiError("branding.custom_domain_conflict")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        new_revision = current_revision + 1
        values = {
            **updates,
            "revision": new_revision,
            "updated_by_user_id": context.user.user_id,
            "updated_request_id": request_id,
            "updated_trace_id": trace_id,
            "updated_at": now_text,
        }
        assignments = ",".join(f"{name}=?" for name in values)
        connection.execute(
            f"UPDATE branding_configs SET {assignments} WHERE tenant_id=? AND revision=?",
            (*values.values(), tenant_id, revision),
        )
        if _count_changes(connection) != 1:
            raise ApiError("branding.revision_conflict")
        after = _load_row(connection, tenant_id)
        changed = tuple(
            sorted(
                {
                    _public_field_name(name)
                    for name in updates
                    if _different(before[name], after[name])
                }
            )
        )
        if not changed:
            raise ApiError("branding.request_invalid")
        event_id = _event_id(after["branding_config_id"], new_revision)
        event = _build_event(after, changed, context, request_id, trace_id, current)
        payload_json = _canonical_json(event.payload)
        # source event 与 original delivery 快照同事务冻结；空订阅也必须留下可复核 marker。
        from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot

        freeze_webhook_event_delivery_snapshot(connection, event, frozen_at=now_text)
        snapshot = [
            row[0]
            for row in connection.execute(
                """
                SELECT webhook_delivery_id FROM webhook_deliveries
                WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
                ORDER BY webhook_delivery_id
                """,
                (tenant_id, event.event_id),
            ).fetchall()
        ]
        # 审计摘要只保留不可逆字段哈希和尺寸，绝不复制 Logo/文案/域名原文。
        audit_summary = _audit_summary(after, changed)
        audit_record = record_audit_log(
            connection,
            context,
            action="branding.updated",
            object_type="branding_config",
            object_id=after["branding_config_id"],
            object_name=None,
            before_summary=_audit_summary(before, ()),
            after_summary=audit_summary,
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
            created_at=current,
        )
        if audit_record is None:
            raise ApiError("branding.event_unavailable")
        connection.execute(
            """
            INSERT INTO branding_events(
              branding_event_id,tenant_id,branding_config_id,audit_log_id,event_type,revision,
              actor_user_id,request_id,trace_id,changed_fields_json,payload_json,
              payload_sha256,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _branding_event_id(event_id),
                tenant_id,
                after["branding_config_id"],
                audit_record.audit_log_id,
                BRANDING_UPDATED_EVENT_TYPE,
                new_revision,
                context.user.user_id,
                request_id,
                trace_id,
                _canonical_json(list(changed)),
                payload_json,
                _sha256(payload_json.encode("utf-8")),
                now_text,
            ),
        )
        connection.execute(
            """
            INSERT INTO branding_event_outbox(
              branding_event_outbox_id,tenant_id,branding_event_id,webhook_event_id,
              payload_json,payload_sha256,status,attempt_count,generation,next_attempt_at,
              subscription_snapshot_json,snapshot_frozen_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?, 'pending',0,0,?,?,?, ?,?)
            """,
            (
                _outbox_id(event_id),
                tenant_id,
                _branding_event_id(event_id),
                event.event_id,
                payload_json,
                _sha256(payload_json.encode("utf-8")),
                now_text,
                _canonical_json(snapshot),
                now_text,
                now_text,
                now_text,
            ),
        )
        commit_audited_transaction(connection)
        return _response(after, request_id, trace_id)
    except ApiError:
        connection.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        if "branding_configs.custom_domain" in str(exc):
            raise ApiError("branding.custom_domain_conflict") from None
        raise ApiError("branding.unavailable") from None
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("branding.unavailable") from None
    finally:
        connection.close()


def get_branding_logo(
    config: AppConfig, context: AuthContext, *, request_id: str, trace_id: str
) -> BrandingLogo:
    """读取当前租户 PNG Logo；缺失和损坏均不返回数据库正文。"""

    connection = _connect(config)
    try:
        row = _load_row(connection, context.user.tenant_id)
        body = row["logo_png"]
        if body is None:
            raise ApiError("branding.logo_not_configured")
        body, _width, _height, digest = validate_png(body)
        if row["logo_sha256"] != digest:
            raise ApiError("branding.unavailable")
        return BrandingLogo(
            body=body,
            etag=f'"sha256-{digest}"',
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        raise
    except (sqlite3.Error, TypeError, ValueError):
        raise ApiError("branding.unavailable") from None
    finally:
        connection.close()


def get_public_branding(
    config: AppConfig, *, tenant_slug: str, request_id: str, trace_id: str
) -> PublicBrandingResponse:
    """按登录表单 slug 返回固定形状投影，未知 slug 与默认品牌完全同形。"""

    connection = _connect(config)
    try:
        row = connection.execute(
            """
            SELECT branding.* FROM tenants tenant
            JOIN branding_configs branding ON branding.tenant_id=tenant.tenant_id
            WHERE tenant.slug=? AND tenant.status='active' AND branding.status='active'
            """,
            (tenant_slug,),
        ).fetchone()
        values = _public_values(row)
        return PublicBrandingResponse(
            branding=PublicBrandingInfo(**values), request_id=request_id, trace_id=trace_id
        )
    except (ApiError, sqlite3.Error, TypeError, ValueError):
        # 公共接口不能用基础设施错误区分租户存在性；固定默认投影仍带请求追踪。
        return PublicBrandingResponse(
            branding=PublicBrandingInfo(**_public_values(None)),
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


def get_public_branding_logo(config: AppConfig, *, tenant_slug: str) -> BrandingLogo:
    """按公共 slug 返回 Logo；未知/无 Logo 同一固定 404，不枚举租户。"""

    connection = _connect(config)
    try:
        row = connection.execute(
            """
            SELECT branding.logo_png,branding.logo_sha256 FROM tenants tenant
            JOIN branding_configs branding ON branding.tenant_id=tenant.tenant_id
            WHERE tenant.slug=? AND tenant.status='active' AND branding.status='active'
            """,
            (tenant_slug,),
        ).fetchone()
        if row is None or row["logo_png"] is None:
            raise ApiError("branding.logo_not_configured")
        body, _width, _height, digest = validate_png(row["logo_png"])
        if row["logo_sha256"] != digest:
            raise ApiError("branding.logo_not_configured")
        return BrandingLogo(body=body, etag=f'"sha256-{digest}"', request_id="", trace_id="")
    except ApiError:
        raise
    except (sqlite3.Error, TypeError, ValueError):
        raise ApiError("branding.logo_not_configured") from None
    finally:
        connection.close()


def _parse_updates(payload: BrandingPatchRequest, before: sqlite3.Row) -> dict[str, object]:
    """解析局部 DTO，保留省略值并把 Logo base64 变成已校验 PNG bytes。"""

    updates: dict[str, object] = {}
    fields = payload.model_fields_set
    for name in (
        "product_name",
        "primary_color",
        "secondary_color",
        "login_title",
        "login_message",
        "custom_domain",
    ):
        if name in fields:
            value = getattr(payload, name)
            if name == "custom_domain":
                _validate_domain(value)
            elif value is None:
                raise ApiError("branding.request_invalid")
            updates[name] = value
    if "logo_png_base64" in fields:
        encoded = payload.logo_png_base64
        if encoded is None:
            updates.update(logo_png=None, logo_sha256=None, logo_width=None, logo_height=None)
        else:
            if encoded.startswith("data:"):
                raise ApiError("branding.logo_invalid")
            try:
                body = base64.b64decode(encoded.encode("ascii"), validate=True)
            except (UnicodeEncodeError, binascii.Error):
                raise ApiError("branding.logo_invalid") from None
            if len(body) > MAX_LOGO_BYTES:
                raise ApiError("branding.logo_too_large")
            body, width, height, digest = validate_png(body)
            updates.update(logo_png=body, logo_sha256=digest, logo_width=width, logo_height=height)
    if "custom_domain" in fields and payload.custom_domain is None:
        updates["custom_domain_status"] = "not_configured"
    elif "custom_domain" in fields:
        updates["custom_domain_status"] = "reserved"
    if not any(_different(before[name], value) for name, value in updates.items()):
        return {}
    return updates


def _validate_domain(value: object) -> None:
    """服务层再次验证域名，防止内部调用绕过 Pydantic 进入数据库。"""

    try:
        validate_custom_domain(value)
    except ValueError:
        raise ApiError("branding.custom_domain_invalid") from None


def _build_event(
    row: sqlite3.Row,
    changed: tuple[str, ...],
    context: AuthContext,
    request_id: str,
    trace_id: str,
    timestamp: datetime,
):
    """从已落库 BrandingConfig 构造可重建 EVT-100。"""

    payload = {
        "schema_version": 1,
        "branding_config_id": row["branding_config_id"],
        "revision": _integer(row["revision"], minimum=2),
        "changed_fields": list(changed),
        "logo_present": row["logo_sha256"] is not None,
        "logo_sha256": row["logo_sha256"],
        "custom_domain_status": row["custom_domain_status"],
    }
    return build_webhook_event(
        tenant_id=context.user.tenant_id,
        event_type=BRANDING_UPDATED_EVENT_TYPE,
        payload=payload,
        actor_id=context.user.user_id,
        object_id=row["branding_config_id"],
        trace_id=trace_id,
        source_request_id=request_id,
        event_id=_event_id(row["branding_config_id"], row["revision"]),
        timestamp=timestamp,
    )


def _audit_summary(row: sqlite3.Row, changed: tuple[str, ...]) -> dict[str, object]:
    """生成通用 AuditLog 允许的标量摘要，不落文案、域名或 Logo bytes。"""

    return {
        "branding_config_id": row["branding_config_id"],
        "status": row["status"],
        "revision": _integer(row["revision"], minimum=1),
        "changed_fields": ",".join(changed),
        "product_name_sha256": _sha256(str(row["product_name"]).encode("utf-8")),
        "login_title_sha256": _sha256(str(row["login_title"]).encode("utf-8")),
        "login_message_sha256": _sha256(str(row["login_message"]).encode("utf-8")),
        "custom_domain_status": row["custom_domain_status"],
        "logo_present": row["logo_sha256"] is not None,
        "logo_sha256": row["logo_sha256"],
        "logo_width": row["logo_width"],
        "logo_height": row["logo_height"],
    }


def _response(row: sqlite3.Row, request_id: str, trace_id: str) -> BrandingResponse:
    """严格映射数据库 row，拒绝向 DTO 泄露租户或操作者字段。"""

    return BrandingResponse(
        branding=BrandingInfo(
            branding_config_id=_safe_id(row["branding_config_id"]),
            product_name=_safe_text(row["product_name"]),
            primary_color=_safe_text(row["primary_color"]),
            secondary_color=_safe_text(row["secondary_color"]),
            login_title=_safe_text(row["login_title"]),
            login_message=_safe_text(row["login_message"]),
            custom_domain=_optional_text(row["custom_domain"]),
            custom_domain_status=row["custom_domain_status"],
            logo_present=row["logo_sha256"] is not None,
            logo_sha256=row["logo_sha256"],
            logo_width=row["logo_width"],
            logo_height=row["logo_height"],
            revision=_integer(row["revision"], minimum=1),
            status="active",
            updated_at=_safe_text(row["updated_at"]),
        ),
        request_id=request_id,
        trace_id=trace_id,
    )


def _public_values(row: sqlite3.Row | None) -> dict[str, object]:
    """构造固定公共投影；slug 只用于同源 Logo URL，不把原文写入日志。"""

    if row is None:
        values = {**_DEFAULT, "logo_present": False}
    else:
        values = {
            "product_name": _safe_text(row["product_name"]),
            "primary_color": _safe_text(row["primary_color"]),
            "secondary_color": _safe_text(row["secondary_color"]),
            "login_title": _safe_text(row["login_title"]),
            "login_message": _safe_text(row["login_message"]),
            "logo_present": row["logo_sha256"] is not None,
        }
    return values


def _load_row(connection: sqlite3.Connection, tenant_id: str) -> sqlite3.Row:
    """按复合租户 owner 读取唯一 active 配置。"""

    row = connection.execute(
        "SELECT * FROM branding_configs WHERE tenant_id=? AND status='active'",
        (tenant_id,),
    ).fetchone()
    if row is None:
        raise ApiError("branding.not_found")
    if not _BRANDING_ID.fullmatch(str(row["branding_config_id"])):
        raise ApiError("branding.unavailable")
    return row


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建数据库连接并折叠路径/驱动异常。"""

    try:
        connection = connect_database(config.database_url)
        if getattr(connection, "backend", "sqlite") == "sqlite":
            connection.execute("PRAGMA busy_timeout=5000")
        return connection
    except DbError:
        raise ApiError("branding.unavailable") from None


def _transaction_now(connection: sqlite3.Connection, override: datetime | None) -> datetime:
    """事务内统一 UTC 秒，避免审计、事件和配置时间漂移。"""

    if override is not None:
        if override.tzinfo is None:
            raise ApiError("branding.unavailable")
        return override.astimezone(UTC).replace(microsecond=0)
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    if row is None or type(row[0]) is not str:
        raise ApiError("branding.unavailable")
    return datetime.fromisoformat(row[0].replace("Z", "+00:00")).astimezone(UTC)


def _db_time(value: datetime) -> str:
    """序列化 UTC 整秒时间。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _event_id(branding_config_id: object, revision: object) -> str:
    """EVT-100 事件 ID 绑定配置与版本，重试不会生成第二个事实。"""

    if not _BRANDING_ID.fullmatch(str(branding_config_id)) or type(revision) is not int:
        raise ApiError("branding.event_unavailable")
    digest = _sha256(f"{branding_config_id}\x1f{revision}".encode("ascii"))[:32]
    return f"evt_brd_{digest}"


def _branding_event_id(event_id: object) -> str:
    """把公开事件 ID 映射到 source row 不透明 ID。"""

    if type(event_id) is not str or _EVENT_ID.fullmatch(event_id) is None:
        raise ApiError("branding.event_unavailable")
    return "bev_" + event_id[8:]


def _outbox_id(event_id: object) -> str:
    """生成单事件唯一 outbox ID。"""

    if type(event_id) is not str or _EVENT_ID.fullmatch(event_id) is None:
        raise ApiError("branding.event_unavailable")
    return "bro_" + event_id[8:]


def _count_changes(connection: sqlite3.Connection) -> int:
    """读取 SQLite changes() 并拒绝异常类型。"""

    value = connection.execute("SELECT changes()").fetchone()[0]
    return _integer(value, minimum=0)


def _integer(value: object, *, minimum: int) -> int:
    """验证 SQLite 整数聚合，拒绝 bool、负数和浮点隐式转换。"""

    if type(value) is not int or value < minimum:
        raise ApiError("branding.unavailable")
    return value


def _different(before: object, after: object) -> bool:
    """使用 SQLite NULL-safe 比较语义判断一次实际变化。"""

    return before != after


def _public_field_name(column_name: str) -> str:
    """Map storage-only branding columns back to the single API field that changed."""

    if column_name in {"logo_png", "logo_sha256", "logo_width", "logo_height"}:
        return "logo_png"
    if column_name == "custom_domain_status":
        return "custom_domain"
    return column_name


def _canonical_json(value: object) -> str:
    """固定 JSON 键序，供事件 payload hash 和数据库校验复用。"""

    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256(value: bytes) -> str:
    """计算非凭据安全摘要；原始文本不进入调用日志。"""

    return hashlib.sha256(value).hexdigest()


def _safe_id(value: object) -> str:
    """校验公开不透明 ID。"""

    if type(value) is not str or _BRANDING_ID.fullmatch(value) is None:
        raise ApiError("branding.unavailable")
    return value


def _safe_text(value: object) -> str:
    """校验已迁移纯文本，不回退或回显坏数据库值。"""

    if type(value) is not str or not value or any(ord(ch) < 32 for ch in value):
        raise ApiError("branding.unavailable")
    return value


def _optional_text(value: object) -> str | None:
    """读取可空域名并保持固定 null 语义。"""

    if value is None:
        return None
    return _safe_text(value)


__all__ = [
    "BrandingLogo",
    "get_branding",
    "get_branding_logo",
    "get_public_branding",
    "get_public_branding_logo",
    "update_branding",
]
