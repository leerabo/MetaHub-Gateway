"""PRD-055 当前用户私有 SavedFilter 的租户隔离 CRUD。

admin 与 developer 都只能操作自己的 active 记录。所有读写条件同时携带 tenant_id、
owner_user_id 和 saved_filter_id；跨租户、同租户其他用户、已删除和不存在 ID 返回
同形 404，避免通过响应差异枚举个人筛选。
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext
from metahub_control.auth.resource_transactions import ResourceWriteTransaction

from .models import SavedFilterInfo, SavedFilterListResponse, SavedFilterResponse
from .query import (
    _db_time,
    _saved_filter_identifier,
    _sort_direction,
    _sort_field,
    _unique_object,
    canonical_filters,
    normalize_filters,
)
from .service import _connect

# SavedFilter 写入字段白名单在入口冻结，避免未知字段绕过版本锁和持久化语义。
CREATE_KEYS = frozenset({"name", "filters", "sort_by", "sort_direction"})
PATCH_KEYS = CREATE_KEYS | {"version"}


def list_saved_filters(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> SavedFilterListResponse:
    """列出当前认证用户最多五十条 active 筛选，绝不扩大到同租户其他用户。"""

    connection = _connect(config)
    try:
        transaction = ResourceWriteTransaction(
            connection, config, context, "request_log.read", read_only=True,
        )
        transaction.begin()
        context = transaction.context
        rows = connection.execute(
            """
            SELECT saved_filter_id, name, filter_json, sort_by, sort_direction,
                   version, created_at, updated_at
            FROM saved_filters
            WHERE tenant_id=? AND owner_user_id=? AND status='active'
            ORDER BY updated_at DESC, saved_filter_id DESC
            LIMIT 50
            """,
            (context.user.tenant_id, context.user.user_id),
        ).fetchall()
        result = SavedFilterListResponse(
            saved_filters=[_saved_filter_info(row) for row in rows],
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.rollback()
        transaction.begin()
        connection.rollback()
        return result
    # 持久记录损坏已经转换成稳定业务错误，继续原样抛出才能保留统一错误码。
    except ApiError:
        raise
    # 数据库查询异常不得携带 SQL 或底层结构返回客户端，统一失败关闭为服务不可用。
    except sqlite3.Error as exc:
        raise ApiError("saved_filter.unavailable") from exc
    finally:
        connection.close()


def create_saved_filter(
    config: AppConfig,
    context: AuthContext,
    body: object,
    *,
    request_id: str,
    trace_id: str,
) -> SavedFilterResponse:
    """创建当前用户个人筛选，并在事务内执行数量和名称冲突检查。"""

    data = _body(body, CREATE_KEYS, required=CREATE_KEYS)
    name, normalized_name = _name(data["name"])
    filters = normalize_filters(data["filters"], error_code="saved_filter.invalid")
    sort_by = _saved_sort_field(data["sort_by"])
    direction = _saved_sort_direction(data["sort_direction"])
    filter_json = canonical_filters(filters)
    saved_filter_id = "sf_" + uuid4().hex
    now = _db_time(datetime.now(UTC))
    connection = _connect(config)
    try:
        transaction = ResourceWriteTransaction(connection, config, context, "request_log.read")
        transaction.begin()
        context = transaction.context
        count = connection.execute(
            """
            SELECT COUNT(*) AS count FROM saved_filters
            WHERE tenant_id=? AND owner_user_id=? AND status='active'
            """,
            (context.user.tenant_id, context.user.user_id),
        ).fetchone()
        if int(count["count"] if count else 0) >= 50:
            # 数量上限按当前租户与 owner 计算，达到上限后禁止继续占用个人筛选配额。
            raise ApiError("saved_filter.limit_exceeded")
        if _name_exists(connection, context, normalized_name):
            # 名称唯一性仅在当前租户与 owner 内生效，冲突时不泄露其他主体记录。
            raise ApiError("saved_filter.name_conflict")
        connection.execute(
            """
            INSERT INTO saved_filters(
              saved_filter_id, tenant_id, owner_user_id, name, name_normalized,
              filter_version, filter_json, sort_by, sort_direction, version,
              status, created_at, updated_at, deleted_at
            )
            VALUES(?, ?, ?, ?, ?, 1, ?, ?, ?, 1, 'active', ?, ?, NULL)
            """,
            (
                saved_filter_id,
                context.user.tenant_id,
                context.user.user_id,
                name,
                normalized_name,
                filter_json,
                sort_by,
                direction,
                now,
                now,
            ),
        )
        row = _load_active(connection, context, saved_filter_id)
        transaction.commit()
    # 业务校验失败也必须回滚已获取的写锁，避免连接关闭前留下不完整事务状态。
    except ApiError:
        _rollback(connection)
        raise
    # 数据库触发器和唯一索引是并发兜底，需要脱敏映射成稳定的领域错误码。
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        raise _integrity_error(exc) from exc
    # 其他 SQLite 故障不暴露表名或 SQL 文本，回滚后统一返回可重试存储错误。
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("saved_filter.unavailable") from exc
    finally:
        connection.close()
    return SavedFilterResponse(
        saved_filter=_saved_filter_info(row),
        request_id=request_id,
        trace_id=trace_id,
    )


def update_saved_filter(
    config: AppConfig,
    context: AuthContext,
    saved_filter_id: str,
    body: object,
    *,
    request_id: str,
    trace_id: str,
) -> SavedFilterResponse:
    """使用乐观 version 更新自己的 active SavedFilter，deleted 记录永不复活。"""

    identifier = _saved_filter_identifier(saved_filter_id)
    data = _body(body, PATCH_KEYS, required={"version"})
    if len(data) == 1:
        # 只有 version 而没有实际变更字段会制造空版本，必须作为无效更新拒绝。
        raise ApiError("saved_filter.invalid")
    version = _version(data["version"])
    connection = _connect(config)
    try:
        transaction = ResourceWriteTransaction(connection, config, context, "request_log.read")
        transaction.begin()
        context = transaction.context
        current = _load_active(connection, context, identifier, required=False)
        if current is None:
            # 不存在、已删除及越过 owner 边界统一返回同形错误，阻断横向记录枚举。
            raise ApiError("saved_filter.not_found")
        current_info = _saved_filter_info(current)
        if current_info.version != version:
            # 客户端版本落后时禁止覆盖较新数据，必须由调用方刷新后重新提交修改。
            raise ApiError("saved_filter.version_conflict")
        name, normalized_name = (
            _name(data["name"])
            if "name" in data
            else (current_info.name, _normalize_name(current_info.name))
        )
        if _name_exists(connection, context, normalized_name, exclude_id=identifier):
            # 更新后的规范化名称仍需满足当前 owner 唯一约束，避免并发产生视觉重名。
            raise ApiError("saved_filter.name_conflict")
        filters = (
            normalize_filters(data["filters"], error_code="saved_filter.invalid")
            if "filters" in data
            else current_info.filters
        )
        sort_by = (
            _saved_sort_field(data["sort_by"])
            if "sort_by" in data
            else current_info.sort_by
        )
        direction = (
            _saved_sort_direction(data["sort_direction"])
            if "sort_direction" in data
            else current_info.sort_direction
        )
        updated_at = _next_time(current["updated_at"])
        changed = connection.execute(
            """
            UPDATE saved_filters
            SET name=?, name_normalized=?, filter_json=?, sort_by=?, sort_direction=?,
                version=version+1, updated_at=?
            WHERE tenant_id=? AND owner_user_id=? AND saved_filter_id=?
              AND status='active' AND version=?
            """,
            (
                name,
                normalized_name,
                canonical_filters(filters),
                sort_by,
                direction,
                updated_at,
                context.user.tenant_id,
                context.user.user_id,
                identifier,
                version,
            ),
        )
        if changed.rowcount != 1:
            # 条件更新未命中说明版本或状态在事务边界内变化，绝不能伪造成功响应。
            raise ApiError("saved_filter.version_conflict")
        row = _load_active(connection, context, identifier)
        transaction.commit()
    # 领域错误需回滚当前写事务并保留原错误码，方便前端区分冲突与输入错误。
    except ApiError:
        _rollback(connection)
        raise
    # 并发唯一约束和触发器拒绝通过稳定业务码返回，不能泄露数据库实现细节。
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        raise _integrity_error(exc) from exc
    # 未分类数据库故障统一回滚并失败关闭，防止部分更新或底层异常信息外泄。
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("saved_filter.unavailable") from exc
    finally:
        connection.close()
    return SavedFilterResponse(
        saved_filter=_saved_filter_info(row),
        request_id=request_id,
        trace_id=trace_id,
    )


def delete_saved_filter(
    config: AppConfig,
    context: AuthContext,
    saved_filter_id: str,
    version: object,
) -> None:
    """以乐观锁软删除自己的 active SavedFilter；不物理删除任何审计关联事实。"""

    identifier = _saved_filter_identifier(saved_filter_id)
    expected = _query_version(version)
    connection = _connect(config)
    try:
        transaction = ResourceWriteTransaction(connection, config, context, "request_log.read")
        transaction.begin()
        context = transaction.context
        current = _load_active(connection, context, identifier, required=False)
        if current is None:
            # 删除也采用同形不存在响应，不能暴露跨租户、其他 owner 或历史删除状态。
            raise ApiError("saved_filter.not_found")
        current_info = _saved_filter_info(current)
        if current_info.version != expected:
            # 删除必须携带最新乐观锁版本，避免旧页面误删已被其他会话修改的筛选。
            raise ApiError("saved_filter.version_conflict")
        # current_info 已完成持久值校验；仍传原始 canonical 文本给单调时间推进器。
        timestamp = _next_time(current["updated_at"])
        changed = connection.execute(
            """
            UPDATE saved_filters
            SET status='deleted', deleted_at=?, version=version+1, updated_at=?
            WHERE tenant_id=? AND owner_user_id=? AND saved_filter_id=?
              AND status='active' AND version=?
            """,
            (
                timestamp,
                timestamp,
                context.user.tenant_id,
                context.user.user_id,
                identifier,
                expected,
            ),
        )
        if changed.rowcount != 1:
            # 状态或版本并发变化时条件更新不会命中，此时必须报告冲突而不是成功。
            raise ApiError("saved_filter.version_conflict")
        transaction.commit()
    # 删除流程中的领域错误同样要释放写事务，同时保留稳定错误码和同形边界。
    except ApiError:
        _rollback(connection)
        raise
    # SQLite 删除失败需要回滚并脱敏，避免向客户端返回 SQL、表名或触发器文本。
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("saved_filter.unavailable") from exc
    finally:
        connection.close()


def _body(value: object, allowed: frozenset[str], *, required: set[str] | frozenset[str]) -> dict[str, Any]:
    """严格验证 JSON object 字段集合；未知字段不能被静默忽略。"""

    if not isinstance(value, dict) or set(value) - allowed or not set(required).issubset(value):
        # 非对象、未知字段或缺少必填项都会造成写入语义不完整，因此统一拒绝。
        raise ApiError("saved_filter.invalid")
    return value


def _name(value: object) -> tuple[str, str]:
    """规范化显示名和唯一键，拒绝控制字符及 NFKC/casefold 后的越界值。"""

    if not isinstance(value, str):
        # 名称只接受 JSON 字符串，禁止依靠隐式类型转换形成不可预测的唯一键。
        raise ApiError("saved_filter.invalid")
    name = value.strip()
    if not name or len(name) > 64 or any(unicodedata.category(char).startswith("C") for char in name):
        # 空名、超长值和控制字符会破坏展示及审计可读性，必须在持久化前阻断。
        raise ApiError("saved_filter.invalid")
    normalized = _normalize_name(name)
    if not normalized or len(normalized) > 64:
        # 规范化后的值才是唯一索引口径，越界时不能仅凭原始显示名放行。
        raise ApiError("saved_filter.invalid")
    return name, normalized


def _normalize_name(name: str) -> str:
    """生成 active 名称唯一键；NFKC 与 casefold 防止视觉等价名称重复。"""

    return unicodedata.normalize("NFKC", name).casefold().strip()


def _version(value: object) -> int:
    """JSON body 乐观版本只接受原生正整数，拒绝 bool、字符串和浮点截断。"""

    if type(value) is not int or value < 1 or value > 9_223_372_036_854_775_807:
        # 严格整数边界与 SQLite 版本列一致，避免布尔值或溢出数绕过乐观锁。
        raise ApiError("saved_filter.invalid")
    return value


def _query_version(value: object) -> int:
    """严格解析 DELETE query 的 ASCII 正整数版本，不复用 JSON 数值宽松语义。"""

    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        # 查询参数必须是规范 ASCII 十进制文本，拒绝 Unicode 数字和隐式数值转换。
        raise ApiError("saved_filter.invalid")
    if len(value) > 1 and value.startswith("0"):
        # 多种文本表示同一版本会产生签名与缓存歧义，因此不接受前导零形式。
        raise ApiError("saved_filter.invalid")
    parsed = int(value)
    if parsed < 1 or parsed > 9_223_372_036_854_775_807:
        # 解析后仍需匹配数据库正整数范围，防止超界版本进入条件更新语句。
        raise ApiError("saved_filter.invalid")
    return parsed


def _saved_sort_field(value: object) -> str:
    """SavedFilter 排序字段错误归入 saved_filter.invalid，不泄漏内部 query 解析分层。"""

    if not isinstance(value, str):
        # 排序字段只接受字符串枚举，禁止隐式转换掩盖调用方协议错误。
        raise ApiError("saved_filter.invalid")
    try:
        return _sort_field(value)
    # 底层查询解析器使用通用错误码，SavedFilter API 必须收敛为自身输入错误。
    except ApiError as exc:
        raise ApiError("saved_filter.invalid") from exc


def _saved_sort_direction(value: object) -> str:
    """校验 SavedFilter 排序方向，并隔离底层查询解析器的通用错误码。"""

    if not isinstance(value, str):
        # 排序方向必须是协议定义字符串，其他类型不能通过隐式转换进入持久层。
        raise ApiError("saved_filter.invalid")
    try:
        return _sort_direction(value)
    # 查询层错误码属于内部实现，保存接口需要转换成稳定的领域输入错误。
    except ApiError as exc:
        raise ApiError("saved_filter.invalid") from exc


def _load_active(
    connection: sqlite3.Connection,
    context: AuthContext,
    saved_filter_id: str,
    *,
    required: bool = True,
) -> sqlite3.Row | None:
    """按完整 owner 边界读取 active 行，阻断跨租户和同租户横向访问。"""

    row = connection.execute(
        """
        SELECT saved_filter_id, name, filter_json, sort_by, sort_direction,
               version, created_at, updated_at
        FROM saved_filters
        WHERE tenant_id=? AND owner_user_id=? AND saved_filter_id=? AND status='active'
        """,
        (context.user.tenant_id, context.user.user_id, saved_filter_id),
    ).fetchone()
    if row is None and required:
        # 完整 owner 条件未命中时统一视为不存在，避免泄露跨租户和他人记录。
        raise ApiError("saved_filter.not_found")
    return row


def _name_exists(
    connection: sqlite3.Connection,
    context: AuthContext,
    normalized_name: str,
    *,
    exclude_id: str | None = None,
) -> bool:
    """检查当前 owner 的 active 同名，软删除记录不阻止新 ID 复用显示名。"""

    row = connection.execute(
        """
        SELECT 1 FROM saved_filters
        WHERE tenant_id=? AND owner_user_id=? AND name_normalized=? AND status='active'
          AND (CAST(? AS TEXT) IS NULL OR saved_filter_id!=?)
        LIMIT 1
        """,
        (
            context.user.tenant_id,
            context.user.user_id,
            normalized_name,
            exclude_id,
            exclude_id,
        ),
    ).fetchone()
    return row is not None


def _stored_filters(value: object) -> dict[str, Any]:
    """重新校验持久 JSON；损坏数据不能直接进入 API 或后续查询。"""

    try:
        parsed = json.loads(str(value), object_pairs_hook=_unique_object)
        return normalize_filters(parsed, error_code="saved_filter.unavailable")
    # 已落库 JSON 的解析或领域校验失败都表示存储损坏，不能归责为调用方输入错误。
    except (ApiError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ApiError("saved_filter.unavailable") from exc


def _saved_filter_info(row: sqlite3.Row) -> SavedFilterInfo:
    """校验并映射持久行；tenant/owner/status/deleted_at 永不暴露给页面。"""

    try:
        saved_filter_id = _saved_filter_identifier(str(row["saved_filter_id"]))
        raw_name = str(row["name"])
        name, _ = _name(raw_name)
        if name != raw_name:
            # 持久名称必须已是规范显示值，读取时不得静默修剪后向客户端伪装正常。
            raise ApiError("saved_filter.unavailable")
        filters = _stored_filters(row["filter_json"])
        sort_by = _stored_sort_field(row["sort_by"])
        direction = _stored_sort_direction(row["sort_direction"])
        version = _version(row["version"])
        created_at = _parse_time(row["created_at"])
        updated_at = _parse_time(row["updated_at"])
        if updated_at < created_at:
            # 更新时间早于创建时间说明审计时序已经损坏，必须失败关闭而非错误排序。
            raise ApiError("saved_filter.unavailable")
        return SavedFilterInfo(
            saved_filter_id=saved_filter_id,
            name=name,
            filters=filters,
            sort_by=sort_by,
            sort_direction=direction,
            version=version,
            created_at=created_at,
            updated_at=updated_at,
        )
    # 领域解析异常需区分已映射存储错误与输入错误，防止错误码语义向外漂移。
    except ApiError as exc:
        if exc.code == "saved_filter.unavailable":
            raise
        # ID、名称、排序或版本若由数据库读出后不合法，属于持久层损坏而非 422 输入错误。
        raise ApiError("saved_filter.unavailable") from exc
    # 缺列或持久值类型异常同样属于数据库契约损坏，统一转换为可重试存储错误。
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError("saved_filter.unavailable") from exc


def _stored_sort_field(value: object) -> str:
    """严格验证持久排序字段，拒绝通过 strip 修复损坏数据。"""

    if not isinstance(value, str):
        # 数据库中的排序字段若不是文本已违反迁移契约，禁止在读取时猜测修复。
        raise ApiError("saved_filter.unavailable")
    try:
        normalized = _sort_field(value)
    # 通用排序解析失败在持久读取路径中代表数据损坏，需要转换成服务不可用。
    except ApiError as exc:
        raise ApiError("saved_filter.unavailable") from exc
    if normalized != value:
        # 持久值必须与规范枚举完全一致，静默 trim 会隐藏外部写入或迁移错误。
        raise ApiError("saved_filter.unavailable")
    return normalized


def _stored_sort_direction(value: object) -> str:
    """严格验证持久排序方向，损坏值统一映射为可重试 503。"""

    if not isinstance(value, str):
        # 数据库排序方向必须是规范文本，类型漂移意味着持久契约已经失效。
        raise ApiError("saved_filter.unavailable")
    try:
        normalized = _sort_direction(value)
    # 查询解析错误不能直接穿透 SavedFilter 读取接口，统一转为持久层不可用。
    except ApiError as exc:
        raise ApiError("saved_filter.unavailable") from exc
    if normalized != value:
        # 非规范大小写或空白说明数据绕过了写入口，读取端必须拒绝而非修正。
        raise ApiError("saved_filter.unavailable")
    return normalized


def _next_time(previous: object) -> str:
    """触发器要求 updated_at 单调递增；同秒更新至少推进一秒。"""

    old = _parse_time(previous)
    return _db_time(max(datetime.now(UTC), old + timedelta(seconds=1)))


def _parse_time(value: object) -> datetime:
    """解析内部 UTC 时间；损坏持久值统一关闭为 SavedFilter 存储错误。"""

    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    # 无法解析的内部时间会破坏排序和版本单调性，必须映射为脱敏存储错误。
    except ValueError as exc:
        raise ApiError("saved_filter.unavailable") from exc
    if parsed.tzinfo is None or _db_time(parsed) != raw:
        # 服务只写秒级 UTC Z；不默默修复无时区或非规范文本，避免排序与乐观锁语义漂移。
        raise ApiError("saved_filter.unavailable")
    return parsed.astimezone(UTC)


def _integrity_error(exc: sqlite3.IntegrityError) -> ApiError:
    """把已知数据库门禁折叠为稳定业务码，不回显 trigger 或 SQL 文本。"""

    text = str(exc)
    if "saved_filter_owner_or_limit_invalid" in text:
        return ApiError("saved_filter.limit_exceeded")
    if "UNIQUE constraint failed" in text or "saved_filter_immutable" in text:
        return ApiError("saved_filter.name_conflict")
    return ApiError("saved_filter.unavailable")


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚失败事务，原始 SQLite 异常由调用处统一脱敏。"""

    try:
        connection.rollback()
    # 回滚失败不能覆盖最初业务异常；连接随后关闭，由调用处返回已确定的稳定错误码。
    except sqlite3.Error:
        pass
