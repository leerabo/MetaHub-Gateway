"""PRD-053 成本异常纯规则、金额、时区与完整性专项测试。"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from metahub_shared.runtime.errors import ApiError

import metahub_control.cost_anomalies.detector as detector_module
import metahub_control.cost_anomalies.scheduler as scheduler_module
from metahub_control.cost_anomalies.detector import (
    BASELINE_BUCKET_COUNT,
    DetectedAnomaly,
    DetectionClaim,
    DetectionDataError,
    DetectionSettings,
    LedgerCostRow,
    WindowEvaluation,
    detection_window,
    evaluate_window,
    is_off_hours,
)

# 测试统一使用冻结阈值与工作时段，确保金额、时区和边界断言不受部署环境影响。
SETTINGS = DetectionSettings(
    multiplier_millis=5000,
    minimum_cost_micros=10_000,
    workday_start_hour=9,
    workday_end_hour=18,
)


def _claim(start: datetime) -> DetectionClaim:
    """构造仅含不透明身份和固定租约的 detector claim。"""

    window = detection_window("tenant-a", "env-a", start, 10)
    return DetectionClaim(
        detection_run_id="car_0123456789abcdef0123456789abcdef",
        tenant_id=window.tenant_id,
        environment_id=window.environment_id,
        window=window,
        evaluation_revision=1,
        claim_token="0123456789abcdef0123456789abcdef",
        lease_owner_id="worker-prd053-test",
        lease_expires_at=start + timedelta(seconds=30),
        operation_request_id="req_prd053_detector_test",
        operation_trace_id="trace_prd053_detector_test",
    )


def _anomaly() -> DetectedAnomaly:
    """构造冷却查询只会读取规则和作用域字段的安全异常候选。"""

    return DetectedAnomaly(
        rule_type="virtual_key_spend_spike",
        severity="critical",
        scope_type="virtual_key",
        scope_id="key-a",
        virtual_key_id="key-a",
        top_virtual_key_id="key-a",
        project_id_snapshot="project-a",
        application_id_snapshot="application-a",
        current_cost_micros=10_000,
        baseline_total_micros=10_000,
        observed_ratio_millis=288_000,
        source_entry_count=1,
        estimated_entry_count=0,
        legacy_entry_count=0,
        affected_virtual_key_count=1,
        representative_request_id="request-1",
        representative_trace_id="trace-1",
        source_ledger_sequence_min=1,
        source_ledger_sequence_max=1,
        recommended_action="apply_temporary_rate_limit",
    )


def _evaluation() -> WindowEvaluation:
    """构造无异常的完整评估，供事务时钟和失败归一化测试使用。"""

    return WindowEvaluation(
        status="completed",
        error_code=None,
        environment_current_complete=True,
        environment_baseline_complete=True,
        source_entry_count=1,
        estimated_entry_count=0,
        legacy_entry_count=0,
        unpriced_entry_count=0,
        partial_scope_count=0,
        anomalies=(),
    )


def _db_time(value: datetime) -> str:
    """把测试时间编码为生产迁移使用的 UTC 秒级文本。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _row(
    sequence: int,
    created_at: datetime,
    amount: object,
    *,
    key_id: str = "key-a",
    evidence: str = "priced",
    normalization: str = "normalized",
    tenant_id: str = "tenant-a",
    environment_id: str = "env-a",
) -> LedgerCostRow:
    """构造不含正文和密钥的最小正式账本输入。"""

    return LedgerCostRow(
        ledger_sequence=sequence,
        tenant_id=tenant_id,
        environment_id=environment_id,
        created_at=created_at,
        normalized_cny_micros=amount,
        cny_normalization_status=normalization,
        price_evidence_status=evidence,
        virtual_key_id=key_id,
        project_id="project-a",
        application_id="application-a",
        source_request_id=f"request-{sequence}",
        source_trace_id=f"trace-{sequence}",
        estimated=False,
    )


def test_spike_uses_288_bucket_baseline_and_python_integer_ratio() -> None:
    """主流程：工作时段内 tenant 与 Key 都按 288 桶整数交叉乘积触发。"""

    start = datetime(2026, 7, 27, 2, 0, tzinfo=UTC)
    window = detection_window("tenant-a", "env-a", start, 2)
    result = evaluate_window(
        window,
        (
            _row(1, start - timedelta(minutes=5), 10_000),
            _row(2, start + timedelta(seconds=1), 10_000),
        ),
        SETTINGS,
    )

    assert result.status == "completed"
    assert result.environment_current_complete is True
    assert result.environment_baseline_complete is True
    assert [item.rule_type for item in result.anomalies] == [
        "tenant_spend_spike",
        "virtual_key_spend_spike",
    ]
    assert result.anomalies[0].observed_ratio_millis == (
        10_000 * BASELINE_BUCKET_COUNT * 1000 // 10_000
    )


def test_exact_threshold_and_zero_baseline_do_not_trigger_spike() -> None:
    """边界：严格“超过”才告警，等于五倍以及零基线都不产生 spike。"""

    start = datetime(2026, 7, 27, 2, 0, tzinfo=UTC)
    window = detection_window("tenant-a", "env-a", start, 2)
    # baseline_total=576000，288 桶均值=2000；current=10000 恰好为五倍。
    equal = evaluate_window(
        window,
        (
            _row(1, start - timedelta(minutes=5), 576_000),
            _row(2, start + timedelta(seconds=1), 10_000),
        ),
        SETTINGS,
    )
    zero = evaluate_window(
        window,
        (_row(2, start + timedelta(seconds=1), 10_000),),
        SETTINGS,
    )

    assert equal.anomalies == ()
    assert zero.anomalies == ()


def test_off_hours_is_independent_from_zero_baseline() -> None:
    """非工作时段：周末 Key 只要当前金额完整即可独立触发，零基线不遮蔽。"""

    start = datetime(2026, 7, 25, 2, 0, tzinfo=UTC)
    window = detection_window("tenant-a", "env-a", start, 1)
    result = evaluate_window(
        window,
        (_row(1, start + timedelta(seconds=1), 10_000),),
        SETTINGS,
    )

    assert is_off_hours(start, SETTINGS) is True
    assert [item.rule_type for item in result.anomalies] == [
        "virtual_key_off_hours"
    ]


def test_unpriced_key_makes_tenant_partial_but_preserves_other_key_rule() -> None:
    """完整性：坏 Key 阻止 tenant 聚合，但不掩盖另一个证据完整 Key 的 spike。"""

    start = datetime(2026, 7, 27, 2, 0, tzinfo=UTC)
    window = detection_window("tenant-a", "env-a", start, 4)
    result = evaluate_window(
        window,
        (
            _row(1, start - timedelta(minutes=5), 10_000, key_id="key-a"),
            _row(2, start + timedelta(seconds=1), 10_000, key_id="key-a"),
            _row(
                3,
                start + timedelta(seconds=2),
                None,
                key_id="key-b",
                evidence="unpriced",
                normalization="unavailable",
            ),
        ),
        SETTINGS,
    )

    assert result.status == "partial"
    assert result.error_code == "cost_anomaly.detector_source_incomplete"
    assert result.environment_current_complete is False
    assert result.unpriced_entry_count == 1
    assert [item.rule_type for item in result.anomalies] == [
        "virtual_key_spend_spike"
    ]


@pytest.mark.parametrize("amount", [True, 1.5, "10000", -1, 1_000_000_000_000_001])
def test_trusted_amount_rejects_noncanonical_runtime_values(amount: object) -> None:
    """异常：bool、REAL、字符串、负数和越界金额都不能冒充可信 micros。"""

    start = datetime(2026, 7, 27, 2, 0, tzinfo=UTC)
    window = detection_window("tenant-a", "env-a", start, 1)
    with pytest.raises(DetectionDataError) as error:
        evaluate_window(
            window,
            (_row(1, start + timedelta(seconds=1), amount),),
            SETTINGS,
        )
    assert error.value.reason_code == "cost_anomaly.detector_amount_invalid"


def test_owner_and_frozen_watermark_are_fail_closed() -> None:
    """租户隔离：跨 tenant 行和超过冻结水位的迟到行必须终止本 revision。"""

    start = datetime(2026, 7, 27, 2, 0, tzinfo=UTC)
    window = detection_window("tenant-a", "env-a", start, 1)
    with pytest.raises(DetectionDataError) as owner_error:
        evaluate_window(
            window,
            (_row(1, start, 10_000, tenant_id="tenant-b"),),
            SETTINGS,
        )
    with pytest.raises(DetectionDataError) as watermark_error:
        evaluate_window(window, (_row(2, start, 10_000),), SETTINGS)

    assert owner_error.value.reason_code == "cost_anomaly.detector_source_owner_invalid"
    assert watermark_error.value.reason_code == "cost_anomaly.detector_source_owner_invalid"

def test_cooldown_allows_exact_thirty_minutes_and_separates_exact_duplicate() -> None:
    """冷却边界：恰好三十分钟允许新告警，同窗口只走精确幂等。"""

    start = datetime(2026, 7, 27, 2, 0, tzinfo=UTC)
    claim = _claim(start)
    anomaly = _anomaly()
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE cost_anomalies(
              tenant_id TEXT, environment_id TEXT, rule_type TEXT,
              scope_type TEXT, scope_id TEXT, window_start TEXT
            )
            """
        )
        values = (
            claim.tenant_id,
            claim.environment_id,
            anomaly.rule_type,
            anomaly.scope_type,
            anomaly.scope_id,
        )
        insert_sql = (
            "INSERT INTO cost_anomalies("
            "tenant_id, environment_id, rule_type, scope_type, scope_id, window_start"
            ") VALUES(?, ?, ?, ?, ?, ?)"
        )

        connection.execute(insert_sql, (*values, _db_time(start - timedelta(minutes=30))))
        assert detector_module._cooldown_suppresses(connection, claim, anomaly) is False
        connection.execute("DELETE FROM cost_anomalies")

        connection.execute(insert_sql, (*values, _db_time(start - timedelta(minutes=25))))
        assert detector_module._cooldown_suppresses(connection, claim, anomaly) is True
        connection.execute("DELETE FROM cost_anomalies")

        connection.execute(insert_sql, (*values, _db_time(start)))
        assert detector_module._exact_anomaly_exists(connection, claim, anomaly) is True
        assert detector_module._cooldown_suppresses(connection, claim, anomaly) is False
    finally:
        connection.close()


def test_persist_reads_completion_clock_after_write_transaction_begins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """租约：等待写锁后重新取完成时刻，不能用评估前的陈旧时间做 CAS。"""

    start = datetime(2026, 7, 27, 2, 0, tzinfo=UTC)
    late = start + timedelta(seconds=31)
    clock = {"value": start}
    observed: list[datetime] = []
    lifecycle: list[str] = []

    def execute(statement: str) -> None:
        """模拟取得 BEGIN IMMEDIATE 写锁后真实时钟跨过 lease。"""

        assert statement == "BEGIN IMMEDIATE"
        clock["value"] = late
        lifecycle.append("begin")

    def rollback() -> None:
        """记录旧 claim 被拒绝后的事务回滚。"""

        lifecycle.append("rollback")

    def close() -> None:
        """记录 detector 独享数据库连接在 finally 中可靠关闭，防止测试泄漏连接。"""

        lifecycle.append("close")

    connection = SimpleNamespace(execute=execute, rollback=rollback, close=close)
    monkeypatch.setattr(detector_module, "_connect_detector", lambda _config: connection)
    monkeypatch.setattr(detector_module, "_detector_now", lambda _now: clock["value"])

    def claim_is_current(_connection, _claim_value, completed_at: datetime) -> bool:
        """捕获完成 CAS 实际使用的持久化时刻。"""

        observed.append(completed_at)
        return False

    monkeypatch.setattr(detector_module, "_claim_is_current", claim_is_current)
    assert detector_module._persist_evaluation(
        SimpleNamespace(),
        _claim(start),
        _evaluation(),
        now=None,
    ) is False
    assert observed == [late]
    assert lifecycle == ["begin", "rollback", "close"]


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (
            DetectionDataError("cost_anomaly.detector_schema_unavailable"),
            "cost_anomaly.detector_schema_unavailable",
        ),
        (sqlite3.IntegrityError("owner trigger"), "cost_anomaly.detector_database_unavailable"),
        (sqlite3.OperationalError("schema changed"), "cost_anomaly.detector_database_unavailable"),
        (ApiError("cost_anomaly.detector_unavailable"), "cost_anomaly.detector_database_unavailable"),
    ],
)
def test_persist_failure_is_finalized_with_fixed_reason(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_reason: str,
) -> None:
    """失败状态：owner trigger、schema/SQL 与统一数据库异常都写固定原因码。"""

    start = datetime(2026, 7, 27, 2, 0, tzinfo=UTC)
    reasons: list[str] = []
    monkeypatch.setattr(detector_module, "_load_claim_rows", lambda _config, _claim: ())
    monkeypatch.setattr(
        detector_module,
        "evaluate_window",
        lambda _window, _rows, _settings: _evaluation(),
    )

    def fail_persist(*_args, **_kwargs):
        """在持久化边界抛出不含业务正文的指定异常。"""

        raise failure

    def capture_failed(
        _config,
        _claim_value,
        reason_code: str,
        *,
        now: datetime | None,
    ) -> bool:
        """捕获最终写入的固定失败码，模拟 CAS 成功。"""

        assert now == start
        reasons.append(reason_code)
        return True

    monkeypatch.setattr(detector_module, "_persist_evaluation", fail_persist)
    monkeypatch.setattr(detector_module, "_finalize_failed_claim", capture_failed)
    assert detector_module.process_claimed_detection(
        SimpleNamespace(
            cost_anomaly_spike_multiplier_millis=5000,
            cost_anomaly_min_cost_micros=10_000,
            cost_anomaly_workday_start_hour=9,
            cost_anomaly_workday_end_hour=18,
        ),
        _claim(start),
        now=start,
    ) is True
    assert reasons == [expected_reason]


def test_sqlite_schema_error_maps_without_using_exception_text() -> None:
    """安全归一化：只按 SQLite 数字错误码识别 schema，不持久化原始异常文本。"""

    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.OperationalError) as captured:
            connection.execute("SELECT missing_column FROM missing_table")
        assert detector_module._sqlite_failure_reason(captured.value) == (
            "cost_anomaly.detector_schema_unavailable"
        )
        assert detector_module._sqlite_failure_reason(
            sqlite3.IntegrityError("owner trigger detail")
        ) == "cost_anomaly.detector_database_unavailable"
    finally:
        connection.close()


@pytest.mark.anyio
async def test_scheduler_shutdown_waits_for_active_thread_tick() -> None:
    """停机：lifespan 不得在当前 to_thread 仍可能写数据库时提前返回。"""

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    stop_event = asyncio.Event()

    def blocking_tick() -> None:
        """模拟已经进入同步数据库阶段、只能自然完成的当前 tick。"""

        started.set()
        release.wait(timeout=2)
        finished.set()

    worker_task = asyncio.create_task(asyncio.to_thread(blocking_tick))
    assert await asyncio.to_thread(started.wait, 1)
    shutdown_task = asyncio.create_task(
        scheduler_module._stop_scheduler_task(stop_event, worker_task)
    )
    try:
        await asyncio.sleep(0.02)
        assert stop_event.is_set()
        assert shutdown_task.done() is False
        assert finished.is_set() is False
    finally:
        release.set()
        await asyncio.wait_for(shutdown_task, timeout=1)
    assert worker_task.done() is True
    assert finished.is_set() is True
