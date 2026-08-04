"""PRD-035 Provider 健康探测、聚合状态机和查询服务。

网络 I/O 始终发生在数据库事务外。Provider Key 只在发起请求前短暂解密，任何日志、
API 响应和异常映射都只使用固定分类，不能包含 Authorization 或上游响应正文。
"""

from __future__ import annotations

import ipaddress
import hashlib
import logging
import socket
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

import httpcore
import httpx

from metahub_control.auth import AuthContext
from metahub_control.providers.service import _validate_provider_auth_combination
from metahub_control.webhooks.service import dispatch_webhook_event
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import decrypt_provider_key
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.provider_adapter_spec import (
    GENERIC_PROBE_PROFILE,
    OPENAI_PROBE_PROFILE,
    provider_adapter_specs,
    provider_types,
)
from metahub_shared.runtime.webhooks import build_webhook_event

from .models import (
    ProbeAttemptInfo,
    ProviderHealthItem,
    ProviderHealthListResponse,
    ProviderHealthSummary,
    ProviderProbeResponse,
)

logger = logging.getLogger("metahub.control.provider_health")

# 只有当前已验证为 OpenAI-compatible 的类型才能用 /models 证明认证有效。
OPENAI_COMPATIBLE_TYPES = frozenset(
    spec.provider_type
    for spec in provider_adapter_specs()
    if spec.probe_profile == OPENAI_PROBE_PROFILE
)
EFFECTIVE_STATUSES = {"healthy", "unhealthy", "observing", "unknown", "disabled"}
PROVIDER_STATUSES = {"active", "disabled"}
PROVIDER_TYPES = provider_types()


@dataclass(frozen=True)
class _Claim:
    """一次探测领取的不可变快照，用 generation 阻止旧配置结果回写。"""

    provider_id: str
    tenant_id: str
    provider_code: str
    provider_type: str
    base_url: str
    auth_type: str
    # probe_profile 由已验证 Shared spec 绑定，网络阶段不再根据 provider_type 自行猜测路径。
    probe_profile: str
    provider_status: str
    health_status: str
    health_generation: int
    consecutive_failures: int
    lease_token: str


@dataclass(frozen=True)
class _Credential:
    """探测期间使用的最小凭据快照；明文仅保存在短生命周期字段中。"""

    credential_id: str
    encrypted_key: str


@dataclass(frozen=True)
class _Attempt:
    """内部探测尝试，credential_id 仅用于数据库更新，不进入对外 DTO。"""

    credential_id: str | None
    result_status: str
    auth_result: str
    http_status: int | None
    latency_ms: int
    error_code: str | None
    summary: str
    checked_at: datetime


class _ValidatedProbeUrl(str):
    """携带已校验 DNS 结果的探测 URL，阻止 HTTP 传输层再次解析到不同地址。"""

    # 这些字段只保存主机名、端口和 IP，不包含 query、凭据或请求正文。
    hostname: str
    port: int
    addresses: tuple[str, ...]

    def __new__(
        cls,
        value: str,
        *,
        hostname: str,
        port: int,
        addresses: tuple[str, ...],
    ) -> _ValidatedProbeUrl:
        """构造兼容普通字符串调用方、同时保留连接固定信息的不可变 URL。"""

        instance = super().__new__(cls, value)
        instance.hostname = hostname.rstrip(".").lower()
        instance.port = port
        instance.addresses = addresses
        return instance


class _PinnedNetworkBackend(httpcore.SyncBackend):
    """把 httpcore 对原域名的 TCP 请求固定到安全校验阶段得到的 IP 集合。"""

    def __init__(
        self,
        target: _ValidatedProbeUrl,
        delegate: httpcore.SyncBackend | None = None,
    ) -> None:
        """保存单次探测目标；delegate 参数仅用于无网络的确定性安全测试。"""

        self._hostname = target.hostname
        self._port = target.port
        self._addresses = target.addresses
        self._delegate = delegate or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.NetworkStream:
        """只连接已验证 IP；HTTP Host 和 TLS SNI 仍由 httpcore 使用原域名生成。"""

        normalized_host = host.rstrip(".").lower()
        if normalized_host != self._hostname or port != self._port:
            # follow_redirects 已关闭，但此处仍做连接层兜底，避免未来调用方式变化扩大探测面。
            raise httpcore.ConnectError("probe destination changed")
        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in self._addresses:
            try:
                return self._delegate.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            # 单个已验证 IP 连接失败时仅尝试同一安全快照内的其余地址，不重新查询 DNS。
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        # 保留最终标准连接异常分类，让 httpx 映射为固定 connect/timeout 摘要。
        if last_error is not None:
            raise last_error
        # 空地址集合表示安全校验合同被破坏，连接层必须再次失败关闭。
        raise httpcore.ConnectError("validated probe address is unavailable")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: object = None,
    ) -> httpcore.NetworkStream:
        """Provider HTTP(S) 探测禁止降级到本机 Unix socket 或绕过目标地址校验。"""

        del path, timeout, socket_options
        # Unix socket 会把上游探测变成本机服务访问，任何调用都必须明确拒绝。
        raise httpcore.ConnectError("unix socket probe is forbidden")

    def sleep(self, seconds: float) -> None:
        """把 httpcore 的短暂等待交给标准后端，不改变重试时序。"""

        self._delegate.sleep(seconds)


class _PinnedHTTPTransport(httpx.HTTPTransport):
    """使用固定 DNS 后端的同步 HTTP 传输，保持标准证书校验与原域名 SNI。"""

    def __init__(self, target: _ValidatedProbeUrl) -> None:
        """建立仅服务单个已验证目标的连接池，禁止代理和跨目标复用。"""

        super().__init__(trust_env=False)
        default_pool = self._pool
        # httpx 0.28 没有公开 resolver 参数；受控替换其 httpcore pool 可在 TCP 层固定 IP，
        # 同时请求 URL 仍保留原域名，因此 Host、SNI 与证书主机名校验不会被削弱。
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            max_connections=1,
            max_keepalive_connections=0,
            keepalive_expiry=0,
            network_backend=_PinnedNetworkBackend(target),
        )
        default_pool.close()


def list_provider_health(
    config: AppConfig,
    context: AuthContext,
    *,
    health_status: str | None,
    provider_status: str | None,
    provider_type: str | None,
    provider_id: str | None,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> ProviderHealthListResponse:
    """查询当前租户健康快照，并按派生 effective 状态做筛选和汇总。"""

    normalized_health = health_status.strip().lower() if health_status else None
    normalized_provider_status = provider_status.strip().lower() if provider_status else None
    # 前端筛选只能使用互斥 effective 枚举，未知值不能下沉为任意 SQL 条件。
    if normalized_health and normalized_health not in EFFECTIVE_STATUSES:
        raise ApiError("provider_health.filter_invalid")
    # Provider 配置状态使用独立白名单，不能和派生健康状态混用。
    if normalized_provider_status and normalized_provider_status not in PROVIDER_STATUSES:
        raise ApiError("provider_health.filter_invalid")
    # 类型筛选复用 Provider 注册白名单，未知值不能被解释成合法的空结果。
    if provider_type and provider_type.strip() not in PROVIDER_TYPES:
        raise ApiError("provider_health.filter_invalid")
    # 分页边界在服务层再次校验，避免内部调用绕过 FastAPI Query 约束。
    if limit < 1 or limit > 100 or offset < 0 or offset > 10000:
        raise ApiError("provider_health.pagination_invalid")

    clauses = ["tenant_id=?"]
    params: list[object] = [context.user.tenant_id]
    for column, value in (
        ("status", normalized_provider_status),
        ("provider_type", provider_type.strip() if provider_type else None),
        ("provider_id", provider_id.strip() if provider_id else None),
    ):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    connection = _connect(config)
    rows = connection.execute(
        f"""
        SELECT provider_id, tenant_id, provider_code, name, provider_type, base_url,
               status, health_status, last_tested_at, last_success_at, last_failure_at,
               last_probe_latency_ms, last_probe_http_status, last_probe_error_code,
               last_test_error, consecutive_probe_failures, next_probe_at, health_updated_at
        FROM providers
        WHERE {' AND '.join(clauses)}
        ORDER BY provider_id ASC
        """,
        params,
    ).fetchall()
    items = [_health_item(row, config) for row in rows]
    if normalized_health:
        items = [item for item in items if item.effective_health_status == normalized_health]
    items.sort(
        key=lambda item: (
            {"unhealthy": 0, "observing": 1, "unknown": 2, "healthy": 3, "disabled": 4}[
                item.effective_health_status
            ],
            item.provider_id,
        )
    )
    summary = _summary(items)
    total = len(items)
    return ProviderHealthListResponse(
        provider_health=items[offset : offset + limit],
        summary=summary,
        total=total,
        limit=limit,
        offset=offset,
        request_id=request_id,
        trace_id=trace_id,
    )


def probe_provider(
    config: AppConfig,
    context: AuthContext,
    provider_id: str,
    *,
    trigger_type: str,
    request_id: str,
    trace_id: str,
) -> ProviderProbeResponse:
    """领取并探测当前租户 Provider，返回不含任何凭据标识的安全结果。"""

    # tenant_id 只取认证上下文，客户端提交的任何同名字段都不会进入服务层。
    return probe_provider_for_tenant(
        config,
        context.user.tenant_id,
        provider_id,
        trigger_type=trigger_type,
        request_id=request_id,
        trace_id=trace_id,
    )


def probe_provider_for_tenant(
    config: AppConfig,
    tenant_id: str,
    provider_id: str,
    *,
    trigger_type: str,
    request_id: str,
    trace_id: str,
) -> ProviderProbeResponse:
    """供手工入口和周期调度共用的租户级探测函数。"""

    claim, credentials = _claim_provider(config, tenant_id, provider_id, trigger_type)
    attempts = _perform_probe(config, claim, credentials)
    result_status = _aggregate_result(attempts)
    transitioned = _complete_probe(
        config,
        claim,
        attempts,
        result_status=result_status,
        trigger_type=trigger_type,
        request_id=request_id,
        trace_id=trace_id,
    )
    connection = _connect(config)
    row = _load_health_row(connection, tenant_id, provider_id)
    item = _health_item(row, config)
    if transitioned:
        _dispatch_unhealthy_event(config, claim, item, attempts, request_id, trace_id)
    return ProviderProbeResponse(
        provider_health=item,
        result_status=result_status,
        attempts=[_attempt_info(index, attempt) for index, attempt in enumerate(attempts, 1)],
        request_id=request_id,
        trace_id=trace_id,
    )


def claim_next_scheduled(config: AppConfig) -> tuple[str, str] | None:
    """读取一个到期且租约可用的 Provider 标识，实际原子领取仍在 probe 函数内完成。"""

    connection = _connect(config)
    now = _db_time(_utcnow())
    row = connection.execute(
        """
        SELECT tenant_id, provider_id
        FROM providers
        WHERE status='active' AND next_probe_at IS NOT NULL AND next_probe_at<=?
          AND (probe_lease_until IS NULL OR probe_lease_until<=?)
        ORDER BY next_probe_at ASC, provider_id ASC
        LIMIT 1
        """,
        (now, now),
    ).fetchone()
    # 空结果表示本轮没有到期任务，调度器会短暂等待而不是制造错误日志。
    return (row["tenant_id"], row["provider_id"]) if row else None


def _claim_provider(
    config: AppConfig, tenant_id: str, provider_id: str, trigger_type: str
) -> tuple[_Claim, list[_Credential]]:
    """短事务领取 Provider，并复制网络阶段所需的配置与密文。"""

    connection = _connect(config)
    now = _utcnow()
    lease_until = now + timedelta(seconds=config.provider_health_lease_seconds)
    token = uuid4().hex
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM providers WHERE tenant_id=? AND provider_id=?",
            (tenant_id, provider_id),
        ).fetchone()
        # 不存在和跨租户 Provider 统一返回 404，不能暴露其他租户资源是否存在。
        if row is None:
            raise ApiError("provider.not_found")
        # 在写入 lease、读取 KeyPool 或执行 DNS/HTTP 前验证完整组合；失败事务会回滚，
        # 不会留下阻塞后续修复操作的探测租约。
        adapter_spec = _validate_provider_auth_combination(
            row["provider_type"],
            row["auth_type"],
        )
        # 周期任务只允许领取 active 且真正到期的行，手工探测不受 due 条件限制。
        if trigger_type == "scheduled" and (
            row["status"] != "active" or not row["next_probe_at"] or row["next_probe_at"] > _db_time(now)
        ):
            # 领取瞬间再次核对状态和时间，防止预读之后的管理变更被忽略。
            raise ApiError("provider_health.probe_not_due")
        # 未过期租约表示其他进程或手工请求正在探测，必须拒绝重复领取。
        if row["probe_lease_until"] and row["probe_lease_until"] > _db_time(now):
            raise ApiError("provider_health.probe_in_progress")
        updated = connection.execute(
            """
            UPDATE providers SET probe_lease_token=?, probe_lease_until=?
            WHERE tenant_id=? AND provider_id=? AND health_generation=?
              AND (probe_lease_until IS NULL OR probe_lease_until<=?)
            """,
            (
                token,
                _db_time(lease_until),
                tenant_id,
                provider_id,
                row["health_generation"],
                _db_time(now),
            ),
        )
        # 条件更新失败说明领取竞争已输，不能继续复制或解密 Provider Key。
        if updated.rowcount != 1:
            raise ApiError("provider_health.probe_in_progress")
        key_rows = connection.execute(
            """
            SELECT provider_credential_id, encrypted_key
            FROM provider_credentials
            WHERE tenant_id=? AND provider_id=? AND status='active'
              AND (rate_limited_until IS NULL OR rate_limited_until<=?)
            ORDER BY weight DESC, provider_credential_id ASC
            LIMIT 21
            """,
            (tenant_id, provider_id, _db_time(now)),
        ).fetchall()
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    claim = _Claim(
        provider_id=row["provider_id"],
        tenant_id=row["tenant_id"],
        provider_code=row["provider_code"],
        provider_type=row["provider_type"],
        base_url=row["base_url"],
        auth_type=row["auth_type"],
        probe_profile=adapter_spec.probe_profile,
        provider_status=row["status"],
        health_status=row["health_status"],
        health_generation=row["health_generation"],
        consecutive_failures=row["consecutive_probe_failures"],
        lease_token=token,
    )
    credentials = [_Credential(row["provider_credential_id"], row["encrypted_key"]) for row in key_rows]
    return claim, credentials


def _perform_probe(config: AppConfig, claim: _Claim, credentials: list[_Credential]) -> list[_Attempt]:
    """在事务外执行受限网络探测，并把所有异常收敛为固定安全分类。"""

    checked_at = _utcnow()
    try:
        # 既有 OpenAI-compatible 路径保持原两参数调用合同；通用 profile 才显式传入，
        # 既兼容历史测试/扩展包装器，也确保 custom 不会被错误追加 /models。
        target = (
            _validated_probe_url(config, claim.base_url)
            if claim.probe_profile == OPENAI_PROBE_PROFILE
            else _validated_probe_url(
                config,
                claim.base_url,
                probe_profile=claim.probe_profile,
            )
        )
    # SSRF、DNS 或 URL 校验失败统一为固定分类，不回显内部地址解析细节。
    except (OSError, ValueError):
        return [_fixed_attempt("failed", "unverified", "endpoint_blocked", "Provider endpoint is blocked by probe policy.", checked_at)]

    # 无认证 Endpoint 不构造 Authorization 请求头，也完全不读取或解密 KeyPool 凭据。
    if claim.auth_type == "none":
        return [_http_attempt(config, target, None, None)]
    if len(credentials) > 20:
        credentials = credentials[:20]
        sampling_truncated = True
    else:
        sampling_truncated = False
    if not credentials:
        return [_fixed_attempt("failed", "unavailable", "credential_unavailable", "No usable provider credential is available.", checked_at)]

    attempts: list[_Attempt] = []
    for credential in credentials:
        try:
            plain_key = decrypt_provider_key(credential.encrypted_key)
        # 单把密文无法解密只标记该凭据不可用，继续尝试其余 Key。
        except ValueError:
            attempts.append(
                _fixed_attempt(
                    "failed",
                    "unavailable",
                    "credential_unavailable",
                    "Provider credential cannot be decrypted.",
                    checked_at,
                    credential_id=credential.credential_id,
                )
            )
            continue
        attempt = _http_attempt(config, target, plain_key, credential.credential_id)
        plain_key = ""
        attempts.append(attempt)
        # 成功或 Endpoint 级故障已经足以形成结论，无需继续消耗其他 Key。
        if attempt.result_status == "success" or attempt.error_code in {
            "connect_failed",
            "tls_failed",
            "timeout",
            "upstream_5xx",
            "redirect_rejected",
        }:
            break
    if sampling_truncated and not any(item.result_status == "success" for item in attempts):
        attempts.append(_fixed_attempt("inconclusive", "unverified", "credential_sample_limited", "Credential sample limit was reached.", checked_at))
    return attempts


def probe_provider_reachability(
    config: AppConfig,
    base_url: str,
    *,
    probe_profile: str,
    timeout_seconds: float,
) -> _Attempt:
    """供旧 Provider 连通性入口复用受限 URL、固定 DNS 和 spec 探测路径。

    该兼容入口只判断 Endpoint 可达性，不读取或发送 Provider Key；type/auth 组合必须由调用方
    在调用前完成 Shared spec 校验。OpenAI `/models` 的历史 404 根路径回退也重新执行完整
    URL/DNS 校验，不能绕过端口、私网或 metadata 限制。
    """

    checked_at = _utcnow()
    try:
        target = _validated_probe_url(config, base_url, probe_profile=probe_profile)
    # DNS、URL 或 SSRF 策略细节不能穿透旧接口，只返回固定阻断分类。
    except (OSError, ValueError):
        return _fixed_attempt(
            "failed",
            "unverified",
            "endpoint_blocked",
            "Provider endpoint is blocked by probe policy.",
            checked_at,
        )
    probe_config = config.model_copy(
        update={"provider_health_timeout_seconds": timeout_seconds}
    )
    attempt = _http_attempt(probe_config, target, None, None)
    if attempt.http_status != 404 or probe_profile != OPENAI_PROBE_PROFILE:
        return attempt
    try:
        fallback = _validated_probe_url(
            config,
            base_url,
            probe_profile=GENERIC_PROBE_PROFILE,
        )
    # 404 回退仍须重新经过完整策略，不能直接复用未经验证的 base_url。
    except (OSError, ValueError):
        return _fixed_attempt(
            "failed",
            "unverified",
            "endpoint_blocked",
            "Provider endpoint is blocked by probe policy.",
            checked_at,
        )
    return _http_attempt(probe_config, fallback, None, None)


def _http_attempt(
    config: AppConfig, target: str, plain_key: str | None, credential_id: str | None
) -> _Attempt:
    """只读取 HTTP 状态的流式探测；底层异常对象和响应正文都不向上返回。"""

    started = time.perf_counter()
    checked_at = _utcnow()
    headers = {"Authorization": f"Bearer {plain_key}"} if plain_key else {}
    try:
        transport = _PinnedHTTPTransport(target) if isinstance(target, _ValidatedProbeUrl) else None
        with httpx.Client(
            timeout=config.provider_health_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            with client.stream("GET", target, headers=headers) as response:
                status = response.status_code
        latency = int((time.perf_counter() - started) * 1000)
        if 200 <= status < 300:
            return _Attempt(credential_id, "success", "accepted", status, latency, None, "Provider probe succeeded.", checked_at)
        # 401/403 证明 Endpoint 可达但当前凭据被拒绝，分类到 Key 而非网络故障。
        if status in {401, 403}:
            return _Attempt(credential_id, "failed", "rejected", status, latency, "authentication_failed", "Provider authentication was rejected.", checked_at)
        if status == 429:
            return _Attempt(credential_id, "inconclusive", "rate_limited", status, latency, "rate_limited", "Provider credential is rate limited.", checked_at)
        if 300 <= status < 400:
            return _Attempt(credential_id, "failed", "unverified", status, latency, "redirect_rejected", "Provider redirect was rejected.", checked_at)
        code = "upstream_5xx" if status >= 500 else "probe_http_error"
        return _Attempt(credential_id, "failed", "unverified", status, latency, code, "Provider returned an unsuccessful status.", checked_at)
    except httpx.TimeoutException:
        code, summary = "timeout", "Provider probe timed out."
    # 连接失败不携带原始异常对象，避免 request headers 沿异常链进入日志。
    except httpx.ConnectError:
        code, summary = "connect_failed", "Provider connection failed."
    except (httpx.InvalidURL, httpx.UnsupportedProtocol):
        code, summary = "endpoint_invalid", "Provider endpoint is invalid."
    # 其余 httpx 传输错误统一收敛为固定枚举，不暴露 URL query 或认证头。
    except httpx.HTTPError:
        code, summary = "transport_error", "Provider transport failed."
    return _Attempt(
        credential_id,
        "failed",
        "unverified",
        None,
        int((time.perf_counter() - started) * 1000),
        code,
        summary,
        checked_at,
    )


def _aggregate_result(attempts: list[_Attempt]) -> str:
    """按 KeyPool 语义聚合结果，避免一把坏 Key 放大为整个 Provider 故障。"""

    if any(item.result_status == "success" for item in attempts):
        return "success"
    # 凭据超过采样上限时证据不完整，禁止把未测试 KeyPool 判为整体故障。
    if any(item.error_code == "credential_sample_limited" for item in attempts):
        return "inconclusive"
    # 全部 429 只说明暂时限流，更新 Key 冷却但不累计 Provider 失败。
    if attempts and all(item.auth_result == "rate_limited" for item in attempts):
        return "inconclusive"
    if any(item.result_status == "failed" for item in attempts):
        return "failed"
    return "inconclusive"


def _complete_probe(
    config: AppConfig,
    claim: _Claim,
    attempts: list[_Attempt],
    *,
    result_status: str,
    trigger_type: str,
    request_id: str,
    trace_id: str,
) -> bool:
    """用 lease token + generation 原子提交日志、Key 状态和 Provider 聚合。"""

    connection = _connect(config)
    now = _utcnow()
    next_probe = None if claim.provider_status == "disabled" else now + timedelta(seconds=config.provider_health_interval_seconds)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            """
            SELECT health_status, consecutive_probe_failures FROM providers
            WHERE tenant_id=? AND provider_id=? AND probe_lease_token=? AND health_generation=?
            """,
            (claim.tenant_id, claim.provider_id, claim.lease_token, claim.health_generation),
        ).fetchone()
        # token 或 generation 不匹配表示配置已变化，整笔回滚且不留下旧日志或事件。
        if current is None:
            raise ApiError("provider_health.probe_superseded")
        for sequence, attempt in enumerate(attempts, 1):
            connection.execute(
                """
                INSERT INTO provider_health_probe_logs(
                  probe_log_id, tenant_id, provider_id, provider_credential_id,
                  health_generation, attempt_sequence, trigger_type, result_status,
                  auth_result, http_status, latency_ms, error_code, result_summary,
                  request_id, trace_id, checked_at, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"phl_{uuid4().hex}", claim.tenant_id, claim.provider_id,
                    attempt.credential_id, claim.health_generation, sequence, trigger_type,
                    attempt.result_status, attempt.auth_result, attempt.http_status,
                    attempt.latency_ms, attempt.error_code, _truncate_utf8(attempt.summary),
                    request_id, trace_id, _db_time(attempt.checked_at), _db_time(now),
                ),
            )
            _update_credential_probe(connection, claim, attempt, now, config)

        if result_status == "inconclusive":
            updated = connection.execute(
                """
                UPDATE providers
                SET probe_lease_token=NULL, probe_lease_until=NULL, next_probe_at=?
                WHERE tenant_id=? AND provider_id=? AND probe_lease_token=? AND health_generation=?
                """,
                (_optional_db_time(next_probe), claim.tenant_id, claim.provider_id, claim.lease_token, claim.health_generation),
            )
            # inconclusive 也必须通过 CAS 释放租约，迟到结果不能推进新配置的排期。
            if updated.rowcount != 1:
                raise ApiError("provider_health.probe_superseded")
            connection.commit()
            return False

        representative = next((item for item in attempts if item.result_status == result_status), attempts[0])
        previous_status = current["health_status"]
        if result_status == "success":
            health_status = "healthy"
            failures = 0
            last_success = _db_time(now)
            last_failure = None
            last_error = None
        else:
            failures = current["consecutive_probe_failures"] + 1
            immediate = trigger_type in {"manual", "legacy_test"}
            health_status = "unhealthy" if immediate or failures >= config.provider_health_failure_threshold else previous_status
            last_success = None
            last_failure = _db_time(now)
            last_error = representative.summary
        updated = connection.execute(
            """
            UPDATE providers
            SET health_status=?, last_tested_at=?, last_test_error=?,
                last_success_at=COALESCE(?, last_success_at),
                last_failure_at=COALESCE(?, last_failure_at),
                last_probe_latency_ms=?, last_probe_http_status=?, last_probe_error_code=?,
                consecutive_probe_failures=?, health_updated_at=?, next_probe_at=?,
                probe_lease_token=NULL, probe_lease_until=NULL
            WHERE tenant_id=? AND provider_id=? AND probe_lease_token=? AND health_generation=?
            """,
            (
                health_status, _db_time(now), _truncate_utf8(last_error) if last_error else None,
                last_success, last_failure, representative.latency_ms, representative.http_status,
                representative.error_code, failures, _db_time(now), _optional_db_time(next_probe),
                claim.tenant_id, claim.provider_id, claim.lease_token, claim.health_generation,
            ),
        )
        # 聚合状态提交必须再次检查 token+generation，确保日志与状态属于同一配置代际。
        if updated.rowcount != 1:
            raise ApiError("provider_health.probe_superseded")
        connection.commit()
        return previous_status != "unhealthy" and health_status == "unhealthy"
    except Exception:
        connection.rollback()
        raise


def _update_credential_probe(
    connection: sqlite3.Connection,
    claim: _Claim,
    attempt: _Attempt,
    now: datetime,
    config: AppConfig,
) -> None:
    """更新仍属于当前 generation 的 Key 探测状态，不触碰 last_used_at。"""

    if not attempt.credential_id:
        return
    # 认证成功重置该 Key 的探测失败计数，但不修改业务调用 last_used_at。
    if attempt.auth_result == "accepted":
        connection.execute(
            """
            UPDATE provider_credentials
            SET probe_health_status='healthy', probe_last_tested_at=?, probe_last_success_at=?,
                probe_last_error_code=NULL, probe_consecutive_failures=0
            WHERE tenant_id=? AND provider_id=? AND provider_credential_id=? AND status='active'
            """,
            (_db_time(now), _db_time(now), claim.tenant_id, claim.provider_id, attempt.credential_id),
        )
    # 认证拒绝只标记单把 Key；Provider 聚合由全部尝试结果另行决定。
    elif attempt.auth_result == "rejected":
        connection.execute(
            """
            UPDATE provider_credentials
            SET probe_health_status='unhealthy', probe_last_tested_at=?,
                probe_last_error_code=?, probe_consecutive_failures=probe_consecutive_failures+1
            WHERE tenant_id=? AND provider_id=? AND provider_credential_id=? AND status='active'
            """,
            (_db_time(now), attempt.error_code, claim.tenant_id, claim.provider_id, attempt.credential_id),
        )
    # 429 只进入短冷却，不把管理状态改成 disabled，也不刷新 Provider 健康证据。
    elif attempt.auth_result == "rate_limited":
        connection.execute(
            """
            UPDATE provider_credentials SET rate_limited_until=?, probe_last_tested_at=?,
                probe_last_error_code='rate_limited'
            WHERE tenant_id=? AND provider_id=? AND provider_credential_id=? AND status='active'
            """,
            (
                _db_time(now + timedelta(seconds=config.provider_health_interval_seconds)),
                _db_time(now), claim.tenant_id, claim.provider_id, attempt.credential_id,
            ),
        )


def _dispatch_unhealthy_event(
    config: AppConfig,
    claim: _Claim,
    item: ProviderHealthItem,
    attempts: list[_Attempt],
    request_id: str,
    trace_id: str,
) -> None:
    """提交健康事实后 best-effort 投递状态转换事件，失败不得回滚健康状态。"""

    event_seed = f"{claim.provider_id}_{claim.health_generation}_{item.health_updated_at}"
    event_digest = hashlib.sha256(event_seed.encode("utf-8")).hexdigest()[:24]
    event = build_webhook_event(
        event_id=f"evt_ph_{event_digest}",
        tenant_id=claim.tenant_id,
        event_type="provider.unhealthy",
        object_id=claim.provider_id,
        trace_id=trace_id,
        payload={
            "provider_id": claim.provider_id,
            "provider_code": claim.provider_code,
            "health_status": item.health_status,
            "failure_code": attempts[-1].error_code if attempts else "probe_failed",
            "consecutive_failures": item.consecutive_probe_failures,
            "checked_at": _db_time(_utcnow()),
        },
    )
    try:
        dispatch_webhook_event(config, claim.tenant_id, event, request_id=request_id, trace_id=trace_id)
    except Exception:
        logger.warning(
            "provider.unhealthy webhook delivery failed",
            extra={"provider_id": claim.provider_id, "tenant_id": claim.tenant_id, "request_id": request_id},
        )


def _validated_probe_url(
    config: AppConfig,
    base_url: str,
    *,
    probe_profile: str = OPENAI_PROBE_PROFILE,
) -> str:
    """校验 scheme、端口和全部解析地址，并按 spec 选择受控探测路径。

    默认参数保留既有内部测试和调用兼容；生产领取流程总是显式传入 Shared spec profile。
    """

    parsed = urlsplit(base_url)
    # 仅允许无 userinfo/fragment 的 HTTP(S) Endpoint，避免凭据嵌入 URL 或协议走私。
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("invalid endpoint")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # 端口白名单限制探测面，不能借 Provider API 扫描任意内网服务。
    if port not in {80, 443, 8000, 8080, 8443}:
        raise ValueError("blocked port")
    allow_networks = [ipaddress.ip_network(value, strict=False) for value in config.provider_health_private_cidrs]
    infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    # DNS 无结果时拒绝请求，不让 httpx 再进行一次不受检查的隐式解析。
    if not infos:
        raise ValueError("unresolved endpoint")
    resolved_addresses: list[str] = []
    for info in infos:
        resolved_address = ipaddress.ip_address(info[4][0])
        # IPv4-mapped IPv6 连接最终仍会访问其嵌入的 IPv4；必须先还原再做 metadata、
        # 回环和 allowlist 判断，否则 ``::ffff:100.100.100.200`` 会伪装成普通 IPv6。
        address = (
            resolved_address.ipv4_mapped
            if isinstance(resolved_address, ipaddress.IPv6Address)
            and resolved_address.ipv4_mapped is not None
            else resolved_address
        )
        # 回环、链路本地、多播和未指定地址始终禁止，不能被私网 allowlist 放开。
        if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
            raise ValueError("blocked address")
        # 常见云 metadata 地址单独拒绝，避免未来 ipaddress 分类变化削弱边界。
        if str(address) in {"169.254.169.254", "100.100.100.200"}:
            raise ValueError("metadata endpoint is blocked")
        # 企业私网、CGNAT、文档保留和其他非 global 地址都必须显式命中部署级 CIDR；
        # metadata/回环等高危类别已在前面无条件拒绝，不能借 allowlist 放开。
        if not address.is_global and not any(address in network for network in allow_networks):
            raise ValueError("non-global endpoint is not allowlisted")
        normalized_address = str(address)
        if normalized_address not in resolved_addresses:
            resolved_addresses.append(normalized_address)
    clean_base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/", "", ""))
    # OpenAI-compatible 用 /models 验证认证合同；通用 Endpoint 只探测已配置根路径，不能
    # 猜测私有 models API。profile 已在 lease 前由冻结目录验证，不接受请求体自由值。
    target = urljoin(clean_base, "models") if probe_profile == OPENAI_PROBE_PROFILE else clean_base
    return _ValidatedProbeUrl(
        target,
        hostname=parsed.hostname,
        port=port,
        addresses=tuple(resolved_addresses),
    )


def _health_item(row: sqlite3.Row, config: AppConfig) -> ProviderHealthItem:
    """把数据库原始状态转换为互斥 effective 状态和脱敏 DTO。"""

    health_updated_at = _parse_time(row["health_updated_at"])
    stale = health_updated_at is None or health_updated_at < _utcnow() - timedelta(seconds=config.provider_health_stale_seconds)
    if row["status"] != "active":
        effective = "disabled"
    elif row["consecutive_probe_failures"] > 0 and row["health_status"] != "unhealthy" and not stale:
        effective = "observing"
    elif stale:
        effective = "unknown"
    else:
        effective = row["health_status"]
    return ProviderHealthItem(
        provider_id=row["provider_id"], tenant_id=row["tenant_id"], provider_code=row["provider_code"],
        name=row["name"], provider_type=row["provider_type"], base_url=_safe_base_url(row["base_url"]),
        provider_status=row["status"], health_status=row["health_status"],
        effective_health_status=effective, is_health_stale=stale,
        last_tested_at=_parse_time(row["last_tested_at"]), last_success_at=_parse_time(row["last_success_at"]),
        last_failure_at=_parse_time(row["last_failure_at"]), last_probe_latency_ms=row["last_probe_latency_ms"],
        last_probe_http_status=row["last_probe_http_status"], last_probe_error_code=row["last_probe_error_code"],
        last_test_error=row["last_test_error"], consecutive_probe_failures=row["consecutive_probe_failures"],
        next_probe_at=_parse_time(row["next_probe_at"]), health_updated_at=health_updated_at,
    )


def _summary(items: list[ProviderHealthItem]) -> ProviderHealthSummary:
    """按 effective 状态聚合互斥桶，active 作为额外配置状态计数。"""

    counts = {status: 0 for status in EFFECTIVE_STATUSES}
    for item in items:
        counts[item.effective_health_status] += 1
    return ProviderHealthSummary(
        total=len(items), active=sum(item.provider_status == "active" for item in items),
        healthy=counts["healthy"], unhealthy=counts["unhealthy"], observing=counts["observing"],
        unknown=counts["unknown"], disabled=counts["disabled"],
    )


def _load_health_row(connection: sqlite3.Connection, tenant_id: str, provider_id: str) -> sqlite3.Row:
    """按 tenant_id 首条件读取 Provider，跨租户 ID 与不存在统一表现为 404。"""

    row = connection.execute(
        """
        SELECT provider_id, tenant_id, provider_code, name, provider_type, base_url, status,
               health_status, last_tested_at, last_success_at, last_failure_at,
               last_probe_latency_ms, last_probe_http_status, last_probe_error_code,
               last_test_error, consecutive_probe_failures, next_probe_at, health_updated_at
        FROM providers WHERE tenant_id=? AND provider_id=?
        """,
        (tenant_id, provider_id),
    ).fetchone()
    # 查询始终带 tenant_id，跨租户与不存在统一映射为 Provider 404。
    if row is None:
        raise ApiError("provider.not_found")
    return row


def _attempt_info(sequence: int, attempt: _Attempt) -> ProbeAttemptInfo:
    """丢弃内部 credential_id，只返回固定安全摘要。"""

    return ProbeAttemptInfo(
        sequence=sequence, result_status=attempt.result_status, auth_result=attempt.auth_result,
        http_status=attempt.http_status, latency_ms=attempt.latency_ms, error_code=attempt.error_code,
        result_summary=attempt.summary, checked_at=attempt.checked_at,
    )


def _fixed_attempt(
    result_status: str,
    auth_result: str,
    error_code: str,
    summary: str,
    checked_at: datetime,
    *,
    credential_id: str | None = None,
) -> _Attempt:
    """构造没有网络 HTTP 状态的固定分类结果。"""

    # 固定结果不携带 HTTP 状态或原始异常，只保留安全分类和零网络延迟。
    return _Attempt(credential_id, result_status, auth_result, None, 0, error_code, summary, checked_at)


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建健康任务连接并设置 SQLite 写竞争的有限等待策略。"""

    # 健康数据必须可追踪落库，未配置数据库时不能退化为内存状态。
    if not config.database_url:
        raise ApiError("infra.database_not_configured")
    try:
        connection = connect_database(config.database_url)
        connection.execute("PRAGMA busy_timeout=5000")
        if config.database_url != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")
        return connection
    # 数据库连接异常转换为统一错误码，不把文件路径或驱动异常暴露给 API。
    except DbError as exc:
        raise ApiError(exc.code) from exc


def _safe_base_url(value: str) -> str:
    """删除 URL userinfo、query 和 fragment 后再返回控制台，防止敏感参数进入页面。"""

    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path, "", ""))


def _truncate_utf8(value: str, limit: int = 240) -> str:
    """按 UTF-8 字节安全截断数据库摘要，避免切断多字节字符。"""

    encoded = value.encode("utf-8")[:limit]
    return encoded.decode("utf-8", errors="ignore")


def _parse_time(value: object) -> datetime | None:
    """解析 SQLite 保存的 UTC 时间；空值保持 None，供新鲜度状态机统一使用。"""

    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _optional_db_time(value: datetime | None) -> str | None:
    """把可空 UTC 时间转换为数据库稳定格式。"""

    return _db_time(value) if value else None


def _db_time(value: datetime) -> str:
    """把 UTC 时间保存为可按字符串比较的 ISO 格式。"""

    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utcnow() -> datetime:
    """集中生成带时区 UTC 时间，便于测试替换时钟。"""

    return datetime.now(UTC)
