"""PRD-049 Feedback 迁移、共享存储和条件回滚的数据库专项测试。

用例从真实 ``0001..0039`` 基线复制独立数据库，验证 tenant-qualified 循环外键、不可变事实、
REPLACE 加固、派生聚合、父事实保护和 down guard；测试数据只含安全 ID/计数，不保存请求或响应正文。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import metahub_shared.db.migrate as migration_module
import metahub_shared.feedback.storage as feedback_storage
import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError
from metahub_shared.feedback import (
    FeedbackActor,
    create_request_feedback,
    normalize_feedback_payload,
    query_feedback_summary,
)
from metahub_shared.runtime.errors import ApiError

# 固定时间和对象前缀让失败证据可读；每个测试仍使用复制数据库保持事实互不污染。
SOURCE_TIME = "2026-07-20T03:00:00Z"
FEEDBACK_TIME = datetime(2026, 7, 20, 3, 1, tzinfo=UTC)
# 运行验收会按精确名称再次读取这六项；测试使用同一冻结集合，防止版本存在但防线漏建。
PRD049_HARDENING_TRIGGERS = frozenset(
    {
        "trg_request_feedback_conflict_insert_prd049",
        "trg_request_feedback_event_conflict_insert_prd049",
        "trg_request_log_feedback_parent_conflict_insert_prd049",
        "trg_request_log_feedback_parent_conflict_update_prd049",
        "trg_routing_attempt_feedback_parent_conflict_insert_prd049",
        "trg_routing_attempt_feedback_parent_conflict_update_prd049",
    }
)
# 十三个名称分别对应十条 INSERT OR REPLACE 与三条 UPDATE OR REPLACE 攻击分支。
PRD049_CONFLICT_BRANCHES = (
    "feedback_global_id",
    "feedback_event_id",
    "feedback_source",
    "feedback_session_operation",
    "feedback_key_operation",
    "event_global_id",
    "event_feedback_id",
    "request_log_insert",
    "attempt_insert_global_id",
    "attempt_insert_sequence",
    "request_log_update",
    "attempt_update_global_id",
    "attempt_update_sequence",
)


class _SummaryCursor:
    """包装总计查询 cursor，在首行被实际读取后触发并发写回调。"""

    def __init__(self, cursor: sqlite3.Cursor, callback) -> None:
        """保存真实 cursor 和一次性回调，不复制或修改任何查询结果。"""

        self._cursor = cursor
        self._callback = callback

    def fetchone(self):
        """先从当前只读快照取总计，再触发另连接提交以验证后续分段仍使用原快照。"""

        row = self._cursor.fetchone()
        callback = self._callback
        self._callback = None
        if callback is not None:
            # 回调只在第一行取出后运行一次，避免后续 fetch 重复创建反馈事实。
            callback()
        return row


class _TrackingSummaryConnection:
    """跟踪 query_only/BEGIN/progress 清理，并可在总计后提交并发 Feedback。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        after_total=None,
        interrupt_total: bool = False,
    ) -> None:
        """绑定真实连接；可选回调用于快照测试，可选中断用于 unavailable 映射。"""

        self._connection = connection
        self._after_total = after_total
        self._interrupt_total = interrupt_total
        self.statements: list[str] = []
        self.progress_handlers: list[object | None] = []
        self.closed = False

    def execute(self, sql: str, parameters=()):
        """记录 SQL 顺序，并只在 Feedback 总计语句执行受控回调或故障注入。"""

        normalized = " ".join(sql.split())
        self.statements.append(normalized)
        if "COUNT(*) AS feedback_count" in normalized and self._interrupt_total:
            # 模拟 SQLite progress handler 的 interrupted 结果，不能泄露底层错误到 API。
            raise sqlite3.OperationalError("interrupted")
        cursor = self._connection.execute(sql, parameters)
        if "COUNT(*) AS feedback_count" in normalized and self._after_total is not None:
            callback = self._after_total
            self._after_total = None
            return _SummaryCursor(cursor, callback)
        return cursor

    def set_progress_handler(self, handler, steps: int) -> None:
        """记录安装/清除动作并委托 SQLite，确保 endpoint deadline 不遗留到复用连接。"""

        self.progress_handlers.append(handler)
        self._connection.set_progress_handler(handler, steps)

    def rollback(self) -> None:
        """撤销只读事务或失败路径，保持测试连接状态与生产 finally 一致。"""

        self._connection.rollback()

    def close(self) -> None:
        """关闭真实连接并记录清理已发生。"""

        self.closed = True
        self._connection.close()


@pytest.fixture(scope="module")
def prd049_migration_snapshot(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """冻结只含 0001..0039 的迁移目录，并在模块结束后恢复调用进程环境。"""

    source_directory = Path(__file__).resolve().parents[3] / "infra" / "migrations"
    snapshot_directory = tmp_path_factory.mktemp("prd049-migrations")
    copied_versions: set[str] = set()
    for source_file in source_directory.glob("*.sql"):
        version = source_file.name[:4]
        if not version.isdigit() or int(version) > 39:
            continue
        shutil.copy2(source_file, snapshot_directory / source_file.name)
        if not source_file.name.endswith(".down.sql"):
            copied_versions.add(version)
    assert copied_versions == {f"{version:04d}" for version in range(1, 40)}

    previous_directory = os.environ.get("METAHUB_MIGRATIONS_DIR")
    # 历史 PRD 专项必须在自己的版本边界内执行 apply/down/up；否则新增迁移会改变
    # rollback_last_migration 的目标，测试即使放宽数量断言也不再验证 PRD-049。
    os.environ["METAHUB_MIGRATIONS_DIR"] = str(snapshot_directory)
    try:
        yield snapshot_directory
    finally:
        if previous_directory is None:
            os.environ.pop("METAHUB_MIGRATIONS_DIR", None)
        else:
            os.environ["METAHUB_MIGRATIONS_DIR"] = previous_directory


@pytest.fixture(scope="module")
def prd049_base_database(
    tmp_path_factory: pytest.TempPathFactory,
    prd049_migration_snapshot: Path,
) -> Path:
    """只执行一次冻结到 0039 的完整迁移和 seed，后续测试复制该关闭连接的基线。"""

    assert prd049_migration_snapshot.is_dir()

    directory = tmp_path_factory.mktemp("prd049-base")
    path = directory / "metahub-prd049-base.db"
    database_url = _sqlite_url(path)
    result = apply_migrations(database_url)
    assert result.applied == [f"{version:04d}" for version in range(1, 40)]
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
    return path


@pytest.fixture()
def database_url(prd049_base_database: Path, tmp_path: Path) -> str:
    """为每个用例复制独立 0039 数据库，避免回滚或故障注入相互影响。"""

    target = tmp_path / "metahub-prd049.db"
    shutil.copy2(prd049_base_database, target)
    return _sqlite_url(target)


def test_prd049_full_migration_has_composite_deferred_fk_and_empty_down(
    database_url: str,
) -> None:
    """完整 39 迁移具备双向 FK 与六项加固；空事实逐级 down 到 0037 后仍可重放。"""

    repeated = apply_migrations(database_url)
    assert repeated.applied == []
    assert repeated.skipped[-1] == "0039"
    connection = connect_database(database_url)
    try:
        assert _table_names(connection).issuperset(
            {"request_feedbacks", "request_feedback_events", "model_feedback_statistics"}
        )
        assert _trigger_names(connection).issuperset(PRD049_HARDENING_TRIGGERS)
        feedback_fk = _foreign_key_columns(
            connection,
            table="request_feedbacks",
            parent="request_feedback_events",
        )
        event_fk = _foreign_key_columns(
            connection,
            table="request_feedback_events",
            parent="request_feedbacks",
        )
        assert feedback_fk == [
            ("tenant_id", "tenant_id"),
            ("feedback_event_id", "feedback_event_id"),
        ]
        assert event_fk == [
            ("tenant_id", "tenant_id"),
            ("request_feedback_id", "request_feedback_id"),
        ]
        for table in ("request_feedbacks", "request_feedback_events"):
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            assert "DEFERRABLE INITIALLY DEFERRED" in sql
    finally:
        connection.close()

    # 第一次 down 只删除 0039 六个守卫；三表与 0038 原触发器必须原样保留。
    hardening = rollback_last_migration(database_url)
    assert hardening.rolled_back == "0039"
    connection = connect_database(database_url)
    try:
        assert _table_names(connection).issuperset(
            {"request_feedbacks", "request_feedback_events", "model_feedback_statistics"}
        )
        assert not _trigger_names(connection).intersection(PRD049_HARDENING_TRIGGERS)
        assert (
            connection.execute(
                "SELECT status FROM schema_migrations WHERE version='0038'"
            ).fetchone()[0]
            == "applied"
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()

    feedback = rollback_last_migration(database_url)
    assert feedback.rolled_back == "0038"
    connection = connect_database(database_url)
    try:
        assert not _table_names(connection).intersection(
            {"request_feedbacks", "request_feedback_events", "model_feedback_statistics"}
        )
        prerequisite_indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert {
            "uq_users_tenant_user_prd044",
            "uq_models_tenant_model_prd044",
            "uq_virtual_keys_tenant_key_prd045",
        }.issubset(prerequisite_indexes)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()

    # 真实 rolled_back 记录必须能按相同 checksum 重新应用 0038/0039，证明逐版本往返不是单向演示。
    reapplied = apply_migrations(database_url)
    assert reapplied.applied == ["0038", "0039"]
    connection = connect_database(database_url)
    try:
        assert _trigger_names(connection).issuperset(PRD049_HARDENING_TRIGGERS)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_prd049_single_sided_feedback_and_event_cannot_commit(database_url: str) -> None:
    """只插 Feedback 必须在 deferred commit 失败，只插 Event 必须被 graph trigger 拒绝。"""

    metadata = _metadata(database_url)
    connection = connect_database(database_url)
    _insert_legacy_request(connection, metadata, request_id="req_prd049_single")
    intent = normalize_feedback_payload(
        {"request_id": "req_prd049_single", "rating": 5, "accepted": True, "tags": ["accurate"]}
    )
    connection.execute("BEGIN")
    _insert_feedback_without_event(
        connection,
        metadata,
        intent=intent,
        feedback_id="rfb_" + "1" * 32,
        event_id="fbe_" + "1" * 32,
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        connection.commit()
    connection.rollback()
    assert connection.execute("SELECT COUNT(*) FROM request_feedbacks").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM model_feedback_statistics").fetchone()[0] == 0

    payload = _event_payload(
        feedback_id="rfb_" + "2" * 32,
        request_id="req_prd049_single",
        model_id=metadata["model_id"],
    )
    # SQLite 对同表多个 BEFORE trigger 不承诺执行顺序，两道防线任一道拒绝都满足单边事实约束。
    with pytest.raises(
        sqlite3.IntegrityError,
        match="request_feedback_event_(graph|payload)_invalid",
    ):
        connection.execute(
            """
            INSERT INTO request_feedback_events(
              feedback_event_id, tenant_id, request_feedback_id, source_request_id,
              event_type, actor_type, actor_id, operation_request_id, trace_id,
              payload_json, created_at
            )
            VALUES(?, ?, ?, ?, 'feedback.created', 'session_user', ?,
                   'op_prd049_event_only', 'trace_prd049_event_only', ?, ?)
            """,
            (
                "fbe_" + "2" * 32,
                metadata["tenant_id"],
                "rfb_" + "2" * 32,
                "req_prd049_single",
                metadata["user_id"],
                json.dumps(payload, separators=(",", ":")),
                SOURCE_TIME,
            ),
        )
    connection.close()


def test_prd049_second_same_model_feedback_updates_aggregate_and_summary(
    database_url: str,
) -> None:
    """Session 与 Virtual Key 对两个同模型调用反馈后，聚合只更新不重复插行且报表口径正确。"""

    metadata = _metadata(database_url)
    connection = connect_database(database_url)
    _insert_legacy_request(connection, metadata, request_id="req_prd049_admin")
    _insert_legacy_request(
        connection,
        metadata,
        request_id="req_prd049_key",
        virtual_key_id=metadata["virtual_key_id"],
    )
    connection.close()

    first = create_request_feedback(
        database_url,
        actor=FeedbackActor(
            tenant_id=metadata["tenant_id"],
            actor_type="session_user",
            actor_user_id=metadata["user_id"],
        ),
        operation_request_id="op_prd049_admin",
        trace_id="trace_prd049_admin",
        intent=normalize_feedback_payload(
            {
                "request_id": "req_prd049_admin",
                "rating": 5,
                "accepted": True,
                "tags": ["helpful", "accurate"],
            }
        ),
        now=FEEDBACK_TIME,
    )
    second = create_request_feedback(
        database_url,
        actor=FeedbackActor(
            tenant_id=metadata["tenant_id"],
            actor_type="virtual_key",
            actor_virtual_key_id=metadata["virtual_key_id"],
        ),
        operation_request_id="op_prd049_key",
        trace_id="trace_prd049_key",
        intent=normalize_feedback_payload(
            {
                "request_id": "req_prd049_key",
                "rating": 3,
                "accepted": False,
                "tags": ["relevant"],
            }
        ),
        now=datetime(2026, 7, 20, 3, 2, tzinfo=UTC),
    )
    assert first.idempotent_replay is False
    assert second.idempotent_replay is False

    connection = connect_database(database_url)
    statistic = connection.execute(
        """
        SELECT feedback_count, rating_sum, rating_3_count, rating_5_count,
               accepted_count, feedback_statistics_version
        FROM model_feedback_statistics WHERE tenant_id=? AND model_id=?
        """,
        (metadata["tenant_id"], metadata["model_id"]),
    ).fetchone()
    assert tuple(statistic) == (2, 8, 1, 1, 1, 2)
    assert connection.execute("SELECT COUNT(*) FROM request_feedback_events").fetchone()[0] == 2
    connection.close()

    summary = query_feedback_summary(
        database_url,
        tenant_id=metadata["tenant_id"],
        from_time=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        to_time=datetime(2026, 7, 20, 4, 0, tzinfo=UTC),
        limit=20,
        offset=0,
    )
    assert summary.summary.feedback_count == 2
    assert summary.summary.rating_sum == 8
    assert summary.summary.average_rating_milli == 4_000
    assert summary.summary.adoption_rate_bps == 5_000
    assert [(item.tag, item.count) for item in summary.summary.tag_counts] == [
        ("accurate", 1),
        ("relevant", 1),
        ("helpful", 1),
    ]
    assert summary.total == 1
    assert len(summary.models) == 1


def test_prd049_summary_isolates_tenant_and_keeps_full_window_totals_when_paginated(
    database_url: str,
) -> None:
    """两个租户和两个模型不会串数据，分页模型行不改变全窗 total、标签和 half-up 权威值。"""

    metadata = _metadata(database_url)
    connection = connect_database(database_url)
    second_model = _insert_additional_model(connection, metadata)
    other = _insert_other_tenant_feedback_metadata(connection)
    _insert_legacy_request(connection, metadata, request_id="req_summary_model_a_1")
    _insert_legacy_request(connection, metadata, request_id="req_summary_model_a_2")
    _insert_legacy_request(connection, second_model, request_id="req_summary_model_b")
    _insert_legacy_request(connection, other, request_id="req_summary_other_tenant")
    connection.close()

    cases = (
        (metadata, "req_summary_model_a_1", "op_summary_a1", 5, True, ["accurate"], 1),
        (metadata, "req_summary_model_a_2", "op_summary_a2", 4, True, ["helpful"], 2),
        (second_model, "req_summary_model_b", "op_summary_b", 1, False, ["inaccurate"], 3),
        (other, "req_summary_other_tenant", "op_summary_other", 2, False, ["slow"], 4),
    )
    for scope, request_id, operation_id, rating, accepted, tags, minute in cases:
        create_request_feedback(
            database_url,
            actor=FeedbackActor(
                tenant_id=scope["tenant_id"],
                actor_type="session_user",
                actor_user_id=scope["user_id"],
            ),
            operation_request_id=operation_id,
            trace_id=f"trace_{operation_id}",
            intent=normalize_feedback_payload(
                {
                    "request_id": request_id,
                    "rating": rating,
                    "accepted": accepted,
                    "tags": tags,
                }
            ),
            now=datetime(2026, 7, 20, 4, minute, tzinfo=UTC),
        )

    paged = query_feedback_summary(
        database_url,
        tenant_id=metadata["tenant_id"],
        from_time=datetime(2026, 7, 20, 4, 0, tzinfo=UTC),
        to_time=datetime(2026, 7, 20, 5, 0, tzinfo=UTC),
        limit=1,
        offset=1,
    )
    assert paged.total == 2
    assert len(paged.models) == 1
    assert paged.models[0].model_id == second_model["model_id"]
    assert paged.summary.feedback_count == 3
    assert paged.summary.rating_sum == 10
    assert paged.summary.average_rating_milli == 3_333
    assert paged.summary.adoption_rate_bps == 6_667
    assert [(item.tag, item.count) for item in paged.summary.tag_counts] == [
        ("accurate", 1),
        ("helpful", 1),
        ("inaccurate", 1),
    ]

    filtered = query_feedback_summary(
        database_url,
        tenant_id=metadata["tenant_id"],
        from_time=datetime(2026, 7, 20, 4, 0, tzinfo=UTC),
        to_time=datetime(2026, 7, 20, 5, 0, tzinfo=UTC),
        model_id=second_model["model_id"],
    )
    assert filtered.total == 1
    assert filtered.summary.feedback_count == 1
    assert filtered.models[0].model_id == second_model["model_id"]

    other_summary = query_feedback_summary(
        database_url,
        tenant_id=other["tenant_id"],
        from_time=datetime(2026, 7, 20, 4, 0, tzinfo=UTC),
        to_time=datetime(2026, 7, 20, 5, 0, tzinfo=UTC),
    )
    assert other_summary.summary.feedback_count == 1
    assert other_summary.summary.rating_sum == 2
    assert all(item.model_id != metadata["model_id"] for item in other_summary.models)


def test_prd049_summary_uses_one_query_only_snapshot_and_maps_deadline_failures(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总计后并发提交不得污染模型/tag 分段；query_only、deadline 与清理失败统一可验证。"""

    metadata = _metadata(database_url)
    connection = connect_database(database_url)
    # WAL 允许另连接在读事务存续期间提交，才能真实验证同一 snapshot 而不是写锁串行。
    connection.execute("PRAGMA journal_mode=WAL").fetchone()
    _insert_legacy_request(connection, metadata, request_id="req_snapshot_existing")
    _insert_legacy_request(connection, metadata, request_id="req_snapshot_concurrent")
    connection.close()
    _record_session_feedback(
        database_url,
        metadata,
        request_id="req_snapshot_existing",
        operation_id="op_snapshot_existing",
    )

    real_connect = feedback_storage.connect_database
    tracked: _TrackingSummaryConnection | None = None

    def submit_concurrent_feedback() -> None:
        """在 Summary 已取总计后用另一真实连接提交第二条同模型反馈。"""

        _record_session_feedback(
            database_url,
            metadata,
            request_id="req_snapshot_concurrent",
            operation_id="op_snapshot_concurrent",
        )

    def connect_with_tracking(url: str | None):
        """只包装 Summary 的首个连接；并发 Feedback 继续使用原始连接器。"""

        nonlocal tracked
        connection_instance = real_connect(url)
        if tracked is None:
            tracked = _TrackingSummaryConnection(
                connection_instance,
                after_total=submit_concurrent_feedback,
            )
            return tracked
        return connection_instance

    monkeypatch.setattr(feedback_storage, "connect_database", connect_with_tracking)
    summary = query_feedback_summary(
        database_url,
        tenant_id=metadata["tenant_id"],
        from_time=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        to_time=datetime(2026, 7, 20, 4, 0, tzinfo=UTC),
    )
    assert summary.summary.feedback_count == 1
    assert summary.models[0].feedback_count == 1
    assert sum(item.count for item in summary.summary.tag_counts) == 1
    assert tracked is not None
    assert tracked.closed is True
    assert tracked.progress_handlers[-1] is None
    query_only_index = tracked.statements.index("PRAGMA query_only=ON")
    begin_index = tracked.statements.index("BEGIN")
    assert query_only_index < begin_index

    # 并发提交已完成，但只能在下一次新 snapshot 中观察到第二条事实。
    monkeypatch.setattr(feedback_storage, "connect_database", real_connect)
    latest = query_feedback_summary(
        database_url,
        tenant_id=metadata["tenant_id"],
        from_time=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
        to_time=datetime(2026, 7, 20, 4, 0, tzinfo=UTC),
    )
    assert latest.summary.feedback_count == 2

    for invalid_deadline in (float("nan"), float("inf"), float("-inf"), 0.0):
        with pytest.raises(ApiError) as captured:
            query_feedback_summary(
                database_url,
                tenant_id=metadata["tenant_id"],
                from_time=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
                to_time=datetime(2026, 7, 20, 4, 0, tzinfo=UTC),
                deadline_seconds=invalid_deadline,
            )
        assert captured.value.code == "feedback.query_invalid"

    interrupted = _TrackingSummaryConnection(real_connect(database_url), interrupt_total=True)
    monkeypatch.setattr(feedback_storage, "connect_database", lambda _url: interrupted)
    with pytest.raises(ApiError) as captured:
        query_feedback_summary(
            database_url,
            tenant_id=metadata["tenant_id"],
            from_time=datetime(2026, 7, 20, 3, 0, tzinfo=UTC),
            to_time=datetime(2026, 7, 20, 4, 0, tzinfo=UTC),
        )
    assert captured.value.code == "feedback.unavailable"
    assert interrupted.closed is True
    assert interrupted.progress_handlers[-1] is None


def test_prd049_aggregate_feedback_event_and_parent_facts_are_protected(
    database_url: str,
) -> None:
    """聚合/Feedback/Event 与父 RequestLog/final Attempt 归因不可漂移，Token 和延迟补账仍允许。"""

    metadata = _metadata(database_url)
    connection = connect_database(database_url)
    _insert_legacy_request(connection, metadata, request_id="req_prd049_parent")
    connection.close()
    result = _record_session_feedback(
        database_url,
        metadata,
        request_id="req_prd049_parent",
        operation_id="op_prd049_parent",
    )

    connection = connect_database(database_url)
    protected_operations = (
        (
            "DELETE FROM model_feedback_statistics WHERE tenant_id=? AND model_id=?",
            (metadata["tenant_id"], metadata["model_id"]),
            "model_feedback_statistics_immutable",
        ),
        (
            "UPDATE request_feedbacks SET rating=4 WHERE tenant_id=? AND request_feedback_id=?",
            (metadata["tenant_id"], result.feedback.request_feedback_id),
            "request_feedback_immutable",
        ),
        (
            "DELETE FROM request_feedback_events WHERE tenant_id=? AND request_feedback_id=?",
            (metadata["tenant_id"], result.feedback.request_feedback_id),
            "request_feedback_event_immutable",
        ),
        (
            "UPDATE request_logs SET model_id='model_drift' WHERE tenant_id=? AND request_id=?",
            (metadata["tenant_id"], "req_prd049_parent"),
            "request_log_feedback_parent_immutable",
        ),
    )
    for sql, params, message in protected_operations:
        with pytest.raises(sqlite3.IntegrityError, match=message):
            connection.execute(sql, params)
        connection.rollback()

    # Token/Usage 补账不改变反馈归因，应在父侧 trigger 存在时继续成功。
    connection.execute(
        """
        UPDATE request_logs
        SET input_tokens=7, output_tokens=3, total_tokens=10, updated_at=?
        WHERE tenant_id=? AND request_id=?
        """,
        ("2026-07-20T03:03:00Z", metadata["tenant_id"], "req_prd049_parent"),
    )
    connection.commit()

    _insert_final_attempt_request(connection, metadata, request_id="req_prd049_attempt")
    connection.close()
    _record_session_feedback(
        database_url,
        metadata,
        request_id="req_prd049_attempt",
        operation_id="op_prd049_attempt",
    )
    connection = connect_database(database_url)
    with pytest.raises(sqlite3.IntegrityError, match="routing_attempt_feedback_parent_immutable"):
        connection.execute(
            "UPDATE routing_attempts SET status='failed' WHERE routing_attempt_id=?",
            ("rta_prd049_attempt",),
        )
    connection.rollback()
    # latency/updated_at 不改变最终模型归因，后续结算可继续写入。
    connection.execute(
        "UPDATE routing_attempts SET latency_ms=25, updated_at=? WHERE routing_attempt_id=?",
        ("2026-07-20T03:04:00Z", "rta_prd049_attempt"),
    )
    connection.commit()
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_prd049_aggregate_rejects_ghost_wrong_update_replace_and_delete(
    database_url: str,
) -> None:
    """直接 SQL 不能制造 ghost Aggregate，也不能错计、REPLACE 或删除已形成的统计。"""

    metadata = _metadata(database_url)
    connection = connect_database(database_url)
    with pytest.raises(sqlite3.IntegrityError, match="model_feedback_statistics_invalid"):
        connection.execute(
            """
            INSERT INTO model_feedback_statistics(
              tenant_id, model_id, feedback_count, rating_sum,
              rating_1_count, rating_2_count, rating_3_count, rating_4_count, rating_5_count,
              accepted_count, feedback_statistics_version, updated_at
            ) VALUES(?, ?, 1, 5, 0, 0, 0, 0, 1, 1, 1, ?)
            """,
            (metadata["tenant_id"], metadata["model_id"], SOURCE_TIME),
        )
    connection.rollback()
    _insert_legacy_request(connection, metadata, request_id="req_prd049_aggregate")
    connection.close()
    _record_session_feedback(
        database_url,
        metadata,
        request_id="req_prd049_aggregate",
        operation_id="op_prd049_aggregate",
    )
    connection = connect_database(database_url)
    with pytest.raises(sqlite3.IntegrityError, match="model_feedback_statistics_invalid"):
        connection.execute(
            """
            UPDATE model_feedback_statistics SET rating_sum=rating_sum+1
            WHERE tenant_id=? AND model_id=?
            """,
            (metadata["tenant_id"], metadata["model_id"]),
        )
    connection.rollback()
    row = connection.execute(
        "SELECT * FROM model_feedback_statistics WHERE tenant_id=? AND model_id=?",
        (metadata["tenant_id"], metadata["model_id"]),
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="model_feedback_statistics_invalid"):
        connection.execute(
            """
            INSERT OR REPLACE INTO model_feedback_statistics(
              tenant_id, model_id, feedback_count, rating_sum,
              rating_1_count, rating_2_count, rating_3_count, rating_4_count, rating_5_count,
              accepted_count, feedback_statistics_version, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(row),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="model_feedback_statistics_immutable"):
        connection.execute(
            "DELETE FROM model_feedback_statistics WHERE tenant_id=? AND model_id=?",
            (metadata["tenant_id"], metadata["model_id"]),
        )
    connection.close()


def test_prd049_hardening_trigger_predicates_are_complete(database_url: str) -> None:
    """六个 0039 trigger 必须锁定全部唯一目标、十一项 RequestLog 快照和 rowid 自排除。"""

    connection = connect_database(database_url)
    try:
        sql_by_name = {name: _trigger_sql(connection, name) for name in PRD049_HARDENING_TRIGGERS}
    finally:
        connection.close()

    feedback_sql = sql_by_name["trg_request_feedback_conflict_insert_prd049"]
    for predicate in (
        "current.request_feedback_id=NEW.request_feedback_id",
        "current.feedback_event_id=NEW.feedback_event_id",
        "current.source_request_id=NEW.source_request_id",
        "current.actor_user_id=NEW.actor_user_id",
        "current.actor_virtual_key_id=NEW.actor_virtual_key_id",
    ):
        assert predicate in feedback_sql
    event_sql = sql_by_name["trg_request_feedback_event_conflict_insert_prd049"]
    assert "current.feedback_event_id=NEW.feedback_event_id" in event_sql
    assert "current.request_feedback_id=NEW.request_feedback_id" in event_sql

    request_insert_sql = sql_by_name["trg_request_log_feedback_parent_conflict_insert_prd049"]
    for column in (
        "virtual_key_id",
        "status",
        "model_id",
        "provider_id",
        "project_id",
        "application_id",
        "environment_id",
        "stream_session_id",
        "stream_status",
        "final_routing_attempt_id",
        "routing_attempt_count",
    ):
        assert f"current.{column} IS NOT NEW.{column}" in request_insert_sql
    request_update_sql = sql_by_name["trg_request_log_feedback_parent_conflict_update_prd049"]
    assert "target.rowid != OLD.rowid" in request_update_sql
    assert "target.tenant_id=NEW.tenant_id" in request_update_sql
    assert "target.request_id=NEW.request_id" in request_update_sql

    attempt_insert_sql = sql_by_name["trg_routing_attempt_feedback_parent_conflict_insert_prd049"]
    attempt_update_sql = sql_by_name["trg_routing_attempt_feedback_parent_conflict_update_prd049"]
    for sql in (attempt_insert_sql, attempt_update_sql):
        assert "target.routing_attempt_id=NEW.routing_attempt_id" in sql
        assert "target.attempt_sequence=NEW.attempt_sequence" in sql
        assert "request.final_routing_attempt_id=target.routing_attempt_id" in sql
    assert "target.rowid != OLD.rowid" in attempt_update_sql


@pytest.mark.parametrize("branch", PRD049_CONFLICT_BRANCHES)
def test_prd049_hardening_rejects_all_thirteen_replace_conflicts_atomically(
    database_url: str,
    branch: str,
) -> None:
    """十三条 REPLACE 分支在默认/递归 trigger 语义下都拒绝，原事实和父记录逐字段不变。"""

    context = _prepare_conflict_branch(database_url, branch=branch)
    connection = connect_database(database_url)
    try:
        before = _protection_snapshot(connection)
        expected_error = _conflict_error(branch)
        for recursive_triggers in (0, 1):
            connection.execute(f"PRAGMA recursive_triggers={recursive_triggers}")
            assert (
                connection.execute("PRAGMA recursive_triggers").fetchone()[0] == recursive_triggers
            )
            with pytest.raises(sqlite3.IntegrityError, match=expected_error):
                _execute_conflict_attack(connection, branch=branch, context=context)
            connection.rollback()
            assert _protection_snapshot(connection) == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_prd049_hardening_keeps_production_upsert_and_non_attribution_updates(
    database_url: str,
) -> None:
    """保护字段相同的 RequestLog upsert、Usage/成本补账和 Attempt 自身非归因更新必须继续成功。"""

    metadata = _metadata(database_url)
    connection = connect_database(database_url)
    _insert_legacy_request(connection, metadata, request_id="req_prd049_compat_log")
    connection.close()
    _record_session_feedback(
        database_url,
        metadata,
        request_id="req_prd049_compat_log",
        operation_id="op_prd049_compat_log",
    )

    connection = connect_database(database_url)
    request_row = _row_dict(
        connection,
        "request_logs",
        "tenant_id=? AND request_id=?",
        (metadata["tenant_id"], "req_prd049_compat_log"),
    )
    request_row.update(
        {
            "latency_ms": 31,
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
            "estimated_cost_amount": 0.125,
            "updated_at": "2026-07-20T03:05:00Z",
        }
    )
    _upsert_request_log(connection, request_row)
    connection.commit()
    # 显式把复合主键列写回自身会触发新增 UPDATE guard；rowid 排除保证它不是伪冲突。
    connection.execute(
        """
        UPDATE request_logs
        SET request_id=request_id, input_tokens=8, output_tokens=4, total_tokens=12,
            estimated_cost_amount=0.150, updated_at=?
        WHERE tenant_id=? AND request_id=?
        """,
        ("2026-07-20T03:06:00Z", metadata["tenant_id"], "req_prd049_compat_log"),
    )
    connection.commit()
    settled = connection.execute(
        """
        SELECT latency_ms, input_tokens, output_tokens, total_tokens, estimated_cost_amount
        FROM request_logs WHERE tenant_id=? AND request_id=?
        """,
        (metadata["tenant_id"], "req_prd049_compat_log"),
    ).fetchone()
    assert tuple(settled) == (31, 8, 4, 12, 0.15)

    _insert_final_attempt_request(connection, metadata, request_id="req_prd049_compat_attempt")
    connection.close()
    _record_session_feedback(
        database_url,
        metadata,
        request_id="req_prd049_compat_attempt",
        operation_id="op_prd049_compat_attempt",
    )
    connection = connect_database(database_url)
    # 两个唯一键列都出现在 SET 中，但实际目标仍是 OLD 自身；延迟/成本结算不得被加固误伤。
    connection.execute(
        """
        UPDATE routing_attempts
        SET routing_attempt_id=routing_attempt_id,
            attempt_sequence=attempt_sequence,
            latency_ms=41,
            estimated_cost_cny=0.200,
            updated_at=?
        WHERE routing_attempt_id='rta_prd049_attempt'
        """,
        ("2026-07-20T03:07:00Z",),
    )
    connection.commit()
    attempt = connection.execute(
        """
        SELECT latency_ms, estimated_cost_cny FROM routing_attempts
        WHERE routing_attempt_id='rta_prd049_attempt'
        """
    ).fetchone()
    assert tuple(attempt) == (41, 0.2)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


@pytest.mark.parametrize("failure_kind", ["prerequisite", "integrity"])
def test_prd049_hardening_up_failure_is_atomic(
    database_url: str,
    failure_kind: str,
) -> None:
    """0039 up 的前置或 FK 完整性失败都不遗留六个 trigger，0038 结构与故障前状态保持一致。"""

    assert rollback_last_migration(database_url).rolled_back == "0039"
    connection = connect_database(database_url)
    if failure_kind == "prerequisite":
        connection.execute("DROP TRIGGER trg_request_feedback_immutable_delete_prd049")
        expected_error = "prd049_hardening_prerequisite_missing"
    else:
        _insert_orphan_provider(connection)
        expected_error = "prd049_hardening_up_integrity_failed"
    connection.commit()
    before_triggers = _trigger_names(connection)
    connection.close()

    with pytest.raises(DbError, match=expected_error):
        apply_migrations(database_url)
    connection = connect_database(database_url)
    try:
        assert not _trigger_names(connection).intersection(PRD049_HARDENING_TRIGGERS)
        assert _trigger_names(connection) == before_triggers
        assert (
            connection.execute(
                "SELECT status FROM schema_migrations WHERE version='0038'"
            ).fetchone()[0]
            == "applied"
        )
        assert (
            connection.execute(
                "SELECT status FROM schema_migrations WHERE version='0039'"
            ).fetchone()[0]
            == "failed"
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if failure_kind == "integrity":
            assert connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()


@pytest.mark.parametrize("failure_kind", ["missing_trigger", "integrity"])
def test_prd049_hardening_down_failure_restores_pretransaction_state(
    database_url: str,
    failure_kind: str,
) -> None:
    """0039 down 失败会回滚已执行 DDL。

    migration 状态与故障前 trigger 集合也必须保持不变。
    """

    connection = connect_database(database_url)
    if failure_kind == "missing_trigger":
        # 删除 down 顺序中的最后一项，让前五次 DROP 真实执行后再失败，证明 DDL 事务会完整恢复。
        connection.execute("DROP TRIGGER trg_request_feedback_conflict_insert_prd049")
        expected_error = "no such trigger"
    else:
        _insert_orphan_provider(connection)
        expected_error = "prd049_hardening_down_integrity_failed"
    connection.commit()
    before_triggers = _trigger_names(connection)
    connection.close()

    with pytest.raises(DbError, match=expected_error):
        rollback_last_migration(database_url)
    connection = connect_database(database_url)
    try:
        assert _trigger_names(connection) == before_triggers
        assert (
            connection.execute(
                "SELECT status FROM schema_migrations WHERE version='0039'"
            ).fetchone()[0]
            == "applied"
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if failure_kind == "integrity":
            assert PRD049_HARDENING_TRIGGERS.issubset(_trigger_names(connection))
            assert connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()


def test_prd049_migration_entrypoints_close_idempotent_and_guard_failure_connections(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0039 幂等 apply 与有事实 down guard 抛错后都关闭迁移入口创建的真实连接。"""

    metadata = _metadata(database_url)
    connection = connect_database(database_url)
    _insert_legacy_request(connection, metadata, request_id="req_prd049_close_guard")
    connection.close()
    _record_session_feedback(
        database_url,
        metadata,
        request_id="req_prd049_close_guard",
        operation_id="op_prd049_close_guard",
    )
    opened_connections: list[sqlite3.Connection] = []
    real_connect = migration_module.connect_database

    def tracked_connect(database_url_value: str | None) -> sqlite3.Connection:
        """登记迁移器独占连接，调用结束后通过真实 SQL 失败确认底层句柄已经关闭。"""

        tracked = real_connect(database_url_value)
        opened_connections.append(tracked)
        return tracked

    monkeypatch.setattr(migration_module, "connect_database", tracked_connect)
    assert apply_migrations(database_url).skipped[-1] == "0039"
    with pytest.raises(DbError, match="prd049_down_business_facts_present"):
        rollback_last_migration(database_url)

    assert len(opened_connections) == 2
    for tracked in opened_connections:
        with pytest.raises(sqlite3.ProgrammingError):
            tracked.execute("SELECT 1")


@pytest.mark.parametrize(
    "fact_kind",
    ["feedback", "event", "aggregate", "subscription", "delivery", "attempt"],
)
def test_prd049_down_guard_preserves_each_business_fact(
    database_url: str,
    fact_kind: str,
) -> None:
    """六类事实任一存在时第一次 0039 down 即失败，两个版本、六项防线和数据保持原子完整。"""

    metadata = _metadata(database_url)
    _inject_down_guard_fact(database_url, metadata, fact_kind=fact_kind)
    connection = connect_database(database_url)
    before = _protection_snapshot(connection)
    before_foreign_keys = [
        tuple(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()
    ]
    connection.close()
    with pytest.raises(DbError, match="prd049_down_business_facts_present"):
        rollback_last_migration(database_url)
    connection = connect_database(database_url)
    try:
        statuses = {
            row["version"]: row["status"]
            for row in connection.execute(
                "SELECT version, status FROM schema_migrations WHERE version IN ('0038', '0039')"
            ).fetchall()
        }
        assert statuses == {"0038": "applied", "0039": "applied"}
        assert PRD049_HARDENING_TRIGGERS.issubset(_trigger_names(connection))
        assert _protection_snapshot(connection) == before
        assert "request_feedbacks" in _table_names(connection)
        assert [
            tuple(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        ] == before_foreign_keys
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def _sqlite_url(path: Path) -> str:
    """把 Windows 临时数据库路径转换为仓库连接器支持的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _table_names(connection: sqlite3.Connection) -> set[str]:
    """读取当前 SQLite 表集合，用于验证正反迁移结构边界。"""

    return {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _foreign_key_columns(
    connection: sqlite3.Connection,
    *,
    table: str,
    parent: str,
) -> list[tuple[str, str]]:
    """按 seq 恢复指定父表的复合 FK 列顺序，防止 tenant 与对象列接反。"""

    rows = [
        row
        for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        if row["table"] == parent
    ]
    return [(row["from"], row["to"]) for row in sorted(rows, key=lambda item: item["seq"])]


def _trigger_names(connection: sqlite3.Connection) -> set[str]:
    """读取持久 trigger 名称，证明迁移版本之外的真实数据库防线状态。"""

    return {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }


def _trigger_sql(connection: sqlite3.Connection, name: str) -> str:
    """读取指定持久 trigger 的原始 SQL，供唯一冲突 predicate 精确内省。"""

    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (name,),
    ).fetchone()
    assert row is not None
    return str(row["sql"])


def _row_dict(
    connection: sqlite3.Connection,
    table: str,
    where: str,
    parameters: tuple[object, ...],
) -> dict[str, object]:
    """读取测试已知表的一行全字段副本；表名和条件只由本文件固定常量提供。"""

    row = connection.execute(
        f"SELECT * FROM {table} WHERE {where}",
        parameters,
    ).fetchone()
    assert row is not None
    return {str(column): row[column] for column in row.keys()}


def _insert_row(
    connection: sqlite3.Connection,
    table: str,
    values: dict[str, object],
    *,
    or_replace: bool = False,
) -> None:
    """按真实全字段快照写入测试行，可选 REPLACE 仅用于复现唯一冲突攻击。"""

    columns = tuple(values)
    verb = "INSERT OR REPLACE" if or_replace else "INSERT"
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"{verb} INTO {table}({', '.join(columns)}) VALUES({placeholders})",
        tuple(values[column] for column in columns),
    )


def _upsert_request_log(
    connection: sqlite3.Connection,
    values: dict[str, object],
) -> None:
    """模拟 Gateway 生产 RequestLog upsert，仅更新允许变化的延迟、Usage、成本与时间字段。"""

    columns = tuple(values)
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"""
        INSERT INTO request_logs({", ".join(columns)}) VALUES({placeholders})
        ON CONFLICT(tenant_id, request_id) DO UPDATE SET
          virtual_key_id=excluded.virtual_key_id,
          status=excluded.status,
          model_id=excluded.model_id,
          provider_id=excluded.provider_id,
          project_id=excluded.project_id,
          application_id=excluded.application_id,
          environment_id=excluded.environment_id,
          stream_session_id=excluded.stream_session_id,
          stream_status=excluded.stream_status,
          final_routing_attempt_id=excluded.final_routing_attempt_id,
          routing_attempt_count=excluded.routing_attempt_count,
          latency_ms=excluded.latency_ms,
          input_tokens=excluded.input_tokens,
          output_tokens=excluded.output_tokens,
          total_tokens=excluded.total_tokens,
          estimated_cost_amount=excluded.estimated_cost_amount,
          updated_at=excluded.updated_at
        """,
        tuple(values[column] for column in columns),
    )


def _protection_snapshot(connection: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    """逐字段快照 Feedback 图、父事实和 Webhook 六类事实，验证失败事务没有隐式删除或改写。"""

    tables = (
        "request_feedbacks",
        "request_feedback_events",
        "model_feedback_statistics",
        "request_logs",
        "routing_attempts",
        "webhook_endpoints",
        "webhook_deliveries",
        "webhook_delivery_attempts",
    )
    return {
        table: [
            tuple(row)
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        ]
        for table in tables
    }


def _conflict_error(branch: str) -> str:
    """把十三个固定攻击分支映射到 0038 已公开的不可变数据库错误。"""

    if branch.startswith("feedback_"):
        return "request_feedback_immutable"
    if branch.startswith("event_"):
        return "request_feedback_event_immutable"
    if branch.startswith("request_log_"):
        return "request_log_feedback_parent_immutable"
    return "routing_attempt_feedback_parent_immutable"


def _prepare_conflict_branch(
    database_url: str,
    *,
    branch: str,
) -> dict[str, object]:
    """为单一冲突分支创建最小目标图和未受保护源行，避免多唯一键同时命中掩盖 predicate。"""

    metadata = _metadata(database_url)
    target_request_id = (
        "req_prd049_conflict_attempt_target"
        if branch.startswith("attempt_")
        else "req_prd049_conflict_target"
    )
    connection = connect_database(database_url)
    if branch.startswith("attempt_"):
        _insert_final_attempt_request(connection, metadata, request_id=target_request_id)
    else:
        _insert_legacy_request(
            connection,
            metadata,
            request_id=target_request_id,
            virtual_key_id=(
                metadata["virtual_key_id"] if branch == "feedback_key_operation" else None
            ),
        )
    connection.close()

    if branch == "feedback_key_operation":
        _record_key_feedback(
            database_url,
            metadata,
            request_id=target_request_id,
            operation_id="op_prd049_conflict_target",
        )
    else:
        _record_session_feedback(
            database_url,
            metadata,
            request_id=target_request_id,
            operation_id="op_prd049_conflict_target",
        )

    connection = connect_database(database_url)
    cross_tenant_branches = {
        "feedback_global_id",
        "feedback_event_id",
        "event_global_id",
        "attempt_insert_global_id",
        "attempt_update_global_id",
    }
    candidate_metadata = (
        _insert_other_tenant_feedback_metadata(connection)
        if branch in cross_tenant_branches
        else metadata
    )
    if branch.startswith("feedback_"):
        _insert_legacy_request(
            connection,
            candidate_metadata,
            request_id="req_prd049_conflict_candidate",
            virtual_key_id=(
                metadata["virtual_key_id"] if branch == "feedback_key_operation" else None
            ),
        )
    if branch == "request_log_update":
        _insert_legacy_request(
            connection,
            metadata,
            request_id="req_prd049_conflict_source",
        )
    if branch in {"attempt_update_global_id", "attempt_update_sequence"}:
        _insert_unreferenced_attempt(
            connection,
            candidate_metadata,
            routing_attempt_id="rta_prd049_conflict_source",
            request_id="req_prd049_conflict_attempt_source",
        )
    if branch.startswith("event_"):
        # 循环一对一图无法单独制造某一个 Event 唯一冲突；移除旧验证 trigger 后由 0039 守卫独立受测，
        # 另一个测试仍以内省锁定两个 predicate，避免依赖同表多个 BEFORE trigger 的执行顺序。
        connection.execute("DROP TRIGGER trg_request_feedback_event_graph_insert_prd049")
        connection.execute("DROP TRIGGER trg_request_feedback_event_payload_insert_prd049")
        connection.commit()

    target_feedback = _row_dict(
        connection,
        "request_feedbacks",
        "tenant_id=? AND source_request_id=?",
        (metadata["tenant_id"], target_request_id),
    )
    target_event = _row_dict(
        connection,
        "request_feedback_events",
        "tenant_id=? AND request_feedback_id=?",
        (metadata["tenant_id"], target_feedback["request_feedback_id"]),
    )
    target_request = _row_dict(
        connection,
        "request_logs",
        "tenant_id=? AND request_id=?",
        (metadata["tenant_id"], target_request_id),
    )
    target_attempt = None
    if branch.startswith("attempt_"):
        target_attempt = _row_dict(
            connection,
            "routing_attempts",
            "routing_attempt_id=?",
            ("rta_prd049_attempt",),
        )
    context: dict[str, object] = {
        "metadata": metadata,
        "candidate_metadata": candidate_metadata,
        "target_request_id": target_request_id,
        "target_feedback": target_feedback,
        "target_event": target_event,
        "target_request": target_request,
        "target_attempt": target_attempt,
    }
    connection.close()
    return context


def _execute_conflict_attack(
    connection: sqlite3.Connection,
    *,
    branch: str,
    context: dict[str, object],
) -> None:
    """执行一个且仅一个候选唯一冲突，让固定错误能准确归因到对应 0039 predicate。"""

    metadata = context["metadata"]
    candidate_metadata = context["candidate_metadata"]
    assert isinstance(metadata, dict)
    assert isinstance(candidate_metadata, dict)

    if branch.startswith("feedback_"):
        target = dict(context["target_feedback"])
        candidate = dict(target)
        candidate.update(
            {
                "request_feedback_id": "rfb_" + "c" * 32,
                "tenant_id": candidate_metadata["tenant_id"],
                "source_request_id": "req_prd049_conflict_candidate",
                "model_id": candidate_metadata["model_id"],
                "provider_id": candidate_metadata["provider_id"],
                "virtual_key_id": None,
                "project_id": None,
                "application_id": None,
                "environment_id": None,
                "actor_type": "session_user",
                "actor_user_id": candidate_metadata["user_id"],
                "actor_virtual_key_id": None,
                "operation_request_id": "op_prd049_conflict_candidate",
                "request_fingerprint": "c" * 64,
                "feedback_event_id": "fbe_" + "c" * 32,
                "trace_id": "trace_prd049_conflict_candidate",
            }
        )
        if branch == "feedback_global_id":
            candidate["request_feedback_id"] = target["request_feedback_id"]
        elif branch == "feedback_event_id":
            candidate["feedback_event_id"] = target["feedback_event_id"]
        elif branch == "feedback_source":
            candidate["source_request_id"] = target["source_request_id"]
        # Session 幂等分支只复用同租户同用户的 operation，其他候选键保持全新以隔离 partial unique。
        elif branch == "feedback_session_operation":
            candidate["operation_request_id"] = target["operation_request_id"]
        else:
            candidate.update(
                {
                    "virtual_key_id": metadata["virtual_key_id"],
                    "actor_type": "virtual_key",
                    "actor_user_id": None,
                    "actor_virtual_key_id": metadata["virtual_key_id"],
                    "operation_request_id": target["operation_request_id"],
                }
            )
        _insert_row(connection, "request_feedbacks", candidate, or_replace=True)
        return

    if branch.startswith("event_"):
        target = dict(context["target_event"])
        candidate = dict(target)
        candidate.update(
            {
                "feedback_event_id": "fbe_" + "d" * 32,
                "tenant_id": candidate_metadata["tenant_id"],
                "request_feedback_id": "rfb_" + "d" * 32,
                "source_request_id": "req_prd049_conflict_event_candidate",
                "actor_id": candidate_metadata["user_id"],
                "operation_request_id": "op_prd049_conflict_event_candidate",
                "trace_id": "trace_prd049_conflict_event_candidate",
            }
        )
        if branch == "event_global_id":
            candidate["feedback_event_id"] = target["feedback_event_id"]
        else:
            candidate["tenant_id"] = target["tenant_id"]
            candidate["request_feedback_id"] = target["request_feedback_id"]
        _insert_row(connection, "request_feedback_events", candidate, or_replace=True)
        return

    if branch == "request_log_insert":
        candidate = dict(context["target_request"])
        candidate["status"] = "failed"
        candidate["error_code"] = "test.safe_failure"
        _insert_row(connection, "request_logs", candidate, or_replace=True)
        return

    if branch == "request_log_update":
        target = dict(context["target_request"])
        source = connection.execute(
            """
            SELECT rowid FROM request_logs
            WHERE tenant_id=? AND request_id='req_prd049_conflict_source'
            """,
            (metadata["tenant_id"],),
        ).fetchone()
        assert source is not None
        assignments = ", ".join(f"{column}=?" for column in target)
        connection.execute(
            f"UPDATE OR REPLACE request_logs SET {assignments} WHERE rowid=?",
            (*target.values(), source["rowid"]),
        )
        return

    target_attempt = context["target_attempt"]
    assert isinstance(target_attempt, dict)
    if branch.startswith("attempt_insert_"):
        candidate = dict(target_attempt)
        if branch == "attempt_insert_global_id":
            candidate.update(
                {
                    "tenant_id": candidate_metadata["tenant_id"],
                    "request_id": "req_prd049_conflict_attempt_candidate",
                    "trace_id": "trace_prd049_conflict_attempt_candidate",
                    "attempt_sequence": 2,
                    "model_id": candidate_metadata["model_id"],
                    "internal_model_id": candidate_metadata["model_alias"],
                    "provider_id": candidate_metadata["provider_id"],
                    "provider_code": candidate_metadata["provider_code"],
                }
            )
        else:
            candidate["routing_attempt_id"] = "rta_prd049_conflict_candidate"
        _insert_row(connection, "routing_attempts", candidate, or_replace=True)
        return

    if branch == "attempt_update_global_id":
        connection.execute(
            """
            UPDATE OR REPLACE routing_attempts
            SET routing_attempt_id=?
            WHERE routing_attempt_id='rta_prd049_conflict_source'
            """,
            (target_attempt["routing_attempt_id"],),
        )
        return

    connection.execute(
        """
        UPDATE OR REPLACE routing_attempts
        SET tenant_id=?, request_id=?, attempt_sequence=?
        WHERE routing_attempt_id='rta_prd049_conflict_source'
        """,
        (
            target_attempt["tenant_id"],
            target_attempt["request_id"],
            target_attempt["attempt_sequence"],
        ),
    )


def _insert_unreferenced_attempt(
    connection: sqlite3.Connection,
    metadata: dict[str, str],
    *,
    routing_attempt_id: str,
    request_id: str,
) -> None:
    """创建没有 Feedback 引用的合法 Attempt，作为 UPDATE OR REPLACE 攻击源行。"""

    connection.execute(
        """
        INSERT INTO routing_attempts(
          routing_attempt_id, tenant_id, request_id, trace_id,
          attempt_sequence, attempt_role, trigger_type,
          model_id, internal_model_id, provider_id, provider_code,
          status, retryable, latency_ms, usage_status,
          created_at, completed_at, updated_at
        ) VALUES(?, ?, ?, ?, 1, 'primary', 'initial', ?, ?, ?, ?,
                 'succeeded', 0, 10, 'not_applicable', ?, ?, ?)
        """,
        (
            routing_attempt_id,
            metadata["tenant_id"],
            request_id,
            f"trace_{request_id}",
            metadata["model_id"],
            metadata["model_alias"],
            metadata["provider_id"],
            metadata["provider_code"],
            SOURCE_TIME,
            "2026-07-20T03:00:01Z",
            "2026-07-20T03:00:01Z",
        ),
    )
    connection.commit()


def _insert_orphan_provider(connection: sqlite3.Connection) -> None:
    """在关闭 FK 的故障注入连接中复制一条 Provider 到不存在租户，供完整性 guard 原子性测试。"""

    provider = _row_dict(connection, "providers", "1=1", ())
    provider.update(
        {
            "provider_id": "provider_prd049_orphan",
            "tenant_id": "tenant_prd049_missing",
            "provider_code": "prd049-orphan",
            "name": "PRD-049 Orphan",
        }
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    _insert_row(connection, "providers", provider)
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")


def _metadata(database_url: str) -> dict[str, str]:
    """按默认 tenant 加载安全 user/Key/model/provider ID，不返回任何 Key 明文。"""

    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT tenant.tenant_id, user.user_id, key.virtual_key_id,
                   model.model_id, model.model_alias, provider.provider_id,
                   provider.provider_code
            FROM tenants tenant
            JOIN users user ON user.tenant_id=tenant.tenant_id
              AND user.role='admin' AND user.status='active'
            JOIN virtual_keys key ON key.tenant_id=tenant.tenant_id AND key.status='active'
            JOIN models model ON model.tenant_id=tenant.tenant_id AND model.status='active'
            JOIN providers provider ON provider.tenant_id=model.tenant_id
              AND provider.provider_id=model.provider_id
            WHERE tenant.status='active'
            ORDER BY tenant.tenant_id, model.model_id
            LIMIT 1
            """
        ).fetchone()
        assert row is not None
        return {key: str(row[key]) for key in row.keys()}
    finally:
        connection.close()


def _insert_additional_model(
    connection: sqlite3.Connection,
    metadata: dict[str, str],
) -> dict[str, str]:
    """在当前 tenant/provider 下创建第二模型，供报表模型筛选与分页测试。"""

    second = dict(metadata)
    second.update(
        {
            "model_id": "model_prd049_summary_second",
            "model_alias": "prd049-summary-second",
        }
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, display_name, capabilities, status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, 'prd049-summary-upstream', 'prd049-summary-upstream',
                 'PRD-049 Summary Second', '{"chat":true}', 'active', ?, ?)
        """,
        (
            second["model_id"],
            second["tenant_id"],
            second["provider_id"],
            second["model_alias"],
            SOURCE_TIME,
            SOURCE_TIME,
        ),
    )
    connection.commit()
    return second


def _insert_other_tenant_feedback_metadata(
    connection: sqlite3.Connection,
) -> dict[str, str]:
    """创建第二租户的最小 user/provider/model 图，证明 Summary 不会跨 tenant 聚合。"""

    metadata = {
        "tenant_id": "tenant_prd049_summary_other",
        "user_id": "user_prd049_summary_other",
        "virtual_key_id": "unused_prd049_summary_other",
        "provider_id": "provider_prd049_summary_other",
        "provider_code": "prd049-summary-other",
        "model_id": "model_prd049_summary_other",
        "model_alias": "prd049-summary-other",
    }
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id, slug, name, service_mode, status, created_at, updated_at
        ) VALUES(?, 'prd049-summary-other', 'PRD-049 Summary Other',
                 'internal_only', 'active', ?, ?)
        """,
        (metadata["tenant_id"], SOURCE_TIME, SOURCE_TIME),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status,
          created_at, updated_at
        ) VALUES(?, ?, 'prd049-summary-other@example.invalid', 'PRD-049 Other Admin',
                 'test-password-hash', 'admin', 'active', ?, ?)
        """,
        (metadata["user_id"], metadata["tenant_id"], SOURCE_TIME, SOURCE_TIME),
    )
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status,
          created_at, updated_at
        ) VALUES(?, ?, ?, 'PRD-049 Other Provider', 'https://example.invalid/provider',
                 'active', ?, ?)
        """,
        (
            metadata["provider_id"],
            metadata["tenant_id"],
            metadata["provider_code"],
            SOURCE_TIME,
            SOURCE_TIME,
        ),
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, display_name, capabilities, status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, 'prd049-other-upstream', 'prd049-other-upstream',
                 'PRD-049 Other Model', '{"chat":true}', 'active', ?, ?)
        """,
        (
            metadata["model_id"],
            metadata["tenant_id"],
            metadata["provider_id"],
            metadata["model_alias"],
            SOURCE_TIME,
            SOURCE_TIME,
        ),
    )
    connection.commit()
    return metadata


def _insert_legacy_request(
    connection: sqlite3.Connection,
    metadata: dict[str, str],
    *,
    request_id: str,
    virtual_key_id: str | None = None,
) -> None:
    """写入零次路由尝试的成功旧式调用日志，并满足既有请求认领触发器约束。"""

    trace_id = f"trace_{request_id}"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        ) VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
        """,
        (metadata["tenant_id"], request_id, trace_id, virtual_key_id, SOURCE_TIME),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
          status, created_at, updated_at, policy_snapshot_status
        ) VALUES(?, ?, ?, ?, ?, ?, 'success', ?, ?, 'disabled')
        """,
        (
            request_id,
            metadata["tenant_id"],
            trace_id,
            virtual_key_id,
            metadata["provider_id"],
            metadata["model_id"],
            SOURCE_TIME,
            SOURCE_TIME,
        ),
    )
    connection.commit()


def _insert_final_attempt_request(
    connection: sqlite3.Connection,
    metadata: dict[str, str],
    *,
    request_id: str,
) -> None:
    """写入 succeeded final Attempt 与交叉一致的 RequestLog，供父侧归因保护测试。"""

    trace_id = f"trace_{request_id}"
    connection.execute(
        """
        INSERT INTO routing_attempts(
          routing_attempt_id, tenant_id, request_id, trace_id,
          attempt_sequence, attempt_role, trigger_type,
          model_id, internal_model_id, provider_id, provider_code,
          status, retryable, latency_ms, usage_status,
          created_at, completed_at, updated_at
        ) VALUES(
          'rta_prd049_attempt', ?, ?, ?, 1, 'primary', 'initial', ?, ?, ?, ?,
          'succeeded', 0, 20, 'not_applicable', ?, ?, ?
        )
        """,
        (
            metadata["tenant_id"],
            request_id,
            trace_id,
            metadata["model_id"],
            metadata["model_alias"],
            metadata["provider_id"],
            metadata["provider_code"],
            SOURCE_TIME,
            "2026-07-20T03:00:01Z",
            "2026-07-20T03:00:01Z",
        ),
    )
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        ) VALUES(?, ?, ?, NULL, NULL, 'disabled', ?)
        """,
        (metadata["tenant_id"], request_id, trace_id, SOURCE_TIME),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, provider_id, model_id, status,
          routing_attempt_count, final_routing_attempt_id,
          created_at, updated_at, policy_snapshot_status
        ) VALUES(?, ?, ?, ?, ?, 'success', 1, 'rta_prd049_attempt', ?, ?, 'disabled')
        """,
        (
            request_id,
            metadata["tenant_id"],
            trace_id,
            metadata["provider_id"],
            metadata["model_id"],
            SOURCE_TIME,
            SOURCE_TIME,
        ),
    )
    connection.commit()


def _record_key_feedback(
    database_url: str,
    metadata: dict[str, str],
    *,
    request_id: str,
    operation_id: str,
):
    """使用目标租户的 Virtual Key 身份创建反馈，用于验证 partial operation 唯一冲突。"""

    return create_request_feedback(
        database_url,
        actor=FeedbackActor(
            tenant_id=metadata["tenant_id"],
            actor_type="virtual_key",
            actor_virtual_key_id=metadata["virtual_key_id"],
        ),
        operation_request_id=operation_id,
        trace_id=f"trace_{operation_id}",
        intent=normalize_feedback_payload(
            {"request_id": request_id, "rating": 4, "accepted": True, "tags": ["relevant"]}
        ),
        now=FEEDBACK_TIME,
    )


def _record_session_feedback(
    database_url: str,
    metadata: dict[str, str],
    *,
    request_id: str,
    operation_id: str,
):
    """使用 shared 事务为当前 tenant 的成功调用创建安全 Session 反馈。"""

    return create_request_feedback(
        database_url,
        actor=FeedbackActor(
            tenant_id=metadata["tenant_id"],
            actor_type="session_user",
            actor_user_id=metadata["user_id"],
        ),
        operation_request_id=operation_id,
        trace_id=f"trace_{operation_id}",
        intent=normalize_feedback_payload(
            {"request_id": request_id, "rating": 5, "accepted": True, "tags": ["accurate"]}
        ),
        now=FEEDBACK_TIME,
    )


def _insert_feedback_without_event(
    connection: sqlite3.Connection,
    metadata: dict[str, str],
    *,
    intent,
    feedback_id: str,
    event_id: str,
) -> None:
    """在显式事务中只插合法 Feedback，用于证明 deferred Event FK 在 commit 强制成对。"""

    connection.execute(
        """
        INSERT INTO request_feedbacks(
          request_feedback_id, tenant_id, source_request_id, model_id, provider_id,
          rating, accepted, tags_json, actor_type, actor_user_id,
          operation_request_id, request_fingerprint, feedback_event_id, trace_id,
          status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'session_user', ?,
                 'op_prd049_single', ?, ?, 'trace_prd049_single', 'recorded', ?, ?)
        """,
        (
            feedback_id,
            metadata["tenant_id"],
            intent.source_request_id,
            metadata["model_id"],
            metadata["provider_id"],
            intent.rating,
            int(intent.accepted),
            json.dumps(list(intent.tags), separators=(",", ":")),
            metadata["user_id"],
            intent.request_fingerprint,
            event_id,
            SOURCE_TIME,
            SOURCE_TIME,
        ),
    )


def _event_payload(*, feedback_id: str, request_id: str, model_id: str) -> dict[str, object]:
    """构造数据库 graph/payload trigger 要求的七字段安全事件对象。"""

    return {
        "request_feedback_id": feedback_id,
        "request_id": request_id,
        "model_id": model_id,
        "actor_type": "session_user",
        "rating": 5,
        "accepted": True,
        "tags": ["accurate"],
    }


def _inject_down_guard_fact(
    database_url: str,
    metadata: dict[str, str],
    *,
    fact_kind: str,
) -> None:
    """按类别注入最小不可降级事实，必要时只在测试副本移除写入防护 trigger。"""

    connection = connect_database(database_url)
    if fact_kind == "feedback":
        _insert_legacy_request(connection, metadata, request_id="req_prd049_guard")
        connection.execute("DROP TRIGGER trg_request_feedback_aggregate_insert_prd049")
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        intent = normalize_feedback_payload(
            {"request_id": "req_prd049_guard", "rating": 5, "accepted": True, "tags": []}
        )
        connection.execute("BEGIN")
        _insert_feedback_without_event(
            connection,
            metadata,
            intent=intent,
            feedback_id="rfb_" + "a" * 32,
            event_id="fbe_" + "a" * 32,
        )
        connection.commit()
    elif fact_kind == "event":
        connection.execute("DROP TRIGGER trg_request_feedback_event_graph_insert_prd049")
        connection.execute("DROP TRIGGER trg_request_feedback_event_payload_insert_prd049")
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        payload = _event_payload(
            feedback_id="rfb_" + "b" * 32,
            request_id="req_prd049_guard_event",
            model_id=metadata["model_id"],
        )
        connection.execute(
            """
            INSERT INTO request_feedback_events(
              feedback_event_id, tenant_id, request_feedback_id, source_request_id,
              event_type, actor_type, actor_id, operation_request_id, trace_id,
              payload_json, created_at
            ) VALUES(?, ?, ?, 'req_prd049_guard_event', 'feedback.created',
                     'session_user', ?, 'op_guard_event', 'trace_guard_event', ?, ?)
            """,
            (
                "fbe_" + "b" * 32,
                metadata["tenant_id"],
                "rfb_" + "b" * 32,
                metadata["user_id"],
                json.dumps(payload, separators=(",", ":")),
                SOURCE_TIME,
            ),
        )
        connection.commit()
    elif fact_kind == "aggregate":
        connection.execute("DROP TRIGGER trg_model_feedback_statistics_validate_insert_prd049")
        connection.execute(
            """
            INSERT INTO model_feedback_statistics(
              tenant_id, model_id, feedback_count, rating_sum,
              rating_1_count, rating_2_count, rating_3_count, rating_4_count, rating_5_count,
              accepted_count, feedback_statistics_version, updated_at
            ) VALUES(?, ?, 0, 0, 0, 0, 0, 0, 0, 0, 0, ?)
            """,
            (metadata["tenant_id"], metadata["model_id"], SOURCE_TIME),
        )
        connection.commit()
    elif fact_kind == "subscription":
        _insert_webhook_endpoint(connection, metadata, event_types=["feedback.created"])
    elif fact_kind in {"delivery", "attempt"}:
        _insert_webhook_endpoint(connection, metadata, event_types=[])
        _insert_feedback_delivery(connection, metadata)
        if fact_kind == "attempt":
            connection.execute(
                """
                INSERT INTO webhook_delivery_attempts(
                  webhook_delivery_attempt_id, tenant_id, webhook_delivery_id,
                  webhook_endpoint_id, attempt_no, status, error_code, error_summary, created_at
                ) VALUES('wda_prd049_guard', ?, 'wdl_prd049_guard',
                         'whep_prd049_guard', 1, 'failed', 'webhook.network_error',
                         'safe category', ?)
                """,
                (metadata["tenant_id"], SOURCE_TIME),
            )
            connection.commit()
    else:  # pragma: no cover - 参数化集合已经固定。
        # 未知类别表示测试自身扩展未同步注入器，必须清晰失败而不能静默跳过 guard。
        raise AssertionError(f"unknown fact kind: {fact_kind}")
    connection.close()


def _insert_webhook_endpoint(
    connection: sqlite3.Connection,
    metadata: dict[str, str],
    *,
    event_types: list[str],
) -> None:
    """写入不含真实 URL secret 的测试 Endpoint，专门验证订阅 down guard。"""

    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
          encrypted_secret, secret_prefix, secret_fingerprint, status,
          created_by, updated_by, created_at, updated_at
        ) VALUES(
          'whep_prd049_guard', ?, 'PRD-049 Guard', 'https://example.invalid/webhook', ?,
          'test-ciphertext', 'test-prefix', 'test-fingerprint', 'active', ?, ?, ?, ?
        )
        """,
        (
            metadata["tenant_id"],
            json.dumps(event_types, separators=(",", ":")),
            metadata["user_id"],
            metadata["user_id"],
            SOURCE_TIME,
            SOURCE_TIME,
        ),
    )
    connection.commit()


def _insert_feedback_delivery(
    connection: sqlite3.Connection,
    metadata: dict[str, str],
) -> None:
    """写入 feedback.created 投递事实，payload 仅使用空测试信封且不含敏感正文。"""

    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
          event_timestamp, payload_json, status, created_at, updated_at
        ) VALUES(
          'wdl_prd049_guard', ?, 'whep_prd049_guard', ?, 'feedback.created',
          ?, '{}', 'pending', ?, ?
        )
        """,
        (metadata["tenant_id"], "fbe_" + "d" * 32, SOURCE_TIME, SOURCE_TIME, SOURCE_TIME),
    )
    connection.commit()
