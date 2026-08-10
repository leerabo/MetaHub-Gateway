"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/src/metahub_gateway/providers/keypool.py
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import decrypt_provider_key
from metahub_shared.response_cache import (
    CredentialResponseScope,
    VersionedResponseCacheSecret,
    build_environment_credential_scope,
    build_managed_credential_scope,
    build_none_credential_scope,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.keypool import (
    ManagedCredentialFact,
    evaluate_provider_key_pool,
)
from metahub_shared.runtime.routing_policy import KeyPoolState

from metahub_gateway.routing.models import GatewayRoute

logger = logging.getLogger("metahub.provider_keypool")

# Provider Key 冷却时间定义上游限流后的退避边界，避免持续命中同一失效 Key。
RATE_LIMIT_COOLDOWN_SECONDS = 60


@dataclass(frozen=True)
class SelectedProviderKey:
    """本次上游调用选中的 Provider Key。

    plain_value 只在内存中交给 Provider Adapter 构造 Authorization Header；日志和
    应用事件不得包含 provider_credential_id/key_prefix。二者只允许在受控内存对象、数据库
    关联和授权管理响应中使用，运行日志通过请求、租户、Provider 与模型元数据关联。
    """

    provider_credential_id: str | None = field(repr=False)
    plain_value: str = field(repr=False)
    source: str
    key_prefix: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ProviderKeyPoolSnapshot:
    """路由阶段使用的无副作用 KeyPool 资格快照。

    该对象只暴露可用性分类，不包含 Key ID、前缀、数量、环境变量名或密文。它不能替代
    真正 Provider 调用前的 ``select_provider_key``；两者之间发生并发禁用、冷却或解密失败
    时，调用链仍必须返回 ``provider_key.unavailable``，不能静默换模型。
    """

    state: KeyPoolState


def snapshot_response_cache_credential_scope(
    config: AppConfig,
    connection: sqlite3.Connection,
    route: GatewayRoute,
    *,
    hmac_key: VersionedResponseCacheSecret,
    as_of: datetime,
) -> CredentialResponseScope | None:
    """无副作用冻结 Response Cache 唯一 Credential 响应域。

    返回 ``None`` 表示当前 KeyPool 无法证明只有一个响应域，调用方必须旁路缓存。函数不解密
    托管 Key、不更新选择时间、不记录 Credential ID/fingerprint；真正 Provider miss 仍使用既有
    ``select_provider_key``，fill commit 再按实际选择结果回验冻结 scope。
    """

    # 网关关键分支必须维持租户、路由、成本和失败追踪一致，并保护上游凭据。
    if route.provider_auth_type == "none":
        return build_none_credential_scope(
            key=hmac_key,
            tenant_id=route.tenant_id,
            provider_id=route.provider_id,
            provider_auth_type=route.provider_auth_type,
            scope_version=1,
        )
    rows = connection.execute(
        """
        SELECT provider_credential_id,key_fingerprint,encrypted_key,status,
               rate_limited_until,probe_health_status,probe_last_tested_at
        FROM provider_credentials
        WHERE tenant_id=? AND provider_id=?
        ORDER BY provider_credential_id
        """,
        (route.tenant_id, route.provider_id),
    ).fetchall()
    managed_rows = [
        row for row in rows if str(row["encrypted_key"] or "").startswith("aesgcm-v1:")
    ]
    if managed_rows:
        selectable = tuple(
            row
            for row in managed_rows
            if _row_is_selectable(row, stale_seconds=config.provider_health_stale_seconds)
        )
        if len(selectable) != 1:
            # 多成员可能对应不同上游账号或过滤配置；本期禁止只摘要 pool 后共享响应。
            return None
        row = selectable[0]
        return build_managed_credential_scope(
            key=hmac_key,
            tenant_id=route.tenant_id,
            provider_id=route.provider_id,
            provider_auth_type=route.provider_auth_type,
            scope_version=1,
            selectable_credential_count=1,
            credential_id=str(row["provider_credential_id"]),
            credential_key_fingerprint=str(row["key_fingerprint"]),
            # 现有 Credential 行没有独立业务 version；key fingerprint 已绑定实际 Secret，
            # 本期 schema version=1，未来若新增 version 必须同时提升 scope_version。
            credential_version=1,
        )

    environment_key = _environment_provider_key(
        config,
        connection,
        tenant_id=route.tenant_id,
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        route_base_url=route.provider_base_url,
    )
    if not environment_key:
        return None
    # 环境 Key 明文只在当前栈用于 keyed fingerprint；digest 后立即丢弃且不进入任何对象或日志。
    environment_fingerprint = hmac.new(
        hmac_key.secret.encode("utf-8"),
        b"metahub/response-cache/environment-credential/v1\x00"
        + environment_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    del environment_key
    return build_environment_credential_scope(
        key=hmac_key,
        tenant_id=route.tenant_id,
        provider_id=route.provider_id,
        provider_auth_type=route.provider_auth_type,
        scope_version=1,
        environment_key_fingerprint=environment_fingerprint,
    )


def response_cache_scope_matches_provider_selection(
    config: AppConfig,
    connection: sqlite3.Connection,
    route: GatewayRoute,
    *,
    scope_kind: str,
    provider_credential_id: str | None,
    provider_key_source: str | None,
    as_of: datetime,
) -> bool:
    """证明实际 Provider 选择仍属于 lookup 时冻结的唯一 Credential 响应域。

    本函数只返回布尔值，不向调用方暴露 Credential ID、fingerprint 或 Secret。托管模式必须
    恰好存在一个当前可选成员，且该成员就是实际请求使用的数据库 Credential；这样旧成员在
    Provider 调用期间被替换后，迟到 fill 只能丢失 lease，不能写入新响应域。
    """

    if scope_kind == "none":
        return (
            route.provider_auth_type == "none"
            and provider_credential_id is None
            and provider_key_source is None
        )
    if scope_kind == "environment":
        # 网关关键分支必须维持租户、路由、成本和失败追踪一致，并保护上游凭据。
        return provider_credential_id is None and provider_key_source == "environment"
    # 网关关键分支必须维持租户、路由、成本和失败追踪一致，并保护上游凭据。
    if scope_kind != "managed" or provider_key_source != "database":
        return False
    rows = connection.execute(
        """
        SELECT provider_credential_id,key_fingerprint,encrypted_key,status,
               rate_limited_until,probe_health_status,probe_last_tested_at
        FROM provider_credentials
        WHERE tenant_id=? AND provider_id=?
        ORDER BY provider_credential_id
        """,
        (route.tenant_id, route.provider_id),
    ).fetchall()
    selectable = tuple(
        row
        for row in rows
        if str(row["encrypted_key"] or "").startswith("aesgcm-v1:")
        and _row_is_selectable(
            row,
            stale_seconds=config.provider_health_stale_seconds,
            now=as_of,
        )
    )
    return (
        len(selectable) == 1
        and provider_credential_id is not None
        and hmac.compare_digest(
            str(selectable[0]["provider_credential_id"]), provider_credential_id
        )
    )


def snapshot_provider_key_pool(
    config: AppConfig,
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    provider_id: str,
    provider_code: str,
    provider_auth_type: str,
    as_of: datetime,
) -> ProviderKeyPoolSnapshot:
    """在调用方冻结事务内读取 Provider 的 KeyPool 可用性。

    参数：连接必须已经由路由器按可信 tenant 建立事务；Provider 字段来自同租户模型图；
    ``as_of`` 是本请求唯一 UTC 时点。返回：仅含 available/unavailable 的安全快照。
    关键约束：不解密、不更新任何 Key、不访问网络；旧明文行不能冒充托管 Key。
    """

    # 无认证端点不需要凭据，不能因 Key 表为空被动态路由错误摘除。
    if provider_auth_type == "none":
        # 无认证 Provider 不需要 Key；将其视为可用可避免动态模式错误剔除本地匿名端点。
        return ProviderKeyPoolSnapshot(state="available")
    rows = connection.execute(
        """
        SELECT
          provider_credential_id, encrypted_key, status, rate_limited_until,
          probe_health_status, probe_last_tested_at
        FROM provider_credentials
        WHERE tenant_id=? AND provider_id=?
        """,
        (tenant_id, provider_id),
    ).fetchall()
    managed_rows = [
        row for row in rows if str(row["encrypted_key"] or "").startswith("aesgcm-v1:")
    ]
    # 一旦存在托管密钥，正式选择器禁止回退环境变量；只有无托管行时才读取默认租户兼容配置。
    environment_key_present = False
    if not managed_rows:
        environment_key_present = bool(
            _environment_provider_key(
                config,
                connection,
                tenant_id=tenant_id,
                provider_id=provider_id,
                provider_code=provider_code,
            )
        )
    eligibility = evaluate_provider_key_pool(
        provider_auth_type=provider_auth_type,
        managed_credentials=tuple(
            ManagedCredentialFact(
                credential_id=str(row["provider_credential_id"]),
                status=str(row["status"] or ""),
                rate_limited_until=row["rate_limited_until"],
                probe_health_status=str(row["probe_health_status"] or ""),
                probe_last_tested_at=row["probe_last_tested_at"],
            )
            for row in managed_rows
        ),
        environment_fallback_available=environment_key_present,
        as_of=as_of,
        stale_seconds=config.provider_health_stale_seconds,
    )
    # dependency_hash 供 Control preview CAS 使用；Gateway 热路径只消费安全状态，不写 Credential 摘要。
    return ProviderKeyPoolSnapshot(state=eligibility.state)


def select_provider_key(config: AppConfig, route: GatewayRoute) -> SelectedProviderKey | None:
    """为当前路由选择可用 Provider Key。

    无认证 Provider 直接返回 None；有托管 Key 时优先从数据库按最少最近使用策略选取，
    没有托管 Key 才允许回退环境变量，便于本地 M0/M1 启动。
    """

    # 模型路由结果决定上游调用，异常时必须用统一错误码停止。
    if route.provider_auth_type == "none":
        return None

    connection = _connect(config)
    rows = connection.execute(
        """
        SELECT
          provider_credential_id, encrypted_key, key_prefix, weight, status,
          last_used_at, rate_limited_until, probe_health_status, probe_last_tested_at
        FROM provider_credentials
        WHERE tenant_id=? AND provider_id=?
        """,
        (route.tenant_id, route.provider_id),
    ).fetchall()
    managed_key_exists = any(str(row["encrypted_key"]).startswith("aesgcm-v1:") for row in rows)
    candidates = [
        row
        for row in rows
        if _row_is_selectable(row, stale_seconds=config.provider_health_stale_seconds)
    ]
    candidates.sort(key=_selection_sort_key)

    for row in candidates:
        encrypted_key = str(row["encrypted_key"] or "")
        # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
        if not encrypted_key.startswith("aesgcm-v1:"):
            # 旧数据或测试数据不具备可解密密文，不能把它当成可用托管 Key。
            continue
        try:
            plain_value = decrypt_provider_key(encrypted_key)
        # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
        except ValueError:
            # 解密失败只记脱敏上下文，继续尝试其它 Key，避免单把坏 Key 放大成全局不可用。
            logger.warning(
                "provider_key.decrypt_failed",
                extra={
                    "event": "provider_key.decrypt_failed",
                    "tenant_id": route.tenant_id,
                    "provider_id": route.provider_id,
                    "payload": {
                        "provider_code": route.provider_code,
                        "model_id": route.model_id,
                    },
                },
            )
            continue
        _record_key_selected(connection, route.tenant_id, row["provider_credential_id"])
        return SelectedProviderKey(
            provider_credential_id=row["provider_credential_id"],
            plain_value=plain_value,
            source="database",
            key_prefix=row["key_prefix"],
        )

    if managed_key_exists:
        # 只要租户配置了托管 Key，就不再回退环境变量，避免绕过 KeyPool 治理。
        raise ApiError("provider_key.unavailable")

    env_key = _environment_provider_key(
        config,
        connection,
        tenant_id=route.tenant_id,
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        route_base_url=route.provider_base_url,
    )
    # 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
    if env_key:
        return SelectedProviderKey(
            provider_credential_id=None,
            plain_value=env_key,
            source="environment",
            key_prefix=None,
        )
    # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
    raise ApiError("provider_key.unavailable")


def mark_provider_key_error(
    database_url: str | None,
    *,
    tenant_id: str,
    provider_id: str,
    provider_code: str,
    model_id: str,
    provider_credential_id: str | None,
    request_id: str,
    trace_id: str,
    error_code: str,
    message: str,
    http_status: int | None,
) -> None:
    """记录 Provider Key 调用失败并在限流时短暂熔断。

    该函数运行在数据面响应后处理路径，写库失败不能影响已经发生的主请求，因此所有
    数据库错误都降级返回。Credential ID 只用于带 tenant_id 的数据库更新，绝不进入日志。
    """

    # Provider 判断会影响上游归因和密钥健康，错误路径必须保持脱敏。
    if not provider_credential_id:
        # 环境变量 Key 没有数据库凭据 ID，无法写 KeyPool 状态。
        return
    try:
        connection = connect_database(database_url)
    # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
    except DbError:
        return

    now = _to_db_time(_utcnow())
    updates = [
        "last_error_at=?",
        "last_error_code=?",
        "last_error_message=?",
        "updated_at=?",
    ]
    values: list[object] = [now, error_code[:80], message[:240], now]
    # 状态切换会影响治理策略和可用性，非法状态必须在入口处拦截。
    if _is_rate_limit_error(error_code, message, http_status):
        # 限流错误进入短冷却窗口，下一次选择会跳过这把 Key。
        updates.extend(["status='rate_limited'", "rate_limited_until=?"])
        values.append(_to_db_time(_utcnow() + timedelta(seconds=RATE_LIMIT_COOLDOWN_SECONDS)))
    values.extend([tenant_id, provider_credential_id])
    try:
        connection.execute(
            f"""
            UPDATE provider_credentials
            SET {', '.join(updates)}
            WHERE tenant_id=? AND provider_credential_id=?
            """,
            values,
        )
        connection.commit()
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except sqlite3.Error:
        connection.rollback()
        return

    # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
    if _is_rate_limit_error(error_code, message, http_status):
        logger.info(
            "provider_key.rate_limited",
            extra={
                "event": "provider_key.rate_limited",
                "request_id": request_id,
                "trace_id": trace_id,
                "tenant_id": tenant_id,
                "provider_id": provider_id,
                "payload": {
                    "provider_code": provider_code,
                    "model_id": model_id,
                    "http_status": http_status,
                    "error_code": error_code,
                },
            },
        )


def _environment_provider_key(
    config: AppConfig,
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    provider_id: str,
    provider_code: str,
    route_base_url: str | None = None,
) -> str | None:
    """在严格租户和 Provider 身份边界内读取本地兼容 Key。

    参数：``tenant_id/provider_id/provider_code`` 来自可信路由图；``route_base_url`` 是调用阶段
    已冻结的 Endpoint。返回：只有非生产默认租户、数据库 Provider 与进程配置完全一致时返回
    内存中的 Key，否则返回 ``None``。关键约束：不得记录 Key、环境变量名或 Endpoint。
    """

    # 生产部署必须使用租户托管 Key；进程级环境变量无法表达租户所有权，不能作为生产凭据。
    if config.environment == "prod":
        return None
    normalized_code = str(provider_code or "").strip().lower()
    configured_base_url = config.provider_base_urls.get(normalized_code)
    environment_key = config.provider_api_keys.get(normalized_code)
    if not normalized_code or not configured_base_url or not environment_key:
        return None
    provider = connection.execute(
        """
        SELECT p.provider_code, p.base_url, p.status, t.slug, t.status AS tenant_status
        FROM providers p
        JOIN tenants t ON t.tenant_id=p.tenant_id
        WHERE p.tenant_id=? AND p.provider_id=?
        """,
        (tenant_id, provider_id),
    ).fetchone()
    # Provider 不存在表示冻结路由已失效或调用方伪造身份，环境 Key 必须按不可用处理。
    if provider is None:
        return None
    # 环境 Key 只属于启动配置指定的默认租户；第二租户即使复用 code 也不得借用该凭据。
    if (
        str(provider["slug"]) != config.default_tenant_slug
        or str(provider["tenant_status"]) != "active"
        or str(provider["status"]) != "active"
        or str(provider["provider_code"]).strip().lower() != normalized_code
    ):
        return None
    database_base_url = _canonical_base_url(str(provider["base_url"] or ""))
    if database_base_url != _canonical_base_url(configured_base_url):
        # 默认租户管理员修改 Endpoint 后也不能把全局 Key 转发到未经启动配置批准的地址。
        return None
    if route_base_url is not None and database_base_url != _canonical_base_url(route_base_url):
        # 调用阶段额外核对冻结路由，阻止伪造 GatewayRoute 绕过数据库 Provider 身份。
        return None
    return environment_key


def _canonical_base_url(value: str) -> str:
    """生成仅去除配置外围空白的 Endpoint 身份文本，不合并任何 URL 差异。"""

    # 尾斜杠、path、端口等任一差异都表示不同身份，不能用宽松 URL 归一化扩大环境 Key 权限。
    return str(value or "").strip()


def provider_key_row_is_selectable(
    row: sqlite3.Row,
    *,
    stale_seconds: int,
    now: datetime | None = None,
) -> bool:
    """公开复用单行 Key 硬资格判断，但不解密或返回任何 Credential 标识。

    路由亲和只需要确认绑定目标仍有资格参与当前快照；真正解密和最终选择仍必须在
    KeyPool 的调用前事务完成，避免路由阶段扩大明文 Key 生命周期。
    """

    return _row_is_selectable(row, stale_seconds=stale_seconds, now=now)


def _row_is_selectable(
    row: sqlite3.Row,
    *,
    stale_seconds: int,
    now: datetime | None = None,
) -> bool:
    """判断数据库 Key 是否可参与本次选择。

    新鲜认证失败的单把 Key 会被跳过；过期探测结论按 unknown Fail Open，避免调度故障永久锁死 KeyPool。
    """

    snapshot_time = (now or _utcnow()).astimezone(UTC)
    probe_time = _parse_optional_db_time(row["probe_last_tested_at"])
    if (
        row["probe_health_status"] == "unhealthy"
        and probe_time is not None
        and probe_time >= snapshot_time - timedelta(seconds=stale_seconds)
    ):
        return False

    status = str(row["status"] or "")
    if status == "active":
        return True
    if status != "rate_limited":
        return False
    rate_limited_until = _parse_optional_db_time(row["rate_limited_until"])
    return rate_limited_until is None or rate_limited_until <= snapshot_time


def _selection_sort_key(row: sqlite3.Row) -> tuple[int, str, int, str]:
    """生成 KeyPool 选择排序键。

    未使用过的 Key 优先，其次按最近使用时间和权重排序，实现 M0 阶段的轻量轮换。
    """

    last_used_at = _parse_optional_db_time(row["last_used_at"])
    used_rank = 0 if last_used_at is None else 1
    last_used_value = last_used_at.isoformat() if last_used_at else ""
    return (used_rank, last_used_value, -int(row["weight"] or 100), str(row["provider_credential_id"]))


def _record_key_selected(connection: sqlite3.Connection, tenant_id: str, provider_credential_id: str) -> None:
    """记录 Key 被选中并解除过期的限流状态。"""

    now = _to_db_time(_utcnow())
    connection.execute(
        """
        UPDATE provider_credentials
        SET status='active', last_used_at=?, rate_limited_until=NULL, updated_at=?
        WHERE tenant_id=? AND provider_credential_id=?
        """,
        (now, now, tenant_id, provider_credential_id),
    )
    connection.commit()


def _connect(config: AppConfig) -> sqlite3.Connection:
    """建立数据库连接并把底层错误转换为统一 API 错误。"""

    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _is_rate_limit_error(error_code: str, message: str, http_status: int | None) -> bool:
    """判断上游错误是否应触发 Key 冷却。"""

    if http_status == 429:
        return True
    haystack = f"{error_code} {message}".lower()
    return "rate" in haystack or "quota" in haystack or "429" in haystack


def _parse_optional_db_time(value: object) -> datetime | None:
    """解析 Provider 与 KeyPool 管理 的 parse optional db time 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """

    if not value:
        return None
    raw = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 datetime 转为数据库统一存储的 UTC 秒级字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回 Provider 与 KeyPool 管理 使用的当前 UTC 时间，去掉微秒以保持数据库字段稳定。
    
    边界：所有状态更新时间共用该口径，避免同一事务内出现混合时间格式。
    """

    return datetime.now(UTC).replace(microsecond=0)
