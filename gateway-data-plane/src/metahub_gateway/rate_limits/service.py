"""数据面限流服务：构造租户、项目、应用、Virtual Key 与全局维度的原子配额请求。

批任务必须使用内部可信令牌估算，普通请求使用本地估算；限流日志仅保留 request_id 和
脱敏维度标识，不得记录密钥、Token、Prompt 或 Response。
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.routing_policy import (
    TokenEstimate,
)
from metahub_shared.runtime.routing_policy import (
    estimate_request_tokens as estimate_token_shape,
)

from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.routing.models import GatewayRoute

from .models import RateLimitHit, RateLimitRule
from .store import (
    BatchRateLimitDimension,
    RateLimitCounterStore,
    RateLimitStoreUnavailable,
    RedisRateLimitStore,
)

logger = logging.getLogger("metahub.gateway.rate_limits")

# 限流窗口和并发 TTL 集中定义，保证内存和 Redis 存储使用同一治理口径。
WINDOW_SECONDS = 60
WINDOW_TTL_SECONDS = 90
CONCURRENCY_TTL_SECONDS = 120


@dataclass
class RateLimitLease:
    """一次请求占用的并发限流租约。

    只有命中并发限制配置时才会生成 `concurrency_key`；请求结束必须释放，流式响应会在
    body 迭代器的 finally 中释放。
    """

    store: RateLimitCounterStore
    concurrency_key: str | None
    released: bool = False

    async def release(self) -> None:
        """释放并发槽位，释放失败只记 warning，不影响已经完成的请求响应。"""

        # 用量成本是审计账本字段，缺失或异常必须进入可追踪分支。
        if self.released or self.concurrency_key is None:
            return
        self.released = True
        try:
            await self.store.release_concurrency(self.concurrency_key)
        except RateLimitStoreUnavailable:
            logger.warning(
                "rate_limit.release_failed",
                extra={
                    "event": "rate_limit.release_failed",
                    "payload": {"reason": "store_unavailable"},
                },
            )


async def enforce_virtual_key_rate_limit(
    config: AppConfig,
    app_state: Any,
    auth_context: GatewayAuthContext,
    route: GatewayRoute,
    payload: dict[str, Any],
    *,
    endpoint: str,
    request_kind: str,
    request_id: str,
    trace_id: str,
    batch_lane: bool = False,
    batch_token_estimate: TokenEstimate | None = None,
) -> RateLimitLease | None:
    """执行 Virtual Key 级 RPM、TPM 和并发准入判断。

    该函数只读取安全元数据和估算 token 数；不会记录 Prompt、Response、Authorization、
    Provider Key 或 Virtual Key 明文。
    """

    rule = load_virtual_key_rate_limit_rule(
        config, auth_context.tenant_id, auth_context.virtual_key_id
    )
    if rule is None or not rule.has_limits():
        return None

    try:
        store = get_rate_limit_store(config, app_state)
    except RateLimitStoreUnavailable as exc:
        _log_rate_limit_unavailable(auth_context, route, endpoint, request_id, trace_id)
        # 已配置限流规则但计数器初始化失败时采用失败关闭，防止请求绕过治理约束。
        raise ApiError("gateway.rate_limit_unavailable") from exc
    if store is None:
        _log_rate_limit_unavailable(auth_context, route, endpoint, request_id, trace_id)
        # 存在 active 限流规则却没有 Redis/注入存储时，必须拒绝而不是静默放行。
        raise ApiError("gateway.rate_limit_unavailable")

    try:
        await store.ping()
        if batch_lane:
            # 批任务限流依赖可信令牌估算；缺失时拒绝，防止绕过租户和全局配额。
            if batch_token_estimate is None:
                # 批任务限流依赖可信令牌估算；缺失时拒绝，防止绕过租户和全局配额。
                raise ApiError("batch.execution_invalid")
            estimated_tokens = batch_token_estimate.total_tokens
            reserve_percent = getattr(config, "batch_realtime_reserve_percent", 25)
            dimensions: list[BatchRateLimitDimension] = []
            for limit_type, configured_limit, key, amount, ttl in (
                ("rpm", rule.rpm_limit, _window_key(rule, "rpm"), 1, WINDOW_TTL_SECONDS),
                (
                    "tpm",
                    rule.tpm_limit,
                    _window_key(rule, "tpm"),
                    estimated_tokens,
                    WINDOW_TTL_SECONDS,
                ),
                (
                    "concurrency",
                    rule.concurrency_limit,
                    _concurrency_key(rule),
                    1,
                    CONCURRENCY_TTL_SECONDS,
                ),
            ):
                if configured_limit is None:
                    continue
                reserve = max(1, math.ceil(configured_limit * reserve_percent / 100))
                ceiling = configured_limit - reserve
                if ceiling <= 0:
                    _raise_exceeded(
                        rule,
                        route,
                        endpoint,
                        request_id,
                        trace_id,
                        limit_type,
                        amount,
                        ceiling,
                        window_seconds=(
                            None if limit_type == "concurrency" else WINDOW_SECONDS
                        ),
                    )
                dimensions.append(
                    BatchRateLimitDimension(
                        limit_type=limit_type,
                        key=key,
                        amount=amount,
                        ceiling=ceiling,
                        ttl_seconds=ttl,
                    )
                )
            decision = await store.try_acquire_batch_limits(tuple(dimensions))
            if not decision.allowed:
                _raise_exceeded(
                    rule,
                    route,
                    endpoint,
                    request_id,
                    trace_id,
                    decision.limit_type or "rpm",
                    decision.current,
                    decision.limit,
                    window_seconds=(
                        None
                        if decision.limit_type == "concurrency"
                        else WINDOW_SECONDS
                    ),
                )
            concurrency_key = (
                _concurrency_key(rule) if rule.concurrency_limit is not None else None
            )
            return RateLimitLease(store=store, concurrency_key=concurrency_key)
        if rule.rpm_limit is not None:
            current = await store.increment_window(
                _window_key(rule, "rpm"),
                1,
                WINDOW_TTL_SECONDS,
            )
            if current > rule.rpm_limit:
                _raise_exceeded(
                    rule, route, endpoint, request_id, trace_id, "rpm", current, rule.rpm_limit
                )

        if rule.tpm_limit is not None:
            estimated_tokens = estimate_request_tokens(request_kind, payload)
            current = await store.increment_window(
                _window_key(rule, "tpm"),
                estimated_tokens,
                WINDOW_TTL_SECONDS,
            )
            if current > rule.tpm_limit:
                _raise_exceeded(
                    rule, route, endpoint, request_id, trace_id, "tpm", current, rule.tpm_limit
                )

        # 用量成本是审计账本字段，缺失或异常必须进入可追踪分支。
        if rule.concurrency_limit is not None:
            # 并发限制需要拿到租约，后续响应结束或异常退出时由 finally 释放槽位。
            concurrency_key = _concurrency_key(rule)
            allowed, current = await store.acquire_concurrency(
                concurrency_key,
                rule.concurrency_limit,
                CONCURRENCY_TTL_SECONDS,
            )
            if not allowed:
                _raise_exceeded(
                    rule,
                    route,
                    endpoint,
                    request_id,
                    trace_id,
                    "concurrency",
                    current,
                    rule.concurrency_limit,
                    window_seconds=None,
                )
            return RateLimitLease(store=store, concurrency_key=concurrency_key)
        return None
    except RateLimitStoreUnavailable as exc:
        _log_rate_limit_unavailable(auth_context, route, endpoint, request_id, trace_id)
        # Redis 在计数过程中不可用时也按失败关闭处理，避免部分计数成功造成状态不一致。
        raise ApiError("gateway.rate_limit_unavailable") from exc


def load_virtual_key_rate_limit_rule(
    config: AppConfig,
    tenant_id: str,
    virtual_key_id: str,
    *,
    now: datetime | None = None,
) -> RateLimitRule | None:
    """读取并合并 Virtual Key 的永久规则与有效临时覆盖。

    参数：部署配置、已认证 tenant_id、virtual_key_id，以及仅供内部测试注入的带时区时刻。
    返回：每个维度分别取最小值后的有效规则；所有维度均无限制时返回 None。
    约束：两类查询都必须带 tenant_id 和 virtual_key_id，且连接在成功和异常路径中关闭。
    """

    connection: sqlite3.Connection | None = None
    try:
        effective_at = _effective_time(now)
        connection = connect_database(config.database_url)
        permanent = connection.execute(
            """
            SELECT rpm_limit, tpm_limit, concurrency_limit
            FROM virtual_key_rate_limit_rules
            WHERE tenant_id=? AND virtual_key_id=? AND status='active'
            """,
            (tenant_id, virtual_key_id),
        ).fetchone()
        # 0043 允许 SQLite 可解析的带 offset 时间；必须按真实时刻比较，文本序会把
        # 等价 UTC 时刻误判为尚未生效或仍未到期。
        overrides = connection.execute(
            """
            SELECT rpm_limit, tpm_limit, concurrency_limit
            FROM temporary_rate_limit_overrides
            WHERE tenant_id=? AND virtual_key_id=? AND status='active'
              AND unixepoch(starts_at)<=unixepoch(?)
              AND unixepoch(expires_at)>unixepoch(?)
            ORDER BY temporary_rate_limit_override_id ASC
            """,
            (tenant_id, virtual_key_id, effective_at, effective_at),
        ).fetchall()
        rows = ([permanent] if permanent is not None else []) + list(overrides)
        rpm_limit = _minimum_limit(rows, "rpm_limit")
        tpm_limit = _minimum_limit(rows, "tpm_limit")
        concurrency_limit = _minimum_limit(rows, "concurrency_limit")
    # 数据库、时间或损坏限制值都统一折叠，不能把 SQL、数据库路径或覆盖详情暴露给调用方。
    except (DbError, sqlite3.Error, TypeError, ValueError) as exc:
        raise ApiError("gateway.rate_limit_unavailable") from exc
    finally:
        if connection is not None:
            connection.close()
    # 永久规则和所有有效覆盖都没有治理维度时必须返回 None；上层据此跳过计数器，
    # 避免只有已释放或已到期覆盖的 Key 因 Redis 未配置被错误地失败关闭。
    if all(value is None for value in (rpm_limit, tpm_limit, concurrency_limit)):
        return None
    return RateLimitRule(
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        rpm_limit=rpm_limit,
        tpm_limit=tpm_limit,
        concurrency_limit=concurrency_limit,
    )


def _minimum_limit(rows: list[sqlite3.Row], field: str) -> int | None:
    """返回某一限流维度的最小正整数，拒绝数据库中的损坏治理值。"""

    values: list[int] = []
    for row in rows:
        value = row[field]
        if value is None:
            continue
        # bool 虽是 int 子类但不具有治理数值语义；损坏行必须失败关闭而不是绕过限制。
        if type(value) is not int or not 1 <= value <= 100_000_000:
            raise ValueError("invalid rate limit")
        values.append(value)
    return min(values) if values else None


def _effective_time(value: datetime | None) -> str:
    """把服务端时刻规范化为数据库 UTC 秒级文本，拒绝含糊的 naive datetime。"""

    current = value if value is not None else datetime.now(UTC)
    if not isinstance(current, datetime) or current.tzinfo is None:
        # 含糊时钟会让临时覆盖提前或延后生效，因此必须失败关闭，不能按本地时区猜测。
        raise ValueError("timezone-aware datetime required")
    return current.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def get_rate_limit_store(config: AppConfig, app_state: Any) -> RateLimitCounterStore | None:
    """获取当前应用的限流计数器存储。

    测试可通过 `app.state.rate_limit_store` 注入内存实现；生产路径在配置 Redis URL 后创建
    Redis 存储。没有 Redis URL 时返回 None，由上层根据是否存在规则决定是否 fail closed。
    """

    injected = getattr(app_state, "rate_limit_store", None)
    if injected is not None:
        return injected
    if not config.redis_url:
        return None
    cached = getattr(app_state, "_metahub_rate_limit_store", None)
    if isinstance(cached, RedisRateLimitStore) and cached.redis_url == config.redis_url:
        return cached
    store = RedisRateLimitStore(config.redis_url)
    app_state._metahub_rate_limit_store = store
    return store


def estimate_request_tokens(request_kind: str, payload: dict[str, Any]) -> int:
    """估算一次请求将消耗的 token 数。

    估算只用于限流准入，不写 usage_ledger；真实 usage 和成本仍由上游响应后的账本逻辑负责。
    """

    # TPM 只取 shared 估算的总量；真实 Usage 仍由 Provider 响应后的账本权威记录。
    return estimate_token_shape(request_kind, payload).total_tokens


def _window_key(rule: RateLimitRule, limit_type: str) -> str:
    """生成固定窗口计数器键，不包含明文 Key 或请求内容。"""

    now = int(time.time())
    window_start = now - (now % WINDOW_SECONDS)
    # 租户判断决定数据隔离边界，不能跨租户复用或返回对象。
    return f"metahub:rl:{rule.tenant_id}:{rule.virtual_key_id}:{limit_type}:{window_start}"


def _concurrency_key(rule: RateLimitRule) -> str:
    """生成并发计数器键，不包含明文 Key 或请求内容。"""

    # 租户判断决定数据隔离边界，不能跨租户复用或返回对象。
    return f"metahub:rl:{rule.tenant_id}:{rule.virtual_key_id}:concurrency"


def _raise_exceeded(
    rule: RateLimitRule,
    route: GatewayRoute,
    endpoint: str,
    request_id: str,
    trace_id: str,
    limit_type: str,
    current: int,
    limit: int,
    *,
    window_seconds: int | None = WINDOW_SECONDS,
) -> None:
    """记录安全限流事件并抛出统一 429 错误。"""

    hit = RateLimitHit(
        limit_type=limit_type,
        current=current,
        limit=limit,
        window_seconds=window_seconds,
    )
    _log_rate_limit_exceeded(rule, route, endpoint, request_id, trace_id, hit)
    # 超限统一抛出 429 错误码，响应体由共享异常处理器补齐 request_id/trace_id。
    raise ApiError("gateway.rate_limit_exceeded")


def _log_rate_limit_exceeded(
    rule: RateLimitRule,
    route: GatewayRoute,
    endpoint: str,
    request_id: str,
    trace_id: str,
    hit: RateLimitHit,
) -> None:
    """记录 rate_limit.exceeded 事件，payload 只包含安全计数摘要。"""

    logger.info(
        "rate_limit.exceeded",
        extra={
            "event": "rate_limit.exceeded",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": rule.tenant_id,
            "actor_id": rule.virtual_key_id,
            "object_id": request_id,
            "payload": {
                "limit_type": hit.limit_type,
                "current": hit.current,
                "limit": hit.limit,
                "window_seconds": hit.window_seconds,
                "endpoint": endpoint,
                "model_id": route.model_id,
                "provider_id": route.provider_id,
            },
        },
    )


def _log_rate_limit_unavailable(
    auth_context: GatewayAuthContext,
    route: GatewayRoute,
    endpoint: str,
    request_id: str,
    trace_id: str,
) -> None:
    """记录限流存储不可用事件，不输出 Redis URL 或认证信息。"""

    logger.warning(
        "rate_limit.unavailable",
        extra={
            "event": "rate_limit.unavailable",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": auth_context.tenant_id,
            "actor_id": auth_context.virtual_key_id,
            "object_id": request_id,
            "payload": {
                "endpoint": endpoint,
                "model_id": route.model_id,
                "provider_id": route.provider_id,
                "reason": "store_unavailable",
            },
        },
    )
