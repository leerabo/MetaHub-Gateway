"""Strictly increasing privacy timestamps at their persisted precision."""

from datetime import UTC, datetime, timedelta

import pytest
from metahub_control.privacy_center import service


@pytest.mark.parametrize("microseconds", [0, 1, 999, 1000, 123456, 999999])
def test_consecutive_transitions_remain_monotonic_after_serialization(microseconds):
    now = datetime(2026, 9, 7, 1, 2, 3, microseconds, tzinfo=UTC)
    previous = service._db_time(now - timedelta(seconds=1))
    for _ in range(3):
        changed = service._db_time(service._changed_time(now, previous))
        assert service._time(changed) > service._time(previous)
        previous = changed
