"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/models/service.py
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from metahub_control.auth import AuthContext
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.routing_policy import (
    MAX_PRICE_DECIMAL_PLACES,
    MAX_PRICE_PER_1K,
)

from .models import CreateModelRequest, ModelCatalogInfo, ModelListResponse, ModelResponse, UpdateModelRequest

logger = logging.getLogger("metahub.models")

# 模型目录的状态、币种、能力和长度上限集中定义，防止无效模型元数据进入路由链路。
VALID_STATUSES = {"active", "disabled"}
VALID_CURRENCIES = {"CNY", "USD"}
VALID_CAPABILITIES = {"chat", "embeddings", "stream", "tools", "json_mode", "vision"}
INTERNAL_MODEL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
MAX_PROVIDER_MODEL_ID_LENGTH = 160
MAX_DISPLAY_NAME_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 240
MAX_CONTEXT_WINDOW = 10_000_000


def list_models(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    provider_id: str | None,
    capability: str | None,
    request_id: str,
    trace_id: str,
) -> ModelListResponse:
    """列出当前租户模型目录。

    这里的 internal_model_id 是调用方在 OpenAI-compatible `model` 字段中使用的
    标准模型 ID；数据库技术主键仍保留为 model_id，避免破坏历史日志外键。
    """

    normalized_status = _normalize_optional_status(status)
    normalized_capability = _normalize_optional_capability(capability)
    params: list[str] = [context.user.tenant_id]
    filters: list[str] = []
    if normalized_status:
        filters.append("m.status=?")
        params.append(normalized_status)
    # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
    # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
    if provider_id:
        filters.append("m.provider_id=?")
        params.append(provider_id)
    where_extra = f" AND {' AND '.join(filters)}" if filters else ""
    connection = _connect(config)
    try:
        rows = connection.execute(
            f"""
            SELECT
              m.model_id, m.tenant_id, m.provider_id, m.model_alias, m.upstream_model_name,
              m.provider_model_id, m.display_name, m.capabilities, m.context_window,
              m.input_price_per_1k, m.output_price_per_1k, m.cache_price_per_1k, m.currency, m.price_schema_version,
              m.status, m.description, m.created_at, m.updated_at, m.disabled_at,
              p.provider_code, p.name AS provider_name
            FROM models m
            JOIN providers p ON p.provider_id = m.provider_id AND p.tenant_id = m.tenant_id
            WHERE m.tenant_id=? {where_extra}
            ORDER BY m.created_at DESC, m.model_alias ASC
            """,
            params,
        ).fetchall()
        models = [_model_info(row) for row in rows]
        if normalized_capability:
            models = [
                model
                for model in models
                if model.capabilities.get(normalized_capability) is True
            ]
        return ModelListResponse(models=models, request_id=request_id, trace_id=trace_id)
    finally:
        connection.close()


def create_model(
    config: AppConfig,
    context: AuthContext,
    payload: CreateModelRequest,
    *,
    request_id: str,
    trace_id: str,
) -> ModelResponse:
    """创建 模型目录 资源，负责输入归一化、唯一性校验和审计所需追踪字段。
    
    安全：写入数据必须绑定认证上下文中的 tenant_id，不能接受客户端伪造租户。
    """


    internal_model_id = _normalize_internal_model_id(payload.internal_model_id)
    provider_id = _normalize_provider_id(payload.provider_id)
    provider_model_id = _normalize_provider_model_id(payload.provider_model_id)
    display_name = _normalize_display_name(payload.display_name)
    capabilities = _normalize_capabilities(payload.capabilities)
    context_window = _normalize_context_window(payload.context_window)
    input_price = _normalize_price(payload.input_price_per_1k)
    output_price = _normalize_price(payload.output_price_per_1k)
    cache_price = _normalize_price(
        payload.cache_price_per_1k
        if payload.cache_price_per_1k is not None
        else payload.input_price_per_1k
    )
    currency = _normalize_currency(payload.currency)
    price_schema_version = _normalize_optional_text(payload.price_schema_version, 80, "model.create_invalid")
    description = _normalize_optional_text(payload.description, MAX_DESCRIPTION_LENGTH, "model.create_invalid")
    status = _normalize_status(payload.status)
    model_id = str(uuid4())
    now = _to_db_time(_utcnow())
    if price_schema_version is None and _price_fields_supplied(payload):
        price_schema_version = _manual_price_version(now)
    disabled_at = now if status == "disabled" else None
    disabled_by = context.user.user_id if status == "disabled" else None
    with ResourceWriteTransaction(_connect(config), config, context, "model.write") as transaction:
        connection = transaction.connection
        context = transaction.context
        _require_provider(connection, context.user.tenant_id, provider_id)
        try:
            connection.execute(
                """
                INSERT INTO models(
                  model_id, tenant_id, provider_id, model_alias, upstream_model_name, provider_model_id,
                  display_name, capabilities, context_window, input_price_per_1k, output_price_per_1k, cache_price_per_1k,
                  currency, price_schema_version, description, status, created_by, disabled_at, disabled_by,
                  created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model_id,
                    context.user.tenant_id,
                    provider_id,
                    internal_model_id,
                    provider_model_id,
                    provider_model_id,
                    display_name,
                    json.dumps(capabilities, ensure_ascii=False, sort_keys=True),
                    context_window,
                    input_price,
                    output_price,
                    cache_price,
                    currency,
                    price_schema_version,
                    description,
                    status,
                    context.user.user_id,
                    disabled_at,
                    disabled_by,
                    now,
                    now,
                ),
            )
            transaction.commit()
        # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            # 租户边界必须来自认证上下文或已校验记录，不能相信客户端传入的 tenant_id。
            if "models.tenant_id, models.model_alias" in str(exc) or "UNIQUE" in str(exc):
                raise ApiError("model.internal_id_exists") from exc
            raise ApiError("model.create_invalid") from exc
        model = _load_model(connection, context.user.tenant_id, model_id)
        _log_updated(context, model, request_id, trace_id, action="created", changed_fields=list(_create_field_names()))
        return ModelResponse(model=model, request_id=request_id, trace_id=trace_id)


def update_model(
    config: AppConfig,
    context: AuthContext,
    model_id: str,
    payload: UpdateModelRequest,
    *,
    request_id: str,
    trace_id: str,
) -> ModelResponse:
    """更新 模型目录 资源，区分未传字段和显式清空，避免覆盖不相关配置。
    
    边界：只允许修改该资源暴露的业务字段，敏感字段通过专门流程处理。
    """


    with ResourceWriteTransaction(_connect(config), config, context, "model.write") as transaction:
        connection = transaction.connection
        context = transaction.context
        before = _load_model(connection, context.user.tenant_id, model_id)
        supplied_fields = _supplied_fields(payload)
        updates: dict[str, object | None] = {}
        price_fields_changed = bool(
            supplied_fields
            & {
                "input_price_per_1k",
                "output_price_per_1k",
                "cache_price_per_1k",
                "currency",
            }
        )

        # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
        # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
        if "provider_id" in supplied_fields:
            provider_id = _normalize_provider_id(payload.provider_id)
            _require_provider(connection, context.user.tenant_id, provider_id)
            updates["provider_id"] = provider_id
        # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
        # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
        if "provider_model_id" in supplied_fields:
            provider_model_id = _normalize_provider_model_id(payload.provider_model_id)
            updates["provider_model_id"] = provider_model_id
            # M0 继续维护旧字段，确保 PRD-009 已验证的数据面转发路径不被破坏。
            updates["upstream_model_name"] = provider_model_id
        if "display_name" in supplied_fields:
            updates["display_name"] = _normalize_display_name(payload.display_name)
        if "capabilities" in supplied_fields:
            updates["capabilities"] = json.dumps(
                _normalize_capabilities(payload.capabilities),
                ensure_ascii=False,
                sort_keys=True,
            )
        if "context_window" in supplied_fields:
            updates["context_window"] = _normalize_context_window(payload.context_window)
        if "input_price_per_1k" in supplied_fields:
            updates["input_price_per_1k"] = _normalize_price(payload.input_price_per_1k)
        if "output_price_per_1k" in supplied_fields:
            updates["output_price_per_1k"] = _normalize_price(payload.output_price_per_1k)
        if "cache_price_per_1k" in supplied_fields:
            updates["cache_price_per_1k"] = _normalize_price(payload.cache_price_per_1k)
        # 成本与币种按模型目录口径处理，避免不同币种或估算值被混成一类。
        if "currency" in supplied_fields:
            updates["currency"] = _normalize_currency(payload.currency)
        if "price_schema_version" in supplied_fields:
            updates["price_schema_version"] = _normalize_optional_text(
                payload.price_schema_version,
                80,
                "model.create_invalid",
            )
        if "description" in supplied_fields:
            updates["description"] = _normalize_optional_text(payload.description, MAX_DESCRIPTION_LENGTH, "model.create_invalid")
        if "status" in supplied_fields:
            updates["status"] = _normalize_status(payload.status)
        if not updates:
            # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
            raise ApiError("model.update_empty")

        now = _to_db_time(_utcnow())
        if price_fields_changed and "price_schema_version" not in supplied_fields:
            updates["price_schema_version"] = _manual_price_version(now)
        set_clauses = [f"{column}=?" for column in updates]
        values = list(updates.values())
        disabled_event = before.status != "disabled" and updates.get("status") == "disabled"
        reactivated = before.status == "disabled" and updates.get("status") == "active"
        if disabled_event:
            set_clauses.extend(["disabled_at=?", "disabled_by=?"])
            values.extend([now, context.user.user_id])
        elif reactivated:
            set_clauses.extend(["disabled_at=NULL", "disabled_by=NULL"])
        set_clauses.append("updated_at=?")
        values.append(now)
        values.extend([context.user.tenant_id, model_id])
        connection.execute(
            f"""
            UPDATE models
            SET {', '.join(set_clauses)}
            WHERE tenant_id=? AND model_id=?
            """,
            values,
        )
        transaction.commit()
        model = _load_model(connection, context.user.tenant_id, model_id)
        _log_updated(context, model, request_id, trace_id, action="updated", changed_fields=sorted(updates))
        return ModelResponse(model=model, request_id=request_id, trace_id=trace_id)


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 模型目录 所需的数据库连接，并把基础设施异常映射到统一错误模型。
    
    边界：调用方只看到业务错误码，不直接暴露数据库路径、SQL 或连接细节。
    """


    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _load_model(connection: sqlite3.Connection, tenant_id: str, model_id: str) -> ModelCatalogInfo:
    """按当前租户加载 模型目录 的 load model 目标对象，供后续写入或详情响应复用。
    
    安全：查询必须带 tenant_id 或可信上下文，避免跨租户读取。
    """


    row = connection.execute(
        """
        SELECT
          m.model_id, m.tenant_id, m.provider_id, m.model_alias, m.upstream_model_name,
          m.provider_model_id, m.display_name, m.capabilities, m.context_window,
          m.input_price_per_1k, m.output_price_per_1k, m.cache_price_per_1k, m.currency, m.price_schema_version,
          m.status, m.description, m.created_at, m.updated_at, m.disabled_at,
          p.provider_code, p.name AS provider_name
        FROM models m
        JOIN providers p ON p.provider_id = m.provider_id AND p.tenant_id = m.tenant_id
        WHERE m.tenant_id=? AND m.model_id=?
        """,
        (tenant_id, model_id),
    ).fetchone()
    if not row:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model.not_found")
    return _model_info(row)


def _require_provider(connection: sqlite3.Connection, tenant_id: str, provider_id: str) -> None:
    """按当前租户加载 模型目录 的 require provider 目标对象，供后续写入或详情响应复用。
    
    安全：查询必须带 tenant_id 或可信上下文，避免跨租户读取。
    """


    row = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? AND provider_id=?",
        (tenant_id, provider_id),
    ).fetchone()
    if not row:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model.provider_invalid")


def _model_info(row: sqlite3.Row) -> ModelCatalogInfo:
    """在 模型目录 内部复用 model info 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    provider_model_id = row["provider_model_id"] or row["upstream_model_name"]
    return ModelCatalogInfo(
        model_id=row["model_id"],
        tenant_id=row["tenant_id"],
        internal_model_id=row["model_alias"],
        provider_id=row["provider_id"],
        provider_code=row["provider_code"],
        provider_name=row["provider_name"],
        provider_model_id=provider_model_id,
        display_name=row["display_name"],
        capabilities=_parse_capabilities(row["capabilities"]),
        context_window=int(row["context_window"] or 0),
        input_price_per_1k=float(row["input_price_per_1k"] or 0),
        output_price_per_1k=float(row["output_price_per_1k"] or 0),
        cache_price_per_1k=float(row["cache_price_per_1k"] or 0),
        currency=row["currency"],
        price_schema_version=row["price_schema_version"],
        status=row["status"],
        description=row["description"],
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
        disabled_at=_parse_optional_db_time(row["disabled_at"]),
    )


def _normalize_internal_model_id(value: str | None) -> str:
    """规范化 模型目录 的 normalize internal model id 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    internal_model_id = (value or "").strip().lower()
    # 模型路由结果决定上游调用，异常时必须用统一错误码停止。
    if not INTERNAL_MODEL_ID_PATTERN.match(internal_model_id):
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model.internal_id_invalid")
    return internal_model_id


def _normalize_provider_id(value: str | None) -> str:
    """规范化 模型目录 的 normalize provider id 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    provider_id = (value or "").strip()
    # Provider 判断会影响上游归因和密钥健康，错误路径必须保持脱敏。
    if not provider_id or len(provider_id) > 80:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model.provider_invalid")
    return provider_id


def _normalize_provider_model_id(value: str | None) -> str:
    """规范化 模型目录 的 normalize provider model id 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    provider_model_id = (value or "").strip()
    # 模型路由结果决定上游调用，异常时必须用统一错误码停止。
    if not provider_model_id or len(provider_model_id) > MAX_PROVIDER_MODEL_ID_LENGTH:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model.provider_model_id_invalid")
    return provider_model_id


def _normalize_display_name(value: str | None) -> str | None:
    """规范化 模型目录 的 normalize display name 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None:
        return None
    display_name = value.strip()
    if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model.create_invalid")
    return display_name or None


def _normalize_capabilities(value: dict[str, object] | None) -> dict[str, bool]:
    """规范化 模型目录 的 normalize capabilities 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None:
        return {"chat": True}
    if not isinstance(value, dict):
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model.capabilities_invalid")
    normalized: dict[str, bool] = {}
    for key, enabled in value.items():
        normalized_key = str(key).strip().lower()
        if normalized_key not in VALID_CAPABILITIES or not isinstance(enabled, bool):
            # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
            raise ApiError("model.capabilities_invalid")
        normalized[normalized_key] = enabled
    return normalized


def _normalize_optional_capability(value: str | None) -> str | None:
    """规范化 模型目录 的 normalize optional capability 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None or value == "":
        return None
    capability = value.strip().lower()
    if capability not in VALID_CAPABILITIES:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model.capabilities_invalid")
    return capability


def _normalize_context_window(value: int | None) -> int:
    """规范化 模型目录 的 normalize context window 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    try:
        context_window = int(value if value is not None else 0)
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except (TypeError, ValueError):
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model.context_window_invalid") from None
    if context_window < 0 or context_window > MAX_CONTEXT_WINDOW:
        raise ApiError("model.context_window_invalid")
    return context_window


def _normalize_price(value: float | None) -> float:
    """按共享价格合同校验模型目录单价并返回兼容浮点值。

    参数：``value`` 是请求 DTO 已解析的每百万 Token 单价，空值沿用历史零价默认值。
    返回：可写入现有模型目录 NUMERIC 字段的非负有限浮点数。
    约束：上限和小数位必须与成本路由、PricePlan 完全一致；目录入口不能写入一个
    随后会让路由或 Usage 失败的价格事实，非法值统一返回 ``model.price_invalid``。
    """

    try:
        price = Decimal(str(value if value is not None else 0))
    # Decimal 内部解析信息不得穿透模型 API；调用方只看到稳定业务错误码。
    except (InvalidOperation, TypeError, ValueError):
        raise ApiError("model.price_invalid") from None
    if not price.is_finite():
        # NaN 和 Infinity 虽能构造成 Decimal，但不能成为可审计目录单价。
        raise ApiError("model.price_invalid")
    decimal_places = max(-price.as_tuple().exponent, 0)
    if (
        price < 0
        or price > MAX_PRICE_PER_1K
        or decimal_places > MAX_PRICE_DECIMAL_PLACES
    ):
        # 共享上限保护在线路由与成本量化，不能由旧目录入口绕过。
        raise ApiError("model.price_invalid")
    return float(price)


def _normalize_currency(value: str | None) -> str:
    """规范化 模型目录 的 normalize currency 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    currency = (value or "CNY").strip().upper()
    # 成本与币种按模型目录口径处理，避免不同币种或估算值被混成一类。
    if currency not in VALID_CURRENCIES:
        raise ApiError("model.currency_invalid")
    return currency


def _normalize_status(value: str | None) -> str:
    """规范化 模型目录 的 normalize status 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    status = (value or "active").strip().lower()
    if status not in VALID_STATUSES:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model.status_invalid")
    return status


def _normalize_optional_status(value: str | None) -> str | None:
    """规范化 模型目录 的 normalize optional status 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None or value == "":
        return None
    return _normalize_status(value)


def _normalize_optional_text(value: str | None, max_length: int, error_code: str) -> str | None:
    """规范化 模型目录 的 normalize optional text 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None:
        return None
    text = value.strip()
    if len(text) > max_length:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError(error_code)
    return text or None


def _parse_capabilities(raw: object) -> dict[str, bool]:
    """解析 模型目录 的 parse capabilities 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """


    try:
        parsed = json.loads(str(raw or "{}"))
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): bool(value) for key, value in parsed.items() if isinstance(value, bool)}


def _parse_optional_db_time(value: object) -> datetime | None:
    """解析 模型目录 的 parse optional db time 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """


    if not value:
        return None
    return _parse_db_time(value)


def _parse_db_time(value: object) -> datetime:
    """解析 模型目录 的 parse db time 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """


    raw = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 模型目录 的 UTC 时间序列化为数据库 ISO 字符串。
    
    边界：统一时间口径可避免分页、审计和测试在时区上产生歧义。
    """


    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回 模型目录 使用的当前 UTC 时间，去掉微秒以保持数据库字段稳定。
    
    边界：所有状态更新时间共用该口径，避免同一事务内出现混合时间格式。
    """


    return datetime.now(UTC).replace(microsecond=0)


def _supplied_fields(payload: UpdateModelRequest) -> set[str]:
    """在 模型目录 内部复用 supplied fields 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    fields = getattr(payload, "model_fields_set", None)
    if fields is None:
        fields = getattr(payload, "__fields_set__", set())
    return set(fields)


def _price_fields_supplied(payload: CreateModelRequest) -> bool:
    """Return whether creation explicitly included any price input.

    A zero price is still a deliberate free price, while an omitted price block
    remains unpriced until an administrator configures it.
    """

    fields = getattr(payload, "model_fields_set", None)
    if fields is None:
        fields = getattr(payload, "__fields_set__", set())
    return bool(
        set(fields)
        & {"input_price_per_1k", "output_price_per_1k", "cache_price_per_1k"}
    )


def _manual_price_version(now: str) -> str:
    """Create an opaque, stable version label for a manually entered price."""

    return f"manual-{now}"


def _create_field_names() -> tuple[str, ...]:
    """在 模型目录 内部复用 create field names 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    return (
        "internal_model_id",
        "provider_id",
        "provider_model_id",
        "display_name",
        "capabilities",
        "context_window",
        "input_price_per_1k",
        "output_price_per_1k",
        "cache_price_per_1k",
        "currency",
        "price_schema_version",
        "description",
        "status",
    )


def _log_updated(
    context: AuthContext,
    model: ModelCatalogInfo,
    request_id: str,
    trace_id: str,
    *,
    action: str,
    changed_fields: list[str],
) -> None:
    """在 模型目录 内部复用 log updated 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    logger.info(
        "model.updated",
        extra={
            "event": "model.updated",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": model.model_id,
            "payload": {
                "action": action,
                "internal_model_id": model.internal_model_id,
                "provider_id": model.provider_id,
                "provider_model_id": model.provider_model_id,
                "status": model.status,
                "changed_fields": changed_fields,
            },
        },
    )
