"""PRD-053 Gateway 临时限流 overlay 聚焦测试。

职责：验证永久规则与临时覆盖的逐维合并、租户/Key 隔离、服务端生效时间和依赖故障折叠。
边界：这里只测试 Gateway 只读 loader，不代替 Control Plane 的动作状态机与数据库触发器测试。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from metahub_gateway.rate_limits.service import load_virtual_key_rate_limit_rule
from metahub_shared.runtime.errors import ApiError


def _database(tmp_path: Path) -> SimpleNamespace:
    """创建只含 loader 所需字段的隔离 SQLite 数据库。"""

    path = tmp_path / "rate-limits.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE virtual_key_rate_limit_rules(
          tenant_id TEXT NOT NULL,
          virtual_key_id TEXT NOT NULL,
          rpm_limit INTEGER,
          tpm_limit INTEGER,
          concurrency_limit INTEGER,
          status TEXT NOT NULL
        );
        CREATE TABLE temporary_rate_limit_overrides(
          temporary_rate_limit_override_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          virtual_key_id TEXT NOT NULL,
          rpm_limit INTEGER,
          tpm_limit INTEGER,
          concurrency_limit INTEGER,
          status TEXT NOT NULL,
          starts_at TEXT NOT NULL,
          expires_at TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()
    return SimpleNamespace(database_url=f"sqlite:///{path.as_posix()}")


def _execute(config: SimpleNamespace, sql: str, values: tuple[object, ...]) -> None:
    """向测试文件写入治理事实并立即关闭连接。"""

    path = config.database_url.removeprefix("sqlite:///")
    connection = sqlite3.connect(path)
    connection.execute(sql, values)
    connection.commit()
    connection.close()


def test_loader_takes_per_dimension_minimum_across_permanent_and_overrides(
    tmp_path: Path,
) -> None:
    """主流程：永久规则与两个有效 overlay 分别按 RPM/TPM/concurrency 取最小值。"""

    config = _database(tmp_path)
    _execute(
        config,
        """
        INSERT INTO virtual_key_rate_limit_rules
        VALUES(?, ?, ?, ?, ?, 'active')
        """,
        ("tenant-a", "key-a", 100, 1000, None),
    )
    for values in (
        (
            "trl_1",
            "tenant-a",
            "key-a",
            10,
            None,
            2,
            "active",
            "2026-07-27T00:00:00Z",
            "2026-07-27T01:00:00Z",
        ),
        (
            "trl_2",
            "tenant-a",
            "key-a",
            20,
            500,
            None,
            "active",
            "2026-07-27T00:00:00Z",
            "2026-07-27T01:00:00Z",
        ),
    ):
        _execute(
            config,
            """
            INSERT INTO temporary_rate_limit_overrides
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )

    rule = load_virtual_key_rate_limit_rule(
        config,
        "tenant-a",
        "key-a",
        now=datetime(2026, 7, 27, 0, 30, tzinfo=UTC),
    )

    assert rule is not None
    assert (rule.rpm_limit, rule.tpm_limit, rule.concurrency_limit) == (10, 500, 2)


def test_loader_compares_offset_timestamps_by_instant(tmp_path: Path) -> None:
    """时间等价：带 offset 的有效期按真实时刻比较，且到期瞬间使用严格开区间。"""

    config = _database(tmp_path)
    for values in (
        (
            "offset-active",
            7,
            "2026-07-27T08:00:00+08:00",
            "2026-07-27T09:00:00+08:00",
        ),
        (
            "offset-expired",
            1,
            "2026-07-27T07:00:00+08:00",
            "2026-07-27T08:30:00+08:00",
        ),
    ):
        _execute(
            config,
            """
            INSERT INTO temporary_rate_limit_overrides
            VALUES(?, 'tenant-a', 'key-a', ?, NULL, NULL, 'active', ?, ?)
            """,
            values,
        )

    rule = load_virtual_key_rate_limit_rule(
        config,
        "tenant-a",
        "key-a",
        now=datetime(2026, 7, 27, 0, 30, tzinfo=UTC),
    )

    assert rule is not None and rule.rpm_limit == 7


def test_loader_ignores_released_expired_future_and_other_tenant_rows(
    tmp_path: Path,
) -> None:
    """隔离与时间：只有同 tenant/Key 且 starts<=now<expires 的 active overlay 生效。"""

    config = _database(tmp_path)
    rows = (
        ("released", "tenant-a", "key-a", 1, "released", "00:00:00", "01:00:00"),
        ("expired", "tenant-a", "key-a", 2, "active", "00:00:00", "00:10:00"),
        ("future", "tenant-a", "key-a", 3, "active", "00:40:00", "01:00:00"),
        ("other", "tenant-b", "key-a", 4, "active", "00:00:00", "01:00:00"),
        ("valid", "tenant-a", "key-a", 5, "active", "00:00:00", "01:00:00"),
    )
    for identity, tenant, key, rpm, status, start, end in rows:
        _execute(
            config,
            """
            INSERT INTO temporary_rate_limit_overrides
            VALUES(?, ?, ?, ?, NULL, NULL, ?, ?, ?)
            """,
            (
                identity,
                tenant,
                key,
                rpm,
                status,
                f"2026-07-27T{start}Z",
                f"2026-07-27T{end}Z",
            ),
        )

    rule = load_virtual_key_rate_limit_rule(
        config,
        "tenant-a",
        "key-a",
        now=datetime(2026, 7, 27, 0, 30, tzinfo=UTC),
    )
    absent = load_virtual_key_rate_limit_rule(
        config,
        "tenant-a",
        "key-b",
        now=datetime(2026, 7, 27, 0, 30, tzinfo=UTC),
    )

    assert rule is not None and rule.rpm_limit == 5
    assert absent is None


def test_loader_rejects_naive_clock_and_database_failure(tmp_path: Path) -> None:
    """异常：含糊时钟和缺表数据库都折叠为统一 Gateway 限流不可用错误。"""

    config = _database(tmp_path)
    with pytest.raises(ApiError) as clock_error:
        load_virtual_key_rate_limit_rule(
            config,
            "tenant-a",
            "key-a",
            now=datetime(2026, 7, 27, 0, 30),
        )

    broken = SimpleNamespace(
        database_url=f"sqlite:///{(tmp_path / 'missing-schema.db').as_posix()}"
    )
    with pytest.raises(ApiError) as database_error:
        load_virtual_key_rate_limit_rule(
            broken,
            "tenant-a",
            "key-a",
            now=datetime(2026, 7, 27, 0, 30, tzinfo=UTC),
        )

    assert clock_error.value.code == "gateway.rate_limit_unavailable"
    assert database_error.value.code == "gateway.rate_limit_unavailable"