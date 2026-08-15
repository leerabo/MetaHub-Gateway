"""PRD-081 标准安全事件 outbox、wire、限流与租约恢复专项。"""

from __future__ import annotations

import shutil
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from metahub_control.audit import AuditRequestMeta
from metahub_control.auth.models import AuthSession, AuthTenant, AuthUser
from metahub_control.auth.service import AuthContext
from metahub_control.security_events.outbox import (
    claim_next_security_event,
    dispatch_claimed_security_event,
)
from metahub_control.webhooks import service as webhook_service
from metahub_control.webhooks.outbox import claim_next_replay, process_claimed_replay
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import encrypt_secret, secret_fingerprint, secret_prefix
from metahub_shared.runtime.config import AppConfig, load_app_config
from metahub_shared.runtime.webhooks import verify_webhook_signature

ENCRYPTION_SECRET = "prd081-runtime-encryption-secret"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@pytest.fixture(scope="module")
def prd081_runtime_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """只迁移和 seed 一次；每个状态机用例复制独立 SQLite 文件。"""

    database = tmp_path_factory.mktemp("prd081-runtime-template") / "template.sqlite3"
    database_url = _url(database)
    result = apply_migrations(database_url)
    assert len(result.applied) == 74 and result.applied[-1] == "0074"
    apply_seed_data(database_url)
    return database


def _clone(template: Path, destination: Path) -> str:
    shutil.copy2(template, destination)
    return _url(destination)


def _config(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
) -> AppConfig:
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", ENCRYPTION_SECRET)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd081-runtime-auth-secret")
    return load_app_config("control-plane-api")


def _identity(database_url: str) -> dict[str, str]:
    with connect_database(database_url) as connection:
        row = connection.execute(
            """
            SELECT tenant.tenant_id,user.user_id
            FROM tenants tenant
            JOIN users user ON user.tenant_id=tenant.tenant_id AND user.role='admin'
            WHERE tenant.slug='default'
            ORDER BY user.user_id LIMIT 1
            """
        ).fetchone()
    assert row is not None
    return {"tenant_id": str(row[0]), "user_id": str(row[1])}


def _insert_endpoint(
    database_url: str,
    identity: dict[str, str],
    *,
    marker: str,
    endpoint_url: str,
    payload_format: str,
    signing_secret: str,
    rate_limit: int = 60,
    created_at: str = "2026-08-15T01:01:00Z",
) -> str:
    endpoint_id = "wh_" + marker * 32
    with connect_database(database_url) as connection:
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
              encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
              max_attempts,created_by,updated_by,created_at,updated_at,payload_format,
              security_event_rate_limit_per_minute
            ) VALUES(?,?,?,?,?, ?,?,?,'active',1000,3,?,?,?,?,?,?)
            """,
            (
                endpoint_id,
                identity["tenant_id"],
                f"SIEM {marker}",
                endpoint_url,
                '["security.event_pushed"]',
                encrypt_secret(signing_secret, encryption_secret=ENCRYPTION_SECRET),
                secret_prefix(signing_secret),
                secret_fingerprint(signing_secret),
                identity["user_id"],
                identity["user_id"],
                created_at,
                created_at,
                payload_format,
                rate_limit,
            ),
        )
        connection.commit()
    return endpoint_id


def _insert_dlp_match(
    database_url: str,
    identity: dict[str, str],
    *,
    marker: str,
    occurred_at: str,
) -> str:
    match_id = "dlpm_" + marker * 32
    rule_id = "dlpr_" + "f" * 32
    with connect_database(database_url) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO dlp_rules(
              dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,action,status,
              priority,created_at,updated_at
            ) VALUES(?,?,'runtime-rule','keyword','not-evidence',?,'block','active',
                     100,'2026-08-15T01:00:00Z','2026-08-15T01:00:00Z')
            """,
            (rule_id, identity["tenant_id"], "e" * 64),
        )
        connection.execute(
            """
            INSERT INTO dlp_matches(
              dlp_match_id,tenant_id,request_id,trace_id,dlp_rule_id,rule_name,
              rule_type,action,message_index,match_text_hash,match_length,created_at
            ) VALUES(?,?,?,?,?,'runtime-rule','keyword','block',0,?,8,?)
            """,
            (
                match_id,
                identity["tenant_id"],
                f"req_prd081_{marker}",
                f"trc_prd081_{marker}",
                rule_id,
                marker * 64,
                occurred_at,
            ),
        )
        connection.commit()
    return match_id


class _ReceiverHandler(BaseHTTPRequestHandler):
    server: _Receiver

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.received.append(
            {
                "path": self.path,
                "body": body,
                "content_type": self.headers.get("Content-Type"),
                "event": self.headers.get("X-MetaHub-Event"),
                "delivery": self.headers.get("X-MetaHub-Delivery"),
                "signature": self.headers.get("X-MetaHub-Signature", ""),
            }
        )
        self.send_response(self.server.status_by_path.get(self.path, 204))
        self.end_headers()

    def log_message(self, _format: str, *args: Any) -> None:
        return None


class _Receiver(ThreadingHTTPServer):
    received: list[dict[str, Any]]
    status_by_path: dict[str, int]


class _RunningReceiver:
    def __enter__(self) -> _Receiver:
        self.server = _Receiver(("127.0.0.1", 0), _ReceiverHandler)
        self.server.received = []
        self.server.status_by_path = {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _receiver_url(receiver: _Receiver, path: str) -> str:
    host, port = receiver.server_address
    return f"http://{host}:{port}{path}"


def _claim_and_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    *,
    worker_id: str,
    at: str,
) -> bool:
    current = _time(at)
    monkeypatch.setattr(webhook_service, "_utcnow", lambda: current)
    claim = claim_next_security_event(config, worker_id=worker_id, now=current)
    assert claim is not None
    return dispatch_claimed_security_event(config, claim, now=current)


def test_json_and_cef_snapshot_send_exact_wire_headers_and_hmac(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prd081_runtime_template: Path,
) -> None:
    """同一聚合按 Endpoint 冻结 JSON/CEF，并对各自实际字节签名。"""

    database_url = _clone(prd081_runtime_template, tmp_path / "json-cef.sqlite3")
    config = _config(monkeypatch, database_url)
    identity = _identity(database_url)
    secrets = {"/json": "whsec_prd081_json", "/cef": "whsec_prd081_cef"}
    with _RunningReceiver() as receiver:
        _insert_endpoint(
            database_url,
            identity,
            marker="1",
            endpoint_url=_receiver_url(receiver, "/json"),
            payload_format="json",
            signing_secret=secrets["/json"],
        )
        _insert_endpoint(
            database_url,
            identity,
            marker="2",
            endpoint_url=_receiver_url(receiver, "/cef"),
            payload_format="cef",
            signing_secret=secrets["/cef"],
        )
        _insert_dlp_match(
            database_url,
            identity,
            marker="3",
            occurred_at="2026-08-15T01:02:01Z",
        )
        assert _claim_and_dispatch(
            monkeypatch,
            config,
            worker_id="worker-prd081-wire",
            at="2026-08-15T01:02:05Z",
        )

    assert len(receiver.received) == 2
    records = {str(item["path"]): item for item in receiver.received}
    json_record = records["/json"]
    cef_record = records["/cef"]
    assert json_record["content_type"] == "application/json"
    assert cef_record["content_type"] == "text/plain; charset=utf-8"
    assert json_record["event"] == cef_record["event"] == "security.event_pushed"
    assert json_record["body"].startswith(b'{"actor_id":"security-event-aggregator"')
    assert cef_record["body"].startswith(
        b"CEF:0|MetaHub|AI Gateway|1|security.dlp_matched|DLP match|8|"
    )
    assert b"\r" not in cef_record["body"] and b"\n" not in cef_record["body"]
    for path, record in records.items():
        assert verify_webhook_signature(
            str(record["signature"]),
            secrets[path],
            record["body"],
        )

    with connect_database(database_url) as connection:
        outbox = connection.execute(
            """SELECT status,attempt_count,snapshot_source_count,snapshot_endpoint_count
                 FROM security_event_outbox"""
        ).fetchone()
        deliveries = connection.execute(
            """SELECT payload_format,status,attempt_count,wire_body_text
                 FROM webhook_deliveries ORDER BY payload_format"""
        ).fetchall()
        claims = connection.execute(
            "SELECT COUNT(*) FROM webhook_security_rate_claims"
        ).fetchone()[0]
    assert tuple(outbox) == ("succeeded", 0, 1, 2)
    assert [(row[0], row[1], row[2]) for row in deliveries] == [
        ("cef", "success", 1),
        ("json", "success", 1),
    ]
    assert claims == 2
    assert {row[3].encode("utf-8") for row in deliveries} == {
        json_record["body"],
        cef_record["body"],
    }


def test_rate_deferral_is_zero_attempt_and_snapshot_excludes_late_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prd081_runtime_template: Path,
) -> None:
    """第二个聚合满额时仅延期；下一分钟只恢复首次冻结的 Endpoint。"""

    database_url = _clone(prd081_runtime_template, tmp_path / "rate-resume.sqlite3")
    config = _config(monkeypatch, database_url)
    identity = _identity(database_url)
    with _RunningReceiver() as receiver:
        first_endpoint = _insert_endpoint(
            database_url,
            identity,
            marker="4",
            endpoint_url=_receiver_url(receiver, "/original"),
            payload_format="json",
            signing_secret="whsec_prd081_rate",
            rate_limit=1,
        )
        _insert_dlp_match(
            database_url,
            identity,
            marker="5",
            occurred_at="2026-08-15T01:02:01Z",
        )
        assert _claim_and_dispatch(
            monkeypatch,
            config,
            worker_id="worker-prd081-rate-first",
            at="2026-08-15T01:02:05Z",
        )
        _insert_dlp_match(
            database_url,
            identity,
            marker="6",
            occurred_at="2026-08-15T01:02:06Z",
        )
        assert _claim_and_dispatch(
            monkeypatch,
            config,
            worker_id="worker-prd081-rate-defer",
            at="2026-08-15T01:02:10Z",
        )
        assert len(receiver.received) == 1

        with connect_database(database_url) as connection:
            pending = connection.execute(
                """
                SELECT outbox.security_event_id,outbox.status,outbox.attempt_count,
                       outbox.next_attempt_at,outbox.snapshot_endpoint_count,
                       delivery.webhook_delivery_id,delivery.attempt_count,
                       delivery.next_retry_at
                FROM security_event_outbox outbox
                JOIN webhook_deliveries delivery
                  ON delivery.tenant_id=outbox.tenant_id
                 AND delivery.event_id=outbox.security_event_id
                WHERE outbox.status='pending'
                """
            ).fetchone()
        assert pending is not None
        assert tuple(pending[1:5]) == (
            "pending",
            0,
            "2026-08-15T01:03:00Z",
            1,
        )
        assert tuple(pending[6:]) == (0, "2026-08-15T01:03:00Z")

        late_endpoint = _insert_endpoint(
            database_url,
            identity,
            marker="7",
            endpoint_url=_receiver_url(receiver, "/late"),
            payload_format="cef",
            signing_secret="whsec_prd081_late",
            rate_limit=600,
            created_at="2026-08-15T01:02:20Z",
        )
        assert _claim_and_dispatch(
            monkeypatch,
            config,
            worker_id="worker-prd081-rate-resume",
            at="2026-08-15T01:03:00Z",
        )

    assert [item["path"] for item in receiver.received] == ["/original", "/original"]
    with connect_database(database_url) as connection:
        resumed = connection.execute(
            """SELECT status,attempt_count,snapshot_endpoint_count
                 FROM security_event_outbox WHERE security_event_id=?""",
            (pending[0],),
        ).fetchone()
        delivery = connection.execute(
            """SELECT webhook_endpoint_id,status,attempt_count,next_retry_at
                 FROM webhook_deliveries WHERE webhook_delivery_id=?""",
            (pending[5],),
        ).fetchone()
        late_count = connection.execute(
            "SELECT COUNT(*) FROM webhook_deliveries WHERE webhook_endpoint_id=?",
            (late_endpoint,),
        ).fetchone()[0]
    assert tuple(resumed) == ("succeeded", 0, 1)
    assert tuple(delivery) == (first_endpoint, "success", 1, None)
    assert late_count == 0


def test_expired_lease_takeover_blocks_stale_worker_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prd081_runtime_template: Path,
) -> None:
    """过期 lease 可提升 generation；旧 token 在任何网络前失效。"""

    database_url = _clone(prd081_runtime_template, tmp_path / "lease-takeover.sqlite3")
    config = _config(monkeypatch, database_url)
    identity = _identity(database_url)
    with _RunningReceiver() as receiver:
        _insert_endpoint(
            database_url,
            identity,
            marker="8",
            endpoint_url=_receiver_url(receiver, "/lease"),
            payload_format="json",
            signing_secret="whsec_prd081_lease",
        )
        _insert_dlp_match(
            database_url,
            identity,
            marker="9",
            occurred_at="2026-08-15T01:02:01Z",
        )
        first = claim_next_security_event(
            config,
            worker_id="worker-prd081-stale",
            now=_time("2026-08-15T01:02:05Z"),
        )
        assert first is not None
        assert claim_next_security_event(
            config,
            worker_id="worker-prd081-too-early",
            now=_time("2026-08-15T01:03:34Z"),
        ) is None
        takeover = claim_next_security_event(
            config,
            worker_id="worker-prd081-takeover",
            now=first.lease_expires_at,
        )
        assert takeover is not None
        assert takeover.lease_generation == first.lease_generation + 1
        monkeypatch.setattr(webhook_service, "_utcnow", lambda: first.lease_expires_at)
        assert dispatch_claimed_security_event(
            config,
            first,
            now=first.lease_expires_at,
        ) is False
        assert receiver.received == []
        assert dispatch_claimed_security_event(
            config,
            takeover,
            now=first.lease_expires_at,
        )

    assert len(receiver.received) == 1
    with connect_database(database_url) as connection:
        row = connection.execute(
            "SELECT status,lease_generation,attempt_count FROM security_event_outbox"
        ).fetchone()
    assert tuple(row) == ("succeeded", 2, 0)


def test_empty_snapshot_is_terminal_and_never_absorbs_later_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prd081_runtime_template: Path,
) -> None:
    """零订阅也冻结明确 marker 并成功，后来 Endpoint 不得补入历史事件。"""

    database_url = _clone(prd081_runtime_template, tmp_path / "empty-snapshot.sqlite3")
    config = _config(monkeypatch, database_url)
    identity = _identity(database_url)
    _insert_dlp_match(
        database_url,
        identity,
        marker="a",
        occurred_at="2026-08-15T01:02:01Z",
    )
    assert _claim_and_dispatch(
        monkeypatch,
        config,
        worker_id="worker-prd081-empty",
        at="2026-08-15T01:02:05Z",
    )
    _insert_endpoint(
        database_url,
        identity,
        marker="b",
        endpoint_url="https://example.invalid/late-siem",
        payload_format="json",
        signing_secret="whsec_prd081_empty_late",
    )
    assert claim_next_security_event(
        config,
        worker_id="worker-prd081-empty-late",
        now=_time("2026-08-15T01:03:00Z"),
    ) is None
    with connect_database(database_url) as connection:
        outbox = connection.execute(
            """SELECT status,snapshot_source_count,snapshot_endpoint_count
                 FROM security_event_outbox"""
        ).fetchone()
        deliveries = connection.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0]
    assert tuple(outbox) == ("succeeded", 1, 0)
    assert deliveries == 0


def test_corrupt_member_isolated_as_deterministic_payload_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prd081_runtime_template: Path,
) -> None:
    """已提交坏 member 不进入瞬时重试；固定码终结且不生成网络 intent。"""

    database_url = _clone(prd081_runtime_template, tmp_path / "payload-invalid.sqlite3")
    config = _config(monkeypatch, database_url)
    identity = _identity(database_url)
    _insert_dlp_match(
        database_url,
        identity,
        marker="c",
        occurred_at="2026-08-15T01:02:01Z",
    )
    with connect_database(database_url) as connection:
        connection.execute("DROP TRIGGER trg_security_event_sources_update_prd081")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE security_event_sources SET occurrence_count=0")
        connection.execute("PRAGMA ignore_check_constraints=OFF")
        connection.commit()
    assert _claim_and_dispatch(
        monkeypatch,
        config,
        worker_id="worker-prd081-payload-invalid",
        at="2026-08-15T01:02:05Z",
    )
    with connect_database(database_url) as connection:
        outbox = connection.execute(
            """SELECT status,attempt_count,last_error_code,snapshot_endpoint_count
                 FROM security_event_outbox"""
        ).fetchone()
        deliveries = connection.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0]
    assert tuple(outbox) == ("failed", 1, "webhook.payload_invalid", None)
    assert deliveries == 0


def test_replay_rate_deferral_preserves_root_and_recovers_next_minute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prd081_runtime_template: Path,
) -> None:
    """安全 replay 满额时零网络延期，并在下一分钟使用冻结 wire 收敛。"""

    database_url = _clone(prd081_runtime_template, tmp_path / "replay-rate.sqlite3")
    config = _config(monkeypatch, database_url)
    identity = _identity(database_url)
    first_minute = _time("2026-08-15T01:02:05Z")
    next_minute = _time("2026-08-15T01:03:00Z")
    with _RunningReceiver() as receiver:
        receiver.status_by_path["/replay"] = 500
        _insert_endpoint(
            database_url,
            identity,
            marker="d",
            endpoint_url=_receiver_url(receiver, "/replay"),
            payload_format="json",
            signing_secret="whsec_prd081_replay",
            rate_limit=1,
        )
        _insert_dlp_match(
            database_url,
            identity,
            marker="e",
            occurred_at="2026-08-15T01:02:01Z",
        )
        assert _claim_and_dispatch(
            monkeypatch,
            config,
            worker_id="worker-prd081-replay-source",
            at="2026-08-15T01:02:05Z",
        )
        assert len(receiver.received) == 3

        with connect_database(database_url) as connection:
            source = connection.execute(
                """SELECT webhook_delivery_id FROM webhook_deliveries
                     WHERE delivery_kind='original' AND status='failed'"""
            ).fetchone()
            user = connection.execute(
                """SELECT user.email,user.display_name,user.role,tenant.slug,tenant.name
                     FROM users user JOIN tenants tenant ON tenant.tenant_id=user.tenant_id
                     WHERE user.tenant_id=? AND user.user_id=?""",
                (identity["tenant_id"], identity["user_id"]),
            ).fetchone()
        assert source is not None and user is not None
        context = AuthContext(
            user=AuthUser(
                user_id=identity["user_id"],
                tenant_id=identity["tenant_id"],
                email=str(user[0]),
                display_name=str(user[1]),
                role=str(user[2]),
            ),
            tenant=AuthTenant(
                tenant_id=identity["tenant_id"],
                slug=str(user[3]),
                name=str(user[4]),
            ),
            session=AuthSession(
                session_id="ses_prd081_replay",
                expires_at=_time("2026-08-16T00:00:00Z"),
            ),
            token="test-token-never-persisted",
            token_jti="jti_prd081_replay",
        )
        monkeypatch.setattr(webhook_service, "_utcnow", lambda: first_minute)
        intent = webhook_service._create_replay_intent(
            config,
            context,
            str(source[0]),
            "prd081-replay-rate",
            expected_endpoint_id=None,
            request_id="req_prd081_replay",
            trace_id="trc_prd081_replay",
            audit_request_meta=AuditRequestMeta(
                client_ip="127.0.0.1",
                user_agent="pytest",
            ),
        )
        claim = claim_next_replay(
            config,
            worker_id="worker-prd081-replay-defer",
            now=first_minute,
            dead_letter_event_id=intent.dead_letter_event_id,
            replay_delivery_id=intent.replay_delivery_id,
        )
        assert claim is not None
        assert process_claimed_replay(config, claim, now=first_minute)
        assert len(receiver.received) == 3

        with connect_database(database_url) as connection:
            deferred_dead = connection.execute(
                """SELECT status,replay_dispatch_attempts,replay_next_attempt_at,
                          replay_claim_token,replay_lease_owner_id,replay_lease_expires_at
                     FROM webhook_dead_letter_events WHERE dead_letter_event_id=?""",
                (intent.dead_letter_event_id,),
            ).fetchone()
            deferred_delivery = connection.execute(
                """SELECT status,attempt_count,next_retry_at,payload_format,wire_body_text
                     FROM webhook_deliveries WHERE webhook_delivery_id=?""",
                (intent.replay_delivery_id,),
            ).fetchone()
            source_wire = connection.execute(
                """SELECT payload_format,wire_body_text
                     FROM webhook_deliveries WHERE webhook_delivery_id=?""",
                (source[0],),
            ).fetchone()
        assert tuple(deferred_dead) == (
            "replay_pending",
            0,
            "2026-08-15T01:03:00Z",
            None,
            None,
            None,
        )
        assert tuple(deferred_delivery[:3]) == (
            "pending",
            0,
            "2026-08-15T01:03:00Z",
        )
        assert tuple(deferred_delivery[3:]) == tuple(source_wire)

        receiver.status_by_path["/replay"] = 204
        monkeypatch.setattr(webhook_service, "_utcnow", lambda: next_minute)
        resumed = claim_next_replay(
            config,
            worker_id="worker-prd081-replay-resume",
            now=next_minute,
        )
        assert resumed is not None
        assert process_claimed_replay(config, resumed, now=next_minute)

    assert len(receiver.received) == 4
    with connect_database(database_url) as connection:
        terminal_dead = connection.execute(
            """SELECT status,replay_dispatch_attempts,replay_next_attempt_at,replayed_at
                 FROM webhook_dead_letter_events WHERE dead_letter_event_id=?""",
            (intent.dead_letter_event_id,),
        ).fetchone()
        terminal_delivery = connection.execute(
            """SELECT status,attempt_count,next_retry_at
                 FROM webhook_deliveries WHERE webhook_delivery_id=?""",
            (intent.replay_delivery_id,),
        ).fetchone()
    assert terminal_dead[0:3] == ("replayed", 1, None)
    assert terminal_dead[3] == "2026-08-15T01:03:00Z"
    assert tuple(terminal_delivery) == ("success", 1, None)
