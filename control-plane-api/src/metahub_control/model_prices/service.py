"""模块职责：实现模型价格计划创建、有效状态推导和安全事件分发。

维护边界：全部读写必须带 tenant_id；金额使用 Decimal；价格事件不得包含 Key、Token、Prompt 或响应正文。
来源文件：control-plane-api/src/metahub_control/model_prices/service.py
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
from metahub_control.webhooks.service import dispatch_webhook_event
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

from .models import (
    CreateModelPricePlanRequest,
    ModelPricePlanInfo,
    ModelPricePlanListResponse,
    ModelPricePlanResponse,
)

logger = logging.getLogger("metahub.control.model_prices")

# 价格币种、状态筛选、版本格式和精度上限共同定义人工价格写入边界。
VALID_CURRENCIES = {"CNY", "USD"}
VALID_EFFECTIVE_STATUSES = {"all", "current", "historical", "scheduled"}
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
MAX_PRICE = Decimal("1000000000")
MAX_DECIMAL_PLACES = 12


def list_model_price_plans(
    config: AppConfig,
    context: AuthContext,
    *,
    model_id: str | None,
    effective_status: str | None,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> ModelPricePlanListResponse:
    """列出当前租户价格计划，并按服务器时刻动态推导历史、当前和待生效状态。"""

    normalized_model_id = str(model_id or "").strip() or None
    normalized_status = str(effective_status or "all").strip().lower()
    if normalized_status not in VALID_EFFECTIVE_STATUSES:
        # 未知状态无法稳定解释价格时间线，必须返回统一查询错误。
        raise ApiError("model_price.query_invalid")
    connection = _connect(config)
    try:
        # 指定模型筛选时先验证当前租户归属，避免跨租户 ID 被用于价格探测。
        if normalized_model_id:
            _require_model(connection, context.user.tenant_id, normalized_model_id)
        params: list[object] = [context.user.tenant_id]
        model_filter = ""
        # 已验证的模型 ID 才能进入 SQL 条件，查询始终同时保留 tenant_id 约束。
        if normalized_model_id:
            model_filter = " AND pp.model_id=?"
            params.append(normalized_model_id)
        rows = connection.execute(
            f"""
            SELECT
              pp.*, m.model_alias AS internal_model_id, m.display_name,
              p.provider_code, u.display_name AS created_by_name
            FROM model_price_plans pp
            JOIN models m ON m.tenant_id=pp.tenant_id AND m.model_id=pp.model_id
            JOIN providers p ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
            LEFT JOIN users u ON u.tenant_id=pp.tenant_id AND u.user_id=pp.created_by
            WHERE pp.tenant_id=? {model_filter}
            ORDER BY pp.model_id ASC, pp.effective_from ASC, pp.created_at ASC
            """,
            params,
        ).fetchall()
        as_of = _utcnow()
        plans = _plan_infos(rows, as_of)
        if normalized_status != "all":
            plans = [plan for plan in plans if plan.effective_status == normalized_status]
        total = len(plans)
        return ModelPricePlanListResponse(
            price_plans=plans[offset : offset + limit],
            total=total,
            limit=limit,
            offset=offset,
            as_of=as_of,
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


def create_model_price_plan(
    config: AppConfig,
    context: AuthContext,
    payload: CreateModelPricePlanRequest,
    *,
    request_id: str,
    trace_id: str,
) -> ModelPricePlanResponse:
    """创建不可变价格计划，在同一事务写审计事件并在提交后尽力分发 Webhook。"""

    model_id = _normalize_model_id(payload.model_id)
    input_price = _normalize_price(payload.input_price_per_1k)
    output_price = _normalize_price(payload.output_price_per_1k)
    currency = _normalize_currency(payload.currency)
    effective_from = _normalize_effective_from(payload.effective_from)
    price_plan_id = f"prp_{uuid4().hex}"
    version = _normalize_version(payload.price_schema_version, effective_from, price_plan_id)
    now = _to_db_time(_utcnow())
    connection = _connect(config)
    model = _require_model(connection, context.user.tenant_id, model_id)
    event_payload = {
        "model_id": model_id,
        "internal_model_id": model["internal_model_id"],
        "input_price_per_1k": _decimal_text(input_price),
        "output_price_per_1k": _decimal_text(output_price),
        "currency": currency,
        "price_schema_version": version,
        "effective_from": effective_from,
    }
    try:
        connection.execute(
            """
            INSERT INTO model_price_plans(
              price_plan_id, tenant_id, model_id, input_price_per_1k, output_price_per_1k,
              currency, price_schema_version, effective_from, source, created_by, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?)
            """,
            (
                price_plan_id,
                context.user.tenant_id,
                model_id,
                _decimal_text(input_price),
                _decimal_text(output_price),
                currency,
                version,
                effective_from,
                context.user.user_id,
                now,
            ),
        )
        _write_price_event(
            connection,
            context,
            price_plan_id=price_plan_id,
            model_id=model_id,
            request_id=request_id,
            trace_id=trace_id,
            payload=event_payload,
            created_at=now,
        )
        _sync_model_current_price(connection, context.user.tenant_id, model_id, now)
        connection.commit()
    # 唯一约束异常必须转换成明确冲突错误，原始 SQL 文本不能穿透 API。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        message = str(exc)
        if "effective_from" in message:
            # 同一模型同一生效时刻只能有一条价格，冲突必须返回稳定 409。
            raise ApiError("model_price.effective_conflict") from exc
        if "price_schema_version" in message:
            # 模型内价格版本必须唯一，便于 Usage 和财务报表稳定追溯。
            raise ApiError("model_price.version_conflict") from exc
        # 其他约束错误统一收敛为安全价格错误，禁止暴露数据库结构细节。
        raise ApiError("model_price.price_invalid") from exc
    plan = _load_plan(connection, context.user.tenant_id, price_plan_id, _utcnow())
    _log_price_updated(context, plan, request_id, trace_id)
    _dispatch_price_webhook(config, context, plan, request_id, trace_id)
    return ModelPricePlanResponse(price_plan=plan, request_id=request_id, trace_id=trace_id)


def _plan_infos(rows: list[sqlite3.Row], as_of: datetime) -> list[ModelPricePlanInfo]:
    """按模型分组推导每条计划的结束时刻和有效状态，不依赖后台定时任务。"""

    plans: list[ModelPricePlanInfo] = []
    for index, row in enumerate(rows):
        next_row = rows[index + 1] if index + 1 < len(rows) and rows[index + 1]["model_id"] == row["model_id"] else None
        effective_from = _parse_db_time(row["effective_from"])
        effective_to = _parse_db_time(next_row["effective_from"]) if next_row else None
        if effective_from > as_of:
            status = "scheduled"
        elif effective_to is None or effective_to > as_of:
            status = "current"
        else:
            status = "historical"
        plans.append(_plan_info(row, effective_to=effective_to, effective_status=status))
    return plans


def _load_plan(
    connection: sqlite3.Connection,
    tenant_id: str,
    price_plan_id: str,
    as_of: datetime,
) -> ModelPricePlanInfo:
    """按 tenant_id 加载刚创建的价格计划，并使用完整模型时间线推导当前状态。"""

    rows = connection.execute(
        """
        SELECT
          pp.*, m.model_alias AS internal_model_id, m.display_name,
          p.provider_code, u.display_name AS created_by_name
        FROM model_price_plans pp
        JOIN models m ON m.tenant_id=pp.tenant_id AND m.model_id=pp.model_id
        JOIN providers p ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
        LEFT JOIN users u ON u.tenant_id=pp.tenant_id AND u.user_id=pp.created_by
        WHERE pp.tenant_id=? AND pp.model_id=(
          SELECT model_id FROM model_price_plans WHERE tenant_id=? AND price_plan_id=?
        )
        ORDER BY pp.effective_from ASC, pp.created_at ASC
        """,
        (tenant_id, tenant_id, price_plan_id),
    ).fetchall()
    for plan in _plan_infos(rows, as_of):
        if plan.price_plan_id == price_plan_id:
            return plan
    # 当前租户查询不到新建计划说明事务状态异常，统一按资源不存在处理。
    raise ApiError("model_price.model_not_found")


def _plan_info(row: sqlite3.Row, *, effective_to: datetime | None, effective_status: str) -> ModelPricePlanInfo:
    """把数据库价格行转换成金额无浮点损失的安全响应 DTO。"""

    return ModelPricePlanInfo(
        price_plan_id=row["price_plan_id"],
        tenant_id=row["tenant_id"],
        model_id=row["model_id"],
        internal_model_id=row["internal_model_id"],
        display_name=row["display_name"],
        provider_code=row["provider_code"],
        input_price_per_1k=_decimal_text(_decimal_value(row["input_price_per_1k"])),
        output_price_per_1k=_decimal_text(_decimal_value(row["output_price_per_1k"])),
        currency=row["currency"],
        price_schema_version=row["price_schema_version"],
        effective_from=_parse_db_time(row["effective_from"]),
        effective_to=effective_to,
        effective_status=effective_status,
        source=row["source"],
        created_by=row["created_by"],
        created_by_name=row["created_by_name"],
        created_at=_parse_db_time(row["created_at"]),
    )


def _require_model(connection: sqlite3.Connection, tenant_id: str, model_id: str) -> sqlite3.Row:
    """在可信租户边界内加载模型摘要，跨租户模型统一表现为不存在。"""

    row = connection.execute(
        """
        SELECT m.model_id, m.model_alias AS internal_model_id, m.display_name
        FROM models m
        WHERE m.tenant_id=? AND m.model_id=?
        """,
        (tenant_id, model_id),
    ).fetchone()
    if not row:
        # 不区分其他租户存在与完全不存在，避免 model_id 成为跨租户探测入口。
        raise ApiError("model_price.model_not_found")
    return row


def _sync_model_current_price(connection: sqlite3.Connection, tenant_id: str, model_id: str, as_of: str) -> None:
    """同步模型目录兼容价格缓存，未来计划不会提前覆盖当前展示价格。"""

    row = connection.execute(
        """
        SELECT input_price_per_1k, output_price_per_1k, currency, price_schema_version
        FROM model_price_plans
        WHERE tenant_id=? AND model_id=? AND effective_from<=?
        ORDER BY effective_from DESC, created_at DESC
        LIMIT 1
        """,
        (tenant_id, model_id, as_of),
    ).fetchone()
    if not row:
        # 只有未来计划时保持模型目录原价格，防止尚未生效价格提前影响旧客户端。
        return
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=?, output_price_per_1k=?, currency=?, price_schema_version=?, updated_at=?
        WHERE tenant_id=? AND model_id=?
        """,
        (
            row["input_price_per_1k"],
            row["output_price_per_1k"],
            row["currency"],
            row["price_schema_version"],
            as_of,
            tenant_id,
            model_id,
        ),
    )


def _write_price_event(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    price_plan_id: str,
    model_id: str,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
    created_at: str,
) -> None:
    """在价格计划主事务内写入 `price.updated` 安全审计事件。"""

    connection.execute(
        """
        INSERT INTO model_price_events(
          price_event_id, tenant_id, price_plan_id, model_id, event_type,
          actor_id, request_id, trace_id, payload_json, created_at
        )
        VALUES(?, ?, ?, ?, 'price.updated', ?, ?, ?, ?, ?)
        """,
        (
            f"pre_{uuid4().hex}",
            context.user.tenant_id,
            price_plan_id,
            model_id,
            context.user.user_id,
            request_id,
            trace_id,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            created_at,
        ),
    )


def _dispatch_price_webhook(
    config: AppConfig,
    context: AuthContext,
    plan: ModelPricePlanInfo,
    request_id: str,
    trace_id: str,
) -> None:
    """主事务提交后尽力投递 `price.updated`，外部失败不能回滚价格计划。"""

    event = build_webhook_event(
        tenant_id=context.user.tenant_id,
        event_type="price.updated",
        actor_id=context.user.user_id,
        object_id=plan.price_plan_id,
        trace_id=trace_id,
        payload={
            "model_id": plan.model_id,
            "price_schema_version": plan.price_schema_version,
            "currency": plan.currency,
            "effective_from": _to_db_time(plan.effective_from),
        },
    )
    try:
        dispatch_webhook_event(
            config,
            context.user.tenant_id,
            event,
            request_id=request_id,
            trace_id=trace_id,
        )
    # Webhook 是外部副作用，任何投递异常都只记录安全摘要，不能撤销已提交价格。
    except Exception:
        logger.exception(
            "model_price.webhook_dispatch_failed",
            extra={
                "event": "model_price.webhook_dispatch_failed",
                "tenant_id": context.user.tenant_id,
                "object_id": plan.price_plan_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _log_price_updated(
    context: AuthContext,
    plan: ModelPricePlanInfo,
    request_id: str,
    trace_id: str,
) -> None:
    """记录价格更新结构化事件，只包含模型、版本、币种和生效时间。"""

    logger.info(
        "price.updated",
        extra={
            "event": "price.updated",
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": plan.price_plan_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "payload": {
                "model_id": plan.model_id,
                "price_schema_version": plan.price_schema_version,
                "currency": plan.currency,
                "effective_from": _to_db_time(plan.effective_from),
            },
        },
    )


def _normalize_model_id(value: object | None) -> str:
    """规范化必填模型 ID，空值返回模型价格专用统一错误。"""

    model_id = str(value or "").strip()
    # 模型 ID 是 tenant_id 约束下的价格归属键，缺失时不能继续写入。
    if not model_id:
        # 模型是价格归属主键，缺失时不能创建无归属财务记录。
        raise ApiError("model_price.model_required")
    return model_id


def _normalize_price(value: object | None) -> Decimal:
    """把价格输入转换成有限非负 Decimal，并限制最大值与小数精度。"""

    try:
        price = Decimal(str(value))
    # 非数字输入必须收敛为稳定业务错误，不能暴露 Decimal 解析细节。
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ApiError("model_price.price_invalid") from exc
    if not price.is_finite() or price < 0 or price > MAX_PRICE or max(-price.as_tuple().exponent, 0) > MAX_DECIMAL_PLACES:
        # NaN、Infinity、负数、异常大值或超精度金额都不能进入成本账本。
        raise ApiError("model_price.price_invalid")
    return price


def _normalize_currency(value: object | None) -> str:
    """规范化原始账单币种，M0.5 仅支持 CNY 和 USD 且不做换汇。"""

    currency = str(value or "").strip().upper()
    # 币种决定成本进入 CNY 或 USD 账本列，未知值绝不能静默回退。
    if currency not in VALID_CURRENCIES:
        # 非白名单币种缺少明确账本列，不能静默归入人民币成本。
        raise ApiError("model_price.currency_invalid")
    return currency


def _normalize_effective_from(value: object | None) -> str:
    """解析必须带时区的 ISO 生效时刻，并归一为秒级 UTC 字符串。"""

    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    # 时间解析失败必须返回统一错误，不把 Python 解析异常暴露给调用方。
    except ValueError as exc:
        raise ApiError("model_price.effective_from_invalid") from exc
    if parsed.tzinfo is None:
        # 无时区时间在企业跨地域部署中存在歧义，必须要求显式时区。
        raise ApiError("model_price.effective_from_invalid")
    return _to_db_time(parsed)


def _normalize_version(value: object | None, effective_from: str, price_plan_id: str) -> str:
    """校验人工版本；未提供时根据生效时间和计划 ID 生成稳定可读版本。"""

    version = str(value or "").strip()
    if not version:
        compact_time = effective_from.replace("-", "").replace(":", "").replace("T", "-").replace("Z", "")
        return f"manual-{compact_time}-{price_plan_id[-8:]}"
    if not VERSION_PATTERN.fullmatch(version):
        # 版本会进入日志和报表，必须限制长度与字符集，避免不可读控制字符。
        raise ApiError("model_price.version_invalid")
    return version


def _decimal_value(value: object) -> Decimal:
    """把数据库 NUMERIC 值恢复成 Decimal，数据异常按统一价格错误处理。"""

    try:
        return Decimal(str(value))
    # 数据库存量异常不能以浮点或任意文本进入财务响应。
    except (InvalidOperation, ValueError) as exc:
        raise ApiError("model_price.price_invalid") from exc


def _decimal_text(value: Decimal) -> str:
    """输出无指数、无无意义尾零的十进制金额字符串，零值固定为 `0`。"""

    text = format(value, "f").rstrip("0").rstrip(".")
    return text or "0"


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建价格服务数据库连接，并把基础设施异常映射成统一 API 错误。"""

    try:
        return connect_database(config.database_url)
    # 数据库异常只能暴露统一错误码，连接路径和 SQL 细节必须留在服务端。
    except DbError as exc:
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _parse_db_time(value: object) -> datetime:
    """解析数据库 UTC 文本为带时区时间对象，供有效状态比较与 API 序列化。"""

    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _to_db_time(value: datetime) -> str:
    """把带时区时间统一序列化为秒级 UTC ISO 字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回秒级 UTC 当前时间，便于有效期判断和测试时钟替换。"""

    return datetime.now(UTC).replace(microsecond=0)
