"""PRD-063 Provider Adapter 目录查询与租户级激活服务。

Shared SDK 目录是唯一可执行来源；数据库只保存租户对某一冻结版本的激活与幂等事实。
查询、重放和切换始终以 tenant_id 为首条件，Webhook 是提交后的尽力副作用，失败不能撤销
已经提交的目录版本。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from metahub_control.auth import AuthContext
from metahub_control.webhooks.service import dispatch_webhook_event
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.provider_adapter_spec import (
    ACTIVE,
    CLASSIFICATIONS,
    OPTIONAL,
    ProviderAdapterSpec,
    catalog_identity,
    provider_adapter_specs,
)
from metahub_shared.runtime.webhooks import build_webhook_event

from .models import (
    AdapterSpecActivationInfo,
    AdapterSpecActivationResponse,
    AdapterSpecCatalogInfo,
    AdapterSpecListResponse,
    ProviderAdapterSpecInfo,
)

logger = logging.getLogger("metahub.control.adapter_specs")

# actor 是跨租户发布服务的固定机器身份，不接受 path、Header 或请求正文覆盖。
ACTIVATION_ACTOR_ID = "adapter-spec-publisher"
_CATALOG_VERSION = re.compile(r"[a-z][a-z0-9_.-]{2,79}\Z")
_CATALOG_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TENANT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def list_adapter_specs(
    config: AppConfig,
    context: AuthContext,
    *,
    classification: str | None,
    status: str | None,
    request_id: str,
    trace_id: str,
) -> AdapterSpecListResponse:
    """返回当前会话租户已激活的冻结 Adapter 目录。

    参数中的筛选只能收窄 Shared SDK 目录，tenant_id 始终取实时认证上下文。数据库 active
    身份与进程目录任一字段漂移都会返回 503，避免页面展示尚未对该租户激活的新代码合同。
    """

    normalized_classification = _normalize_classification(classification)
    normalized_status = _normalize_status(status)
    identity = catalog_identity()
    connection = _connect(config)
    try:
        rows = connection.execute(
            """
            SELECT catalog_version,catalog_hash,spec_count,activated_at
            FROM provider_adapter_catalog_activations
            WHERE tenant_id=? AND status='active'
            ORDER BY activated_at DESC,activation_id DESC
            """,
            (context.user.tenant_id,),
        ).fetchall()
    # SQL、数据库路径和迁移细节不能穿透到授权页面；统一 503 仍由中间件补齐链路 ID。
    except sqlite3.Error:
        raise ApiError("adapter.spec_catalog_unavailable") from None
    finally:
        connection.close()
    if len(rows) != 1:
        # 缺失或多个 active 都表示租户目录事实不可信，不能选择其中一条继续展示。
        raise ApiError("adapter.spec_catalog_unavailable")
    row = rows[0]
    if (
        row["catalog_version"] != identity.version
        or row["catalog_hash"] != identity.catalog_hash
        or type(row["spec_count"]) is not int
        or row["spec_count"] != identity.spec_count
    ):
        # 目录三元组必须完整一致；只比较 version 会漏掉同版本内容漂移。
        raise ApiError("adapter.spec_catalog_unavailable")
    activated_at = _parse_db_time(row["activated_at"])
    specs = [
        _spec_info(spec, tenant_id=context.user.tenant_id)
        for spec in provider_adapter_specs()
        if (normalized_classification is None or spec.classification == normalized_classification)
        and (normalized_status is None or spec.status == normalized_status)
    ]
    return AdapterSpecListResponse(
        catalog=AdapterSpecCatalogInfo(
            version=identity.version,
            hash=identity.catalog_hash,
            activated_at=activated_at,
        ),
        specs=specs,
        request_id=request_id,
        trace_id=trace_id,
    )


def activate_adapter_spec_catalog(
    config: AppConfig,
    tenant_id: str,
    *,
    catalog_version: str,
    catalog_hash: str,
    request_id: str,
    trace_id: str,
) -> AdapterSpecActivationResponse:
    """幂等激活当前进程目录，并在真实版本切换后发送治理事件。

    重放查询先于“是否仍是当前代码目录”的判断，因此代码升级后旧 request_id 仍可精确返回
    原事实。相同版本的新 request 也写入不可变 no-op 收据，避免后续升级后失去幂等证据；该
    收据从未成为 active，也不会触发 ``adapter.spec_updated``。
    """

    normalized_tenant = _normalize_tenant_id(tenant_id)
    normalized_version = _normalize_catalog_version(catalog_version)
    normalized_hash = _normalize_catalog_hash(catalog_hash)
    connection = _connect(config)
    event_data: tuple[AdapterSpecActivationInfo, str] | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM tenants WHERE tenant_id=?",
            (normalized_tenant,),
        ).fetchone() is None:
            # path tenant 不存在和非法格式都保持同一 404，避免内部接口形成租户枚举信号。
            raise ApiError("adapter.spec_tenant_not_found")

        replay = connection.execute(
            """
            SELECT * FROM provider_adapter_catalog_activations
            WHERE tenant_id=? AND request_id=?
            """,
            (normalized_tenant, request_id),
        ).fetchone()
        if replay is not None:
            if (
                replay["catalog_version"] != normalized_version
                or replay["catalog_hash"] != normalized_hash
            ):
                # 同一 request_id 绑定不同目录身份属于幂等冲突，绝不能覆盖原始收据。
                raise ApiError("adapter.spec_activation_conflict")
            result = _activation_response(
                replay,
                changed=bool(replay["changed"]),
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.rollback()
            return result

        identity = catalog_identity()
        if (
            normalized_version != identity.version
            or normalized_hash != identity.catalog_hash
        ):
            # 激活接口只接受当前二进制冻结身份，不能上传或选择任意历史目录。
            raise ApiError("adapter.spec_activation_invalid")

        active = connection.execute(
            """
            SELECT * FROM provider_adapter_catalog_activations
            WHERE tenant_id=? AND status='active'
            """,
            (normalized_tenant,),
        ).fetchone()
        if active is not None and active["catalog_version"] == identity.version and (
            active["catalog_hash"] != identity.catalog_hash
            or active["spec_count"] != identity.spec_count
        ):
            # catalog_version 是冻结发布身份；相同版本下 hash/count 漂移表示发布制品没有
            # 正确升版。若继续提交，changed 会变为 true，但 exact Webhook 会拒绝“同版本
            # 更新”，最终形成已经切换却没有治理事件的不可审计状态，因此必须在事务内阻断。
            raise ApiError("adapter.spec_catalog_invalid")
        now = _db_time(_utcnow())
        activation_id = f"paca_{uuid4().hex}"
        changed = active is not None and (
            active["catalog_version"] != identity.version
            or active["catalog_hash"] != identity.catalog_hash
            or active["spec_count"] != identity.spec_count
        )
        if active is not None and not changed:
            # 同版本新 request 仍须留下不可变收据；status=superseded 表示它从未替代 active。
            _insert_activation(
                connection,
                activation_id=activation_id,
                tenant_id=normalized_tenant,
                catalog_version=identity.version,
                catalog_hash=identity.catalog_hash,
                spec_count=identity.spec_count,
                status="superseded",
                changed=False,
                request_id=request_id,
                trace_id=trace_id,
                activated_at=now,
                superseded_at=now,
            )
        else:
            previous_version = str(active["catalog_version"]) if active is not None else ""
            if active is not None:
                # 旧 active 的核心身份保持不可变，只允许在同一事务完成一次 active->superseded。
                connection.execute(
                    """
                    UPDATE provider_adapter_catalog_activations
                    SET status='superseded',superseded_at=?,updated_at=?
                    WHERE tenant_id=? AND activation_id=? AND status='active'
                    """,
                    (now, now, normalized_tenant, active["activation_id"]),
                )
            _insert_activation(
                connection,
                activation_id=activation_id,
                tenant_id=normalized_tenant,
                catalog_version=identity.version,
                catalog_hash=identity.catalog_hash,
                spec_count=identity.spec_count,
                status="active",
                changed=changed,
                request_id=request_id,
                trace_id=trace_id,
                activated_at=now,
                superseded_at=None,
            )
            if changed:
                event_data = (
                    AdapterSpecActivationInfo(
                        activation_id=activation_id,
                        tenant_id=normalized_tenant,
                        catalog_version=identity.version,
                        catalog_hash=identity.catalog_hash,
                        spec_count=identity.spec_count,
                        status="active",
                        activated_by=ACTIVATION_ACTOR_ID,
                        activated_at=_parse_db_time(now),
                        superseded_at=None,
                    ),
                    previous_version,
                )
        row = connection.execute(
            """
            SELECT * FROM provider_adapter_catalog_activations
            WHERE tenant_id=? AND activation_id=?
            """,
            (normalized_tenant, activation_id),
        ).fetchone()
        if row is None:
            # 事务内写后不可回读说明约束或触发器异常，禁止返回未经证明的成功结果。
            raise sqlite3.IntegrityError("activation write missing")
        connection.commit()
    # 业务错误也必须回滚 BEGIN IMMEDIATE，避免失败重放遗留锁或部分状态。
    except ApiError:
        connection.rollback()
        raise
    # 唯一约束竞争与其他结构约束需分别映射，且都不能暴露 SQLite 原始文本。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # request 唯一键竞争只暴露稳定冲突；其他 trigger/约束异常统一为目录不可用。
        if "request_id" in str(exc).lower() or "unique" in str(exc).lower():
            raise ApiError("adapter.spec_activation_conflict") from None
        raise ApiError("adapter.spec_catalog_unavailable") from None
    # 非约束类数据库错误统一回滚并返回稳定 503，不把 SQL 或本地路径写入响应。
    except sqlite3.Error:
        connection.rollback()
        raise ApiError("adapter.spec_catalog_unavailable") from None
    finally:
        connection.close()

    result = _activation_response(
        row,
        changed=bool(row["changed"]),
        request_id=request_id,
        trace_id=trace_id,
    )
    if event_data is not None:
        _dispatch_spec_updated(
            config,
            event_data[0],
            previous_catalog_version=event_data[1],
            request_id=request_id,
            trace_id=trace_id,
        )
    return result


def _insert_activation(
    connection: sqlite3.Connection,
    *,
    activation_id: str,
    tenant_id: str,
    catalog_version: str,
    catalog_hash: str,
    spec_count: int,
    status: str,
    changed: bool,
    request_id: str,
    trace_id: str,
    activated_at: str,
    superseded_at: str | None,
) -> None:
    """写入一条租户激活或 no-op 收据，所有字段由服务端受控值构造。"""

    connection.execute(
        """
        INSERT INTO provider_adapter_catalog_activations(
          activation_id,tenant_id,catalog_version,catalog_hash,spec_count,status,changed,
          request_id,trace_id,activated_by,activated_at,superseded_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            activation_id,
            tenant_id,
            catalog_version,
            catalog_hash,
            spec_count,
            status,
            1 if changed else 0,
            request_id,
            trace_id,
            ACTIVATION_ACTOR_ID,
            activated_at,
            superseded_at,
            activated_at,
            activated_at,
        ),
    )


def _dispatch_spec_updated(
    config: AppConfig,
    activation: AdapterSpecActivationInfo,
    *,
    previous_catalog_version: str,
    request_id: str,
    trace_id: str,
) -> None:
    """提交后尽力派发精确目录切换事件，失败日志不包含异常、URL 或响应正文。"""

    try:
        event = build_webhook_event(
            tenant_id=activation.tenant_id,
            event_type="adapter.spec_updated",
            payload={
                "catalog_version": activation.catalog_version,
                "catalog_hash": activation.catalog_hash,
                "previous_catalog_version": previous_catalog_version,
                "spec_count": activation.spec_count,
                "activated_at": _db_time(activation.activated_at),
            },
            actor_id=ACTIVATION_ACTOR_ID,
            object_id=activation.activation_id,
            trace_id=trace_id,
            source_request_id=request_id,
            event_id=f"evt_{activation.activation_id}",
            timestamp=activation.activated_at,
        )
        dispatch_webhook_event(
            config,
            activation.tenant_id,
            event,
            request_id=request_id,
            trace_id=trace_id,
        )
    except Exception:
        # Webhook 是提交后的外部副作用；只记录固定安全身份，不附 exception/traceback。
        logger.error(
            "adapter spec webhook delivery unavailable",
            extra={
                "event": "adapter.spec_webhook_failed",
                "error_code": "webhook.delivery_unavailable",
                "tenant_id": activation.tenant_id,
                "object_id": activation.activation_id,
                "request_id": request_id,
                "trace_id": trace_id,
            },
        )


def _activation_response(
    row: sqlite3.Row,
    *,
    changed: bool,
    request_id: str,
    trace_id: str,
) -> AdapterSpecActivationResponse:
    """把同租户数据库事实转换为不含请求正文和内部 SQL 字段的响应。"""

    return AdapterSpecActivationResponse(
        activation=AdapterSpecActivationInfo(
            activation_id=str(row["activation_id"]),
            tenant_id=str(row["tenant_id"]),
            catalog_version=str(row["catalog_version"]),
            catalog_hash=str(row["catalog_hash"]),
            spec_count=int(row["spec_count"]),
            status=str(row["status"]),
            activated_by=str(row["activated_by"]),
            activated_at=_parse_db_time(row["activated_at"]),
            superseded_at=(
                _parse_db_time(row["superseded_at"])
                if row["superseded_at"] is not None
                else None
            ),
        ),
        changed=changed,
        request_id=request_id,
        trace_id=trace_id,
    )


def _spec_info(spec: ProviderAdapterSpec, *, tenant_id: str) -> ProviderAdapterSpecInfo:
    """投影页面需要的安全短标识，不复制 Header、完整字段族或错误规则正文。"""

    return ProviderAdapterSpecInfo(
        spec_id=spec.spec_id,
        tenant_id=tenant_id,
        provider_type=spec.provider_type,
        display_name=spec.display_name,
        classification=spec.classification,
        adapter_name=spec.adapter_name,
        adapter_version=spec.adapter_version,
        protocol_family=spec.protocol_family,
        allowed_auth_types=spec.allowed_auth_types,
        model_id_strategy=spec.model_id_strategy,
        usage_profile=spec.usage_profile_id,
        error_profile=spec.error_profile_id,
        stream_profile=spec.stream_profile_id,
        price_fields=spec.price_fields,
        probe_profile=spec.probe_profile,
        platform_examples=spec.platform_examples,
        status=spec.status,
        contract_case_id=spec.contract_case_id,
    )


def _normalize_classification(value: str | None) -> str | None:
    """校验可选四分类筛选，空字符串也视为非法而非无筛选。"""

    if value is None:
        return None
    if value not in CLASSIFICATIONS:
        # 未知分类不能降级为空结果，否则调用方无法区分拼写错误与真实无数据。
        raise ApiError("adapter.spec_filter_invalid")
    return value


def _normalize_status(value: str | None) -> str | None:
    """校验可选 active/optional 筛选，不做宽松大小写转换。"""

    if value is None:
        return None
    if value not in {ACTIVE, OPTIONAL}:
        # 状态筛选严格使用冻结枚举，禁止宽松大小写转换造成缓存键漂移。
        raise ApiError("adapter.spec_filter_invalid")
    return value


def _normalize_tenant_id(value: object) -> str:
    """校验内部 path tenant；非法与不存在统一为 404，避免形成租户枚举侧信道。"""

    # tenant 标识只允许固定 wire 字符集；格式错误与不存在都保持同一对外语义。
    if type(value) is not str or not _TENANT_ID.fullmatch(value):
        # 非法 tenant 与不存在 tenant 统一 404，防止通过格式差异探测租户命名规则。
        raise ApiError("adapter.spec_tenant_not_found")
    return value


def _normalize_catalog_version(value: object) -> str:
    """校验目录版本 wire 格式，失败不回显正文。"""

    if type(value) is not str or not _CATALOG_VERSION.fullmatch(value):
        # 版本不是自由文本；控制字符或超长值不能进入幂等事实和事件载荷。
        raise ApiError("adapter.spec_activation_invalid")
    return value


def _normalize_catalog_hash(value: object) -> str:
    """要求完整小写 SHA-256，禁止缩写、Unicode 或大写 hash。"""

    if type(value) is not str or not _CATALOG_HASH.fullmatch(value):
        # 只接受完整小写 SHA-256，避免同一目录产生多种可重放文本身份。
        raise ApiError("adapter.spec_activation_invalid")
    return value


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建短连接，并把数据库配置和路径异常折叠为目录不可用。"""

    try:
        return connect_database(config.database_url)
    # 数据库配置错误只转换为目录不可用，不能把连接路径或底层异常穿透到 API。
    except DbError as exc:
        raise ApiError("adapter.spec_catalog_unavailable") from exc


def _parse_db_time(value: object) -> datetime:
    """解析 canonical UTC 秒；损坏事实必须失败关闭而非猜测时区。"""

    if type(value) is not str or not value.endswith("Z"):
        # 非 canonical UTC 字符串不能用于事件时间绑定，拒绝猜测本地时区。
        raise ApiError("adapter.spec_catalog_unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 损坏时间事实统一返回目录不可用，不泄露持久字段原文。
    except ValueError:
        raise ApiError("adapter.spec_catalog_unavailable") from None
    if parsed.tzinfo is None or _db_time(parsed) != value:
        # 能解析但不 canonical 的时间同样拒绝，保证 API 与 Webhook 精确比较一致。
        raise ApiError("adapter.spec_catalog_unavailable")
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    """把受信时间统一编码为无微秒 UTC，供数据库、响应和事件精确绑定。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回激活事务使用的当前 UTC 秒，避免字段之间产生微秒漂移。"""

    return datetime.now(UTC).replace(microsecond=0)
