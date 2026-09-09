"""Reach a target migration only on a disposable test database using guarded downs."""

from contextlib import closing

from metahub_shared.db import connect_database, rollback_last_migration


def rollback_later_migrations(database_url: str, target: str) -> list[str]:
    with closing(connect_database(database_url)) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations WHERE status='applied' ORDER BY version"
            )
        ]
    assert target in versions, "target migration must already be applied"
    later = [version for version in versions if version > target]
    for version in reversed(later):
        assert rollback_last_migration(
            database_url, expected_version=version
        ).rolled_back == version
    return later
