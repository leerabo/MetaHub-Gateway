"""Atomic archive contracts with synthetic receipts, not deployed isolation evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
import test_prd137_security_assurance as api
from httpx import ASGITransport, AsyncClient
from metahub_control.security_assurance import service
from metahub_control.security_assurance.receipts import (
    FileAssuranceReceiptVerifier,
    TrustKey,
    execution_request_payload,
    utc_text,
)
from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.errors import DbError
from metahub_shared.db.schema import table_exists

base_database = api.base_database
runtime = api.runtime


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _create(runtime, monkeypatch, runner, key="prd137-archive-contract-0001"):
    app = api._app(monkeypatch, runtime, runner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await api._create(client, runtime, token=api.PLATFORM_A_TOKEN, key=key)


def _rows(runtime):
    with closing(connect_database(runtime.config.database_url)) as connection:
        return connection.execute("SELECT * FROM assurance_receipt_archives").fetchall()


@pytest.mark.anyio
async def test_archive_exact_bytes_replay_and_immutable(runtime, monkeypatch):
    captured = {}

    def runner(request, lease):
        content = (" \n" + json.dumps(json.loads(api._passed_runner(request, lease)), indent=2)
                   + "\n ").encode("utf-8")
        captured.update(request=request, content=content)
        return content

    response = await _create(runtime, monkeypatch, runner)
    assert response.status_code == 201, response.text
    assert response.json()["assessment"]["status"] == "passed"
    row, = _rows(runtime)
    assert row["envelope_json"].encode("utf-8") == captured["content"]
    assert row["request_json"].encode() == canonical_json_bytes(
        execution_request_payload(captured["request"]),
    )
    for prefix in ("envelope", "request", "trust_key"):
        assert row[f"{prefix}_sha256"] == _sha(row[f"{prefix}_json"])
        assert f"{prefix}_json" not in response.text
    trust = json.loads(Path(runtime.config.assurance_receipt_trust_bundle_path).read_text())
    assert json.loads(row["trust_key_json"]) == TrustKey.model_validate_json(
        json.dumps(trust["keys"][0]),
    ).model_dump(mode="json")
    assert row["workspace_id"] == runtime.workspace_id
    assert row["tenant_id"] == runtime.tenant_id
    assert row["fencing_generation"] == row["verifier_version"] == 1

    def replay_runner(*_args):
        pytest.fail("An idempotent replay must not invoke the runner.")

    replay = await _create(runtime, monkeypatch, replay_runner)
    assert replay.status_code in (200, 201), replay.text
    assert [dict(item) for item in _rows(runtime)] == [dict(row)]
    statements = [
        "UPDATE assurance_receipt_archives SET verifier_version=1",
        "DELETE FROM assurance_receipt_archives",
    ]
    with closing(connect_database(runtime.config.database_url)) as connection:
        if getattr(connection, "backend", "sqlite") == "postgresql":
            statements.append("TRUNCATE assurance_receipt_archives")
    for statement in statements:
        with closing(connect_database(runtime.config.database_url)) as connection:
            with pytest.raises(sqlite3.Error):
                connection.execute(statement)
            connection.rollback()
    with pytest.raises(DbError):
        rollback_last_migration(runtime.config.database_url, expected_version="0142")
    assert [dict(item) for item in _rows(runtime)] == [dict(row)]
    assert not apply_migrations(runtime.config.database_url).applied


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["archive_insert", "after_archive"])
async def test_archive_and_result_rollback_together(runtime, monkeypatch, mode):
    table = "assurance_receipt_archives" if mode == "archive_insert" else "isolation_test_runs"
    with closing(connect_database(runtime.config.database_url)) as connection:
        if getattr(connection, "backend", "sqlite") == "postgresql":
            connection.execute(
                """CREATE FUNCTION fail_archive_test() RETURNS trigger LANGUAGE plpgsql AS $$
                   BEGIN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='archive_test_failure';
                   END; $$""",
            )
            connection.execute(
                f"CREATE TRIGGER fail_archive_test BEFORE INSERT ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION fail_archive_test()",
            )
        else:
            connection.execute(
                f"CREATE TRIGGER fail_archive_test BEFORE INSERT ON {table} "
                "BEGIN SELECT RAISE(ABORT,'archive_test_failure'); END",
            )
        connection.commit()
    response = await _create(runtime, monkeypatch, api._passed_runner)
    assert response.status_code == 503, response.text
    with closing(connect_database(runtime.config.database_url)) as connection:
        for name in ("assurance_receipt_archives", "isolation_test_runs", "pen_test_findings",
                     "assurance_cleanup_journal"):
            assert connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM security_assessments",
        ).fetchone()[0] == "running"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["revoked", "generation"])
async def test_final_fencing_controls_archive_retention(runtime, monkeypatch, mode):
    persist = service._persist_execution

    def final_transition(*args, **kwargs):
        assert kwargs["receipt"] is not None
        with closing(connect_database(runtime.config.database_url)) as connection:
            if mode == "revoked":
                connection.execute(
                    "UPDATE platform_operators SET revision=revision+1 WHERE operator_id=?",
                    (api.OPERATOR_A_ID,),
                )
            else:
                stamp = connection.execute(
                    "SELECT updated_at FROM assurance_runner_leases",
                ).fetchone()[0]
                connection.execute(
                    "UPDATE assurance_runner_leases SET fencing_generation=fencing_generation+1,"
                    "updated_at=?",
                    (service._db_time(service._parse_time(stamp) + timedelta(milliseconds=1)),),
                )
            connection.commit()
        return persist(*args, **kwargs)

    monkeypatch.setattr(service, "_persist_execution", final_transition)
    response = await _create(runtime, monkeypatch, api._passed_runner)
    if mode == "generation":
        assert response.status_code == 409, response.text
        assert not _rows(runtime)
    else:
        assert response.status_code == 201, response.text
        assert response.json()["assessment"]["status"] == "failed"
        assert {run["outcome"] for run in response.json()["assessment"]["runs"]} == {"error"}
        assert len(_rows(runtime)) == 1
        with closing(connect_database(runtime.config.database_url)) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM assurance_cleanup_journal",
            ).fetchone()[0] == 0


@pytest.mark.anyio
async def test_archive_insert_rejects_foreign_and_corrupt_binding(runtime, monkeypatch):
    checks = []

    def runner(request, lease):
        content = api._passed_runner(request, lease)
        verified = FileAssuranceReceiptVerifier(
            Path(runtime.config.assurance_receipt_trust_bundle_path),
        ).verify(request, content)
        original = {
            "security_assessment_id": request.security_assessment_id,
            "workspace_id": request.workspace_id, "tenant_id": request.tenant_id,
            "fencing_generation": 1, "envelope_json": verified.envelope_json,
            "envelope_sha256": _sha(verified.envelope_json), "request_json": verified.request_json,
            "request_sha256": _sha(verified.request_json),
            "trust_key_json": verified.trust_key_json,
            "trust_key_sha256": _sha(verified.trust_key_json), "verifier_version": 1,
            "verified_at": utc_text(verified.verified_at),
        }
        for field, value in (
            ("workspace_id", "wsp_" + "f" * 32), ("tenant_id", "foreign-owner"),
            ("fencing_generation", 2), ("envelope_sha256", "f" * 64),
            ("request_sha256", "f" * 64), ("trust_key_sha256", "f" * 64),
            ("security_assessment_id", "sas_" + "f" * 32),
        ):
            forged = original | {field: value}
            with closing(connect_database(runtime.config.database_url)) as connection:
                with pytest.raises(sqlite3.Error):
                    connection.execute(
                        "INSERT INTO assurance_receipt_archives (" + ",".join(forged) + ") VALUES ("
                        + ",".join("?" for _ in forged) + ")", tuple(forged.values()),
                    )
                connection.rollback()
            checks.append(field)
        return content

    response = await _create(runtime, monkeypatch, runner)
    assert response.status_code == 201, response.text
    assert response.json()["assessment"]["status"] == "passed"
    assert len(checks) == 7
    assert len(_rows(runtime)) == 1


def test_archive_empty_migration_roundtrip_and_manifest(runtime):
    url = runtime.config.database_url
    assert not apply_migrations(url).applied
    with closing(connect_database(url)) as connection:
        manifest = connection.execute(
            "SELECT * FROM tenant_deletion_table_manifest "
            "WHERE source_table='assurance_receipt_archives'",
        ).fetchone()
        assert (manifest["disposition"], manifest["retention_class"]) == (
            "archive_then_delete", "security",
        )
        triggers = connection.execute(
            "SELECT * FROM tenant_deletion_trigger_manifest "
            "WHERE source_table='assurance_receipt_archives' "
            "ORDER BY trigger_name",
        ).fetchall()
        native = getattr(connection, "backend", "sqlite") == "postgresql"
        assert len(triggers) == (4 if native else 3)
        for trigger in triggers:
            assert _sha(trigger["trigger_sql"]) == trigger["trigger_sha256"]
        assert _sha("\x1e".join(
            f"{trigger['trigger_name']}\x1f{trigger['trigger_sha256']}" for trigger in triggers
        )) == manifest["trigger_set_sha256"]
        if not native:
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name='assurance_receipt_archives'",
            ).fetchone()[0]
            columns = connection.execute(
                'PRAGMA table_info("assurance_receipt_archives")',
            ).fetchall()
            column_text = "\x1e".join("\x1f".join(
                "" if row[key] is None else str(row[key])
                for key in ("cid", "name", "type", "notnull", "dflt_value", "pk")
            ) for row in columns)
        else:
            columns = connection.execute(
                """SELECT a.attnum,a.attname,quote_ident(a.attname) AS quoted_name,
                          format_type(a.atttypid,a.atttypmod) AS type_name,a.attnotnull,
                          pg_get_expr(d.adbin,d.adrelid) AS default_sql
                   FROM pg_attribute a LEFT JOIN pg_attrdef d
                     ON d.adrelid=a.attrelid AND d.adnum=a.attnum
                   WHERE a.attrelid='assurance_receipt_archives'::regclass
                     AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""",
            ).fetchall()
            definitions = [row["quoted_name"] + " " + row["type_name"]
                           + (" DEFAULT " + row["default_sql"] if row["default_sql"] else "")
                           + (" NOT NULL" if row["attnotnull"] else "") for row in columns]
            definitions.extend(row[0] for row in connection.execute(
                "SELECT 'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid) "
                "FROM pg_constraint WHERE conrelid='assurance_receipt_archives'::regclass "
                "ORDER BY conname",
            ).fetchall())
            table_sql = "CREATE TABLE assurance_receipt_archives (" + ",".join(definitions) + ")"
            column_text = "\x1e".join("\x1f".join((
                str(row["attnum"]), row["attname"], row["type_name"],
                str(row["attnotnull"]).lower(), row["default_sql"] or "",
            )) for row in columns)
            actual_triggers = connection.execute(
                "SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) "
                "FROM pg_trigger WHERE tgrelid='assurance_receipt_archives'::regclass "
                "AND NOT tgisinternal ORDER BY tgname",
            ).fetchall()
            assert [tuple(row) for row in actual_triggers] == [
                (row["trigger_name"], row["trigger_sql"]) for row in triggers
            ]
        assert _sha(table_sql) == manifest["table_sql_sha256"]
        assert _sha(column_text) == manifest["column_signature_sha256"]
    rollback_last_migration(url, expected_version="0142")
    with closing(connect_database(url)) as connection:
        assert not table_exists(connection, "assurance_receipt_archives")
        for table in ("tenant_deletion_table_manifest", "tenant_deletion_trigger_manifest",
                      "tenant_deletion_manifest_extensions_prd137"):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE source_table='assurance_receipt_archives'",
            ).fetchone()[0] == 0
    assert apply_migrations(url).applied == ["0142"]
    assert not apply_migrations(url).applied
