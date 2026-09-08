"""Atomic local Endpoint registration, safe listing, and catalog probing."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import encrypt_provider_key, secret_fingerprint, secret_prefix
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.audit import (
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext
from metahub_control.provider_health.service import probe_local_provider_catalog

from .models import (
    CreateLocalEndpointRequest,
    LocalEndpointCreateResponse,
    LocalEndpointInfo,
    LocalEndpointListResponse,
    LocalEndpointTestResponse,
    LocalEndpointTestResult,
    LocalInitialModelInfo,
)

_PROVIDER_CODE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_MODEL_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,159}$")
_LOCAL_ENDPOINT_ID = re.compile(r"^lme_[0-9a-f]{32}$")
_ENGINE_KINDS = frozenset({"openai_compatible", "vllm", "ollama"})
_AUTH_TYPES = frozenset({"none", "bearer"})
_CAPABILITIES = frozenset(
    {"chat", "embeddings", "stream", "tools", "json_mode", "vision"}
)
_MAX_PRICE = Decimal("1000000")


def list_local_endpoints(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> LocalEndpointListResponse:
    connection = _connect(config)
    try:
        rows = connection.execute(
            _LOCAL_ENDPOINT_SELECT
            + " WHERE endpoint.tenant_id=? ORDER BY endpoint.created_at DESC LIMIT 200",
            (context.user.tenant_id,),
        ).fetchall()
        return LocalEndpointListResponse(
            local_endpoints=[_endpoint_info(row) for row in rows],
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


def create_local_endpoint(
    config: AppConfig,
    context: AuthContext,
    payload: CreateLocalEndpointRequest,
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> LocalEndpointCreateResponse:
    values = _normalize_create(payload)
    intent_hash = _intent_hash(values)
    tenant_id = context.user.tenant_id
    now = _db_time(_utcnow())
    provider_id = str(uuid4())
    model_id = str(uuid4())
    local_endpoint_id = f"lme_{uuid4().hex}"
    connection = _connect(config)
    audit_record = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        receipt = connection.execute(
            """
            SELECT intent_hash,local_endpoint_id FROM local_endpoint_create_receipts
            WHERE tenant_id=? AND request_id=?
            """,
            (tenant_id, request_id),
        ).fetchone()
        if receipt is not None:
            if receipt["intent_hash"] != intent_hash:
                raise ApiError("local_endpoint.idempotency_conflict")
            endpoint = _load_endpoint(connection, tenant_id, receipt["local_endpoint_id"])
            connection.rollback()
            return LocalEndpointCreateResponse(
                local_endpoint=endpoint,
                created=False,
                request_id=request_id,
                trace_id=trace_id,
            )

        encrypted: tuple[str, str, str] | None = None
        if values["auth_type"] == "bearer":
            token = payload.bearer_token.get_secret_value() if payload.bearer_token else ""
            token = _normalize_token(token)
            try:
                encrypted = (
                    encrypt_provider_key(
                        token, encryption_secret=config.provider_key_encryption_secret,
                    ),
                    secret_fingerprint(token),
                    secret_prefix(token),
                )
            except ValueError as exc:
                raise ApiError("local_endpoint.unavailable") from exc
            finally:
                token = ""

        connection.execute(
            """
            INSERT INTO providers(
              provider_id,tenant_id,provider_code,name,base_url,status,provider_type,
              auth_type,description,maintenance_note,health_status,created_by,
              created_at,updated_at,next_probe_at
            ) VALUES(?,?,?,?,?,'active','local_endpoint',?,NULL,NULL,'unknown',?,?,?,?)
            """,
            (
                provider_id,
                tenant_id,
                values["provider_code"],
                values["name"],
                values["base_url"],
                values["auth_type"],
                context.user.user_id,
                now,
                now,
                now,
            ),
        )
        initial = values["initial_model"]
        connection.execute(
            """
            INSERT INTO models(
              model_id,tenant_id,provider_id,model_alias,upstream_model_name,
              provider_model_id,display_name,capabilities,context_window,
              input_price_per_1k,output_price_per_1k,currency,price_schema_version,
              description,status,created_by,disabled_at,disabled_by,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,'active',?,NULL,NULL,?,?)
            """,
            (
                model_id,
                tenant_id,
                provider_id,
                initial["internal_model_id"],
                initial["provider_model_id"],
                initial["provider_model_id"],
                initial["display_name"],
                json.dumps(initial["capabilities"], sort_keys=True, separators=(",", ":")),
                initial["context_window"],
                str(initial["input_price_per_1k"]),
                str(initial["output_price_per_1k"]),
                initial["currency"],
                context.user.user_id,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO local_model_endpoints(
              local_endpoint_id,tenant_id,provider_id,initial_model_id,engine_kind,
              revision,catalog_revision,catalog_status,created_by,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,1,0,'unknown',?,'active',?,?)
            """,
            (
                local_endpoint_id,
                tenant_id,
                provider_id,
                model_id,
                values["engine_kind"],
                context.user.user_id,
                now,
                now,
            ),
        )
        if encrypted is not None:
            encrypted_value, fingerprint, prefix = encrypted
            connection.execute(
                """
                INSERT INTO provider_credentials(
                  provider_credential_id,tenant_id,provider_id,key_fingerprint,encrypted_key,
                  name,key_prefix,weight,status,created_by,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,100,'active',?,?,?)
                """,
                (
                    str(uuid4()),
                    tenant_id,
                    provider_id,
                    fingerprint,
                    encrypted_value,
                    "Local Endpoint credential",
                    prefix,
                    context.user.user_id,
                    now,
                    now,
                ),
            )
        connection.execute(
            """
            INSERT INTO local_endpoint_create_receipts(
              tenant_id,request_id,intent_hash,local_endpoint_id,created_at
            ) VALUES(?,?,?,?,?)
            """,
            (tenant_id, request_id, intent_hash, local_endpoint_id, now),
        )
        endpoint = _load_endpoint(connection, tenant_id, local_endpoint_id)
        audit_record = record_audit_log(
            connection,
            context,
            action="provider.created",
            object_type="provider",
            object_id=provider_id,
            object_name=values["provider_code"],
            before_summary={},
            after_summary=_provider_audit_summary(values),
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
    except ApiError:
        connection.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise ApiError("local_endpoint.conflict") from exc
    except (DbError, sqlite3.Error) as exc:
        connection.rollback()
        raise ApiError("local_endpoint.unavailable") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    if audit_record is not None:
        dispatch_audit_recorded(
            config,
            context,
            audit_record,
            request_id=request_id,
            trace_id=trace_id,
        )
    return LocalEndpointCreateResponse(
        local_endpoint=endpoint,
        created=True,
        request_id=request_id,
        trace_id=trace_id,
    )


def test_local_endpoint(
    config: AppConfig,
    context: AuthContext,
    local_endpoint_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> LocalEndpointTestResponse:
    if not _LOCAL_ENDPOINT_ID.fullmatch(local_endpoint_id):
        raise ApiError("local_endpoint.not_found")
    tenant_id = context.user.tenant_id
    connection = _connect(config)
    try:
        owner = connection.execute(
            """
            SELECT endpoint.provider_id,endpoint.revision,endpoint.catalog_revision,
                   endpoint.initial_model_id,model.provider_model_id,provider.health_generation
            FROM local_model_endpoints endpoint
            JOIN providers provider ON provider.tenant_id=endpoint.tenant_id
              AND provider.provider_id=endpoint.provider_id
            JOIN models model ON model.tenant_id=endpoint.tenant_id
              AND model.model_id=endpoint.initial_model_id
            WHERE endpoint.tenant_id=? AND endpoint.local_endpoint_id=?
            """,
            (tenant_id, local_endpoint_id),
        ).fetchone()
        if owner is None:
            raise ApiError("local_endpoint.not_found")
        owner_snapshot = dict(owner)
    finally:
        connection.close()

    probe = probe_local_provider_catalog(
        config,
        context,
        owner_snapshot["provider_id"],
        request_id=request_id,
        trace_id=trace_id,
    )
    succeeded = probe.response.result_status == "success"
    attempt = (
        next(item for item in probe.response.attempts if item.result_status == "success")
        if succeeded
        else probe.response.attempts[0]
    )
    checked_at = attempt.checked_at.astimezone(UTC).replace(microsecond=0)
    now = _db_time(_utcnow())
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            """
            SELECT endpoint.revision,endpoint.catalog_revision,endpoint.last_discovered_at,
                   provider.health_generation,provider.probe_lease_token
            FROM local_model_endpoints endpoint
            JOIN providers provider ON provider.tenant_id=endpoint.tenant_id
              AND provider.provider_id=endpoint.provider_id
            WHERE endpoint.tenant_id=? AND endpoint.local_endpoint_id=?
            """,
            (tenant_id, local_endpoint_id),
        ).fetchone()
        if (
            current is None
            or current["revision"] != owner_snapshot["revision"]
            or current["health_generation"] != owner_snapshot["health_generation"]
            or current["probe_lease_token"] is not None
        ):
            raise ApiError("provider_health.probe_superseded")
        if succeeded:
            catalog_revision = int(current["catalog_revision"]) + 1
            connection.execute(
                """
                UPDATE local_model_endpoints
                SET catalog_revision=?,catalog_status='ready',last_discovered_at=?,
                    last_catalog_error_code=NULL,updated_at=?
                WHERE tenant_id=? AND local_endpoint_id=? AND revision=?
                """,
                (
                    catalog_revision,
                    _db_time(checked_at),
                    now,
                    tenant_id,
                    local_endpoint_id,
                    owner_snapshot["revision"],
                ),
            )
            _replace_catalog_projection(
                connection,
                tenant_id=tenant_id,
                local_endpoint_id=local_endpoint_id,
                model_ids=probe.model_ids,
                revision=catalog_revision,
                initial_model_id=owner_snapshot["initial_model_id"],
                initial_provider_model_id=owner_snapshot["provider_model_id"],
                now=now,
            )
            error_code = None
        else:
            catalog_revision = int(current["catalog_revision"])
            error_code = _catalog_error_code(attempt.error_code)
            connection.execute(
                """
                UPDATE local_model_endpoints
                SET catalog_status='error',last_catalog_error_code=?,updated_at=?
                WHERE tenant_id=? AND local_endpoint_id=? AND revision=?
                """,
                (
                    error_code,
                    now,
                    tenant_id,
                    local_endpoint_id,
                    owner_snapshot["revision"],
                ),
            )
        endpoint = _load_endpoint(connection, tenant_id, local_endpoint_id)
        connection.commit()
    except ApiError:
        connection.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise ApiError("provider_health.probe_superseded") from exc
    except (DbError, sqlite3.Error) as exc:
        connection.rollback()
        raise ApiError("local_endpoint.unavailable") from exc
    finally:
        connection.close()

    return LocalEndpointTestResponse(
        local_endpoint=endpoint,
        test_result=LocalEndpointTestResult(
            status="success" if succeeded else "failed",
            http_status=attempt.http_status,
            latency_ms=attempt.latency_ms,
            error_code=error_code,
            discovered_model_count=(
                len(probe.model_ids) if succeeded else endpoint.discovered_model_count
            ),
            catalog_truncated=probe.catalog_truncated if succeeded else False,
            checked_at=checked_at,
        ),
        request_id=request_id,
        trace_id=trace_id,
    )


def _replace_catalog_projection(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    local_endpoint_id: str,
    model_ids: tuple[str, ...],
    revision: int,
    initial_model_id: str,
    initial_provider_model_id: str,
    now: str,
) -> None:
    discovered = set(model_ids)
    rows = connection.execute(
        """
        SELECT local_model_catalog_entry_id,provider_model_id
        FROM local_model_catalog_entries
        WHERE tenant_id=? AND local_endpoint_id=?
        """,
        (tenant_id, local_endpoint_id),
    ).fetchall()
    existing = {row["provider_model_id"]: row for row in rows}
    for provider_model_id in sorted(discovered):
        linked_model_id = (
            initial_model_id if provider_model_id == initial_provider_model_id else None
        )
        if provider_model_id in existing:
            connection.execute(
                """
                UPDATE local_model_catalog_entries
                SET availability_status='available',last_seen_revision=?,
                    linked_model_id=COALESCE(linked_model_id,?),updated_at=?
                WHERE tenant_id=? AND local_endpoint_id=? AND provider_model_id=?
                """,
                (
                    revision,
                    linked_model_id,
                    now,
                    tenant_id,
                    local_endpoint_id,
                    provider_model_id,
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO local_model_catalog_entries(
                  local_model_catalog_entry_id,tenant_id,local_endpoint_id,provider_model_id,
                  availability_status,first_seen_revision,last_seen_revision,linked_model_id,
                  created_at,updated_at
                ) VALUES(?,?,?,?,'available',?,?,?,?,?)
                """,
                (
                    f"lmcat_{uuid4().hex}",
                    tenant_id,
                    local_endpoint_id,
                    provider_model_id,
                    revision,
                    revision,
                    linked_model_id,
                    now,
                    now,
                ),
            )
    for provider_model_id in sorted(set(existing) - discovered):
        connection.execute(
            """
            UPDATE local_model_catalog_entries
            SET availability_status='missing',updated_at=?
            WHERE tenant_id=? AND local_endpoint_id=? AND provider_model_id=?
            """,
            (now, tenant_id, local_endpoint_id, provider_model_id),
        )


def _normalize_create(payload: CreateLocalEndpointRequest) -> dict[str, object]:
    provider_code = (payload.provider_code or "").strip().lower()
    name = _display(payload.name, 80)
    engine_kind = (payload.engine_kind or "").strip().lower()
    auth_type = (payload.auth_type or "").strip().lower()
    if not _PROVIDER_CODE.fullmatch(provider_code):
        raise ApiError("local_endpoint.request_invalid")
    if engine_kind not in _ENGINE_KINDS:
        raise ApiError("local_endpoint.engine_invalid")
    if auth_type not in _AUTH_TYPES:
        raise ApiError("local_endpoint.auth_invalid")
    has_token = payload.bearer_token is not None
    if (auth_type == "bearer") != has_token:
        raise ApiError("local_endpoint.auth_invalid")
    base_url = _base_url(payload.base_url, engine_kind)
    initial = payload.initial_model
    if initial is None:
        raise ApiError("local_endpoint.catalog_invalid")
    internal_model_id = _model_code(initial.internal_model_id)
    provider_model_id = _model_code(initial.provider_model_id)
    display_name = _display(initial.display_name, 160)
    capabilities = _capabilities(initial.capabilities)
    if type(initial.context_window) is not int or not 1 <= initial.context_window <= 10_000_000:
        raise ApiError("local_endpoint.catalog_invalid")
    input_price = _price(initial.input_price_per_1k)
    output_price = _price(initial.output_price_per_1k)
    if initial.currency != "CNY":
        raise ApiError("local_endpoint.catalog_invalid")
    return {
        "provider_code": provider_code,
        "name": name,
        "engine_kind": engine_kind,
        "base_url": base_url,
        "auth_type": auth_type,
        "initial_model": {
            "internal_model_id": internal_model_id,
            "provider_model_id": provider_model_id,
            "display_name": display_name,
            "capabilities": capabilities,
            "context_window": initial.context_window,
            "input_price_per_1k": input_price,
            "output_price_per_1k": output_price,
            "currency": "CNY",
        },
    }


def _base_url(value: str | None, engine_kind: str) -> str:
    base_url = (value or "").strip().rstrip("/")
    if len(base_url) > 2048 or any(ord(char) < 33 or ord(char) == 127 for char in base_url):
        raise ApiError("local_endpoint.url_invalid")
    parsed = urlsplit(base_url)
    try:
        port = parsed.port
    except ValueError:
        raise ApiError("local_endpoint.url_invalid") from None
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
        or (path and not path.endswith("/v1"))
        or (engine_kind == "ollama" and not path.endswith("/v1"))
    ):
        raise ApiError("local_endpoint.url_invalid")
    return base_url


def _model_code(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if not _MODEL_CODE.fullmatch(normalized):
        raise ApiError("local_endpoint.catalog_invalid")
    return normalized


def _display(value: str | None, maximum: int) -> str:
    normalized = (value or "").strip()
    if not normalized or len(normalized) > maximum or any(
        ord(char) < 32 or 127 <= ord(char) <= 159 for char in normalized
    ):
        raise ApiError("local_endpoint.request_invalid")
    return normalized


def _capabilities(value: dict[str, object] | None) -> dict[str, bool]:
    if (
        not isinstance(value, dict)
        or not value
        or any(key not in _CAPABILITIES or enabled is not True for key, enabled in value.items())
    ):
        raise ApiError("local_endpoint.catalog_invalid")
    return {key: True for key in sorted(value)}


def _price(value: Decimal | None) -> Decimal:
    try:
        price = Decimal(value) if value is not None else Decimal("NaN")
    except (InvalidOperation, TypeError, ValueError):
        raise ApiError("local_endpoint.catalog_invalid") from None
    if (
        not price.is_finite()
        or price < 0
        or price > _MAX_PRICE
        or max(-price.as_tuple().exponent, 0) > 6
    ):
        raise ApiError("local_endpoint.catalog_invalid")
    return price


def _normalize_token(value: str) -> str:
    if not value or len(value) > 8192 or "\x00" in value or "\r" in value or "\n" in value:
        raise ApiError("local_endpoint.auth_invalid")
    return value


def _intent_hash(values: dict[str, object]) -> str:
    def convert(value: object) -> object:
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, dict):
            return {key: convert(item) for key, item in sorted(value.items())}
        return value

    canonical = json.dumps(
        convert(values),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _provider_audit_summary(values: dict[str, object]) -> dict[str, object]:
    return {
        "name_hash": hashlib.sha256(str(values["name"]).encode("utf-8")).hexdigest(),
        "provider_code": values["provider_code"],
        "provider_type": "local_endpoint",
        "base_url_hash": hashlib.sha256(str(values["base_url"]).encode("utf-8")).hexdigest(),
        "auth_type": values["auth_type"],
        "description_hash": None,
        "maintenance_note_hash": None,
        "status": "active",
    }


def _load_endpoint(
    connection: sqlite3.Connection,
    tenant_id: str,
    local_endpoint_id: str,
) -> LocalEndpointInfo:
    row = connection.execute(
        _LOCAL_ENDPOINT_SELECT
        + " WHERE endpoint.tenant_id=? AND endpoint.local_endpoint_id=?",
        (tenant_id, local_endpoint_id),
    ).fetchone()
    if row is None:
        raise ApiError("local_endpoint.not_found")
    return _endpoint_info(row)


def _endpoint_info(row: sqlite3.Row) -> LocalEndpointInfo:
    return LocalEndpointInfo(
        local_endpoint_id=row["local_endpoint_id"],
        tenant_id=row["tenant_id"],
        provider_id=row["provider_id"],
        provider_code=row["provider_code"],
        name=row["name"],
        engine_kind=row["engine_kind"],
        endpoint_hint=_endpoint_hint(row["base_url"]),
        auth_type=row["auth_type"],
        status=row["endpoint_status"],
        health_status=row["health_status"],
        last_tested_at=_parse_time(row["last_tested_at"]),
        revision=int(row["revision"]),
        catalog_revision=int(row["catalog_revision"]),
        catalog_status=row["catalog_status"],
        last_discovered_at=_parse_time(row["last_discovered_at"]),
        discovered_model_count=int(row["discovered_model_count"]),
        credential_configured=bool(row["credential_configured"]),
        initial_model=LocalInitialModelInfo(
            model_id=row["initial_model_id"],
            internal_model_id=row["internal_model_id"],
            provider_model_id=row["provider_model_id"],
            display_name=row["display_name"],
            capabilities=_parse_capabilities(row["capabilities"]),
            context_window=int(row["context_window"]),
            input_price_per_1k=float(row["input_price_per_1k"]),
            output_price_per_1k=float(row["output_price_per_1k"]),
            currency=row["currency"],
            status=row["model_status"],
        ),
    )


def _endpoint_hint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    host = parsed.hostname or ""
    category = "private"
    try:
        address = ipaddress.ip_address(host)
        if address.is_loopback:
            category = "loopback"
        elif address.is_private:
            category = "private"
        else:
            category = "public"
    except ValueError:
        if "." in host and not host.endswith((".local", ".internal", ".lan")):
            category = "public"
    return f"{parsed.scheme}://[{category}]/v1"


def _parse_capabilities(value: str) -> dict[str, bool]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        raise ApiError("local_endpoint.unavailable") from None
    if not isinstance(parsed, dict) or any(
        key not in _CAPABILITIES or enabled is not True for key, enabled in parsed.items()
    ):
        raise ApiError("local_endpoint.unavailable")
    return {key: True for key in sorted(parsed)}


def _catalog_error_code(value: str | None) -> str:
    return (
        "local_endpoint.catalog_invalid"
        if value in {"response_invalid", "catalog_invalid"}
        else "local_endpoint.unavailable"
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError as exc:
        raise ApiError("local_endpoint.unavailable") from exc


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


_LOCAL_ENDPOINT_SELECT = """
SELECT endpoint.local_endpoint_id,endpoint.tenant_id,endpoint.provider_id,
       endpoint.engine_kind,endpoint.revision,endpoint.catalog_revision,
       endpoint.catalog_status,endpoint.last_discovered_at,
       endpoint.status AS endpoint_status,
       provider.provider_code,provider.name,provider.base_url,provider.auth_type,
       provider.health_status,provider.last_tested_at,
       model.model_id AS initial_model_id,model.model_alias AS internal_model_id,
       model.provider_model_id,model.display_name,model.capabilities,model.context_window,
       model.input_price_per_1k,model.output_price_per_1k,model.currency,
       model.status AS model_status,
       (SELECT COUNT(*) FROM local_model_catalog_entries catalog
        WHERE catalog.tenant_id=endpoint.tenant_id
          AND catalog.local_endpoint_id=endpoint.local_endpoint_id
          AND catalog.availability_status='available') AS discovered_model_count,
       EXISTS(SELECT 1 FROM provider_credentials credential
        WHERE credential.tenant_id=endpoint.tenant_id
          AND credential.provider_id=endpoint.provider_id
          AND credential.status='active') AS credential_configured
FROM local_model_endpoints endpoint
JOIN providers provider ON provider.tenant_id=endpoint.tenant_id
  AND provider.provider_id=endpoint.provider_id
JOIN models model ON model.tenant_id=endpoint.tenant_id
  AND model.model_id=endpoint.initial_model_id
"""
