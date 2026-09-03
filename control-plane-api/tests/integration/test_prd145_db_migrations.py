"""PRD-145 console-experience migration matrices and rollback guards."""

from __future__ import annotations

import secrets
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
STAMP = "2026-09-03T00:00:00Z"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


def test_prd145_fresh_matrix_and_pristine_roundtrip(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "prd145-fresh.sqlite3")
    result = apply_migrations(database_url)
    assert result.applied[-1] == "0137"
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM platform_console_permission_extensions"
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT count(*) FROM platform_console_role_permissions"
        ).fetchone()[0] == 14
        assert connection.execute(
            "SELECT count(*) FROM workspace_console_permission_extensions"
        ).fetchone()[0] == 5
        assert connection.execute(
            "SELECT count(*) FROM workspace_console_role_permissions"
        ).fetchone()[0] == 24
        assert connection.execute(
            "SELECT count(*) FROM console_experience_scheduler_state"
        ).fetchone()[0] == 10
        assert connection.execute(
            "SELECT count(*) FROM console_experience_migration_cursors"
        ).fetchone()[0] == 6
        assert connection.execute(
            """SELECT count(*) FROM sqlite_schema
               WHERE type='trigger'
                 AND name='trg_global_account_preference_insert_prd145'"""
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """UPDATE platform_console_permission_extensions
                   SET introduced_in_migration='0137'
                   WHERE permission_code='platform.console.overview.read'"""
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert rollback_last_migration(
        database_url, expected_version="0137"
    ).rolled_back == "0137"
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            """SELECT count(*) FROM sqlite_schema
               WHERE name IN('account_preferences',
                 'trg_global_account_preference_insert_prd145')"""
        ).fetchone()[0] == 0
    assert apply_migrations(database_url).applied == ["0137"]


def test_prd145_backfills_locale_and_refuses_fact_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations_0136 = _migration_subset(tmp_path / "migrations-0136", 136)
    database_url = _url(tmp_path / "prd145-backfill.sqlite3")
    assert apply_migrations(database_url, migrations_dir=migrations_0136).applied[-1] == "0136"
    monkeypatch.setenv(
        "METAHUB_SEED_ADMIN_PASSWORD", f"Aa9!{secrets.token_urlsafe(24)}"
    )
    seed = apply_seed_data(database_url)
    with closing(connect_database(database_url)) as connection:
        identity = connection.execute(
            """SELECT link.account_id,link.legacy_user_id
               FROM account_identity_links link
               JOIN users user
                 ON user.tenant_id=link.legacy_tenant_id
                AND user.user_id=link.legacy_user_id
               WHERE link.legacy_tenant_id=? AND user.email=? AND link.status='active'""",
            (seed.tenant_id, seed.admin_email),
        ).fetchone()
        assert identity is not None
        account_id = identity["account_id"]
        connection.execute(
            """INSERT INTO user_preferences(
                 preference_id,tenant_id,user_id,active_navigation_item_id,
                 sidebar_collapsed,locale,created_at,updated_at)
               VALUES(?,?,?,'overview',0,'en-US',?,?)""",
            (
                "upr_prd145_legacy",
                seed.tenant_id,
                identity["legacy_user_id"],
                STAMP,
                STAMP,
            ),
        )
        connection.commit()
    assert apply_migrations(database_url).applied == ["0137"]
    with closing(connect_database(database_url)) as connection:
        preference = connection.execute(
            "SELECT * FROM account_preferences WHERE account_id=?",
            (account_id,),
        ).fetchone()
        assert preference["locale"] == "en-US"
        assert preference["source"] == "legacy_backfill"
        account_id = "gac_" + "e" * 32
        connection.execute(
            """INSERT INTO global_accounts(
                 account_id,email_normalized,phone_e164,display_name,status,login_enabled,
                 credential_revision,session_revision,mfa_state,failed_login_count,
                 locked_until,last_login_at,created_at,updated_at)
               VALUES(?,? ,NULL,'PRD-145 New Account','active',0,1,1,'not_enrolled',
                 0,NULL,NULL,?,?)""",
            (account_id, "prd145-new@example.invalid", STAMP, STAMP),
        )
        created = connection.execute(
            "SELECT locale,revision,source FROM account_preferences WHERE account_id=?",
            (account_id,),
        ).fetchone()
        assert tuple(created) == ("zh-CN", 1, "identity")
        connection.commit()
    with pytest.raises(DbError, match="Rollback failed"):
        rollback_last_migration(database_url, expected_version="0137")
