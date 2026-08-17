"""租户隔离的伙伴资料、客户映射、用量聚合与附件服务。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.auth import AuthContext, require_transaction_permission

from .models import (
    CreatePartnerCustomerRequest,
    CreatePartnerRequest,
    DeliveryApiPaths,
    DeliveryBranding,
    DeliveryCustomer,
    DeliveryEnvelope,
    DeliveryPackage,
    PartnerCommissionCursor,
    PartnerCustomerInfo,
    PartnerCustomerResponse,
    PartnerCustomersResponse,
    PartnerCustomerUsage,
    PartnerInfo,
    PartnerProjectInfo,
    PartnerProjectsResponse,
    PartnerResponse,
    PartnerWindow,
    UpdateBrandingRequest,
    UpdatePartnerCustomerRequest,
)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_WINDOW_DAYS = 366
MAX_CSV_ROWS = 10_000
MAX_CSV_BYTES = 8 * 1024 * 1024
MAX_DELIVERY_BYTES = 1024 * 1024
_SAFE_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{16,160}$")
_CUSTOMER_ID = re.compile(r"^pcu_[0-9a-f]{32}$")
_QUERY = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")


@dataclass(frozen=True, slots=True)
class DateWindow:
    """报表查询使用的 UTC 日期窗口，结束日期包含在内。"""

    start: date
    end: date

    @property
    def start_text(self) -> str:
        return self.start.isoformat()

    @property
    def end_text(self) -> str:
        return self.end.isoformat()

    @property
    def start_timestamp(self) -> str:
        return f"{self.start_text}T00:00:00Z"

    @property
    def end_exclusive_timestamp(self) -> str:
        return f"{(self.end + timedelta(days=1)).isoformat()}T00:00:00Z"


@dataclass(frozen=True, slots=True)
class Attachment:
    """受大小和内容类型约束的安全下载附件。"""

    body: bytes
    content_type: str
    filename: str
    request_id: str
    trace_id: str


def normalize_idempotency_key(value: object) -> str:
    """接受一个安全 ASCII 幂等键，处理完成后不保留原值。"""

    if type(value) is not str or _SAFE_IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise ApiError("partner.request_invalid")
    return value


def normalize_revision(value: object) -> int:
    """解析强整数 If-Match revision；本合同不接受 entity-tag 语法。"""

    if type(value) is not str or not value.isascii() or not value.isdigit():
        raise ApiError("partner.request_invalid")
    revision = int(value)
    if not 1 <= revision <= 2_147_483_647:
        raise ApiError("partner.request_invalid")
    return revision


def normalize_customer_id(value: object) -> str:
    """校验伙伴客户标识格式，并将非法值映射为统一 404。"""

    if type(value) is not str or _CUSTOMER_ID.fullmatch(value) is None:
        raise ApiError("partner.customer_not_found")
    return value


def normalize_query(value: object) -> str | None:
    """校验列表查询文本，拒绝控制字符和超长输入。"""

    if value is None:
        return None
    if type(value) is not str or _QUERY.fullmatch(value) is None:
        raise ApiError("partner.request_invalid")
    return value


def parse_window(
    start_date: str | None, end_date: str | None, *, now: datetime | None = None
) -> DateWindow:
    """返回包含首尾的 UTC 日期窗口，默认最近 30 个完整 UTC 日。"""

    if (start_date is None) != (end_date is None):
        raise ApiError("partner.request_invalid")
    if start_date is None:
        current = (now or datetime.now(UTC)).astimezone(UTC).date()
        end = current - timedelta(days=1)
        return DateWindow(start=end - timedelta(days=29), end=end)
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date or "")
    except ValueError:
        raise ApiError("partner.request_invalid") from None
    if start.isoformat() != start_date or end.isoformat() != end_date:
        raise ApiError("partner.request_invalid")
    days = (end - start).days + 1
    if not 1 <= days <= MAX_WINDOW_DAYS:
        raise ApiError("partner.request_invalid")
    return DateWindow(start=start, end=end)


def parse_pagination(limit: object, offset: object) -> tuple[int, int]:
    """解析有界分页参数，防止跨租户大范围读取。"""

    if type(limit) is not int or type(offset) is not int:
        raise ApiError("partner.request_invalid")
    if not 1 <= limit <= MAX_PAGE_SIZE or not 0 <= offset <= 2_147_483_647:
        raise ApiError("partner.request_invalid")
    return limit, offset


def create_partner(
    config: AppConfig,
    context: AuthContext,
    payload: CreatePartnerRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    now: datetime | None = None,
) -> PartnerResponse:
    """在当前租户内幂等创建伙伴配置并写入审计操作。"""

    tenant_id = context.user.tenant_id
    key_hash = _sha256(idempotency_key.encode("ascii"))
    payload_json = _canonical_json(payload.model_dump(mode="json"))
    payload_hash = _sha256(payload_json.encode("utf-8"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        require_transaction_permission(
            connection,
            context,
            "partner_portal.configure",
            unavailable_error_code="partner.unavailable",
        )
        existing = connection.execute(
            "SELECT * FROM partners WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
        if existing is not None:
            if (
                existing["idempotency_key_sha256"] == key_hash
                and existing["payload_sha256"] == payload_hash
            ):
                connection.commit()
                return _partner_response(existing, request_id, trace_id)
            raise ApiError("partner.already_configured")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        partner_id = f"par_{hashlib.sha256(f'{tenant_id}\x1f{key_hash}'.encode()).hexdigest()[:32]}"
        connection.execute(
            """
            INSERT INTO partners(
              partner_id,tenant_id,partner_code,display_name,portal_name,brand_primary_color,
              support_text,default_commission_rate_bps,status,revision,idempotency_key_sha256,
              payload_sha256,created_by_user_id,operation_request_id,operation_trace_id,
              created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,1000,'active',1,?,?,?,?,?,?,?)
            """,
            (
                partner_id,
                tenant_id,
                payload.partner_code,
                payload.display_name,
                payload.portal_name,
                payload.brand_primary_color,
                payload.support_text,
                key_hash,
                payload_hash,
                context.user.user_id,
                request_id,
                trace_id,
                now_text,
                now_text,
            ),
        )
        _record_operation(
            connection,
            tenant_id=tenant_id,
            partner_id=partner_id,
            action="partner.created",
            object_id=partner_id,
            actor_user_id=context.user.user_id,
            request_id=request_id,
            trace_id=trace_id,
            summary={
                "revision": 1,
                "branding_sha256": _branding_hash(
                    payload.portal_name, payload.brand_primary_color, payload.support_text
                ),
            },
            created_at=now_text,
        )
        row = connection.execute(
            "SELECT * FROM partners WHERE tenant_id=? AND partner_id=?", (tenant_id, partner_id)
        ).fetchone()
        if row is None:
            raise ApiError("partner.unavailable")
        connection.commit()
        return _partner_response(row, request_id, trace_id)
    except ApiError:
        connection.rollback()
        raise
    except sqlite3.IntegrityError:
        connection.rollback()
        if _existing_partner_matches(config, tenant_id, key_hash, payload_hash):
            return get_partner_profile(config, context, request_id=request_id, trace_id=trace_id)
        raise ApiError("partner.already_configured") from None
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("partner.unavailable") from None
    finally:
        connection.close()


def get_partner_profile(
    config: AppConfig, context: AuthContext, *, request_id: str, trace_id: str
) -> PartnerResponse:
    """读取当前租户伙伴配置，未配置时返回统一 404。"""

    connection = _connect(config)
    try:
        row = _partner_row(connection, context.user.tenant_id)
        return _partner_response(row, request_id, trace_id)
    except ApiError:
        raise
    except (sqlite3.Error, TypeError, ValueError):
        raise ApiError("partner.unavailable") from None
    finally:
        connection.close()


def update_partner_branding(
    config: AppConfig,
    context: AuthContext,
    payload: UpdateBrandingRequest,
    *,
    revision: int,
    request_id: str,
    trace_id: str,
    now: datetime | None = None,
) -> PartnerResponse:
    """用 revision CAS 更新白标配置并记录安全摘要。"""

    fields = payload.model_fields_set
    if (
        not fields
        or ("portal_name" in fields and payload.portal_name is None)
        or ("brand_primary_color" in fields and payload.brand_primary_color is None)
        or ("support_text" in fields and payload.support_text == "")
    ):
        raise ApiError("partner.request_invalid")
    connection = _connect(config)
    tenant_id = context.user.tenant_id
    try:
        connection.execute("BEGIN IMMEDIATE")
        for permission in ("partner_portal.read", "partner_branding.manage"):
            require_transaction_permission(
                connection, context, permission, unavailable_error_code="partner.unavailable"
            )
        row = _partner_row(connection, tenant_id)
        portal_name = payload.portal_name if "portal_name" in fields else row["portal_name"]
        color = (
            payload.brand_primary_color
            if "brand_primary_color" in fields
            else row["brand_primary_color"]
        )
        support = payload.support_text if "support_text" in fields else row["support_text"]
        if (portal_name, color, support) == (
            row["portal_name"],
            row["brand_primary_color"],
            row["support_text"],
        ):
            raise ApiError("partner.request_invalid")
        now_text = _db_time(_transaction_now(connection, now))
        connection.execute(
            """
            UPDATE partners
            SET portal_name=?,brand_primary_color=?,support_text=?,revision=revision+1,
              operation_request_id=?,operation_trace_id=?,updated_at=?
            WHERE tenant_id=? AND partner_id=? AND revision=?
            """,
            (
                portal_name,
                color,
                support,
                request_id,
                trace_id,
                now_text,
                tenant_id,
                row["partner_id"],
                revision,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ApiError("partner.revision_conflict")
        updated = connection.execute(
            "SELECT * FROM partners WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
        if updated is None:
            raise ApiError("partner.unavailable")
        _record_operation(
            connection,
            tenant_id=tenant_id,
            partner_id=row["partner_id"],
            action="partner.branding_updated",
            object_id=row["partner_id"],
            actor_user_id=context.user.user_id,
            request_id=request_id,
            trace_id=trace_id,
            summary={
                "revision": updated["revision"],
                "branding_sha256": _branding_hash(portal_name, color, support),
            },
            created_at=now_text,
        )
        connection.commit()
        return _partner_response(updated, request_id, trace_id)
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("partner.unavailable") from None
    finally:
        connection.close()


def list_partner_projects(
    config: AppConfig, context: AuthContext, *, request_id: str, trace_id: str
) -> PartnerProjectsResponse:
    """列出当前租户可映射项目，不返回跨租户数据。"""

    connection = _connect(config)
    try:
        partner = _partner_row(connection, context.user.tenant_id)
        rows = connection.execute(
            """
            SELECT project.project_id,project.project_code,project.name
            FROM projects project
            WHERE project.tenant_id=? AND project.status='active'
              AND NOT EXISTS(SELECT 1 FROM partner_customers customer
                WHERE customer.tenant_id=project.tenant_id
                  AND customer.project_id=project.project_id)
            ORDER BY lower(project.name),project.project_code,project.project_id
            """,
            (partner["tenant_id"],),
        ).fetchall()
        items = [
            PartnerProjectInfo(
                project_id=_text(row["project_id"]),
                project_code=_text(row["project_code"]),
                name=_text(row["name"]),
            )
            for row in rows
        ]
        return PartnerProjectsResponse(
            items=items, total=len(items), request_id=request_id, trace_id=trace_id
        )
    except ApiError:
        raise
    except (sqlite3.Error, TypeError, ValueError):
        raise ApiError("partner.unavailable") from None
    finally:
        connection.close()


def create_partner_customer(
    config: AppConfig,
    context: AuthContext,
    payload: CreatePartnerCustomerRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    now: datetime | None = None,
) -> PartnerCustomerResponse:
    """在当前租户内幂等创建客户映射和分润游标。"""

    tenant_id = context.user.tenant_id
    key_hash = _sha256(idempotency_key.encode("ascii"))
    payload_json = _canonical_json(payload.model_dump(mode="json"))
    payload_hash = _sha256(payload_json.encode("utf-8"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for permission in ("partner_portal.read", "partner_customer.manage"):
            require_transaction_permission(
                connection, context, permission, unavailable_error_code="partner.unavailable"
            )
        partner = _partner_row(connection, tenant_id)
        existing = connection.execute(
            """SELECT * FROM partner_customers
                WHERE tenant_id=? AND partner_id=? AND idempotency_key_sha256=?""",
            (tenant_id, partner["partner_id"], key_hash),
        ).fetchone()
        if existing is not None:
            if existing["payload_sha256"] != payload_hash:
                raise ApiError("partner.customer_conflict")
            connection.commit()
            return _customer_response_after_commit(
                config, context, existing["partner_customer_id"], request_id, trace_id
            )
        project = connection.execute(
            """
            SELECT project_id FROM projects WHERE tenant_id=? AND project_id=? AND status='active'
              AND NOT EXISTS(SELECT 1 FROM partner_customers customer
                WHERE customer.tenant_id=projects.tenant_id
                  AND customer.project_id=projects.project_id)
            """,
            (tenant_id, payload.project_id),
        ).fetchone()
        if project is None:
            raise ApiError("partner.project_not_found")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        high_water = _ledger_high_water(connection, tenant_id)
        identity = f"{tenant_id}\x1f{partner['partner_id']}\x1f{key_hash}"
        customer_id = f"pcu_{hashlib.sha256(identity.encode()).hexdigest()[:32]}"
        connection.execute(
            """
            INSERT INTO partner_customers(
              partner_customer_id,tenant_id,partner_id,project_id,customer_code,display_name,
              status,linked_from_ledger_sequence,commission_cutoff_sequence,commission_rate_bps,
              revision,idempotency_key_sha256,payload_sha256,created_by_user_id,disabled_by_user_id,
              operation_request_id,operation_trace_id,disable_request_id,disable_trace_id,
              created_at,updated_at,disabled_at)
            VALUES(?,?,?,?,?,?,'active',?,NULL,1000,1,?,?,?,NULL,?,?,NULL,NULL,?,?,NULL)
            """,
            (
                customer_id,
                tenant_id,
                partner["partner_id"],
                payload.project_id,
                payload.customer_code,
                payload.display_name,
                high_water,
                key_hash,
                payload_hash,
                context.user.user_id,
                request_id,
                trace_id,
                now_text,
                now_text,
            ),
        )
        connection.execute(
            """
            INSERT INTO partner_commission_cursors(
              tenant_id,partner_customer_id,partner_id,project_id,status,
              last_scanned_ledger_sequence,generation,lease_owner_id,lease_token,
              lease_expires_at,next_run_at,consecutive_failure_count,last_error_code,
              created_at,updated_at)
            VALUES(?,?,?,?,'pending',?,0,NULL,NULL,NULL,?,0,NULL,?,?)
            """,
            (
                tenant_id,
                customer_id,
                partner["partner_id"],
                payload.project_id,
                high_water,
                now_text,
                now_text,
                now_text,
            ),
        )
        event = _customer_created_event(
            tenant_id=tenant_id,
            partner_id=partner["partner_id"],
            customer_id=customer_id,
            customer_code=payload.customer_code,
            project_id=payload.project_id,
            actor_user_id=context.user.user_id,
            request_id=request_id,
            trace_id=trace_id,
            timestamp=current,
        )
        digest = hashlib.sha256(customer_id.encode("ascii")).hexdigest()[:32]
        event_json = _canonical_json(event.payload)
        connection.execute(
            """
            INSERT INTO partner_customer_outbox(
              partner_customer_outbox_id,tenant_id,partner_id,partner_customer_id,
              webhook_event_id,actor_user_id,source_request_id,source_trace_id,event_timestamp,
              payload_json,payload_sha256,status,attempt_count,generation,lease_owner_id,
              lease_token,lease_expires_at,next_attempt_at,last_error_code,delivered_at,
              created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',0,0,NULL,NULL,NULL,?,NULL,NULL,?,?)
            """,
            (
                f"pco_{digest}",
                tenant_id,
                partner["partner_id"],
                customer_id,
                event.event_id,
                context.user.user_id,
                request_id,
                trace_id,
                now_text,
                event_json,
                _sha256(event_json.encode("utf-8")),
                now_text,
                now_text,
                now_text,
            ),
        )
        _record_operation(
            connection,
            tenant_id=tenant_id,
            partner_id=partner["partner_id"],
            action="partner.customer_created",
            object_id=customer_id,
            actor_user_id=context.user.user_id,
            request_id=request_id,
            trace_id=trace_id,
            summary={"revision": 1, "linked_from_ledger_sequence": high_water},
            created_at=now_text,
        )
        connection.commit()
        return _customer_response_after_commit(config, context, customer_id, request_id, trace_id)
    except ApiError:
        connection.rollback()
        raise
    except sqlite3.IntegrityError:
        connection.rollback()
        match = _customer_by_idempotency(config, tenant_id, key_hash)
        if match is not None and match["payload_sha256"] == payload_hash:
            return _customer_response_after_commit(
                config, context, match["partner_customer_id"], request_id, trace_id
            )
        raise ApiError("partner.customer_conflict") from None
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("partner.unavailable") from None
    finally:
        connection.close()


def update_partner_customer(
    config: AppConfig,
    context: AuthContext,
    customer_id: str,
    payload: UpdatePartnerCustomerRequest,
    *,
    revision: int,
    request_id: str,
    trace_id: str,
    now: datetime | None = None,
) -> PartnerCustomerResponse:
    """按 revision CAS 禁用客户映射并固定分润 cutoff。"""

    fields = payload.model_fields_set
    if (
        len(fields) != 1
        or fields not in ({"display_name"}, {"status"})
        or ("display_name" in fields and payload.display_name is None)
        or ("status" in fields and payload.status != "disabled")
    ):
        raise ApiError("partner.request_invalid")
    customer_id = normalize_customer_id(customer_id)
    connection = _connect(config)
    tenant_id = context.user.tenant_id
    try:
        connection.execute("BEGIN IMMEDIATE")
        for permission in ("partner_portal.read", "partner_customer.manage"):
            require_transaction_permission(
                connection, context, permission, unavailable_error_code="partner.unavailable"
            )
        partner = _partner_row(connection, tenant_id)
        row = _customer_row(connection, tenant_id, partner["partner_id"], customer_id)
        if row["status"] != "active":
            raise ApiError("partner.revision_conflict")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        if fields == {"display_name"}:
            if payload.display_name == row["display_name"]:
                raise ApiError("partner.request_invalid")
            connection.execute(
                """
                UPDATE partner_customers SET display_name=?,revision=revision+1,
                  operation_request_id=?,operation_trace_id=?,updated_at=?
                WHERE tenant_id=? AND partner_id=? AND partner_customer_id=?
                  AND status='active' AND revision=?
                """,
                (
                    payload.display_name,
                    request_id,
                    trace_id,
                    now_text,
                    tenant_id,
                    partner["partner_id"],
                    customer_id,
                    revision,
                ),
            )
            action = None
        else:
            cutoff = _ledger_high_water(connection, tenant_id)
            connection.execute(
                """
                UPDATE partner_customers SET status='disabled',commission_cutoff_sequence=?,
                  revision=revision+1,disabled_by_user_id=?,operation_request_id=?,operation_trace_id=?,
                  disable_request_id=?,disable_trace_id=?,updated_at=?,disabled_at=?
                WHERE tenant_id=? AND partner_id=? AND partner_customer_id=?
                  AND status='active' AND revision=?
                """,
                (
                    cutoff,
                    context.user.user_id,
                    request_id,
                    trace_id,
                    request_id,
                    trace_id,
                    now_text,
                    now_text,
                    tenant_id,
                    partner["partner_id"],
                    customer_id,
                    revision,
                ),
            )
            action = "partner.customer_disabled"
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ApiError("partner.revision_conflict")
        updated = _customer_row(connection, tenant_id, partner["partner_id"], customer_id)
        if action is not None:
            _record_operation(
                connection,
                tenant_id=tenant_id,
                partner_id=partner["partner_id"],
                action=action,
                object_id=customer_id,
                actor_user_id=context.user.user_id,
                request_id=request_id,
                trace_id=trace_id,
                summary={"revision": updated["revision"], "status": updated["status"]},
                created_at=now_text,
            )
        connection.commit()
        return _customer_response_after_commit(config, context, customer_id, request_id, trace_id)
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("partner.unavailable") from None
    finally:
        connection.close()


def list_partner_customers(
    config: AppConfig,
    context: AuthContext,
    *,
    window: DateWindow,
    status: str | None,
    query: str | None,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> PartnerCustomersResponse:
    """读取租户内客户分页及历史用量摘要。"""

    if status not in {None, "active", "disabled"}:
        raise ApiError("partner.request_invalid")
    query = normalize_query(query)
    limit, offset = parse_pagination(limit, offset)
    connection = _connect(config)
    try:
        partner = _partner_row(connection, context.user.tenant_id)
        where = ["tenant_id=?", "partner_id=?"]
        params: list[object] = [context.user.tenant_id, partner["partner_id"]]
        if status is not None:
            where.append("status=?")
            params.append(status)
        if query is not None:
            where.append(
                "(instr(lower(customer_code),lower(?))>0 OR instr(lower(display_name),lower(?))>0)"
            )
            params.extend((query, query))
        where_sql = " AND ".join(where)
        total = _count(
            connection.execute(
                f"SELECT COUNT(*) FROM partner_customers WHERE {where_sql}", params
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""SELECT * FROM partner_customers WHERE {where_sql}
                ORDER BY created_at,partner_customer_id LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        high_water = _ledger_high_water(connection, context.user.tenant_id)
        items = [_customer_info(connection, row, window, high_water) for row in rows]
        return PartnerCustomersResponse(
            window=PartnerWindow(start_date=window.start_text, end_date=window.end_text),
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        raise
    except (sqlite3.Error, TypeError, ValueError):
        raise ApiError("partner.unavailable") from None
    finally:
        connection.close()


def generate_delivery_package(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
    now: datetime | None = None,
) -> Attachment:
    """生成不含敏感正文的客户交付包附件。"""

    connection = _connect(config)
    tenant_id = context.user.tenant_id
    try:
        connection.execute("BEGIN IMMEDIATE")
        for permission in ("partner_portal.read", "partner_delivery.download"):
            require_transaction_permission(
                connection, context, permission, unavailable_error_code="partner.unavailable"
            )
        partner = _partner_row(connection, tenant_id)
        rows = connection.execute(
            """
            SELECT customer_code,display_name,status FROM partner_customers
            WHERE tenant_id=? AND partner_id=? ORDER BY customer_code,partner_customer_id
            """,
            (tenant_id, partner["partner_id"]),
        ).fetchall()
        current = _transaction_now(connection, now)
        package = DeliveryPackage(
            partner_code=_text(partner["partner_code"]),
            branding=DeliveryBranding(
                portal_name=_text(partner["portal_name"]),
                brand_primary_color=_text(partner["brand_primary_color"]),
                support_text=_optional_text(partner["support_text"]),
            ),
            customers=[
                DeliveryCustomer(
                    customer_code=_text(row["customer_code"]),
                    display_name=_text(row["display_name"]),
                    status=row["status"],
                )
                for row in rows
            ],
            api_paths=DeliveryApiPaths(),
            generated_at=_db_time(current),
            request_id=request_id,
            trace_id=trace_id,
        )
        package_dict = package.model_dump(mode="json", by_alias=True)
        manifest_hash = _sha256(_canonical_json(package_dict).encode("utf-8"))
        body = (
            _canonical_json(
                DeliveryEnvelope(package=package, manifest_sha256=manifest_hash).model_dump(
                    mode="json", by_alias=True
                )
            )
            + "\n"
        ).encode("utf-8")
        if len(body) > MAX_DELIVERY_BYTES:
            raise ApiError("partner.export_too_large")
        now_text = _db_time(current)
        _record_operation(
            connection,
            tenant_id=tenant_id,
            partner_id=partner["partner_id"],
            action="partner.delivery_downloaded",
            object_id=partner["partner_id"],
            actor_user_id=context.user.user_id,
            request_id=request_id,
            trace_id=trace_id,
            summary={"customer_count": len(rows), "manifest_sha256": manifest_hash},
            created_at=now_text,
        )
        connection.commit()
        return Attachment(
            body=body,
            content_type="application/json; charset=utf-8",
            filename=f"metahub-partner-delivery-{partner['partner_code']}-{current.date().isoformat()}.json",
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("partner.export_unavailable") from None
    finally:
        connection.close()


def _customer_response_after_commit(
    config: AppConfig, context: AuthContext, customer_id: str, request_id: str, trace_id: str
) -> PartnerCustomerResponse:
    connection = _connect(config)
    try:
        partner = _partner_row(connection, context.user.tenant_id)
        row = _customer_row(connection, context.user.tenant_id, partner["partner_id"], customer_id)
        window = parse_window(None, None)
        info = _customer_info(
            connection, row, window, _ledger_high_water(connection, context.user.tenant_id)
        )
        return PartnerCustomerResponse(customer=info, request_id=request_id, trace_id=trace_id)
    except ApiError:
        raise
    except (sqlite3.Error, TypeError, ValueError):
        raise ApiError("partner.unavailable") from None
    finally:
        connection.close()


def _customer_info(
    connection: sqlite3.Connection, row: sqlite3.Row, window: DateWindow, high_water: int
) -> PartnerCustomerInfo:
    tenant_id = _text(row["tenant_id"])
    project_id = _text(row["project_id"])
    requests = connection.execute(
        """
        SELECT COUNT(*) request_count,
          COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),0) success_count,
          COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) failed_count
        FROM request_logs WHERE tenant_id=? AND project_id=?
          AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)
        """,
        (tenant_id, project_id, window.start_timestamp, window.end_exclusive_timestamp),
    ).fetchone()
    usage = connection.execute(
        """
        SELECT COALESCE(SUM(input_tokens),0) input_tokens,
          COALESCE(SUM(output_tokens),0) output_tokens,COALESCE(SUM(total_tokens),0) total_tokens,
          COALESCE(SUM(cache_tokens),0) cache_tokens,
          COALESCE(SUM(CASE WHEN estimated=1 THEN 1 ELSE 0 END),0) estimated_records
        FROM usage_ledger WHERE tenant_id=? AND project_id=?
          AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)
        """,
        (tenant_id, project_id, window.start_timestamp, window.end_exclusive_timestamp),
    ).fetchone()
    costs = connection.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN currency='CNY' THEN amount_micros ELSE 0 END),0) cny,
          COALESCE(SUM(CASE WHEN currency='USD' THEN amount_micros ELSE 0 END),0) usd
        FROM finops_ledger_entries WHERE tenant_id=? AND project_id=? AND entry_type='usage_charge'
          AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)
        """,
        (tenant_id, project_id, window.start_timestamp, window.end_exclusive_timestamp),
    ).fetchone()
    cursor = connection.execute(
        "SELECT * FROM partner_commission_cursors WHERE tenant_id=? AND partner_customer_id=?",
        (tenant_id, row["partner_customer_id"]),
    ).fetchone()
    if cursor is None:
        raise ApiError("partner.commission_unavailable")
    target = min(
        high_water,
        row["commission_cutoff_sequence"]
        if row["commission_cutoff_sequence"] is not None
        else high_water,
    )
    last = _count(cursor["last_scanned_ledger_sequence"])
    return PartnerCustomerInfo(
        partner_customer_id=_text(row["partner_customer_id"]),
        project_id=project_id,
        customer_code=_text(row["customer_code"]),
        display_name=_text(row["display_name"]),
        status=row["status"],
        commission_rate_bps=_count(row["commission_rate_bps"]),
        revision=_count(row["revision"]),
        linked_from_ledger_sequence=_count(row["linked_from_ledger_sequence"]),
        commission_cutoff_sequence=_optional_count(row["commission_cutoff_sequence"]),
        created_at=_text(row["created_at"]),
        updated_at=_text(row["updated_at"]),
        disabled_at=_optional_text(row["disabled_at"]),
        usage=PartnerCustomerUsage(
            start_date=window.start_text,
            end_date=window.end_text,
            request_count=_count(requests["request_count"]),
            success_count=_count(requests["success_count"]),
            failed_count=_count(requests["failed_count"]),
            input_tokens=_count(usage["input_tokens"]),
            output_tokens=_count(usage["output_tokens"]),
            total_tokens=_count(usage["total_tokens"]),
            cache_tokens=_count(usage["cache_tokens"]),
            cost_cny_micros=_count(costs["cny"]),
            cost_usd_micros=_count(costs["usd"]),
            estimated_records=_count(usage["estimated_records"]),
        ),
        commission_cursor=PartnerCommissionCursor(
            status=cursor["status"],
            last_scanned_ledger_sequence=last,
            source_high_water_sequence=target,
            freshness_status="current" if last >= target else "lagging",
        ),
    )


def _record_operation(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    partner_id: str,
    action: str,
    object_id: str,
    actor_user_id: str,
    request_id: str,
    trace_id: str,
    summary: dict[str, object],
    created_at: str,
) -> None:
    summary_json = _canonical_json(summary)
    connection.execute(
        """
        INSERT INTO partner_portal_operations(
          partner_operation_id,tenant_id,partner_id,action,object_id,actor_user_id,
          request_id,trace_id,summary_json,summary_sha256,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"pop_{uuid4().hex}",
            tenant_id,
            partner_id,
            action,
            object_id,
            actor_user_id,
            request_id,
            trace_id,
            summary_json,
            _sha256(summary_json.encode("utf-8")),
            created_at,
        ),
    )


def _partner_row(connection: sqlite3.Connection, tenant_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM partners WHERE tenant_id=? AND status='active'", (tenant_id,)
    ).fetchone()
    if row is None:
        raise ApiError("partner.not_configured")
    return row


def _customer_row(
    connection: sqlite3.Connection, tenant_id: str, partner_id: str, customer_id: str
) -> sqlite3.Row:
    row = connection.execute(
        """SELECT * FROM partner_customers
            WHERE tenant_id=? AND partner_id=? AND partner_customer_id=?""",
        (tenant_id, partner_id, customer_id),
    ).fetchone()
    if row is None:
        raise ApiError("partner.customer_not_found")
    return row


def _partner_response(row: sqlite3.Row, request_id: str, trace_id: str) -> PartnerResponse:
    return PartnerResponse(
        partner=PartnerInfo(
            partner_id=_text(row["partner_id"]),
            partner_code=_text(row["partner_code"]),
            display_name=_text(row["display_name"]),
            portal_name=_text(row["portal_name"]),
            brand_primary_color=_text(row["brand_primary_color"]),
            support_text=_optional_text(row["support_text"]),
            status=row["status"],
            default_commission_rate_bps=_count(row["default_commission_rate_bps"]),
            revision=_count(row["revision"]),
            created_at=_text(row["created_at"]),
            updated_at=_text(row["updated_at"]),
        ),
        request_id=request_id,
        trace_id=trace_id,
    )


def _customer_created_event(
    *,
    tenant_id: str,
    partner_id: str,
    customer_id: str,
    customer_code: str,
    project_id: str,
    actor_user_id: str,
    request_id: str,
    trace_id: str,
    timestamp: datetime,
):
    digest = hashlib.sha256(customer_id.encode("ascii")).hexdigest()[:32]
    return build_webhook_event(
        tenant_id=tenant_id,
        event_type="partner.customer_created",
        payload={
            "schema_version": "1.0",
            "partner_id": partner_id,
            "partner_customer_id": customer_id,
            "customer_code": customer_code,
            "project_id": project_id,
            "status": "active",
            "commission_rate_bps": 1000,
        },
        actor_id=actor_user_id,
        object_id=customer_id,
        trace_id=trace_id,
        source_request_id=request_id,
        event_id=f"evt_pcu_{digest}",
        timestamp=timestamp,
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        connection = connect_database(config.database_url)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection
    except DbError:
        raise ApiError("partner.unavailable") from None


def _existing_partner_matches(
    config: AppConfig, tenant_id: str, key_hash: str, payload_hash: str
) -> bool:
    connection = _connect(config)
    try:
        row = connection.execute(
            "SELECT idempotency_key_sha256,payload_sha256 FROM partners WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        return row is not None and row[0] == key_hash and row[1] == payload_hash
    finally:
        connection.close()


def _customer_by_idempotency(
    config: AppConfig, tenant_id: str, key_hash: str
) -> sqlite3.Row | None:
    connection = _connect(config)
    try:
        return connection.execute(
            "SELECT * FROM partner_customers WHERE tenant_id=? AND idempotency_key_sha256=?",
            (tenant_id, key_hash),
        ).fetchone()
    finally:
        connection.close()


def _ledger_high_water(connection: sqlite3.Connection, tenant_id: str) -> int:
    return _count(
        connection.execute(
            "SELECT COALESCE(MAX(ledger_sequence),0) FROM finops_ledger_entries WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()[0]
    )


def _transaction_now(connection: sqlite3.Connection, override: datetime | None) -> datetime:
    if override is not None:
        if override.tzinfo is None:
            raise ApiError("partner.unavailable")
        return override.astimezone(UTC).replace(microsecond=0)
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    if row is None:
        raise ApiError("partner.unavailable")
    return datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")).astimezone(UTC)


def _db_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ApiError("partner.unavailable")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _branding_hash(portal_name: str, color: str, support: str | None) -> str:
    return _sha256(_canonical_json([portal_name, color, support]).encode("utf-8"))


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise ApiError("partner.unavailable")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _text(value)


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 9_223_372_036_854_775_807:
        raise ApiError("partner.unavailable")
    return value


def _optional_count(value: object) -> int | None:
    return None if value is None else _count(value)


__all__ = [
    "Attachment",
    "DateWindow",
    "create_partner",
    "create_partner_customer",
    "generate_delivery_package",
    "get_partner_profile",
    "list_partner_customers",
    "list_partner_projects",
    "normalize_customer_id",
    "normalize_idempotency_key",
    "normalize_query",
    "normalize_revision",
    "parse_pagination",
    "parse_window",
    "update_partner_branding",
    "update_partner_customer",
]
