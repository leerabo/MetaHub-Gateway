"""Database failures retain their original identity through context managers."""

from contextlib import contextmanager

import pytest
from metahub_shared.db.errors import DbError


def test_database_error_survives_contextmanager_traceback_assignment():
    @contextmanager
    def transaction():
        yield

    original = DbError("db.migration_failed", "Migration refused", "Inspect migration")
    with pytest.raises(DbError) as caught:
        with transaction():
            raise original
    assert caught.value is original
    assert caught.value.code == "db.migration_failed"
    assert str(caught.value) == "Migration refused"
    assert caught.value.__traceback__ is not None
