"""PRD-065 角色首页配置读取与安全聚合服务。

所有查询都先以认证上下文中的 tenant_id 作为过滤条件，并在同一 SQLite 只读快照中完成。返回值只包含固定卡片、计数和状态，
不会读取或返回 Provider Key、Token、Prompt、Response、Endpoint、邮箱及上游错误正文。
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from metahub_control.auth import AuthContext
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .catalog import (
    ROLE_DASHBOARD_VIEW_BY_CODE,
    ROLE_DASHBOARD_VIEW_CODES,
    RoleDashboardCardCode,
    RoleDashboardViewCode,
    available_view_codes,
    card_navigation_target,
)
from .models import (
    DashboardWindow,
    RoleDashboardCard,
    RoleDashboardConfigInfo,
    RoleDashboardEmptyState,
    RoleDashboardFact,
    RoleDashboardResponse,
    RoleDashboardViewOption,
)

# 首页业务日、持久配置格式、固定角色集合和状态阈值共同冻结服务口径，禁止由客户端覆盖这些治理边界。
SHANGHAI = ZoneInfo("Asia/Shanghai")
_CONFIG_ID_PATTERN = re.compile(r"^rdc_[0-9a-f]{32}$")
_ROLE_CODES = frozenset(
    {"admin", "developer", "viewer", "finance_admin", "security_admin", "ops_admin"}
)
_SUCCESS_WARNING_THRESHOLD = 95.0
_SUCCESS_CRITICAL_THRESHOLD = 90.0
_ERROR_WARNING_THRESHOLD = 5.0
_ERROR_CRITICAL_THRESHOLD = 10.0
_LATENCY_WARNING_MS = 1000
_LATENCY_CRITICAL_MS = 3000


class _RoleDashboardConfigError(ValueError):
    """表示持久化角色首页配置不满足服务合同，公开响应必须折叠为固定错误码。"""


class _RoleDashboardDataError(ValueError):
    """表示聚合依赖中的状态或数值损坏，公开响应不得包含底层行内容。"""


@dataclass(frozen=True)
class _StoredRoleDashboardConfig:
    """经过严格校验的 tenant-scoped 角色首页配置。"""

    config_id: str
    tenant_id: str
    role_code: str
    default_view_code: RoleDashboardViewCode
    allowed_view_codes: tuple[RoleDashboardViewCode, ...]
    config_revision: int


@dataclass(frozen=True)
class _BusinessDayWindow:
    """同时保存上海业务日展示边界和 UTC 数据库查询边界。"""

    start_local: datetime
    end_local: datetime
    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True)
class _AggregateSnapshot:
    """同一只读事务内得到的六类卡片及空态判定事实。"""

    cards: dict[RoleDashboardCardCode, RoleDashboardCard]
    is_empty: bool


def get_role_dashboard(
    config: AppConfig,
    context: AuthContext,
    *,
    requested_view: str | None,
    request_id: str,
    trace_id: str,
) -> RoleDashboardResponse:
    """读取当前租户、当前角色的 PAGE-092 安全聚合。

    参数中的 requested_view 只允许由路由严格解析后传入；tenant、role 和权限只能来自 AuthContext。配置、视图选择和六类聚合
    共用一个显式只读事务，避免切换视图时把不同时间点的授权配置与业务指标拼在一起。
    """

    generated_at = _utcnow()
    window = _business_day_window(generated_at)
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        # 显式 BEGIN 固定后续所有 SELECT 的 SQLite 快照；成功后使用 rollback 结束纯只读事务。
        connection.execute("BEGIN")
        stored = _load_role_config(
            connection,
            tenant_id=context.user.tenant_id,
            role_code=context.user.role,
        )
        visible_codes = available_view_codes(stored.allowed_view_codes, context.permission_codes)
        selected_code = _select_view(stored, visible_codes, requested_view)
        aggregate = _load_aggregate_snapshot(
            connection,
            config=config,
            tenant_id=context.user.tenant_id,
            permission_codes=context.permission_codes,
            window=window,
            now=generated_at,
        )
        connection.rollback()
    # 已规范化的权限或查询错误必须原样上抛，避免误折叠为依赖故障并改变 HTTP 语义。
    except ApiError:
        _rollback_quietly(connection)
        raise
    # 配置损坏统一返回固定 500，底层行值和 JSON 正文不得进入响应或日志。
    except _RoleDashboardConfigError as exc:
        _rollback_quietly(connection)
        # 对外只暴露配置合同错误码，不能泄露 tenant 配置内容或数据库结构。
        raise ApiError("dashboard.config_invalid") from exc
    # 聚合值损坏和数据库异常统一折叠为可重试 503，避免公开 SQL、路径或事实正文。
    except (_RoleDashboardDataError, sqlite3.Error, DbError) as exc:
        _rollback_quietly(connection)
        # 所有依赖失败共享稳定错误码和当前 request_id，便于排障同时保持脱敏。
        raise ApiError("dashboard.unavailable") from exc
    finally:
        _close_quietly(connection)

    view_definition = ROLE_DASHBOARD_VIEW_BY_CODE[selected_code]
    return RoleDashboardResponse(
        config=RoleDashboardConfigInfo(
            role_dashboard_config_id=stored.config_id,
            config_revision=stored.config_revision,
            default_view_code=stored.default_view_code,
            selected_view_code=selected_code,
            available_views=[
                RoleDashboardViewOption(
                    code=code,
                    label=ROLE_DASHBOARD_VIEW_BY_CODE[code].label,
                    is_default=code == stored.default_view_code,
                )
                for code in visible_codes
            ],
        ),
        window=DashboardWindow(
            timezone="Asia/Shanghai",
            start_at=window.start_local,
            end_at=window.end_local,
            generated_at=generated_at,
        ),
        cards=[aggregate.cards[card_code] for card_code in view_definition.card_order],
        empty_state=_empty_state(aggregate.is_empty, selected_code),
        request_id=request_id,
        trace_id=trace_id,
    )


def role_dashboard_event_is_current(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    view_code: object,
    config_revision: object,
) -> bool:
    """复核角色首页事件仍对应当前租户配置和实时权限。

    该检查只能证明事件提交时的配置仍有效；没有一次性 nonce 时不能把它描述为对某次 GET 的强因果证明。前端仍须在 GET 成功、
    owner 复核和 DOM 投影完成后才发送事件。
    """

    if (
        type(view_code) is not str
        or view_code not in ROLE_DASHBOARD_VIEW_CODES
        or type(config_revision) is not int
        or config_revision < 1
    ):
        return False
    try:
        stored = _load_role_config(
            connection,
            tenant_id=context.user.tenant_id,
            role_code=context.user.role,
        )
    # 事件提交时配置若已损坏或撤销，必须失败关闭，不能沿用 GET 阶段的陈旧授权。
    except _RoleDashboardConfigError:
        return False
    visible_codes = available_view_codes(stored.allowed_view_codes, context.permission_codes)
    return stored.config_revision == config_revision and view_code in visible_codes


def _load_role_config(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    role_code: str,
) -> _StoredRoleDashboardConfig:
    """按 tenant_id 首条件精确读取并验证一条角色首页配置。"""

    # tenant 和角色只能来自已认证上下文；异常 owner 不允许继续查询或回退到静态默认值。
    if type(tenant_id) is not str or not tenant_id or role_code not in _ROLE_CODES:
        raise _RoleDashboardConfigError()
    rows = connection.execute(
        """
        SELECT role_dashboard_config_id, tenant_id, role_code, default_view_code,
               allowed_view_codes_json, config_revision, status, created_at, updated_at
        FROM role_dashboard_configs
        WHERE tenant_id=? AND role_code=?
        """,
        (tenant_id, role_code),
    ).fetchall()
    # 缺失、重复或 disabled 都表示当前 tenant/role 没有可服务配置，不能回退到前端静态目录。
    if len(rows) != 1:
        raise _RoleDashboardConfigError()
    row = rows[0]
    try:
        allowed_raw = json.loads(row["allowed_view_codes_json"])
    # allowed 目录无法解析时按配置损坏处理，不能猜测或使用前端硬编码扩大可见视图。
    except (json.JSONDecodeError, TypeError) as exc:
        raise _RoleDashboardConfigError() from exc
    if (
        type(row["role_dashboard_config_id"]) is not str
        or _CONFIG_ID_PATTERN.fullmatch(row["role_dashboard_config_id"]) is None
        or row["tenant_id"] != tenant_id
        or row["role_code"] != role_code
        or row["status"] != "active"
        or type(row["config_revision"]) is not int
        or row["config_revision"] < 1
        or type(row["default_view_code"]) is not str
        or row["default_view_code"] not in ROLE_DASHBOARD_VIEW_CODES
        or type(allowed_raw) is not list
        or not 1 <= len(allowed_raw) <= len(ROLE_DASHBOARD_VIEW_CODES)
        or any(
            type(code) is not str or code not in ROLE_DASHBOARD_VIEW_CODES
            for code in allowed_raw
        )
        or len(allowed_raw) != len(set(allowed_raw))
        or row["default_view_code"] not in allowed_raw
    ):
        # owner、revision、状态或目录任一不满足冻结合同都必须整体拒绝，防止残缺配置补权。
        raise _RoleDashboardConfigError()
    created_at = _parse_utc_second(row["created_at"], config_error=True)
    updated_at = _parse_utc_second(row["updated_at"], config_error=True)
    if updated_at < created_at:
        # 时间版本倒退意味着配置事实不可信，不能向页面返回可能陈旧的授权布局。
        raise _RoleDashboardConfigError()
    return _StoredRoleDashboardConfig(
        config_id=row["role_dashboard_config_id"],
        tenant_id=tenant_id,
        role_code=role_code,
        default_view_code=row["default_view_code"],
        allowed_view_codes=tuple(allowed_raw),
        config_revision=row["config_revision"],
    )


def _select_view(
    stored: _StoredRoleDashboardConfig,
    visible_codes: tuple[RoleDashboardViewCode, ...],
    requested_view: str | None,
) -> RoleDashboardViewCode:
    """选择显式视图或执行唯一允许的 executive 安全回退。"""

    if requested_view is not None:
        # 已知但未配置或已被实时撤权的视图统一返回 403，不能靠分段按钮隐藏代替后端拒绝。
        if requested_view not in stored.allowed_view_codes or requested_view not in visible_codes:
            raise ApiError("auth.permission_denied")
        return requested_view  # type: ignore[return-value]
    if stored.default_view_code in visible_codes:
        return stored.default_view_code
    if "executive" in visible_codes:
        return "executive"
    # 不允许回退到配置外视图或任意第一个视图；最终无 executive 表示配置/授权组合无法安全服务。
    raise _RoleDashboardConfigError()


def _load_aggregate_snapshot(
    connection: sqlite3.Connection,
    *,
    config: AppConfig,
    tenant_id: str,
    permission_codes: tuple[str, ...],
    window: _BusinessDayWindow,
    now: datetime,
) -> _AggregateSnapshot:
    """在一个连接和一致性快照中构造六类固定卡片。"""

    start_db = _to_db_time(window.start_utc)
    end_db = _to_db_time(window.end_utc)
    request_row = _request_totals(connection, tenant_id, start_db, end_db)
    total_requests = _strict_non_negative_int(request_row["total_requests"])
    success_count = _strict_non_negative_int(request_row["success_count"])
    failed_count = _strict_non_negative_int(request_row["failed_count"])
    p95_latency_ms = _p95_latency(connection, tenant_id, start_db, end_db)
    cost_card, priced_requests = _cost_card(
        connection, tenant_id, permission_codes, start_db, end_db
    )
    security_card = _security_card(
        connection, tenant_id, permission_codes, start_db, end_db
    )
    adoption_card, active_resources = _adoption_card(connection, tenant_id, permission_codes)
    provider_card = _provider_health_card(
        connection,
        config=config,
        tenant_id=tenant_id,
        permission_codes=permission_codes,
        now=now,
    )
    budget_card = _budget_risk_card(
        connection,
        tenant_id,
        permission_codes,
        window.start_local.date().isoformat(),
        start_db,
        end_db,
    )
    stability_card = _stability_card(
        total_requests=total_requests,
        success_count=success_count,
        failed_count=failed_count,
        p95_latency_ms=p95_latency_ms,
        permission_codes=permission_codes,
    )
    cards = {
        card.code: card
        for card in (
            cost_card,
            security_card,
            stability_card,
            adoption_card,
            provider_card,
            budget_card,
        )
    }
    if set(cards) != {
        "cost",
        "security",
        "stability",
        "adoption",
        "provider_health",
        "budget_risk",
    }:
        # 固定六卡缺失或重复表示服务端目录漂移，禁止返回不完整治理首页。
        raise _RoleDashboardDataError()
    return _AggregateSnapshot(
        cards=cards,
        is_empty=total_requests == 0 and priced_requests == 0 and active_resources == 0,
    )


def _request_totals(
    connection: sqlite3.Connection,
    tenant_id: str,
    start_db: str,
    end_db: str,
) -> sqlite3.Row:
    """聚合当前 tenant 今日请求数，SQL 第一过滤条件固定为 tenant_id。"""

    row = connection.execute(
        """
        SELECT COUNT(*) AS total_requests,
               COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),0) AS success_count,
               COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failed_count
        FROM request_logs
        WHERE tenant_id=? AND created_at>=? AND created_at<?
        """,
        (tenant_id, start_db, end_db),
    ).fetchone()
    if row is None:
        # 聚合查询必须始终产生单行结果；缺行视为依赖异常，不能静默伪造成零请求。
        raise _RoleDashboardDataError()
    return row


def _p95_latency(
    connection: sqlite3.Connection,
    tenant_id: str,
    start_db: str,
    end_db: str,
) -> int:
    """按当前 tenant 今日非空延迟计算 P95，不把全量延迟读入 Python。"""

    count_row = connection.execute(
        """
        SELECT COUNT(*) AS value
        FROM request_logs
        WHERE tenant_id=? AND created_at>=? AND created_at<? AND latency_ms IS NOT NULL
        """,
        (tenant_id, start_db, end_db),
    ).fetchone()
    count = _strict_non_negative_int(count_row["value"] if count_row else None)
    if count == 0:
        return 0
    offset = max(0, math.ceil(count * 0.95) - 1)
    row = connection.execute(
        """
        SELECT latency_ms AS value
        FROM request_logs
        WHERE tenant_id=? AND created_at>=? AND created_at<? AND latency_ms IS NOT NULL
        ORDER BY latency_ms ASC LIMIT 1 OFFSET ?
        """,
        (tenant_id, start_db, end_db, offset),
    ).fetchone()
    return _strict_non_negative_int(row["value"] if row else None)


def _cost_card(
    connection: sqlite3.Connection,
    tenant_id: str,
    permission_codes: tuple[str, ...],
    start_db: str,
    end_db: str,
) -> tuple[RoleDashboardCard, int]:
    """从正式 FinOps posted 账本构造成本网成本卡。

    只有已定价、已规范化为 CNY 的 usage_charge 才计入成本；unpriced、legacy 或无法换算项单独计数，不能伪装成 0 元正常成本。
    """

    row = connection.execute(
        """
        SELECT
          COALESCE(SUM(CASE
            WHEN price_evidence_status='priced' AND cny_normalization_status='normalized'
            THEN normalized_cny_micros ELSE 0 END),0) AS priced_cny_micros,
          COUNT(DISTINCT CASE
            WHEN price_evidence_status='priced' AND cny_normalization_status='normalized'
            THEN source_request_id END) AS priced_requests,
          COALESCE(SUM(CASE
            WHEN price_evidence_status='priced' AND cny_normalization_status='normalized'
            THEN total_tokens ELSE 0 END),0) AS priced_tokens,
          COUNT(DISTINCT CASE
            WHEN price_evidence_status!='priced' OR cny_normalization_status!='normalized'
            THEN source_request_id END) AS unpriced_requests
        FROM finops_ledger_entries
        WHERE tenant_id=? AND created_at>=? AND created_at<?
          AND entry_type='usage_charge' AND direction='debit' AND status='posted'
        """,
        (tenant_id, start_db, end_db),
    ).fetchone()
    if row is None:
        # 成本聚合缺行时不能把 unknown/unpriced 请求伪装为零成本正常状态。
        raise _RoleDashboardDataError()
    micros = _strict_non_negative_int(row["priced_cny_micros"])
    priced_requests = _strict_non_negative_int(row["priced_requests"])
    priced_tokens = _strict_non_negative_int(row["priced_tokens"])
    unpriced_requests = _strict_non_negative_int(row["unpriced_requests"])
    status = "warning" if unpriced_requests else ("empty" if priced_requests == 0 else "ok")
    return (
        RoleDashboardCard(
            code="cost",
            label="成本",
            status=status,
            primary_value=round(micros / 1_000_000, 6),
            primary_unit="cny",
            summary="今日正式 CNY 用量成本",
            facts=[
                _fact("priced_requests", "成功计费请求", priced_requests, "count"),
                _fact("total_tokens", "计费 Token", priced_tokens, "tokens"),
                _fact("unpriced_requests", "待定价请求", unpriced_requests, "count"),
            ],
            target_navigation_item_id=card_navigation_target("cost", permission_codes),
        ),
        priced_requests,
    )


def _security_card(
    connection: sqlite3.Connection,
    tenant_id: str,
    permission_codes: tuple[str, ...],
    start_db: str,
    end_db: str,
) -> RoleDashboardCard:
    """构造只含 DLP/安全计数的卡片，不读取 pattern、Prompt 或失败正文。"""

    row = connection.execute(
        """
        SELECT
          COALESCE(SUM(CASE
            WHEN dlp_match_count>0 THEN dlp_match_count ELSE 0 END),0) AS dlp_matches,
          COALESCE(SUM(CASE WHEN dlp_action='block' THEN 1 ELSE 0 END),0) AS dlp_blocked,
          COALESCE(SUM(CASE
            WHEN status='failed' AND (error_code LIKE 'security.%' OR dlp_action='block')
            THEN 1 ELSE 0 END),0) AS security_failures
        FROM request_logs
        WHERE tenant_id=? AND created_at>=? AND created_at<?
        """,
        (tenant_id, start_db, end_db),
    ).fetchone()
    policy_row = connection.execute(
        "SELECT COUNT(*) AS value FROM policies WHERE tenant_id=? AND status='active'",
        (tenant_id,),
    ).fetchone()
    if row is None or policy_row is None:
        # 安全事实任一聚合缺失都必须失败关闭，避免页面展示不完整的风险结论。
        raise _RoleDashboardDataError()
    matches = _strict_non_negative_int(row["dlp_matches"])
    blocked = _strict_non_negative_int(row["dlp_blocked"])
    failures = _strict_non_negative_int(row["security_failures"])
    active_policies = _strict_non_negative_int(policy_row["value"])
    status = "critical" if blocked or failures else ("warning" if matches else "ok")
    return RoleDashboardCard(
        code="security",
        label="安全",
        status=status,
        primary_value=float(blocked),
        primary_unit="count",
        summary="今日安全阻断请求",
        facts=[
            _fact("dlp_matches", "DLP 命中", matches, "count"),
            _fact("dlp_blocked", "DLP 阻断", blocked, "count"),
            _fact("security_failures", "安全类失败", failures, "count"),
            _fact("active_policies", "启用策略", active_policies, "count"),
        ],
        target_navigation_item_id=card_navigation_target("security", permission_codes),
    )


def _stability_card(
    *,
    total_requests: int,
    success_count: int,
    failed_count: int,
    p95_latency_ms: int,
    permission_codes: tuple[str, ...],
) -> RoleDashboardCard:
    """按 PRD-026 相同阈值构造稳定性卡片。"""

    success_rate = _percentage(success_count, total_requests)
    error_rate = _percentage(failed_count, total_requests)
    if total_requests == 0:
        status = "empty"
    elif (
        success_rate < _SUCCESS_CRITICAL_THRESHOLD
        or error_rate > _ERROR_CRITICAL_THRESHOLD
        or p95_latency_ms >= _LATENCY_CRITICAL_MS
    ):
        status = "critical"
    elif (
        success_rate < _SUCCESS_WARNING_THRESHOLD
        or error_rate > _ERROR_WARNING_THRESHOLD
        or p95_latency_ms >= _LATENCY_WARNING_MS
    ):
        status = "warning"
    else:
        status = "ok"
    return RoleDashboardCard(
        code="stability",
        label="稳定性",
        status=status,
        primary_value=success_rate,
        primary_unit="percent",
        summary="今日请求成功率",
        facts=[
            _fact("request_count", "请求数", total_requests, "count"),
            _fact("failed_requests", "失败请求", failed_count, "count"),
            _fact("error_rate", "错误率", error_rate, "percent"),
            _fact("p95_latency_ms", "P95 延迟", p95_latency_ms, "ms"),
        ],
        target_navigation_item_id=card_navigation_target("stability", permission_codes),
    )


def _adoption_card(
    connection: sqlite3.Connection,
    tenant_id: str,
    permission_codes: tuple[str, ...],
) -> tuple[RoleDashboardCard, int]:
    """按五个接入资源的 active 状态聚合采用进度，只返回数量不返回名称或 Key 前缀。"""

    row = connection.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM providers WHERE tenant_id=? AND status='active') AS providers,
          (SELECT COUNT(*) FROM models WHERE tenant_id=? AND status='active') AS models,
          (SELECT COUNT(*) FROM projects WHERE tenant_id=? AND status='active') AS projects,
          (SELECT COUNT(*) FROM applications WHERE tenant_id=? AND status='active') AS applications,
          (SELECT COUNT(*) FROM virtual_keys WHERE tenant_id=? AND status='active') AS virtual_keys
        """,
        (tenant_id, tenant_id, tenant_id, tenant_id, tenant_id),
    ).fetchone()
    if row is None:
        # 五类接入资源必须在同一 tenant 快照中完整聚合，缺行不能被当作空租户。
        raise _RoleDashboardDataError()
    counts = {
        "providers": _strict_non_negative_int(row["providers"]),
        "models": _strict_non_negative_int(row["models"]),
        "projects": _strict_non_negative_int(row["projects"]),
        "applications": _strict_non_negative_int(row["applications"]),
        "virtual_keys": _strict_non_negative_int(row["virtual_keys"]),
    }
    completed_steps = sum(value > 0 for value in counts.values())
    total_resources = sum(counts.values())
    status = "ok" if completed_steps == 5 else ("empty" if completed_steps == 0 else "warning")
    return (
        RoleDashboardCard(
            code="adoption",
            label="接入进度",
            status=status,
            primary_value=float(completed_steps),
            primary_unit="steps",
            summary="五项接入能力完成度",
            facts=[
                _fact("active_providers", "Provider", counts["providers"], "count"),
                _fact("active_models", "模型", counts["models"], "count"),
                _fact("active_projects", "项目", counts["projects"], "count"),
                _fact("active_applications", "应用", counts["applications"], "count"),
                _fact("active_virtual_keys", "Virtual Key", counts["virtual_keys"], "count"),
            ],
            target_navigation_item_id=card_navigation_target("adoption", permission_codes),
        ),
        total_resources,
    )


def _provider_health_card(
    connection: sqlite3.Connection,
    *,
    config: AppConfig,
    tenant_id: str,
    permission_codes: tuple[str, ...],
    now: datetime,
) -> RoleDashboardCard:
    """按 Provider 状态页同口径聚合 effective 健康桶，不返回 Endpoint 或错误正文。"""

    rows = connection.execute(
        """
        SELECT status, health_status, consecutive_probe_failures, health_updated_at
        FROM providers
        WHERE tenant_id=?
        ORDER BY provider_id ASC
        """,
        (tenant_id,),
    ).fetchall()
    counts = {"active": 0, "healthy": 0, "unhealthy": 0, "observing": 0, "unknown": 0}
    for row in rows:
        status = row["status"]
        health_status = row["health_status"]
        failures = row["consecutive_probe_failures"]
        if (
            status not in {"active", "disabled"}
            or health_status not in {"unknown", "healthy", "unhealthy"}
        ):
            # 未知 Provider 状态会破坏健康计数口径，不能降级映射成 healthy 或 unknown。
            raise _RoleDashboardDataError()
        if type(failures) is not int or failures < 0:
            # 探测失败次数损坏时拒绝整张卡，避免负数或类型转换掩盖真实故障。
            raise _RoleDashboardDataError()
        updated_at = _parse_optional_utc(row["health_updated_at"])
        stale = updated_at is None or updated_at < now - timedelta(
            seconds=config.provider_health_stale_seconds
        )
        if status != "active":
            continue
        counts["active"] += 1
        if failures > 0 and health_status != "unhealthy" and not stale:
            effective = "observing"
        elif stale:
            effective = "unknown"
        else:
            effective = health_status
        counts[effective] += 1
    card_status = (
        "critical"
        if counts["unhealthy"]
        else "warning"
        if counts["observing"] or counts["unknown"]
        else "empty"
        if counts["active"] == 0
        else "ok"
    )
    return RoleDashboardCard(
        code="provider_health",
        label="Provider 健康",
        status=card_status,
        primary_value=float(counts["healthy"]),
        primary_unit="count",
        summary="当前健康 Provider",
        facts=[
            _fact("active", "启用", counts["active"], "count"),
            _fact("healthy", "健康", counts["healthy"], "count"),
            _fact("unhealthy", "异常", counts["unhealthy"], "count"),
            _fact("observing", "观察中", counts["observing"], "count"),
            _fact("unknown", "未知", counts["unknown"], "count"),
        ],
        target_navigation_item_id=card_navigation_target("provider_health", permission_codes),
    )


def _budget_risk_card(
    connection: sqlite3.Connection,
    tenant_id: str,
    permission_codes: tuple[str, ...],
    budget_date: str,
    start_db: str,
    end_db: str,
) -> RoleDashboardCard:
    """聚合今日预算越限、阻断和当前未关闭成本异常。"""

    event_row = connection.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN action_taken='allow' THEN 1 ELSE 0 END),0) AS threshold_events,
          COALESCE(SUM(CASE WHEN action_taken='block' THEN 1 ELSE 0 END),0) AS exhausted_events
        FROM budget_scope_events
        WHERE tenant_id=? AND budget_date=?
        """,
        (tenant_id, budget_date),
    ).fetchone()
    request_row = connection.execute(
        """
        SELECT COALESCE(SUM(CASE
          WHEN budget_action='block' THEN 1 ELSE 0 END),0) AS blocked_requests
        FROM request_logs
        WHERE tenant_id=? AND created_at>=? AND created_at<?
        """,
        (tenant_id, start_db, end_db),
    ).fetchone()
    anomaly_row = connection.execute(
        """
        SELECT COUNT(*) AS open_anomalies,
               COALESCE(SUM(CASE
                 WHEN severity='critical' THEN 1 ELSE 0 END),0) AS critical_anomalies
        FROM cost_anomalies
        WHERE tenant_id=? AND status IN ('open','acknowledged')
        """,
        (tenant_id,),
    ).fetchone()
    if event_row is None or request_row is None or anomaly_row is None:
        # 预算事件、阻断请求和成本异常必须来自同一快照，任一缺失都不能生成风险结论。
        raise _RoleDashboardDataError()
    threshold = _strict_non_negative_int(event_row["threshold_events"])
    exhausted = _strict_non_negative_int(event_row["exhausted_events"])
    blocked = _strict_non_negative_int(request_row["blocked_requests"])
    open_anomalies = _strict_non_negative_int(anomaly_row["open_anomalies"])
    critical_anomalies = _strict_non_negative_int(anomaly_row["critical_anomalies"])
    primary = threshold + exhausted + open_anomalies
    status = (
        "critical"
        if exhausted or blocked or critical_anomalies
        else "warning"
        if primary
        else "ok"
    )
    return RoleDashboardCard(
        code="budget_risk",
        label="预算风险",
        status=status,
        primary_value=float(primary),
        primary_unit="count",
        summary="当前预算与成本风险",
        facts=[
            _fact("threshold_events", "今日阈值事件", threshold, "count"),
            _fact("exhausted_events", "今日预算耗尽", exhausted, "count"),
            _fact("blocked_requests", "今日预算阻断", blocked, "count"),
            _fact("open_anomalies", "待处理成本异常", open_anomalies, "count"),
            _fact("critical_anomalies", "严重成本异常", critical_anomalies, "count"),
        ],
        target_navigation_item_id=card_navigation_target("budget_risk", permission_codes),
    )


def _fact(code: str, label: str, value: int | float, unit: str) -> RoleDashboardFact:
    """构造固定聚合事实并统一转换为非负 float。"""

    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        # 非有限数或负数不得进入响应 JSON，否则会污染卡片展示和后续审计证据。
        raise _RoleDashboardDataError()
    return RoleDashboardFact(code=code, label=label, value=numeric, unit=unit)


def _empty_state(is_empty: bool, view_code: RoleDashboardViewCode) -> RoleDashboardEmptyState:
    """按当前视图生成固定空态说明，不加入未经导航授权的动作链接。"""

    if not is_empty:
        return RoleDashboardEmptyState(is_empty=False, title="", description="")
    descriptions = {
        "executive": "完成首个 Provider、模型和调用后，这里会显示经营与治理摘要。",
        "finance": "产生首条正式计费调用后，这里会显示成本与预算风险。",
        "security": "启用治理策略并产生调用后，这里会显示安全聚合。",
        "developer": "完成模型接入并发起调用后，这里会显示接入与稳定性指标。",
        "operations": "启用 Provider 并完成健康检查后，这里会显示运行状态。",
    }
    return RoleDashboardEmptyState(
        is_empty=True,
        title="当前视图暂无运行数据",
        description=descriptions[view_code],
    )


def _business_day_window(now: datetime) -> _BusinessDayWindow:
    """计算 Asia/Shanghai 今日窗口并派生 UTC 查询边界。"""

    local_now = now.astimezone(SHANGHAI)
    start_local = datetime.combine(local_now.date(), time.min, tzinfo=SHANGHAI)
    end_local = start_local + timedelta(days=1)
    return _BusinessDayWindow(
        start_local=start_local,
        end_local=end_local,
        start_utc=start_local.astimezone(UTC),
        end_utc=end_local.astimezone(UTC),
    )


def _percentage(part: int, total: int) -> float:
    """计算两位小数百分比，空分母保持 0 并由卡片 empty 状态解释。"""

    return 0.0 if total <= 0 else round(part * 100.0 / total, 2)


def _strict_non_negative_int(value: object) -> int:
    """严格读取 SQLite 非负整数，避免损坏数据被静默归零。"""

    if type(value) is not int or value < 0:
        # SQLite 弱类型值必须严格拒绝，不能把损坏的用量或成本计数静默转换。
        raise _RoleDashboardDataError()
    return value


def _parse_utc_second(value: object, *, config_error: bool = False) -> datetime:
    """解析秒级 UTC 时间；调用方可选择把失败归类为配置损坏。"""

    error_type = _RoleDashboardConfigError if config_error else _RoleDashboardDataError
    # 数据库时间必须是固定 UTC 秒格式，避免宽松解析把配置 revision 或业务窗口错位。
    if type(value) is not str or len(value) != 20 or not value.endswith("Z"):
        raise error_type()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 无法解析的时间按调用方指定的配置或数据错误分类，公开响应仍保持统一错误码。
    except ValueError as exc:
        raise error_type() from exc
    if parsed.tzinfo is None:
        # 缺失时区会让上海业务日边界产生歧义，因此不能假定本地时区继续聚合。
        raise error_type()
    return parsed.astimezone(UTC)


def _parse_optional_utc(value: object) -> datetime | None:
    """解析可空 Provider 健康时间，非空损坏值必须失败关闭。"""

    if value is None:
        return None
    if type(value) is not str:
        # 非空健康时间必须是可审计字符串，其他 SQLite 类型一律视为依赖数据损坏。
        raise _RoleDashboardDataError()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Provider 健康时间解析失败时拒绝整卡，不能把损坏时间简单视为 stale 掩盖异常。
    except ValueError as exc:
        raise _RoleDashboardDataError() from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 datetime 转换为数据库统一使用的 UTC 秒级字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回秒级 UTC 当前时间，集中封装便于专项测试固定业务日。"""

    return datetime.now(UTC).replace(microsecond=0)


def _rollback_quietly(connection: sqlite3.Connection | None) -> None:
    """在失败路径尽力结束只读事务，不用第二个数据库异常覆盖统一错误。"""

    if connection is None:
        return
    try:
        connection.rollback()
    # 清理失败不能覆盖最初的统一 Dashboard 错误，也不得公开 SQLite 异常正文。
    except sqlite3.Error:
        pass


def _close_quietly(connection: sqlite3.Connection | None) -> None:
    """尽力关闭当前请求连接，避免 Dashboard 高频刷新累积文件句柄。"""

    if connection is None:
        return
    try:
        connection.close()
    # 关闭失败仅属于资源清理问题，不能改变已经确定的安全响应或泄露连接细节。
    except sqlite3.Error:
        pass
