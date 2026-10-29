"""PRD-048 模型质量单样本原子增量和可重建时间线测试。"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from metahub_gateway.quality import record_model_quality_observation
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.connection import database_connection_pool_scope, database_request_scope


def _sqlite_url(path: Path) -> str:
    """把临时文件转换为项目迁移器支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


@pytest.mark.parametrize(
    "scope", [nullcontext, database_connection_pool_scope, database_request_scope],
)
def test_quality_observation_respects_connection_owner_and_rollback(tmp_path, scope):
    database_url = _sqlite_url(tmp_path / "quality-connection-owner.db")
    setup = connect_database(database_url)
    setup.execute("""CREATE TABLE model_quality_scores (
        tenant_id TEXT, model_id TEXT, status TEXT,
        sample_count INTEGER DEFAULT 0, success_count INTEGER DEFAULT 0,
        error_count INTEGER DEFAULT 0, latency_sample_count INTEGER DEFAULT 0,
        total_latency_ms INTEGER DEFAULT 0, statistics_version INTEGER DEFAULT 0,
        first_observed_at TEXT, last_observed_at TEXT, updated_at TEXT)""")
    setup.execute("""INSERT INTO model_quality_scores (tenant_id,model_id,status,updated_at)
        VALUES ('tenant','model','active','2026-09-01T00:00:00Z')""")
    setup.commit()
    setup.close()
    with scope():
        connection = connect_database(database_url)
        try:
            for commit in (False, True):
                connection.execute("BEGIN IMMEDIATE")
                record_model_quality_observation(
                    connection, tenant_id="tenant", model_id="model", succeeded=True,
                    latency_ms=23, observed_at=datetime(2026, 9, 17, tzinfo=UTC),
                )
                assert connection.in_transaction
                if commit:
                    connection.commit()
                else:
                    connection.rollback()
        finally:
            connection.rollback()
            connection.close()
    verify = connect_database(database_url)
    try:
        row = verify.execute("""SELECT sample_count,success_count,error_count,total_latency_ms
            FROM model_quality_scores""").fetchone()
        assert tuple(row) == (1, 1, 0, 23)
    finally:
        verify.close()


def test_quality_observation_uses_time_extrema_for_out_of_order_commits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """乱序提交仍保存最早/最晚秒级时刻，使 down/up 的 MIN/MAX 重建逐字段一致。

    参数：一条较晚观察和随后提交的较早观察。返回值：无。关键约束：updated_at 不得被
    较早事实回退，计数和整数延迟仍各增加一次。
    """

    database_url = _sqlite_url(tmp_path / "quality-observation-extrema.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? ORDER BY model_id LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    score = connection.execute(
        "SELECT updated_at FROM model_quality_scores WHERE tenant_id=? AND model_id=?",
        (seed.tenant_id, model["model_id"]),
    ).fetchone()
    baseline = datetime.fromisoformat(str(score["updated_at"]).replace("Z", "+00:00"))
    if baseline.tzinfo is None:
        # SQLite datetime('now') 按 UTC 生成无时区文本，测试只在这里恢复其既有语义。
        baseline = baseline.replace(tzinfo=UTC)
    baseline = baseline.astimezone(UTC).replace(microsecond=0)
    earlier = baseline + timedelta(seconds=1, microseconds=100_000)
    later = baseline + timedelta(seconds=2, microseconds=900_000)
    earlier_text = earlier.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    later_text = later.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    for succeeded, latency_ms, observed_at in ((True, 7, later), (False, 11, earlier)):
        connection.execute("BEGIN IMMEDIATE")
        record_model_quality_observation(
            connection,
            tenant_id=seed.tenant_id,
            model_id=model["model_id"],
            succeeded=succeeded,
            latency_ms=latency_ms,
            observed_at=observed_at,
        )
        connection.commit()

    score = connection.execute(
        "SELECT sample_count, success_count, error_count, total_latency_ms, "
        "first_observed_at, last_observed_at, updated_at "
        "FROM model_quality_scores WHERE tenant_id=? AND model_id=?",
        (seed.tenant_id, model["model_id"]),
    ).fetchone()
    assert tuple(score) == (
        2,
        1,
        1,
        18,
        earlier_text,
        later_text,
        later_text,
    )
