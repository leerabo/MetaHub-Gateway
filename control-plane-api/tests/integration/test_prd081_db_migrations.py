"""PRD-081 0074 SIEM wire、聚合 outbox、限流和可逆迁移专项测试。"""

from __future__ import annotations

import json
import shutil
import sqlite3
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
NOW = "2026-08-15T01:02:01Z"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


def _identity(connection: sqlite3.Connection) -> dict[str, str]:
    row = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,provider.provider_id,model.model_id
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id AND user.role='admin'
        JOIN providers provider ON provider.tenant_id=tenant.tenant_id
        JOIN models model ON model.tenant_id=provider.tenant_id
                         AND model.provider_id=provider.provider_id
        WHERE tenant.slug='default'
        ORDER BY user.user_id,provider.provider_id,model.model_id LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return {
        "tenant_id": str(row[0]),
        "user_id": str(row[1]),
        "provider_id": str(row[2]),
        "model_id": str(row[3]),
    }


def _assert_healthy(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.fixture(scope="module")
def prd081_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """构建含历史 JSON delivery 的 seeded 0073 模板和正式 0074 迁移目录。"""

    root = tmp_path_factory.mktemp("prd081-migration-templates")
    migrations_0073 = _migration_subset(root / "migrations-0073", 73)
    migrations_0074 = _migration_subset(root / "migrations-0074", 74)
    database = root / "template-0073.sqlite3"
    database_url = _url(database)
    result = apply_migrations(database_url, migrations_dir=migrations_0073)
    assert len(result.applied) == 73 and result.applied[-1] == "0073"
    apply_seed_data(database_url)
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
              encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,max_attempts,
              created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,?,?,?,'enc_test','mhs_test','fingerprint_test','active',5000,3,?,?,?,?)
            """,
            (
                "whep_" + "1" * 32,
                identity["tenant_id"],
                "Historical JSON",
                "https://example.invalid/webhook",
                '["security.dlp_matched"]',
                identity["user_id"],
                identity["user_id"],
                NOW,
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
              event_timestamp,payload_json,status,attempt_count,max_attempts,
              delivery_kind,secret_version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'{}','pending',0,3,'original',1,?,?)
            """,
            (
                "whd_" + "2" * 32,
                identity["tenant_id"],
                "whep_" + "1" * 32,
                "evt_" + "3" * 32,
                "security.dlp_matched",
                NOW,
                NOW,
                NOW,
            ),
        )
        connection.commit()
    return {
        "database_0073": database,
        "migrations_0074": migrations_0074,
    }


def _clone(templates: dict[str, Path], destination: Path) -> str:
    shutil.copy2(templates["database_0073"], destination)
    return _url(destination)


def test_0074_backfills_history_and_roundtrips_cleanly(
    prd081_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """历史 Endpoint/delivery 升级为 JSON/60，空事实数据库可无损回退再升级。"""

    database_url = _clone(prd081_templates, tmp_path / "roundtrip.sqlite3")
    result = apply_migrations(
        database_url,
        migrations_dir=prd081_templates["migrations_0074"],
    )
    assert result.applied == ["0074"]
    with connect_database(database_url) as connection:
        endpoint = connection.execute(
            """SELECT payload_format,security_event_rate_limit_per_minute
                 FROM webhook_endpoints WHERE webhook_endpoint_id=?""",
            ("whep_" + "1" * 32,),
        ).fetchone()
        delivery = connection.execute(
            """SELECT payload_format,wire_body_text,payload_json,
                      security_event_rate_limit_per_minute
                 FROM webhook_deliveries WHERE webhook_delivery_id=?""",
            ("whd_" + "2" * 32,),
        ).fetchone()
        assert tuple(endpoint) == ("json", 60)
        assert tuple(delivery) == ("json", "{}", "{}", 60)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name LIKE '%prd081'"
            ).fetchone()[0]
            == 28
        )
        _assert_healthy(connection)

    down = rollback_last_migration(
        database_url,
        migrations_dir=prd081_templates["migrations_0074"],
        expected_version="0074",
    )
    assert down.rolled_back == "0074"
    with connect_database(database_url) as connection:
        assert (
            connection.execute(
                "SELECT version FROM schema_migrations WHERE status='applied' "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()[0]
            == "0073"
        )
        assert "payload_format" not in {
            str(row[1]) for row in connection.execute("PRAGMA table_info(webhook_endpoints)")
        }
        assert "wire_body_text" not in {
            str(row[1]) for row in connection.execute("PRAGMA table_info(webhook_deliveries)")
        }
        historical = connection.execute(
            "SELECT payload_json,status FROM webhook_deliveries WHERE webhook_delivery_id=?",
            ("whd_" + "2" * 32,),
        ).fetchone()
        assert tuple(historical) == ("{}", "pending")
        _assert_healthy(connection)

    assert apply_migrations(
        database_url,
        migrations_dir=prd081_templates["migrations_0074"],
    ).applied == ["0074"]


def test_dlp_sources_aggregate_and_late_generation_are_atomic(
    prd081_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """同窗口同组 DLP source 聚合；generation 被领取后迟到 source 新建下一代。"""

    database_url = _clone(prd081_templates, tmp_path / "dlp-aggregate.sqlite3")
    apply_migrations(database_url, migrations_dir=prd081_templates["migrations_0074"])
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        rule_id = "dlpr_" + "4" * 32
        connection.execute(
            """
            INSERT INTO dlp_rules(
              dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,action,status,
              priority,created_at,updated_at
            ) VALUES(?,?,'safe-name','keyword','not-evidence',?,'block','active',100,?,?)
            """,
            (rule_id, identity["tenant_id"], "5" * 64, NOW, NOW),
        )
        for marker, second in (("6", "01"), ("7", "02")):
            occurred_at = f"2026-08-15T01:02:{second}Z"
            connection.execute(
                """
                INSERT INTO dlp_matches(
                  dlp_match_id,tenant_id,request_id,trace_id,dlp_rule_id,rule_name,
                  rule_type,action,message_index,match_text_hash,match_length,created_at
                ) VALUES(?,?,?,?,?,'safe-name','keyword','block',0,?,8,?)
                """,
                (
                    "dlpm_" + marker * 32,
                    identity["tenant_id"],
                    f"req_prd081_{marker}",
                    f"trc_prd081_{marker}",
                    rule_id,
                    marker * 64,
                    occurred_at,
                ),
            )
        aggregate = connection.execute(
            """SELECT security_event_id,generation,event_count,source_event_count,
                      first_request_id,last_request_id
                 FROM security_event_aggregates"""
        ).fetchone()
        assert tuple(aggregate[1:]) == (0, 2, 2, "req_prd081_6", "req_prd081_7")
        connection.execute(
            """
            UPDATE security_event_outbox SET status='processing',attempt_count=1,
              lease_generation=1,lease_token='lease_prd081_token',
              lease_owner_id='worker_prd081',lease_expires_at='2026-08-15T01:03:00Z',
              snapshot_source_count=2,snapshot_endpoint_count=0,updated_at=?
            WHERE security_event_id=?
            """,
            ("2026-08-15T01:02:06Z", aggregate[0]),
        )
        connection.execute(
            """
            INSERT INTO dlp_matches(
              dlp_match_id,tenant_id,request_id,trace_id,dlp_rule_id,rule_name,
              rule_type,action,message_index,match_text_hash,match_length,created_at
            ) VALUES(?,?,?,?,?,'safe-name','keyword','block',0,?,8,
                     '2026-08-15T01:02:03Z')
            """,
            (
                "dlpm_" + "8" * 32,
                identity["tenant_id"],
                "req_prd081_8",
                "trc_prd081_8",
                rule_id,
                "8" * 64,
            ),
        )
        generations = connection.execute(
            """SELECT generation,event_count,source_event_count
                 FROM security_event_aggregates ORDER BY generation"""
        ).fetchall()
        assert [tuple(row) for row in generations] == [(0, 2, 2), (1, 1, 1)]
        assert connection.execute("SELECT COUNT(*) FROM security_event_sources").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM security_event_outbox").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="security_event_source_immutable"):
            connection.execute(
                "UPDATE security_event_sources SET occurrence_count=2 WHERE tenant_id=?",
                (identity["tenant_id"],),
            )
        _assert_healthy(connection)


def test_provider_anomaly_projection_is_minimal_and_blocks_down(
    prd081_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """decrypt_failed 只生成安全投影；存在事实时 down guard 不允许静默丢失。"""

    database_url = _clone(prd081_templates, tmp_path / "provider-anomaly.sqlite3")
    apply_migrations(database_url, migrations_dir=prd081_templates["migrations_0074"])
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        connection.execute(
            """
            INSERT INTO provider_key_anomaly_events(
              provider_key_anomaly_event_id,tenant_id,provider_id,model_id,anomaly_type,
              request_id,trace_id,candidate_count,occurred_at,created_at
            ) VALUES(?,?,?,?,'decrypt_failed','req_prd081_decrypt','trc_prd081_decrypt',
                     2,?,?)
            """,
            (
                "pkae_" + "9" * 32,
                identity["tenant_id"],
                identity["provider_id"],
                identity["model_id"],
                NOW,
                NOW,
            ),
        )
        source = connection.execute(
            """SELECT source_event_type,category,severity,severity_score,action,outcome,
                      resource_type,resource_id,occurrence_count
                 FROM security_event_sources"""
        ).fetchone()
        assert tuple(source) == (
            "security.provider_key_anomaly",
            "provider_credential",
            "critical",
            9,
            "decrypt_failed",
            "rejected",
            "provider",
            identity["provider_id"],
            2,
        )
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(provider_key_anomaly_events)")
        }
        assert not columns & {
            "provider_credential_id",
            "key_prefix",
            "encrypted_key",
            "error_message",
            "endpoint_url",
        }
        connection.commit()
    with pytest.raises(DbError) as failure:
        rollback_last_migration(
            database_url,
            migrations_dir=prd081_templates["migrations_0074"],
            expected_version="0074",
        )
    assert failure.value.code == "db.migration_failed"


def test_cef_delivery_replay_and_rate_claim_freeze_wire(
    prd081_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """CEF Endpoint、replay 与持久 rate slot 都复用来源 wire/rate，且不增加正文证据。"""

    database_url = _clone(prd081_templates, tmp_path / "cef-delivery.sqlite3")
    apply_migrations(database_url, migrations_dir=prd081_templates["migrations_0074"])
    with connect_database(database_url) as connection:
        identity = _identity(connection)
        endpoint_id = "whep_" + "a" * 32
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
              encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,max_attempts,
              created_by,updated_by,created_at,updated_at,payload_format,
              security_event_rate_limit_per_minute
            ) VALUES(?,?,?,?,?,'enc_test','mhs_test','fingerprint_cef','active',5000,3,
                     ?,?,?,?,'cef',1)
            """,
            (
                endpoint_id,
                identity["tenant_id"],
                "CEF SIEM",
                "https://example.invalid/siem",
                '["security.event_pushed"]',
                identity["user_id"],
                identity["user_id"],
                NOW,
                NOW,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="webhook_format_invalid"):
            connection.execute(
                "UPDATE webhook_endpoints SET event_types_json=? WHERE webhook_endpoint_id=?",
                ('["security.event_pushed","security.dlp_matched"]', endpoint_id),
            )

        envelope = {
            "event_id": "sevt_" + "b" * 32,
            "event_type": "security.event_pushed",
            "tenant_id": identity["tenant_id"],
        }
        payload_json = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
        wire = "CEF:0|MetaHub|AI Gateway|1|security.dlp_matched|DLP match|8|cnt=1"
        original_id = "whd_" + "c" * 32
        connection.execute(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
              event_timestamp,payload_json,status,attempt_count,max_attempts,delivery_kind,
              secret_version,created_at,updated_at,payload_format,wire_body_text,
              security_event_rate_limit_per_minute
            ) VALUES(?,?,?,?,?,? ,?,'pending',0,3,'original',1,?,?,'cef',?,1)
            """,
            (
                original_id,
                identity["tenant_id"],
                endpoint_id,
                envelope["event_id"],
                envelope["event_type"],
                NOW,
                payload_json,
                NOW,
                NOW,
                wire,
            ),
        )
        replay_id = "whd_" + "d" * 32
        connection.execute(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
              event_timestamp,payload_json,status,attempt_count,max_attempts,delivery_kind,
              replayed_from_delivery_id,replay_idempotency_key,replayed_by,replayed_at,
              secret_version,created_at,updated_at,payload_format,wire_body_text,
              security_event_rate_limit_per_minute
            ) VALUES(?,?,?,?,?,?,?,'pending',0,3,'replay',?,?,?,?,1,?,?,'cef',?,1)
            """,
            (
                replay_id,
                identity["tenant_id"],
                endpoint_id,
                envelope["event_id"],
                envelope["event_type"],
                NOW,
                payload_json,
                original_id,
                "idem-prd081",
                identity["user_id"],
                NOW,
                NOW,
                NOW,
                wire,
            ),
        )
        connection.execute(
            """
            INSERT INTO webhook_security_rate_claims(
              webhook_security_rate_claim_id,tenant_id,webhook_endpoint_id,
              webhook_delivery_id,bucket_started_at,bucket_ended_at,slot_number,
              rate_limit_per_minute,created_at,expires_at
            ) VALUES(?,?,?,?,?,?,1,1,?,?)
            """,
            (
                "wsrc_" + "c" * 32,
                identity["tenant_id"],
                endpoint_id,
                original_id,
                "2026-08-15T01:02:00Z",
                "2026-08-15T01:03:00Z",
                NOW,
                "2026-09-14T01:03:00Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="webhook_delivery_wire_immutable"):
            connection.execute(
                "UPDATE webhook_deliveries SET wire_body_text='CEF:0|changed' "
                "WHERE webhook_delivery_id=?",
                (original_id,),
            )
        replay = connection.execute(
            "SELECT payload_format,wire_body_text,security_event_rate_limit_per_minute "
            "FROM webhook_deliveries WHERE webhook_delivery_id=?",
            (replay_id,),
        ).fetchone()
        claim = connection.execute(
            "SELECT slot_number,rate_limit_per_minute FROM webhook_security_rate_claims"
        ).fetchone()
        assert tuple(replay) == ("cef", wire, 1)
        assert tuple(claim) == (1, 1)
        _assert_healthy(connection)
