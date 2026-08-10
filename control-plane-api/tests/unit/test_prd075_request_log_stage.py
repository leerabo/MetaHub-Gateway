"""PRD-075 RequestLog 列表 SQL 与详情 Python 的终止阶段一致性门禁。"""

from __future__ import annotations

import sqlite3

import pytest
from metahub_control.request_logs.query import TERMINAL_STAGE_SQL
from metahub_control.request_logs.service import _terminal_stage


@pytest.mark.parametrize(
    ("error_code", "expected"),
    (
        ("gateway.experiment_unavailable", "routing"),
        ("gateway.experiment_variant_unavailable", "routing"),
        ("gateway.experiment_execution_timeout", "provider"),
    ),
)
def test_experiment_terminal_stage_sql_and_python_are_identical(
    error_code: str,
    expected: str,
) -> None:
    """同一失败在列表和详情中必须产生相同固定阶段。"""

    connection = sqlite3.connect(":memory:")
    sql = (
        f"SELECT ({TERMINAL_STAGE_SQL}) "
        "FROM (SELECT 'failed' AS status, ? AS error_code) AS rl "
        "LEFT JOIN (SELECT NULL AS decision, NULL AS scan_status) AS oss ON 1=1"
    )
    sql_stage = connection.execute(sql, (error_code,)).fetchone()[0]
    assert sql_stage == expected
    assert _terminal_stage("failed", error_code) == expected
