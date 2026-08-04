"""PRD-053 成本异常确定性规则与正式账本输入模型。

检测器只消费 PRD-051 正式 `usage_charge`，在 Python 中用任意精度整数聚合。模块内
不执行网络请求，也不修改 Gateway 热路径；数据库 claim/落库封装在文件后半部，纯规则
函数可用冻结时钟独立验证误报、边界和逐 Key 完整性。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable, Literal
from zoneinfo import ZoneInfo
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError


# 以下常量冻结检测窗口、冷却期、租约、金额上限与业务时区，避免部署差异改变证据语义。
WINDOW_SECONDS = 5 * 60
BASELINE_SECONDS = 24 * 60 * 60
BASELINE_BUCKET_COUNT = BASELINE_SECONDS // WINDOW_SECONDS
CLOSE_DELAY_SECONDS = 120
LOOKBACK_DAYS = 7
COOLDOWN_MINUTES = 30
MITIGATION_ELIGIBILITY_HOURS = 24
DETECTION_LEASE_SECONDS = 30
DETECTION_VERSION = "cost-anomaly-rules-v1"
MAX_LEDGER_MICROS = 1_000_000_000_000_000
TRUSTED_PRICE_EVIDENCE = frozenset({"priced", "legacy_amount_only"})
SHANGHAI = ZoneInfo("Asia/Shanghai")


class DetectionDataError(ValueError):
    """表示正式账本行无法满足稳定金额或 owner 合同。

    异常只携带固定内部原因码，调用方不得把数据库值、SQL 或原始异常拼入日志与 run。
    """

    def __init__(self, reason_code: str) -> None:
        """保存固定失败原因；参数必须来自本模块常量分支。"""

        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class DetectionSettings:
    """一次检测使用的部署级规则快照。"""

    multiplier_millis: int
    minimum_cost_micros: int
    workday_start_hour: int
    workday_end_hour: int


@dataclass(frozen=True, slots=True)
class DetectionWindow:
    """一个 tenant/Environment 的闭合五分钟候选窗口。"""

    tenant_id: str
    environment_id: str
    window_start: datetime
    window_end: datetime
    baseline_start: datetime
    baseline_end: datetime
    frozen_watermark: int


@dataclass(frozen=True, slots=True)
class LedgerCostRow:
    """检测器从正式账本恢复的最小 Usage Charge 行。

    `normalized_cny_micros` 保留 object 类型，纯规则会显式拒绝 SQLite REAL、bool、字符串
    或 NULL 冒充可信整数。Project/Application 是账本时点快照，不从当前 Key 反向改写。
    """

    ledger_sequence: int
    tenant_id: str
    environment_id: str
    created_at: datetime
    normalized_cny_micros: object
    cny_normalization_status: str
    price_evidence_status: str
    virtual_key_id: str | None
    project_id: str | None
    application_id: str | None
    source_request_id: str | None
    source_trace_id: str | None
    estimated: bool


@dataclass(frozen=True, slots=True)
class DetectedAnomaly:
    """待持久化的不可变异常证据。"""

    rule_type: Literal[
        "tenant_spend_spike",
        "virtual_key_spend_spike",
        "virtual_key_off_hours",
    ]
    severity: Literal["warning", "critical"]
    scope_type: Literal["tenant", "virtual_key"]
    scope_id: str
    virtual_key_id: str | None
    top_virtual_key_id: str | None
    project_id_snapshot: str | None
    application_id_snapshot: str | None
    current_cost_micros: int
    baseline_total_micros: int
    observed_ratio_millis: int | None
    source_entry_count: int
    estimated_entry_count: int
    legacy_entry_count: int
    affected_virtual_key_count: int
    representative_request_id: str | None
    representative_trace_id: str | None
    source_ledger_sequence_min: int
    source_ledger_sequence_max: int
    recommended_action: str


@dataclass(frozen=True, slots=True)
class WindowEvaluation:
    """一次窗口规则评估结果及 run 级安全计数。"""

    status: Literal["completed", "partial"]
    error_code: str | None
    environment_current_complete: bool
    environment_baseline_complete: bool
    source_entry_count: int
    estimated_entry_count: int
    legacy_entry_count: int
    unpriced_entry_count: int
    partial_scope_count: int
    anomalies: tuple[DetectedAnomaly, ...]


@dataclass(frozen=True, slots=True)
class _ScopeTotals:
    """内部作用域金额、完整性和代表行聚合。"""

    complete: bool
    total_micros: int
    trusted_rows: tuple[tuple[LedgerCostRow, int], ...]
    untrusted_count: int


def align_window_start(value: datetime) -> datetime:
    """把带时区时刻向下对齐至 UTC 五分钟边界。

    参数：任意带时区 datetime。
    返回：UTC 左闭窗口起点。
    约束：naive datetime 会造成跨时区误判，因此固定失败。
    """

    normalized = _aware_utc(value)
    epoch = int(normalized.timestamp())
    return datetime.fromtimestamp(epoch - epoch % WINDOW_SECONDS, tz=UTC)


def detection_window(
    tenant_id: str,
    environment_id: str,
    candidate_time: datetime,
    frozen_watermark: int,
) -> DetectionWindow:
    """构造五分钟候选及其前置 24 小时基线。"""

    start = align_window_start(candidate_time)
    end = start + timedelta(seconds=WINDOW_SECONDS)
    return DetectionWindow(
        tenant_id=tenant_id,
        environment_id=environment_id,
        window_start=start,
        window_end=end,
        baseline_start=start - timedelta(seconds=BASELINE_SECONDS),
        baseline_end=start,
        frozen_watermark=_positive_integer(frozen_watermark, "cost_anomaly.detector_source_owner_invalid"),
    )


def evaluate_window(
    window: DetectionWindow,
    rows: Iterable[LedgerCostRow],
    settings: DetectionSettings,
) -> WindowEvaluation:
    """执行 tenant spike、逐 Key spike 和非工作时段三类确定性规则。

    参数：冻结窗口、`<= frozen_watermark` 的正式账本行和规则快照。
    返回：可原子落库的异常候选及 run 统计。
    约束：坏 Key 只使自身规则 partial；Environment 聚合不完整时 tenant spike 跳过，
    其他可信 Key 仍可产生异常。
    """

    _validate_settings(settings)
    current_rows: list[LedgerCostRow] = []
    baseline_rows: list[LedgerCostRow] = []
    for row in rows:
        _validate_owner_and_time(window, row)
        if row.ledger_sequence > window.frozen_watermark:
            # 调用方查询遗漏水位条件会破坏重评快照，必须失败而非静默跳过。
            raise DetectionDataError("cost_anomaly.detector_source_owner_invalid")
        if window.window_start <= row.created_at < window.window_end:
            current_rows.append(row)
        elif window.baseline_start <= row.created_at < window.baseline_end:
            baseline_rows.append(row)

    current = _scope_totals(current_rows)
    baseline = _scope_totals(baseline_rows)
    current_by_key = _group_by_key(current_rows)
    baseline_by_key = _group_by_key(baseline_rows)
    partial_scopes = int(not current.complete or not baseline.complete)
    unpriced_count = current.untrusted_count + baseline.untrusted_count
    anomalies: list[DetectedAnomaly] = []

    # tenant spike 依赖完整 Environment current/baseline；任一未知价都不能按零稀释均值。
    if current.complete and baseline.complete and _is_spike(
        current.total_micros,
        baseline.total_micros,
        settings,
    ):
        anomalies.append(
            _anomaly_from_scope(
                window,
                settings,
                rule_type="tenant_spend_spike",
                severity="critical",
                scope_type="tenant",
                scope_id=window.tenant_id,
                current=current,
                baseline=baseline,
                affected_virtual_key_count=len(current_by_key),
                recommended_action="review_impacted_keys",
            )
        )

    for virtual_key_id in sorted(current_by_key):
        key_current = _scope_totals(current_by_key[virtual_key_id])
        key_baseline = _scope_totals(baseline_by_key.get(virtual_key_id, ()))
        if not key_current.complete or not key_baseline.complete:
            partial_scopes += 1
        if key_current.complete and key_baseline.complete and _is_spike(
            key_current.total_micros,
            key_baseline.total_micros,
            settings,
        ):
            anomalies.append(
                _anomaly_from_scope(
                    window,
                    settings,
                    rule_type="virtual_key_spend_spike",
                    severity="critical",
                    scope_type="virtual_key",
                    scope_id=virtual_key_id,
                    current=key_current,
                    baseline=key_baseline,
                    affected_virtual_key_count=1,
                    recommended_action="apply_temporary_rate_limit",
                )
            )
        # off-hours 只依赖该 Key 当前窗口完整；基线未知不能掩盖当前异常活动。
        if (
            key_current.complete
            and key_current.total_micros >= settings.minimum_cost_micros
            and is_off_hours(window.window_start, settings)
        ):
            anomalies.append(
                _anomaly_from_scope(
                    window,
                    settings,
                    rule_type="virtual_key_off_hours",
                    severity="warning",
                    scope_type="virtual_key",
                    scope_id=virtual_key_id,
                    current=key_current,
                    baseline=key_baseline,
                    affected_virtual_key_count=1,
                    recommended_action="apply_temporary_rate_limit",
                    ratio_required=False,
                )
            )

    all_rows = [*current_rows, *baseline_rows]
    return WindowEvaluation(
        status="partial" if partial_scopes else "completed",
        error_code="cost_anomaly.detector_source_incomplete" if partial_scopes else None,
        environment_current_complete=current.complete,
        environment_baseline_complete=baseline.complete,
        source_entry_count=len(all_rows),
        estimated_entry_count=sum(bool(row.estimated) for row in all_rows),
        legacy_entry_count=sum(row.price_evidence_status == "legacy_amount_only" for row in all_rows),
        unpriced_entry_count=unpriced_count,
        partial_scope_count=partial_scopes,
        anomalies=tuple(anomalies),
    )


def is_off_hours(window_start: datetime, settings: DetectionSettings) -> bool:
    """按 Asia/Shanghai 和桶起点判断非工作时段。

    周末全天为非工作时段；工作日使用左闭右开 `[start_hour, end_hour)`，v1 不加载
    法定节假日日历，保证部署间结果一致。
    """

    _validate_settings(settings)
    local = _aware_utc(window_start).astimezone(SHANGHAI)
    if local.weekday() >= 5:
        return True
    return not (settings.workday_start_hour <= local.hour < settings.workday_end_hour)


def _scope_totals(rows: Iterable[LedgerCostRow]) -> _ScopeTotals:
    """逐行验证价格证据并用 Python int 聚合一个作用域。"""

    trusted: list[tuple[LedgerCostRow, int]] = []
    untrusted = 0
    total = 0
    for row in rows:
        amount = _trusted_amount(row)
        if amount is None:
            untrusted += 1
            continue
        total += amount
        trusted.append((row, amount))
    return _ScopeTotals(
        complete=untrusted == 0,
        total_micros=total,
        trusted_rows=tuple(trusted),
        untrusted_count=untrusted,
    )


def _trusted_amount(row: LedgerCostRow) -> int | None:
    """恢复可信 CNY micros；未知价格返回 None，损坏金额固定失败。"""

    if (
        row.cny_normalization_status != "normalized"
        or row.price_evidence_status not in TRUSTED_PRICE_EVIDENCE
    ):
        return None
    value = row.normalized_cny_micros
    # bool 是 int 子类但不具备金额语义，必须使用 type 精确判断。
    if type(value) is not int or value < 0 or value > MAX_LEDGER_MICROS:
        raise DetectionDataError("cost_anomaly.detector_amount_invalid")
    return value


def _group_by_key(rows: Iterable[LedgerCostRow]) -> dict[str, tuple[LedgerCostRow, ...]]:
    """按非空 Virtual Key 分组；无 Key 行仅参与 Environment 聚合。"""

    grouped: dict[str, list[LedgerCostRow]] = {}
    for row in rows:
        if row.virtual_key_id:
            grouped.setdefault(row.virtual_key_id, []).append(row)
    return {key: tuple(value) for key, value in grouped.items()}


def _is_spike(current: int, baseline_total: int, settings: DetectionSettings) -> bool:
    """用交叉相乘判定严格超过 N 倍，零基线和微额固定不触发。"""

    return (
        current >= settings.minimum_cost_micros
        and baseline_total > 0
        and current * BASELINE_BUCKET_COUNT * 1000
        > baseline_total * settings.multiplier_millis
    )


def _anomaly_from_scope(
    window: DetectionWindow,
    settings: DetectionSettings,
    *,
    rule_type: Literal[
        "tenant_spend_spike",
        "virtual_key_spend_spike",
        "virtual_key_off_hours",
    ],
    severity: Literal["warning", "critical"],
    scope_type: Literal["tenant", "virtual_key"],
    scope_id: str,
    current: _ScopeTotals,
    baseline: _ScopeTotals,
    affected_virtual_key_count: int,
    recommended_action: str,
    ratio_required: bool = True,
) -> DetectedAnomaly:
    """从已验证作用域构造不可变候选，不生成数据库身份或外部事件。"""

    if not current.trusted_rows:
        # 没有可信金额分录时无法选出代表请求，必须失败关闭而不是生成空来源异常。
        raise DetectionDataError("cost_anomaly.detector_amount_invalid")
    ordered = sorted(current.trusted_rows, key=lambda item: (item[1], item[0].ledger_sequence))
    representative, _representative_amount = ordered[-1]
    sequences = [row.ledger_sequence for row, _amount in current.trusted_rows]
    key_totals: dict[str, int] = {}
    for row, amount in current.trusted_rows:
        if row.virtual_key_id:
            key_totals[row.virtual_key_id] = key_totals.get(row.virtual_key_id, 0) + amount
    top_key = max(key_totals, key=lambda key: (key_totals[key], key)) if key_totals else None
    ratio = (
        current.total_micros * BASELINE_BUCKET_COUNT * 1000 // baseline.total_micros
        if ratio_required and baseline.total_micros > 0
        else None
    )
    return DetectedAnomaly(
        rule_type=rule_type,
        severity=severity,
        scope_type=scope_type,
        scope_id=scope_id,
        virtual_key_id=scope_id if scope_type == "virtual_key" else None,
        top_virtual_key_id=top_key,
        project_id_snapshot=representative.project_id,
        application_id_snapshot=representative.application_id,
        current_cost_micros=current.total_micros,
        baseline_total_micros=baseline.total_micros,
        observed_ratio_millis=ratio,
        source_entry_count=len(current.trusted_rows),
        estimated_entry_count=sum(row.estimated for row, _amount in current.trusted_rows),
        legacy_entry_count=sum(
            row.price_evidence_status == "legacy_amount_only"
            for row, _amount in current.trusted_rows
        ),
        affected_virtual_key_count=affected_virtual_key_count,
        representative_request_id=representative.source_request_id,
        representative_trace_id=representative.source_trace_id,
        source_ledger_sequence_min=min(sequences),
        source_ledger_sequence_max=max(sequences),
        recommended_action=recommended_action,
    )


def _validate_owner_and_time(window: DetectionWindow, row: LedgerCostRow) -> None:
    """拒绝跨租户/Environment、水位或 naive 时间行污染检测结果。"""

    if (
        row.tenant_id != window.tenant_id
        or row.environment_id != window.environment_id
        or type(row.ledger_sequence) is not int
        or row.ledger_sequence <= 0
    ):
        # tenant、Environment 或账本序号失真会污染所有权边界，因此整次评估必须失败。
        raise DetectionDataError("cost_anomaly.detector_source_owner_invalid")
    _aware_utc(row.created_at)


def _validate_settings(settings: DetectionSettings) -> None:
    """验证部署快照边界，配置损坏时不产生不可信告警。"""

    if (
        type(settings.multiplier_millis) is not int
        or not 1001 <= settings.multiplier_millis <= 100_000
        or type(settings.minimum_cost_micros) is not int
        or not 1 <= settings.minimum_cost_micros <= 1_000_000_000_000
        or type(settings.workday_start_hour) is not int
        or type(settings.workday_end_hour) is not int
        or not 0 <= settings.workday_start_hour < settings.workday_end_hour <= 23
    ):
        # 阈值或工作时段超出冻结合同会让同一窗口产生不同结论，必须拒绝该配置快照。
        raise DetectionDataError("cost_anomaly.detector_clock_invalid")


def _aware_utc(value: datetime) -> datetime:
    """把带时区时间统一为 UTC；naive 或非 datetime 固定失败。"""

    # naive 时间无法稳定比较窗口与租约，禁止依赖宿主机本地时区做隐式解释。
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DetectionDataError("cost_anomaly.detector_clock_invalid")
    return value.astimezone(UTC)


def _positive_integer(value: object, reason: str) -> int:
    """恢复严格正整数，bool/REAL/字符串均拒绝。"""

    # bool、浮点或非正值不能作为序号和 revision，否则 CAS 与水位顺序不再可信。
    if type(value) is not int or value <= 0:
        raise DetectionDataError(reason)
    return value


@dataclass(frozen=True, slots=True)
class DetectionClaim:
    """一次数据库检测租约的不可变快照。

    claim token、revision 与 lease 三者共同构成完成 CAS；旧 worker 即使稍后恢复，也不能
    覆盖新 revision。对象只保存不透明 ID、时间和水位，不保存账本正文或密钥。
    """

    detection_run_id: str
    tenant_id: str
    environment_id: str
    window: DetectionWindow
    evaluation_revision: int
    claim_token: str
    lease_owner_id: str
    lease_expires_at: datetime
    operation_request_id: str
    operation_trace_id: str


def claim_next_detection(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> DetectionClaim | None:
    """领取最近七天内一条需要首次评估、迟到重评或 lease 恢复的窗口。

    参数：部署配置、进程内 worker ID，以及仅供专项测试注入的服务端时刻。
    返回：成功领取的不可变 claim；没有候选时返回 None。
    约束：候选、run 写入和 revision 递增位于同一 BEGIN IMMEDIATE 事务，所有连接均关闭。
    """

    current = _detector_now(now)
    owner = _lease_owner(worker_id)
    eligible_end = align_window_start(current - timedelta(seconds=CLOSE_DELAY_SECONDS))
    lookback_start = eligible_end - timedelta(days=LOOKBACK_DAYS)
    lease_expires = current + timedelta(seconds=DETECTION_LEASE_SECONDS)
    connection = _connect_detector(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            WITH current_windows AS (
              SELECT
                tenant_id,
                environment_id,
                strftime(
                  '%Y-%m-%dT%H:%M:%SZ',
                  unixepoch(created_at) - (unixepoch(created_at) % 300),
                  'unixepoch'
                ) AS window_start
              FROM finops_ledger_entries
              WHERE entry_type='usage_charge' AND direction='debit' AND status='posted'
                AND environment_id IS NOT NULL
                AND unixepoch(created_at)>=unixepoch(?)
                AND unixepoch(created_at)<unixepoch(?)
              GROUP BY tenant_id, environment_id, window_start
            ),
            candidates AS (
              SELECT
                current_windows.tenant_id,
                current_windows.environment_id,
                current_windows.window_start,
                (
                  SELECT MAX(source.ledger_sequence)
                  FROM finops_ledger_entries source
                  WHERE source.tenant_id=current_windows.tenant_id
                    AND source.environment_id=current_windows.environment_id
                    AND source.entry_type='usage_charge'
                    AND source.direction='debit'
                    AND source.status='posted'
                    AND unixepoch(source.created_at)>=unixepoch(current_windows.window_start)-86400
                    AND unixepoch(source.created_at)<unixepoch(current_windows.window_start)+300
                ) AS ledger_watermark
              FROM current_windows
            )
            SELECT
              candidates.tenant_id,
              candidates.environment_id,
              candidates.window_start,
              candidates.ledger_watermark,
              runs.detection_run_id,
              runs.evaluation_revision,
              runs.status AS run_status,
              runs.lease_expires_at AS run_lease_expires_at,
              runs.operation_request_id,
              runs.operation_trace_id
            FROM candidates
            LEFT JOIN cost_anomaly_detection_runs runs
              ON runs.tenant_id=candidates.tenant_id
             AND runs.environment_id=candidates.environment_id
             AND runs.window_start=candidates.window_start
            WHERE runs.detection_run_id IS NULL
               OR (
                 (runs.status!='processing' OR unixepoch(runs.lease_expires_at)<=unixepoch(?))
                 AND (
                   candidates.ledger_watermark>runs.ledger_watermark
                   OR runs.status='processing'
                 )
               )
            ORDER BY candidates.window_start ASC,
                     candidates.tenant_id ASC,
                     candidates.environment_id ASC
            LIMIT 1
            """,
            (_to_db_time(lookback_start), _to_db_time(eligible_end), _to_db_time(current)),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        window_start = _parse_db_time(row["window_start"])
        window = detection_window(
            str(row["tenant_id"]),
            str(row["environment_id"]),
            window_start,
            _positive_integer(row["ledger_watermark"], "cost_anomaly.detector_source_owner_invalid"),
        )
        claim_token = uuid4().hex
        timestamp = _to_db_time(current)
        if row["detection_run_id"] is None:
            run_id = f"car_{uuid4().hex}"
            revision = 1
            # operation IDs 标识整个 detection run；只有首次创建才能生成，后续重评必须复用。
            operation_request_id = f"req_cost_anomaly_{uuid4().hex}"
            operation_trace_id = f"trace_cost_anomaly_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO cost_anomaly_detection_runs(
                  detection_run_id, tenant_id, environment_id,
                  window_start, window_end, baseline_start, baseline_end,
                  ledger_watermark, evaluation_revision, status,
                  environment_current_complete, environment_baseline_complete,
                  partial_scope_count, source_entry_count, estimated_entry_count,
                  legacy_entry_count, unpriced_entry_count, anomaly_count, suppressed_count,
                  error_code, claim_token, lease_owner_id, lease_expires_at,
                  operation_request_id, operation_trace_id,
                  started_at, completed_at, created_at, updated_at
                )
                VALUES(
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, 'processing',
                  NULL, NULL, 0, 0, 0, 0, 0, 0, 0,
                  NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?
                )
                """,
                (
                    run_id,
                    window.tenant_id,
                    window.environment_id,
                    _to_db_time(window.window_start),
                    _to_db_time(window.window_end),
                    _to_db_time(window.baseline_start),
                    _to_db_time(window.baseline_end),
                    window.frozen_watermark,
                    revision,
                    claim_token,
                    owner,
                    _to_db_time(lease_expires),
                    operation_request_id,
                    operation_trace_id,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            run_id = str(row["detection_run_id"])
            # 同一 run 的迟到重评与 lease 接管共享固定调用链，确保事件信封 trace 稳定。
            operation_request_id = _operation_identifier(row["operation_request_id"])
            operation_trace_id = _operation_identifier(row["operation_trace_id"])
            previous_revision = _positive_integer(
                row["evaluation_revision"],
                "cost_anomaly.detector_source_owner_invalid",
            )
            revision = previous_revision + 1
            updated = connection.execute(
                """
                UPDATE cost_anomaly_detection_runs
                SET ledger_watermark=?, evaluation_revision=?, status='processing',
                    environment_current_complete=NULL, environment_baseline_complete=NULL,
                    partial_scope_count=0, source_entry_count=0, estimated_entry_count=0,
                    legacy_entry_count=0, unpriced_entry_count=0,
                    anomaly_count=0, suppressed_count=0, error_code=NULL,
                    claim_token=?, lease_owner_id=?, lease_expires_at=?,
                    started_at=?, completed_at=NULL, updated_at=?
                WHERE tenant_id=? AND environment_id=? AND detection_run_id=?
                  AND evaluation_revision=?
                  AND (status!='processing' OR unixepoch(lease_expires_at)<=unixepoch(?))
                """,
                (
                    window.frozen_watermark,
                    revision,
                    claim_token,
                    owner,
                    _to_db_time(lease_expires),
                    timestamp,
                    timestamp,
                    window.tenant_id,
                    window.environment_id,
                    run_id,
                    previous_revision,
                    timestamp,
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
        connection.commit()
        return DetectionClaim(
            detection_run_id=run_id,
            tenant_id=window.tenant_id,
            environment_id=window.environment_id,
            window=window,
            evaluation_revision=revision,
            claim_token=claim_token,
            lease_owner_id=owner,
            lease_expires_at=lease_expires,
            operation_request_id=operation_request_id,
            operation_trace_id=operation_trace_id,
        )
    # 已归一化领域错误保留固定 code，但仍须回滚当前 claim 写事务。
    except ApiError:
        _rollback_safely(connection)
        raise
    # claim SQL 失败只暴露统一内部码，原始 SQL、触发器正文和数据值不得外泄。
    except sqlite3.Error as exc:
        _rollback_safely(connection)
        raise ApiError("cost_anomaly.detector_unavailable") from exc
    finally:
        connection.close()


def process_claimed_detection(
    config: AppConfig,
    claim: DetectionClaim,
    *,
    now: datetime | None = None,
) -> bool:
    """读取冻结账本、运行规则，并以持久化时刻完成或失败当前 claim。

    返回 True 表示当前 revision 已原子完成或写入固定 failed；False 表示 lease 已过期或
    已被新 worker 接管。生产路径在取得写锁后重新读取 UTC，不能用评估前的陈旧时刻绕过
    lease；任何 SQLite/owner trigger/schema 异常只转换为固定原因码，不读取异常正文。
    """

    try:
        rows = _load_claim_rows(config, claim)
        evaluation = evaluate_window(
            claim.window,
            rows,
            DetectionSettings(
                multiplier_millis=int(config.cost_anomaly_spike_multiplier_millis),
                minimum_cost_micros=int(config.cost_anomaly_min_cost_micros),
                workday_start_hour=int(config.cost_anomaly_workday_start_hour),
                workday_end_hour=int(config.cost_anomaly_workday_end_hour),
            ),
        )
        return _persist_evaluation(config, claim, evaluation, now=now)
    # 纯规则校验只产生白名单原因码，可安全写入 detection run 的失败状态。
    except DetectionDataError as exc:
        reason_code = exc.reason_code
    # 数据库与统一 API 异常不得携带原始正文进入持久状态，只记录固定不可用原因。
    except (ApiError, sqlite3.Error):
        # owner trigger、schema 和数据库异常都只折叠为固定码，禁止检查或记录原始正文。
        reason_code = "cost_anomaly.detector_database_unavailable"
    except (AttributeError, TypeError, ValueError):
        reason_code = "cost_anomaly.detector_clock_invalid"
    return _finalize_failed_claim(config, claim, reason_code, now=now)


def _load_claim_rows(config: AppConfig, claim: DetectionClaim) -> tuple[LedgerCostRow, ...]:
    """在单一只读快照中物化 claim 的正式 Usage Charge，随后关闭数据库连接。"""

    connection = _connect_detector(config)
    try:
        connection.execute("BEGIN")
        rows = connection.execute(
            """
            SELECT
              ledger_sequence, tenant_id, environment_id, created_at,
              normalized_cny_micros, cny_normalization_status, price_evidence_status,
              virtual_key_id, project_id, application_id,
              source_request_id, source_trace_id, estimated
            FROM finops_ledger_entries
            WHERE tenant_id=? AND environment_id=?
              AND entry_type='usage_charge' AND direction='debit' AND status='posted'
              AND unixepoch(created_at)>=unixepoch(?)
              AND unixepoch(created_at)<unixepoch(?)
              AND ledger_sequence<=?
            ORDER BY ledger_sequence ASC
            """,
            (
                claim.tenant_id,
                claim.environment_id,
                _to_db_time(claim.window.baseline_start),
                _to_db_time(claim.window.window_end),
                claim.window.frozen_watermark,
            ),
        ).fetchall()
        result = tuple(
            LedgerCostRow(
                ledger_sequence=row["ledger_sequence"],
                tenant_id=str(row["tenant_id"]),
                environment_id=str(row["environment_id"]),
                created_at=_parse_db_time(row["created_at"]),
                normalized_cny_micros=row["normalized_cny_micros"],
                cny_normalization_status=str(row["cny_normalization_status"]),
                price_evidence_status=str(row["price_evidence_status"]),
                virtual_key_id=row["virtual_key_id"],
                project_id=row["project_id"],
                application_id=row["application_id"],
                source_request_id=row["source_request_id"],
                source_trace_id=row["source_trace_id"],
                estimated=bool(row["estimated"]),
            )
            for row in rows
        )
        connection.commit()
        return result
    # 账本 owner、金额或时间合同失败必须先回滚只读快照，再原样交给 run 收口。
    except DetectionDataError:
        _rollback_safely(connection)
        raise
    # 读取正式账本失败时统一转换领域错误，禁止调用方检查数据库异常正文。
    except sqlite3.Error as exc:
        _rollback_safely(connection)
        raise ApiError("cost_anomaly.detector_unavailable") from exc
    finally:
        connection.close()


def _persist_evaluation(
    config: AppConfig,
    claim: DetectionClaim,
    evaluation: WindowEvaluation,
    *,
    now: datetime | None = None,
) -> bool:
    """在有效 claim 下执行精确幂等、冷却判定并原子完成 run。

    `now` 仅供冻结时钟测试和单轮运行验收使用。生产路径必须在取得 SQLite 写锁之后再
    读取完成时刻，避免排队等待写锁时仍用评估前的旧时钟错误通过 lease CAS。
    """

    connection = _connect_detector(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        completed_at = _detector_now(now)
        if not _claim_is_current(connection, claim, completed_at):
            connection.rollback()
            return False
        inserted = 0
        suppressed = 0
        for anomaly in evaluation.anomalies:
            # 同窗口迟到重评属于精确幂等，不能记成业务冷却，否则运行指标会误报抑制量。
            if _exact_anomaly_exists(connection, claim, anomaly):
                continue
            if _cooldown_suppresses(connection, claim, anomaly):
                suppressed += 1
                continue
            _insert_anomaly(connection, claim, anomaly, completed_at, config)
            inserted += 1
        timestamp = _to_db_time(completed_at)
        updated = connection.execute(
            """
            UPDATE cost_anomaly_detection_runs
            SET status=?,
                environment_current_complete=?,
                environment_baseline_complete=?,
                partial_scope_count=?,
                source_entry_count=?,
                estimated_entry_count=?,
                legacy_entry_count=?,
                unpriced_entry_count=?,
                anomaly_count=?,
                suppressed_count=?,
                error_code=?,
                claim_token=NULL,
                lease_owner_id=NULL,
                lease_expires_at=NULL,
                completed_at=?,
                updated_at=?
            WHERE tenant_id=? AND environment_id=? AND detection_run_id=?
              AND evaluation_revision=? AND status='processing' AND claim_token=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                evaluation.status,
                int(evaluation.environment_current_complete),
                int(evaluation.environment_baseline_complete),
                evaluation.partial_scope_count,
                evaluation.source_entry_count,
                evaluation.estimated_entry_count,
                evaluation.legacy_entry_count,
                evaluation.unpriced_entry_count,
                inserted,
                suppressed,
                evaluation.error_code,
                timestamp,
                timestamp,
                claim.tenant_id,
                claim.environment_id,
                claim.detection_run_id,
                claim.evaluation_revision,
                claim.claim_token,
                timestamp,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    # 校验或固定映射错误必须回滚 anomaly 与 run 的同一事务，避免留下半成品。
    except DetectionDataError:
        _rollback_safely(connection)
        raise
    # SQLite 写入失败先完整回滚，再按数字错误码归一化且不读取异常文本。
    except sqlite3.Error as exc:
        _rollback_safely(connection)
        # 只读取 SQLite 数字错误码；触发器正文、SQL 和数据库值均不得进入 run 或日志。
        raise DetectionDataError(_sqlite_failure_reason(exc)) from exc
    finally:
        connection.close()


def _insert_anomaly(
    connection: sqlite3.Connection,
    claim: DetectionClaim,
    anomaly: DetectedAnomaly,
    detected_at: datetime,
    config: AppConfig,
) -> None:
    """插入一条不可变检测证据，并把稳定 Webhook event 置为 pending。"""

    # 异常必须可追溯到同租户源请求；缺少 request_id 或 trace_id 时禁止创建事件。
    if not anomaly.representative_request_id or not anomaly.representative_trace_id:
        raise DetectionDataError("cost_anomaly.detector_source_owner_invalid")
    anomaly_id = f"can_{uuid4().hex}"
    event_id = f"caevt_{uuid4().hex}"
    timestamp = _to_db_time(detected_at)
    eligible_until = _to_db_time(
        detected_at + timedelta(hours=MITIGATION_ELIGIBILITY_HOURS)
    )
    connection.execute(
        """
        INSERT INTO cost_anomalies(
          cost_anomaly_id, event_id, tenant_id, environment_id, detection_run_id,
          rule_type, severity, status, scope_type, scope_id,
          virtual_key_id, top_virtual_key_id,
          project_id_snapshot, application_id_snapshot,
          window_start, window_end, baseline_start, baseline_end,
          current_cost_micros, baseline_total_micros, baseline_bucket_count,
          observed_ratio_millis, threshold_multiplier_millis, minimum_cost_micros,
          cooldown_minutes, source_entry_count, estimated_entry_count,
          legacy_entry_count, affected_virtual_key_count,
          representative_request_id, representative_trace_id,
          source_ledger_sequence_min, source_ledger_sequence_max,
          recommended_action, detection_version, detected_at, mitigation_eligible_until,
          event_dispatch_status, event_dispatch_attempts, event_next_attempt_at,
          event_dispatched_at, event_claim_token, event_lease_owner_id,
          event_lease_expires_at, event_last_error_code, created_at, updated_at
        )
        VALUES(
          ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?,
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
          'pending', 0, ?, NULL, NULL, NULL, NULL, NULL, ?, ?
        )
        """,
        (
            anomaly_id,
            event_id,
            claim.tenant_id,
            claim.environment_id,
            claim.detection_run_id,
            anomaly.rule_type,
            anomaly.severity,
            anomaly.scope_type,
            anomaly.scope_id,
            anomaly.virtual_key_id,
            anomaly.top_virtual_key_id,
            anomaly.project_id_snapshot,
            anomaly.application_id_snapshot,
            _to_db_time(claim.window.window_start),
            _to_db_time(claim.window.window_end),
            _to_db_time(claim.window.baseline_start),
            _to_db_time(claim.window.baseline_end),
            str(anomaly.current_cost_micros),
            str(anomaly.baseline_total_micros),
            BASELINE_BUCKET_COUNT,
            str(anomaly.observed_ratio_millis)
            if anomaly.observed_ratio_millis is not None
            else None,
            int(config.cost_anomaly_spike_multiplier_millis),
            int(config.cost_anomaly_min_cost_micros),
            COOLDOWN_MINUTES,
            anomaly.source_entry_count,
            anomaly.estimated_entry_count,
            anomaly.legacy_entry_count,
            anomaly.affected_virtual_key_count,
            anomaly.representative_request_id,
            anomaly.representative_trace_id,
            anomaly.source_ledger_sequence_min,
            anomaly.source_ledger_sequence_max,
            anomaly.recommended_action,
            DETECTION_VERSION,
            timestamp,
            eligible_until,
            timestamp,
            timestamp,
            timestamp,
        ),
    )


def _cooldown_suppresses(
    connection: sqlite3.Connection,
    claim: DetectionClaim,
    anomaly: DetectedAnomaly,
) -> bool:
    """按业务 window_start 执行同规则、同 scope 的开区间三十分钟冷却。

    下界使用严格大于，使恰好相隔三十分钟的持续异常可以形成新事实；上界严格小于，把
    同窗口幂等留给 `_exact_anomaly_exists`，避免迟到重评被误计为 cooldown suppression。
    """

    cooldown_start = claim.window.window_start - timedelta(minutes=COOLDOWN_MINUTES)
    row = connection.execute(
        """
        SELECT 1
        FROM cost_anomalies
        WHERE tenant_id=? AND environment_id=?
          AND rule_type=? AND scope_type=? AND scope_id=?
          AND unixepoch(window_start)>unixepoch(?)
          AND unixepoch(window_start)<unixepoch(?)
        LIMIT 1
        """,
        (
            claim.tenant_id,
            claim.environment_id,
            anomaly.rule_type,
            anomaly.scope_type,
            anomaly.scope_id,
            _to_db_time(cooldown_start),
            _to_db_time(claim.window.window_start),
        ),
    ).fetchone()
    return row is not None


def _exact_anomaly_exists(
    connection: sqlite3.Connection,
    claim: DetectionClaim,
    anomaly: DetectedAnomaly,
) -> bool:
    """判断同租户、Environment、规则、scope 和业务窗口的事实是否已经存在。

    查询与插入位于同一个 `BEGIN IMMEDIATE` 中；数据库唯一键仍是并发安全的最终防线。
    这里显式查询是为了让迟到重评成为可解释的 no-op，而不是依赖唯一冲突走失败路径。
    """

    row = connection.execute(
        """
        SELECT 1
        FROM cost_anomalies
        WHERE tenant_id=? AND environment_id=?
          AND rule_type=? AND scope_type=? AND scope_id=?
          AND window_start=?
        LIMIT 1
        """,
        (
            claim.tenant_id,
            claim.environment_id,
            anomaly.rule_type,
            anomaly.scope_type,
            anomaly.scope_id,
            _to_db_time(claim.window.window_start),
        ),
    ).fetchone()
    return row is not None


def _claim_is_current(
    connection: sqlite3.Connection,
    claim: DetectionClaim,
    now: datetime,
) -> bool:
    """验证 run 的 tenant、revision、token、owner 与未过期 lease 全部仍匹配。"""

    row = connection.execute(
        """
        SELECT 1
        FROM cost_anomaly_detection_runs
        WHERE tenant_id=? AND environment_id=? AND detection_run_id=?
          AND evaluation_revision=? AND status='processing'
          AND claim_token=? AND lease_owner_id=?
          AND unixepoch(lease_expires_at)>unixepoch(?)
        """,
        (
            claim.tenant_id,
            claim.environment_id,
            claim.detection_run_id,
            claim.evaluation_revision,
            claim.claim_token,
            claim.lease_owner_id,
            _to_db_time(now),
        ),
    ).fetchone()
    return row is not None


def _finalize_failed_claim(
    config: AppConfig,
    claim: DetectionClaim,
    reason_code: str,
    *,
    now: datetime | None = None,
) -> bool:
    """仅在当前 lease 仍有效时把 run 原子终结为固定 failed 原因。

    与成功持久化相同，失败完成时刻必须在取得写锁后读取，防止锁等待期间 lease 已过期却
    仍被旧 worker 写成 failed。`now` 只用于冻结时钟测试和单轮运行验收。
    """

    allowed = {
        "cost_anomaly.detector_source_owner_invalid",
        "cost_anomaly.detector_amount_invalid",
        "cost_anomaly.detector_schema_unavailable",
        "cost_anomaly.detector_database_unavailable",
        "cost_anomaly.detector_clock_invalid",
    }
    reason = reason_code if reason_code in allowed else "cost_anomaly.detector_database_unavailable"
    connection = _connect_detector(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        completed_at = _detector_now(now)
        timestamp = _to_db_time(completed_at)
        updated = connection.execute(
            """
            UPDATE cost_anomaly_detection_runs
            SET status='failed',
                environment_current_complete=NULL,
                environment_baseline_complete=NULL,
                partial_scope_count=0,
                source_entry_count=0,
                estimated_entry_count=0,
                legacy_entry_count=0,
                unpriced_entry_count=0,
                anomaly_count=0,
                suppressed_count=0,
                error_code=?,
                claim_token=NULL,
                lease_owner_id=NULL,
                lease_expires_at=NULL,
                completed_at=?,
                updated_at=?
            WHERE tenant_id=? AND environment_id=? AND detection_run_id=?
              AND evaluation_revision=? AND status='processing'
              AND claim_token=? AND lease_owner_id=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                reason,
                timestamp,
                timestamp,
                claim.tenant_id,
                claim.environment_id,
                claim.detection_run_id,
                claim.evaluation_revision,
                claim.claim_token,
                claim.lease_owner_id,
                timestamp,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    # failed CAS 自身写库失败时只能回滚并返回统一内部码，不能伪报已完成。
    except sqlite3.Error as exc:
        _rollback_safely(connection)
        raise ApiError("cost_anomaly.detector_unavailable") from exc
    finally:
        connection.close()


def _connect_detector(config: AppConfig) -> sqlite3.Connection:
    """创建检测器独享连接，并把连接失败折叠为固定内部错误。"""

    try:
        return connect_database(config.database_url)
    # 连接层错误可能含数据库路径等部署信息，只允许向上暴露固定不可用码。
    except DbError as exc:
        raise ApiError("cost_anomaly.detector_unavailable") from exc


def _sqlite_failure_reason(exc: sqlite3.Error) -> str:
    """仅按 SQLite 数字错误码把持久化失败映射为稳定内部原因。

    `SQLITE_ERROR` 和 `SQLITE_SCHEMA` 表示当前代码依赖的表、列或语句合同不可用；其余
    锁、I/O、约束和 owner trigger 失败统一归为数据库不可用。禁止读取 `str(exc)`，因为
    原始异常可能包含 SQL、触发器正文或业务数据。
    """

    error_code = getattr(exc, "sqlite_errorcode", None)
    base_code = error_code & 0xFF if isinstance(error_code, int) else None
    # 仅数字主错误码可参与分类，绝不能通过异常正文猜测 schema 或触发器类型。
    if base_code in {sqlite3.SQLITE_ERROR, sqlite3.SQLITE_SCHEMA}:
        return "cost_anomaly.detector_schema_unavailable"
    return "cost_anomaly.detector_database_unavailable"


def _lease_owner(value: object) -> str:
    """校验内部 worker ID，防止控制字符进入租约或日志定位字段。"""

    normalized = str(value or "").strip()
    # worker 标识进入租约与排障字段，空值、超长值和控制字符必须在写库前拒绝。
    if not normalized or len(normalized) > 128 or any(ord(char) < 32 for char in normalized):
        raise ApiError("cost_anomaly.detector_unavailable")
    return normalized


def _operation_identifier(value: object) -> str:
    """恢复 detection run 首次创建的固定操作标识，供迟到重评和 lease 接管复用。"""

    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or any(ord(character) < 32 for character in value)
    ):
        # 损坏或含控制字符的链路标识不能进入 claim、事件信封或内部诊断字段。
        raise ApiError("cost_anomaly.detector_unavailable")
    return value


def _detector_now(value: datetime | None) -> datetime:
    """返回带时区 UTC 服务端时刻；HTTP 和普通环境变量不得覆盖该值。"""

    current = value if value is not None else datetime.now(UTC)
    # lease CAS 只能使用明确时区的服务端时刻，naive 冻结时钟也必须失败关闭。
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise DetectionDataError("cost_anomaly.detector_clock_invalid")
    return current.astimezone(UTC).replace(microsecond=0)


def _to_db_time(value: datetime) -> str:
    """把带时区时刻稳定编码为 SQLite 可比较的 UTC 秒级文本。"""

    return _aware_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_db_time(value: object) -> datetime:
    """解析数据库 UTC 时间；损坏值按检测时钟合同失败关闭。"""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # 损坏数据库时间不能按本地时区猜测，统一终止当前检测 revision。
    except (TypeError, ValueError) as exc:
        raise DetectionDataError("cost_anomaly.detector_clock_invalid") from exc
    return _aware_utc(parsed)


def _rollback_safely(connection: sqlite3.Connection) -> None:
    """尽力回滚当前事务；原始 SQLite 异常不得覆盖稳定领域错误。"""

    try:
        connection.rollback()
    # 回滚异常不得覆盖最初的固定领域错误，也不得把 SQLite 正文写入日志。
    except sqlite3.Error:
        return