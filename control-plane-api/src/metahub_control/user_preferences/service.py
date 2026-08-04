"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/user_preferences/service.py
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from metahub_control.auth import AuthContext
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.i18n import available_locales, is_supported_locale

from .models import UpdateUserPreferenceRequest, UserPreferenceResponse, UserPreferenceSettings


def get_user_preferences(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> UserPreferenceResponse:
    """读取当前用户语言偏好。

    租户和用户只来自 AuthContext；接口不接受 tenant_id 或 user_id，避免用户读取其他租户的语言偏好。
    """

    try:
        connection = _connect(config)
        row = _ensure_preference(connection, context)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except (sqlite3.Error, DbError) as exc:
        # 用户偏好是控制台基础能力，数据库异常必须收敛为统一错误码并保留 request_id。
        raise ApiError("user_preference.unavailable") from exc
    return UserPreferenceResponse(
        preference=_preference_settings(row),
        request_id=request_id,
        trace_id=trace_id,
    )


def update_user_preferences(
    config: AppConfig,
    context: AuthContext,
    payload: UpdateUserPreferenceRequest,
    *,
    request_id: str,
    trace_id: str,
) -> UserPreferenceResponse:
    """更新当前用户语言偏好。

    只允许 locale 进入数据库；额外字段会被拒绝，防止前端夹带 tenant_id、user_id、Token 或业务上下文。
    """

    if payload.model_extra or payload.locale is None:
        # 偏好 API 只能更新受控字段，未知字段不能静默丢弃，否则可能隐藏前端越权尝试。
        raise ApiError("user_preference.payload_invalid")
    locale = payload.locale.strip()
    if not is_supported_locale(locale):
        # locale 是受控枚举，不能把浏览器原始 Accept-Language 或任意字符串写入业务表。
        raise ApiError("user_preference.locale_invalid")
    try:
        connection = _connect(config)
        row = _ensure_preference(connection, context)
        if row["locale"] != locale:
            _update_locale(connection, context, old_locale=row["locale"], new_locale=locale, request_id=request_id, trace_id=trace_id)
        refreshed = _load_preference(connection, context.user.tenant_id, context.user.user_id)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except (sqlite3.Error, DbError) as exc:
        # 更新失败时不能暴露 SQL 或路径，只返回用户偏好统一错误。
        raise ApiError("user_preference.unavailable") from exc
    return UserPreferenceResponse(
        preference=_preference_settings(refreshed),
        request_id=request_id,
        trace_id=trace_id,
    )


def _ensure_preference(connection: sqlite3.Connection, context: AuthContext) -> sqlite3.Row:
    """确保当前用户存在偏好记录，缺失时创建默认中文偏好。"""

    row = _load_preference(connection, context.user.tenant_id, context.user.user_id, missing_ok=True)
    if row is not None:
        return row
    now = _to_db_time(_utcnow())
    connection.execute(
        """
        INSERT INTO user_preferences(
          preference_id, tenant_id, user_id, active_navigation_item_id, sidebar_collapsed, locale, created_at, updated_at
        )
        VALUES(?, ?, ?, 'overview', 0, 'zh-CN', ?, ?)
        """,
        (f"upr_{uuid4().hex}", context.user.tenant_id, context.user.user_id, now, now),
    )
    connection.commit()
    # 新建后按同一 tenant_id/user_id 重新读取，保证返回结构和普通读取路径一致。
    return _load_preference(connection, context.user.tenant_id, context.user.user_id)


def _load_preference(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
    *,
    missing_ok: bool = False,
) -> sqlite3.Row:
    """按租户和用户读取偏好记录。"""

    row = connection.execute(
        """
        SELECT preference_id, tenant_id, user_id, locale, created_at, updated_at
        FROM user_preferences
        WHERE tenant_id=? AND user_id=?
        """,
        (tenant_id, user_id),
    ).fetchone()
    if row is None and not missing_ok:
        # 偏好缺失通常说明迁移或 seed 状态异常，由上层统一映射为 user_preference.unavailable。
        raise DbError("db.query_failed", "User preference row is missing.", "Check user preference seed state.")
    return row


# locale 变更需要和审计事件同事务提交，避免偏好已改但事件缺失。
def _update_locale(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    old_locale: str,
    new_locale: str,
    request_id: str,
    trace_id: str,
) -> None:
    """更新当前用户 locale 并写入 user.locale_changed 审计事件。

    该函数把偏好表更新和事件表写入放在同一个提交点，避免语言切换成功但审计链路缺失。
    """

    # locale 变更事件只记录稳定枚举和链路 ID，不写入任何自由文本、Token 或前端上下文。
    now = _to_db_time(_utcnow())
    connection.execute(
        """
        UPDATE user_preferences
        SET locale=?, updated_at=?
        WHERE tenant_id=? AND user_id=?
        """,
        (new_locale, now, context.user.tenant_id, context.user.user_id),
    )
    connection.execute(
        """
        INSERT INTO user_preference_events(
          event_id, tenant_id, user_id, event_type, old_locale, new_locale,
          request_id, trace_id, payload_json, created_at
        )
        VALUES(?, ?, ?, 'user.locale_changed', ?, ?, ?, ?, ?, ?)
        """,
        (
            f"upe_{uuid4().hex}",
            context.user.tenant_id,
            context.user.user_id,
            old_locale,
            new_locale,
            request_id,
            trace_id,
            json.dumps({"old_locale": old_locale, "new_locale": new_locale}, ensure_ascii=False, sort_keys=True),
            now,
        ),
    )
    connection.commit()


def _preference_settings(row: sqlite3.Row) -> UserPreferenceSettings:
    """把数据库用户偏好行转换为 API DTO。"""

    return UserPreferenceSettings(
        preference_id=row["preference_id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        locale=row["locale"],
        available_locales=available_locales(),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接控制面数据库，连接失败交给调用方映射为用户偏好统一错误。"""

    return connect_database(config.database_url)


def _utcnow() -> datetime:
    """返回 UTC 当前时间，统一用户偏好和事件时间口径。"""

    return datetime.now(UTC).replace(microsecond=0)


def _to_db_time(value: datetime) -> str:
    """把 datetime 转为数据库使用的 UTC ISO 字符串。"""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# 数据库时间解析集中在这里，避免各响应字段出现不一致的时区处理。
def _parse_db_time(value: object) -> datetime:
    """解析数据库 UTC ISO 时间字符串并归一到带时区 datetime。

    控制面响应必须保持统一 UTC 语义，避免 SQLite 朴素时间和 ISO 字符串混用造成前端展示不稳定。
    """

    # SQLite 可能返回朴素时间字符串，统一归一到 UTC 以保持 API 响应时区稳定。
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
