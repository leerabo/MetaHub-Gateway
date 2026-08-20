"""PRD-091 migration 0083 projection, conflict, and rollback-guard contracts."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import time
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
    verify_password,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
STAMP = "2030-01-01T00:00:00Z"
STAMP_1H = "2030-01-01T01:00:00Z"
STAMP_1D = "2030-01-02T00:00:00Z"
IDENTITY_TABLES = {
    "global_accounts",
    "account_credentials",
    "account_identity_links",
    "global_sessions",
    "identity_events",
    "identity_event_outbox",
    "identity_migration_conflicts",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _clone(source: Path, destination: Path) -> str:
    shutil.copy2(source, destination)
    return _url(destination)


def _version(connection: sqlite3.Connection) -> str:
    return str(
        connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()[0]
    )


def _healthy(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _identity_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name IN "
            f"({','.join('?' for _ in IDENTITY_TABLES)})",
            tuple(sorted(IDENTITY_TABLES)),
        )
    }


def _projection(connection: sqlite3.Connection, tenant_id: str, user_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT
          user.email,user.display_name,user.password_hash,user.status AS legacy_status,
          account.account_id,account.email_normalized,account.status AS account_status,
          account.login_enabled,account.credential_revision,account.session_revision,
          credential.credential_id,credential.algorithm,credential.secret_hash,
          credential.status AS credential_status,credential.version,
          link.identity_link_id,link.source_type,link.legacy_tenant_id,link.legacy_user_id,
          link.status AS link_status
        FROM users user
        JOIN account_identity_links link
          ON link.legacy_tenant_id=user.tenant_id AND link.legacy_user_id=user.user_id
        JOIN global_accounts account ON account.account_id=link.account_id
        JOIN account_credentials credential
          ON credential.account_id=account.account_id
         AND credential.credential_type='password' AND credential.status='active'
        WHERE user.tenant_id=? AND user.user_id=?
        """,
        (tenant_id, user_id),
    ).fetchone()
    assert row is not None
    return row


@pytest.fixture(scope="module")
def empty_0082(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd091-empty-0082")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 82:
            shutil.copy2(source, migrations / source.name)

    database = root / "empty.sqlite3"
    result = apply_migrations(_url(database), migrations_dir=migrations)
    assert result.applied == [f"{version:04d}" for version in range(1, 83)]
    connection = connect_database(_url(database))
    try:
        assert _version(connection) == "0082"
        _healthy(connection)
    finally:
        connection.close()
    return database


def test_0083_fresh_up_seed_is_idempotent_and_projection_preserves_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _url(tmp_path / "fresh-0083.sqlite3")
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "Prd091-Seed-Idempotency-Only")

    result = apply_migrations(database_url)
    assert result.applied == [f"{version:04d}" for version in range(1, 84)]
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0083"
        assert _identity_tables(connection) == IDENTITY_TABLES
        assert connection.execute("SELECT COUNT(*) FROM global_accounts").fetchone()[0] == 0
        _healthy(connection)
    finally:
        connection.close()

    first_seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        user = connection.execute(
            "SELECT user_id,password_hash FROM users WHERE tenant_id=? AND email=?",
            (first_seed.tenant_id, first_seed.admin_email.lower()),
        ).fetchone()
        assert user is not None
        first_projection = _projection(
            connection,
            first_seed.tenant_id,
            str(user["user_id"]),
        )
        first_hash = str(user["password_hash"])
        assert first_projection["secret_hash"] == first_hash
        assert first_projection["algorithm"] == "pbkdf2_sha256_legacy"
        assert first_projection["email_normalized"] == first_seed.admin_email.lower()
        assert first_projection["account_status"] == first_projection["legacy_status"] == "active"
        assert first_projection["login_enabled"] == 1
        assert first_projection["credential_status"] == first_projection["link_status"] == "active"
        assert first_projection["source_type"] == "legacy_user"
        assert first_projection["legacy_tenant_id"] == first_seed.tenant_id
        assert first_projection["legacy_user_id"] == user["user_id"]
        assert str(first_projection["account_id"]).startswith("gac_")
        assert str(first_projection["credential_id"]).startswith("acr_")
        assert str(first_projection["identity_link_id"]).startswith("ail_")
        _healthy(connection)
    finally:
        connection.close()

    second_seed = apply_seed_data(database_url)
    assert second_seed.tenant_id == first_seed.tenant_id
    assert second_seed.admin_email.lower() == first_seed.admin_email.lower()
    connection = connect_database(database_url)
    try:
        assert tuple(
            connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM users),
                  (SELECT COUNT(*) FROM global_accounts),
                  (SELECT COUNT(*) FROM account_credentials),
                  (SELECT COUNT(*) FROM account_identity_links),
                  (SELECT COUNT(*) FROM identity_migration_conflicts)
                """
            ).fetchone()
        ) == (1, 1, 1, 1, 0)
        user = connection.execute(
            "SELECT user_id,password_hash FROM users WHERE tenant_id=? AND email=?",
            (first_seed.tenant_id, first_seed.admin_email.lower()),
        ).fetchone()
        assert user is not None
        repeated_projection = _projection(
            connection,
            first_seed.tenant_id,
            str(user["user_id"]),
        )
        assert user["password_hash"] == first_hash
        assert repeated_projection["secret_hash"] == first_hash
        assert repeated_projection["credential_revision"] == 1
        assert repeated_projection["session_revision"] == 1
        assert repeated_projection["version"] == 1
        assert repeated_projection["account_id"] == first_projection["account_id"]
        assert repeated_projection["credential_id"] == first_projection["credential_id"]
        assert repeated_projection["identity_link_id"] == first_projection["identity_link_id"]
        _healthy(connection)
    finally:
        connection.close()


def test_0082_seed_backfills_one_to_one_and_pristine_down_reapplies(
    empty_0082: Path,
    tmp_path: Path,
) -> None:
    database_url = _clone(empty_0082, tmp_path / "roundtrip-0083.sqlite3")
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        legacy = connection.execute(
            """
            SELECT user_id,tenant_id,email,display_name,password_hash,status
            FROM users WHERE tenant_id=? AND email=?
            """,
            (seed.tenant_id, seed.admin_email.lower()),
        ).fetchone()
        assert legacy is not None
        legacy_snapshot = tuple(legacy)
    finally:
        connection.close()

    assert apply_migrations(database_url).applied == ["0083"]
    connection = connect_database(database_url)
    try:
        projected = _projection(connection, str(legacy["tenant_id"]), str(legacy["user_id"]))
        projection_ids = (
            projected["account_id"],
            projected["credential_id"],
            projected["identity_link_id"],
        )
        assert projected["secret_hash"] == legacy["password_hash"]
        assert tuple(
            connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM users),
                  (SELECT COUNT(*) FROM global_accounts),
                  (SELECT COUNT(*) FROM account_credentials),
                  (SELECT COUNT(*) FROM account_identity_links)
                """
            ).fetchone()
        ) == (1, 1, 1, 1)
        _healthy(connection)
    finally:
        connection.close()

    repeated = apply_migrations(database_url)
    assert repeated.applied == [] and repeated.skipped[-1] == "0083"
    rolled_back = rollback_last_migration(database_url, expected_version="0083")
    assert rolled_back.rolled_back == "0083"
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0082"
        assert _identity_tables(connection) == set()
        assert (
            tuple(
                connection.execute(
                    """
                SELECT user_id,tenant_id,email,display_name,password_hash,status
                FROM users WHERE tenant_id=? AND user_id=?
                """,
                    (legacy["tenant_id"], legacy["user_id"]),
                ).fetchone()
            )
            == legacy_snapshot
        )
        _healthy(connection)
    finally:
        connection.close()

    assert apply_migrations(database_url).applied == ["0083"]
    connection = connect_database(database_url)
    try:
        reapplied = _projection(connection, str(legacy["tenant_id"]), str(legacy["user_id"]))
        assert (
            reapplied["account_id"],
            reapplied["credential_id"],
            reapplied["identity_link_id"],
        ) == projection_ids
        assert reapplied["secret_hash"] == legacy["password_hash"]
        _healthy(connection)
    finally:
        connection.close()


def test_duplicate_normalized_email_isolated_without_account_merge(
    empty_0082: Path,
    tmp_path: Path,
) -> None:
    database_url = _clone(empty_0082, tmp_path / "duplicate-email-0083.sqlite3")
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        original = connection.execute(
            "SELECT user_id,password_hash FROM users WHERE tenant_id=? AND email=?",
            (seed.tenant_id, seed.admin_email.lower()),
        ).fetchone()
        assert original is not None
        connection.execute(
            """
            INSERT INTO tenants(
              tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('tenant_prd091_duplicate','prd091-duplicate','PRD-091 Duplicate',
              'internal_only','active',?,?)
            """,
            (STAMP, STAMP),
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at)
            VALUES(?,?,' Admin@MetaHub.Local ','Duplicate Admin',?,'admin','active',?,?)
            """,
            (
                "usr_" + "d" * 32,
                "tenant_prd091_duplicate",
                original["password_hash"],
                STAMP,
                STAMP,
            ),
        )
        connection.commit()
        _healthy(connection)
    finally:
        connection.close()

    assert apply_migrations(database_url).applied == ["0083"]
    connection = connect_database(database_url)
    try:
        accounts = connection.execute(
            """
            SELECT account_id,email_normalized,status,login_enabled
            FROM global_accounts ORDER BY account_id
            """
        ).fetchall()
        assert len(accounts) == 2
        assert len({str(row["account_id"]) for row in accounts}) == 2
        assert {str(row["email_normalized"]) for row in accounts} == {"admin@metahub.local"}
        assert {str(row["status"]) for row in accounts} == {"migration_conflict"}
        assert {int(row["login_enabled"]) for row in accounts} == {0}

        assert (
            connection.execute(
                "SELECT COUNT(DISTINCT account_id) FROM account_identity_links"
            ).fetchone()[0]
            == 2
        )
        assert connection.execute("SELECT COUNT(*) FROM account_credentials").fetchone()[0] == 2
        assert (
            connection.execute(
                """
            SELECT COUNT(*)
            FROM users user
            JOIN account_identity_links link
              ON link.legacy_tenant_id=user.tenant_id AND link.legacy_user_id=user.user_id
            JOIN account_credentials credential ON credential.account_id=link.account_id
            WHERE credential.secret_hash=user.password_hash
            """
            ).fetchone()[0]
            == 2
        )

        conflicts = connection.execute(
            """
            SELECT identifier_fingerprint,reason_code,status
            FROM identity_migration_conflicts ORDER BY conflict_id
            """
        ).fetchall()
        assert len(conflicts) == 2
        assert len({str(row["identifier_fingerprint"]) for row in conflicts}) == 1
        assert all(len(str(row["identifier_fingerprint"])) == 64 for row in conflicts)
        assert {str(row["reason_code"]) for row in conflicts} == {"duplicate_identifier"}
        assert {str(row["status"]) for row in conflicts} == {"open"}
        _healthy(connection)
    finally:
        connection.close()


def test_unbounded_legacy_password_profile_is_quarantined_before_kdf(
    empty_0082: Path,
    tmp_path: Path,
) -> None:
    database_url = _clone(empty_0082, tmp_path / "unbounded-password-0083.sqlite3")
    seed = apply_seed_data(database_url)
    malicious_hash = "pbkdf2_sha256$999999999$" + "A" * 24 + "$" + "A" * 44
    connection = connect_database(database_url)
    try:
        connection.execute(
            "UPDATE users SET password_hash=? WHERE tenant_id=? AND email=?",
            (malicious_hash, seed.tenant_id, seed.admin_email.lower()),
        )
        connection.commit()
    finally:
        connection.close()

    assert apply_migrations(database_url).applied == ["0083"]
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT account.status,account.login_enabled,credential.algorithm,conflict.reason_code
            FROM global_accounts account
            JOIN account_credentials credential ON credential.account_id=account.account_id
            JOIN identity_migration_conflicts conflict ON conflict.account_id=account.account_id
            """
        ).fetchone()
        assert tuple(row) == (
            "migration_conflict",
            0,
            "unknown",
            "credential_unrecognized",
        )
    finally:
        connection.close()

    started = time.perf_counter()
    assert verify_password("Canary-Password-Only", malicious_hash) is False
    assert time.perf_counter() - started < 0.05


@pytest.mark.parametrize("fact_kind", ["session", "event"])
def test_security_fact_blocks_0083_down_atomically(
    empty_0082: Path,
    tmp_path: Path,
    fact_kind: str,
) -> None:
    database_url = _clone(empty_0082, tmp_path / f"guard-{fact_kind}-0083.sqlite3")
    seed = apply_seed_data(database_url)
    assert apply_migrations(database_url).applied == ["0083"]
    connection = connect_database(database_url)
    try:
        account_id = str(
            connection.execute(
                """
                SELECT link.account_id FROM account_identity_links link
                JOIN users user
                  ON user.tenant_id=link.legacy_tenant_id
                 AND user.user_id=link.legacy_user_id
                WHERE user.tenant_id=? AND user.email=?
                """,
                (seed.tenant_id, seed.admin_email.lower()),
            ).fetchone()[0]
        )
        if fact_kind == "session":
            connection.execute(
                """
                INSERT INTO global_sessions(
                  session_id,account_id,token_hmac,csrf_hmac,status,
                  credential_revision,session_revision,device_family,client_risk_hmac,
                  expires_at,idle_expires_at,last_seen_at,revoked_at,revoked_reason,
                  created_at,updated_at)
                VALUES(?,?,?,?,'active',1,1,'Test Browser',NULL,?,?,NULL,NULL,NULL,?,?)
                """,
                (
                    "gss_" + "a" * 32,
                    account_id,
                    "b" * 64,
                    "c" * 64,
                    STAMP_1D,
                    STAMP_1H,
                    STAMP,
                    STAMP,
                ),
            )
        else:
            payload = "{}"
            connection.execute(
                """
                INSERT INTO identity_events(
                  identity_event_id,account_id,session_id,event_type,reason_code,
                  request_id,trace_id,payload_json,payload_sha256,occurred_at)
                VALUES(?,NULL,NULL,'identity.login_failed','invalid_identity',?,?,?,?,?)
                """,
                (
                    "iev_" + "d" * 32,
                    "req_prd091_event_guard",
                    "trc_prd091_event_guard",
                    payload,
                    hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                    STAMP,
                ),
            )
        connection.commit()
        _healthy(connection)
    finally:
        connection.close()

    with pytest.raises(DbError):
        rollback_last_migration(database_url, expected_version="0083")

    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0083"
        assert _identity_tables(connection) == IDENTITY_TABLES
        if fact_kind == "session":
            assert connection.execute("SELECT COUNT(*) FROM global_sessions").fetchone()[0] == 1
        else:
            assert connection.execute("SELECT COUNT(*) FROM identity_events").fetchone()[0] == 1
        _healthy(connection)
    finally:
        connection.close()
