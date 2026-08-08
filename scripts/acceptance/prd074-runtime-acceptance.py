"""Self-contained runtime acceptance for PRD-074 Batch Scheduler and Worker.

The runner creates an isolated SQLite database and all runtime artifacts under the repository,
starts only processes it owns, and uses fixed loopback ports. Provider and webhook request bodies
are handled only in local variables and are never retained or logged. The published evidence is a
small status/count projection and always keeps ``all_108_prds_complete`` false.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

# Direct execution must not depend on a caller-provided PYTHONPATH. Only repository source roots
# are added, before any MetaHub module is imported.
_BOOTSTRAP_ROOT = Path(__file__).resolve().parents[2]
for _source_root in (
    _BOOTSTRAP_ROOT / "shared-sdk" / "python",
    _BOOTSTRAP_ROOT / "gateway-data-plane" / "src",
    _BOOTSTRAP_ROOT / "control-plane-api" / "src",
):
    sys.path.insert(0, str(_source_root))

from metahub_shared.db import (  # noqa: E402
    apply_migrations,
    apply_seed_data,
    connect_database,
)
from metahub_shared.db.security import hash_password  # noqa: E402
from metahub_shared.rbac import permission_matrix_sha256  # noqa: E402
from metahub_shared.runtime.webhooks import verify_webhook_signature  # noqa: E402

ROOT = _BOOTSTRAP_ROOT
WORK_ROOT = ROOT / ".tmp" / "prd074-runtime"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd074-runtime"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
LOOPBACK_HOST = "127.0.0.1"
GATEWAY_PORT = 18740
CONTROL_PORT = 18741
PROVIDER_PORT = 18742
WEBHOOK_PORT = 18743
FIXED_PORTS = (GATEWAY_PORT, CONTROL_PORT, PROVIDER_PORT, WEBHOOK_PORT)
MAX_STUB_BODY_BYTES = 512 * 1024
MIGRATION_CONTRACT = (67, "0067")


class AcceptanceFailure(RuntimeError):
    """A stable failure coordinate that never embeds response bodies or secrets."""

    def __init__(self, stage: str, code: str) -> None:
        super().__init__(f"{stage}:{code}")
        self.stage = stage
        self.code = code


def require(condition: bool, stage: str, code: str) -> None:
    if not condition:
        raise AcceptanceFailure(stage, code)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def workspace_path(value: str | Path, boundary: str | Path, *, stage: str) -> Path:
    """Resolve a path and require both it and its boundary to remain under this repository."""

    candidate = Path(value).resolve()
    allowed = Path(boundary).resolve()
    root = ROOT.resolve()
    try:
        candidate.relative_to(allowed)
        allowed.relative_to(root)
    except ValueError:
        raise AcceptanceFailure(stage, "path_outside_workspace") from None
    return candidate


def loopback_url(value: str, *, port: int, stage: str, path: str = "") -> str:
    """Accept one exact HTTP loopback origin and an optional fixed path."""

    try:
        parsed = urlsplit(value)
        actual_port = parsed.port
    except ValueError:
        raise AcceptanceFailure(stage, "url_invalid") from None
    valid = (
        parsed.scheme == "http"
        and parsed.hostname == LOOPBACK_HOST
        and actual_port == port
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path == path
    )
    require(valid, stage, "url_not_fixed_loopback")
    return value


def safe_run_id(value: str | None) -> str:
    candidate = value or datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ")
    normalized = candidate.replace("-", "").replace("_", "")
    require(
        3 <= len(candidate) <= 80 and normalized.isascii() and normalized.isalnum(),
        "config.run_id",
        "invalid",
    )
    return candidate


def create_run_directories(run_id: str) -> tuple[Path, Path]:
    """Create exclusive work/evidence directories without overwriting an earlier run."""

    for parent in (WORK_ROOT.parent, ARTIFACT_ROOT.parent):
        require(parent.is_dir() and not parent.is_symlink(), "config.parent", "invalid")
    WORK_ROOT.mkdir(exist_ok=True)
    ARTIFACT_ROOT.mkdir(exist_ok=True)
    require(not WORK_ROOT.is_symlink(), "config.work_root", "invalid")
    require(not ARTIFACT_ROOT.is_symlink(), "config.artifact_root", "invalid")
    work = workspace_path(WORK_ROOT / run_id, WORK_ROOT, stage="config.work")
    artifact = workspace_path(ARTIFACT_ROOT / run_id, ARTIFACT_ROOT, stage="config.artifact")
    require(not work.exists() and not artifact.exists(), "config.run_directory", "exists")
    work.mkdir()
    artifact.mkdir()
    for name in ("logs", "temp", "storage", "spool"):
        (work / name).mkdir()
    return work, artifact


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    admin_password: str = field(repr=False)
    developer_password: str = field(repr=False)
    provider_key: str = field(repr=False)
    virtual_key: str = field(repr=False)
    auth_secret: str = field(repr=False)
    cache_secret: str = field(repr=False)
    internal_chat_token: str = field(repr=False)
    internal_batch_token: str = field(repr=False)
    batch_secret: str = field(repr=False)
    file_secret: str = field(repr=False)
    prompt_secret: str = field(repr=False)
    provider_encryption_secret: str = field(repr=False)
    realtime_text: str = field(repr=False)
    realtime_answer: str = field(repr=False)
    file_texts: tuple[str, ...] = field(repr=False)
    file_names: tuple[str, ...] = field(repr=False)
    job_names: tuple[str, ...] = field(repr=False)
    assistant_text: str = field(repr=False)

    @classmethod
    def create(cls) -> RuntimeSecrets:
        suffix = secrets.token_urlsafe(18)
        return cls(
            admin_password=f"Prd074-Admin-Aa1!{secrets.token_urlsafe(24)}",
            developer_password=f"Prd074-Developer-Aa1!{secrets.token_urlsafe(24)}",
            provider_key=f"sk-prd074-{secrets.token_urlsafe(32)}",
            virtual_key=f"mhv_prd074_{secrets.token_urlsafe(32)}",
            auth_secret=f"auth-prd074-{secrets.token_urlsafe(40)}",
            cache_secret=f"cache-prd074-{secrets.token_urlsafe(40)}",
            internal_chat_token=f"chat-prd074-{secrets.token_urlsafe(40)}",
            internal_batch_token=f"batch-token-prd074-{secrets.token_urlsafe(40)}",
            batch_secret=f"batch-crypto-prd074-{secrets.token_urlsafe(40)}",
            file_secret=f"file-crypto-prd074-{secrets.token_urlsafe(40)}",
            prompt_secret=f"prompt-crypto-prd074-{secrets.token_urlsafe(40)}",
            provider_encryption_secret=f"provider-crypto-prd074-{secrets.token_urlsafe(40)}",
            realtime_text=f"PRD074_REALTIME_INPUT_{suffix}",
            realtime_answer=f"PRD074_REALTIME_OUTPUT_{secrets.token_urlsafe(18)}",
            file_texts=tuple(
                f"PRD074_BATCH_FILE_{index}_{secrets.token_urlsafe(24)}" for index in range(4)
            ),
            file_names=tuple(
                f"prd074-private-{index}-{secrets.token_urlsafe(12)}.txt" for index in range(4)
            ),
            job_names=tuple(
                f"PRD074 private job {index} {secrets.token_urlsafe(16)}" for index in range(4)
            ),
            assistant_text=f"PRD074_BATCH_RESULT_{secrets.token_urlsafe(24)}",
        )

    def private_values(
        self,
        *,
        session_tokens: tuple[str, ...] = (),
        webhook_secret: str | None = None,
    ) -> tuple[str, ...]:
        values = (
            self.admin_password,
            self.developer_password,
            self.provider_key,
            self.virtual_key,
            self.auth_secret,
            self.cache_secret,
            self.internal_chat_token,
            self.internal_batch_token,
            self.batch_secret,
            self.file_secret,
            self.prompt_secret,
            self.provider_encryption_secret,
            self.realtime_text,
            self.realtime_answer,
            *self.file_texts,
            *self.file_names,
            *self.job_names,
            self.assistant_text,
            *session_tokens,
        )
        return values + ((webhook_secret,) if webhook_secret else ())


@dataclass(frozen=True, slots=True)
class RuntimeGraph:
    database_url: str = field(repr=False)
    database: Path = field(repr=False)
    tenant_id: str
    developer_user_id: str
    developer_email: str
    project_id: str
    environment_id: str
    model_alias_id: str
    model_alias: str
    virtual_key_id: str


@dataclass(slots=True)
class ManagedProcess:
    name: str
    process: subprocess.Popen[bytes]
    stdout_file: Any = field(repr=False)
    stderr_file: Any = field(repr=False)


class OwnedProcessSet:
    """Tracks exact Popen handles; cleanup never enumerates or kills unrelated listeners."""

    def __init__(self) -> None:
        self.items: list[ManagedProcess] = []

    def add(self, item: ManagedProcess) -> ManagedProcess:
        self.items.append(item)
        return item

    def stop(self, item: ManagedProcess) -> None:
        stop_owned_process(item)
        if item in self.items:
            self.items.remove(item)

    def close(self) -> None:
        while self.items:
            stop_owned_process(self.items.pop())


class FixedThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def _read_stub_body(handler: BaseHTTPRequestHandler) -> bytes:
    values = handler.headers.get_all("Content-Length", failobj=[])
    if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
        raise ValueError("length")
    length = int(values[0])
    if not 0 < length <= MAX_STUB_BODY_BYTES:
        raise ValueError("length")
    body = handler.rfile.read(length)
    if len(body) != length:
        raise ValueError("length")
    return body


def _send_stub_json(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


class ProviderStub:
    """Fixed-port OpenAI-compatible stub retaining counters and synchronization only."""

    def __init__(self, secrets_: RuntimeSecrets) -> None:
        self._secrets = secrets_
        self._lock = threading.Lock()
        self.realtime_seen = threading.Event()
        self.release_realtime = threading.Event()
        self.realtime_calls = 0
        self.batch_calls = 0
        self.invalid_calls = 0
        self.active_calls = 0
        self.max_active_calls = 0
        self.server: FixedThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> str:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                try:
                    body = _read_stub_body(self)
                    payload = json.loads(body.decode("utf-8"))
                    messages = payload.get("messages") if isinstance(payload, dict) else None
                    contents = (
                        [
                            item.get("content")
                            for item in messages
                            if isinstance(item, dict) and isinstance(item.get("content"), str)
                        ]
                        if isinstance(messages, list)
                        else []
                    )
                    valid = (
                        self.path == "/v1/chat/completions"
                        and self.headers.get("Authorization")
                        == f"Bearer {owner._secrets.provider_key}"
                        and isinstance(payload, dict)
                        and payload.get("stream") is False
                        and payload.get("model") == "local-chat-model"
                        and bool(contents)
                    )
                    if not valid:
                        raise ValueError("invalid")
                    realtime = owner._secrets.realtime_text in contents
                    del body, payload, messages, contents
                except (ValueError, UnicodeError, json.JSONDecodeError):
                    with owner._lock:
                        owner.invalid_calls += 1
                    _send_stub_json(self, 400, {"error": {"message": "invalid request"}})
                    return
                with owner._lock:
                    owner.active_calls += 1
                    owner.max_active_calls = max(owner.max_active_calls, owner.active_calls)
                    if realtime:
                        owner.realtime_calls += 1
                    else:
                        owner.batch_calls += 1
                try:
                    if realtime:
                        owner.realtime_seen.set()
                        if not owner.release_realtime.wait(timeout=45):
                            _send_stub_json(
                                self,
                                503,
                                {
                                    "error": {
                                        "message": "runtime gate timeout",
                                        "type": "server_error",
                                    }
                                },
                            )
                            return
                        answer = owner._secrets.realtime_answer
                    else:
                        answer = owner._secrets.assistant_text
                    _send_stub_json(
                        self,
                        200,
                        {
                            "id": "chatcmpl_prd074_runtime",
                            "object": "chat.completion",
                            "model": "local-chat-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": answer},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 19,
                                "completion_tokens": 7,
                                "total_tokens": 26,
                                "prompt_tokens_details": {"cached_tokens": 0},
                            },
                        },
                    )
                finally:
                    with owner._lock:
                        owner.active_calls -= 1

            def log_message(self, _format: str, *_args: object) -> None:
                return

        try:
            self.server = FixedThreadingHTTPServer((LOOPBACK_HOST, PROVIDER_PORT), Handler)
        except OSError:
            raise AcceptanceFailure("stub.provider", "port_unavailable") from None
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="prd074-provider-stub",
            daemon=True,
        )
        self.thread.start()
        return loopback_url(
            f"http://{LOOPBACK_HOST}:{PROVIDER_PORT}/v1",
            port=PROVIDER_PORT,
            path="/v1",
            stage="stub.provider_url",
        )

    def stop(self) -> None:
        self.release_realtime.set()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "realtime_calls": self.realtime_calls,
                "batch_calls": self.batch_calls,
                "invalid_calls": self.invalid_calls,
                "max_concurrency": self.max_active_calls,
            }


class WebhookStub:
    """Webhook receiver retaining event-type counts and validation booleans, never payloads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._signing_secret: str | None = None
        self.created_count = 0
        self.completed_count = 0
        self.invalid_count = 0
        self.signature_failures = 0
        self.forbidden_field_hits = 0
        self.server: FixedThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def set_signing_secret(self, value: str) -> None:
        require(bool(value), "stub.webhook", "secret_missing")
        self._signing_secret = value

    def start(self) -> str:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                try:
                    body = _read_stub_body(self)
                    payload = json.loads(body.decode("utf-8"))
                    event_type = payload.get("event_type") if isinstance(payload, dict) else None
                    forbidden = _contains_forbidden_webhook_field(payload)
                    secret = owner._signing_secret
                    signature_ok = bool(secret) and verify_webhook_signature(
                        self.headers.get("X-MetaHub-Signature", ""),
                        str(secret),
                        body,
                    )
                    header_matches = self.headers.get("X-MetaHub-Event") == event_type
                    del body, payload
                except (ValueError, UnicodeError, json.JSONDecodeError):
                    with owner._lock:
                        owner.invalid_count += 1
                    _send_stub_json(self, 400, {"ok": False})
                    return
                with owner._lock:
                    if not signature_ok:
                        owner.signature_failures += 1
                    if forbidden:
                        owner.forbidden_field_hits += 1
                    if event_type == "batch_job.created":
                        owner.created_count += 1
                    elif event_type == "batch_job.completed":
                        owner.completed_count += 1
                    else:
                        owner.invalid_count += 1
                    valid = (
                        signature_ok
                        and header_matches
                        and not forbidden
                        and event_type
                        in {
                            "batch_job.created",
                            "batch_job.completed",
                        }
                    )
                _send_stub_json(self, 204 if valid else 400, {})

            def log_message(self, _format: str, *_args: object) -> None:
                return

        try:
            self.server = FixedThreadingHTTPServer((LOOPBACK_HOST, WEBHOOK_PORT), Handler)
        except OSError:
            raise AcceptanceFailure("stub.webhook", "port_unavailable") from None
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="prd074-webhook-stub",
            daemon=True,
        )
        self.thread.start()
        return loopback_url(
            f"http://{LOOPBACK_HOST}:{WEBHOOK_PORT}/events",
            port=WEBHOOK_PORT,
            path="/events",
            stage="stub.webhook_url",
        )

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "created": self.created_count,
                "completed": self.completed_count,
                "invalid": self.invalid_count,
                "signature_failures": self.signature_failures,
                "forbidden_field_hits": self.forbidden_field_hits,
            }


def _contains_forbidden_webhook_field(value: object) -> bool:
    forbidden = {
        "prompt",
        "response",
        "assistant",
        "content",
        "authorization",
        "provider_key",
        "access_token",
        "grant",
        "lease_token",
        "ciphertext",
    }
    if isinstance(value, dict):
        return any(
            str(key).lower() in forbidden or _contains_forbidden_webhook_field(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_webhook_field(item) for item in value)
    return False


def assert_fixed_ports_available() -> None:
    sockets: list[socket.socket] = []
    try:
        for port in FIXED_PORTS:
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                candidate.bind((LOOPBACK_HOST, port))
            except OSError:
                candidate.close()
                raise AcceptanceFailure("config.ports", "fixed_port_unavailable") from None
            sockets.append(candidate)
    finally:
        for candidate in sockets:
            candidate.close()


def database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def prepare_database(work: Path, provider_base_url: str, secrets_: RuntimeSecrets) -> RuntimeGraph:
    """Migrate a fresh database to 0067 twice and create the minimal tenant binding."""

    database = workspace_path(work / "prd074-runtime.sqlite3", work, stage="database.path")
    url = database_url(database)
    seed_environment = {
        "METAHUB_DATABASE_URL": url,
        "METAHUB_SEED_ADMIN_PASSWORD": secrets_.admin_password,
        "METAHUB_SEED_PROVIDER_API_KEY": secrets_.provider_key,
        "METAHUB_SEED_VIRTUAL_KEY": secrets_.virtual_key,
        "METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL": provider_base_url,
    }
    previous = {name: os.environ.get(name) for name in seed_environment}
    os.environ.update(seed_environment)
    try:
        first = apply_migrations(url)
        seed = apply_seed_data(url)
        second = apply_migrations(url)
    finally:
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
    connection = connect_database(url)
    try:
        migration = connection.execute(
            "SELECT COUNT(*),MAX(version) FROM schema_migrations WHERE status='applied'"
        ).fetchone()
        require(
            migration is not None
            and tuple(migration) == MIGRATION_CONTRACT
            and "0067" in {*first.applied, *first.skipped}
            and not second.applied,
            "database.migration",
            "not_0067",
        )
        require(
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
            "database.integrity",
            "not_ok",
        )
        require(
            connection.execute("PRAGMA foreign_key_check").fetchall() == [],
            "database.foreign_keys",
            "violations",
        )
        tenant_id = str(seed.tenant_id)
        environment = connection.execute(
            "SELECT environment_id,code FROM environments "
            "WHERE tenant_id=? AND code='dev' AND status='active'",
            (tenant_id,),
        ).fetchone()
        model = connection.execute(
            "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='default-chat' "
            "AND status='active'",
            (tenant_id,),
        ).fetchone()
        admin = connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? AND email='admin@metahub.local'",
            (tenant_id,),
        ).fetchone()
        virtual_key = connection.execute(
            "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? AND status='active'",
            (tenant_id,),
        ).fetchone()
        require(
            all(item is not None for item in (environment, model, admin, virtual_key)),
            "database.seed",
            "graph_missing",
        )
        now = utc_now()
        developer_user_id = "user_prd074_runtime_developer"
        developer_email = f"prd074-runtime-{secrets.token_hex(8)}@example.invalid"
        project_id = "prj_prd074_runtime"
        model_alias_id = "mal_prd074_runtime"
        model_alias = "prd074-runtime-batch"
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,'developer','active',?,?)
            """,
            (
                developer_user_id,
                tenant_id,
                developer_email,
                "PRD074 Runtime Developer",
                hash_password(secrets_.developer_password),
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO projects(
              project_id,tenant_id,project_code,name,description,owner_user_id,status,
              created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,?,'PRD074 Runtime',NULL,?,'active',?,?,?,?)
            """,
            (
                project_id,
                tenant_id,
                "prd074-runtime",
                admin["user_id"],
                admin["user_id"],
                admin["user_id"],
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id,tenant_id,alias,environment,default_model_id,fallback_model_id,
              status,description,created_by,disabled_at,disabled_by,created_at,updated_at,
              environment_id
            ) VALUES(?,?,?,?,?,NULL,'active','PRD074 runtime',?,NULL,NULL,?,?,?)
            """,
            (
                model_alias_id,
                tenant_id,
                model_alias,
                environment["code"],
                model["model_id"],
                admin["user_id"],
                now,
                now,
                environment["environment_id"],
            ),
        )
        connection.execute(
            """
            UPDATE virtual_keys SET project_id=?,environment_id=?,updated_at=?
            WHERE tenant_id=? AND virtual_key_id=?
            """,
            (
                project_id,
                environment["environment_id"],
                now,
                tenant_id,
                virtual_key["virtual_key_id"],
            ),
        )
        connection.execute(
            """
            UPDATE models
            SET input_price_per_1k=0.1,output_price_per_1k=0.2,currency='CNY',
                price_schema_version='prd074-runtime-v1',updated_at=?
            WHERE tenant_id=? AND model_id=?
            """,
            (now, tenant_id, model["model_id"]),
        )
        connection.execute(
            """
            INSERT INTO budget_rules(
              budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,
              daily_budget_cny,warning_threshold_percent,exhausted_threshold_percent,
              exhausted_action,status,created_by,updated_by,created_at,updated_at
            ) VALUES('br_prd074_runtime',?,?,'virtual_key',?,'PRD074 Runtime Budget',
                     100,80,100,'block','active',?,?,?,?)
            """,
            (
                tenant_id,
                environment["environment_id"],
                virtual_key["virtual_key_id"],
                admin["user_id"],
                admin["user_id"],
                now,
                now,
            ),
        )
        connection.commit()
        return RuntimeGraph(
            database_url=url,
            database=database,
            tenant_id=tenant_id,
            developer_user_id=developer_user_id,
            developer_email=developer_email,
            project_id=project_id,
            environment_id=str(environment["environment_id"]),
            model_alias_id=model_alias_id,
            model_alias=model_alias,
            virtual_key_id=str(virtual_key["virtual_key_id"]),
        )
    except (sqlite3.Error, AcceptanceFailure):
        connection.rollback()
        raise
    finally:
        connection.close()


def service_environment(
    work: Path,
    graph: RuntimeGraph,
    provider_base_url: str,
    secrets_: RuntimeSecrets,
) -> dict[str, str]:
    """Build a bounded child environment with every writable path inside the run directory."""

    allowed_system = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "OS",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
    }
    environment = {
        name: value for name, value in os.environ.items() if name.upper() in allowed_system
    }
    source_roots = (
        ROOT / "shared-sdk" / "python",
        ROOT / "gateway-data-plane" / "src",
        ROOT / "control-plane-api" / "src",
        ROOT / "examples" / "python" / "src",
    )
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(str(item) for item in source_roots),
            "TEMP": str(work / "temp"),
            "TMP": str(work / "temp"),
            "METAHUB_ENV": "dev",
            "METAHUB_LOG_LEVEL": "INFO",
            "METAHUB_DATABASE_URL": graph.database_url,
            "METAHUB_AUTH_TOKEN_SECRET": secrets_.auth_secret,
            "METAHUB_CACHE_AFFINITY_HMAC_SECRET": secrets_.cache_secret,
            "METAHUB_INTERNAL_CHAT_TOKEN": secrets_.internal_chat_token,
            "METAHUB_INTERNAL_BATCH_TOKEN": secrets_.internal_batch_token,
            "METAHUB_BATCH_ENCRYPTION_SECRET": secrets_.batch_secret,
            "METAHUB_FILE_ENCRYPTION_SECRET": secrets_.file_secret,
            "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET": secrets_.prompt_secret,
            "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": secrets_.provider_encryption_secret,
            "METAHUB_GATEWAY_API_BASE_URL": f"http://{LOOPBACK_HOST}:{GATEWAY_PORT}",
            "METAHUB_CONTROL_API_BASE_URL": f"http://{LOOPBACK_HOST}:{CONTROL_PORT}",
            "METAHUB_ALLOWED_ORIGINS": f"http://{LOOPBACK_HOST}:18744",
            "METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL": provider_base_url,
            "METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY": secrets_.provider_key,
            "METAHUB_SEED_VIRTUAL_KEY": secrets_.virtual_key,
            "METAHUB_FILE_STORAGE_ROOT": str(work / "storage"),
            "METAHUB_FINOPS_USAGE_SPOOL_ROOT": str(work / "spool"),
            "METAHUB_PROVIDER_HEALTH_ENABLED": "false",
            "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED": "false",
            "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED": "false",
            "METAHUB_USAGE_EXPORT_ENABLED": "false",
            "METAHUB_COST_ANOMALY_ENABLED": "false",
            "METAHUB_REQUEST_EXPLANATION_EVENT_ENABLED": "false",
            "METAHUB_REQUEST_LOG_QUERY_EVENT_ENABLED": "false",
            "METAHUB_BATCH_WORKER_ENABLED": "true",
            "METAHUB_BATCH_POLL_INTERVAL_SECONDS": "0.1",
            "METAHUB_BATCH_GLOBAL_CONCURRENCY": "1",
            "METAHUB_BATCH_TENANT_CONCURRENCY": "1",
            "METAHUB_BATCH_LEASE_SECONDS": "30",
            "METAHUB_BATCH_HEARTBEAT_SECONDS": "5",
            "METAHUB_BATCH_GATEWAY_CONCURRENCY": "1",
            "METAHUB_BATCH_REALTIME_COOLDOWN_MILLISECONDS": "250",
        }
    )
    return environment


def start_process(
    name: str,
    module: str,
    *,
    work: Path,
    environment: dict[str, str],
    port: int | None = None,
    app: str | None = None,
) -> ManagedProcess:
    """Start one repository-Python process and direct both logs into this run."""

    python = workspace_path(PYTHON, ROOT, stage=f"process.{name}.python")
    require(python.is_file(), f"process.{name}", "repository_python_missing")
    command = [str(python), "-m", module]
    if port is not None:
        require(isinstance(app, str) and bool(app), f"process.{name}", "app_missing")
        command.extend(
            [
                app,
                "--host",
                LOOPBACK_HOST,
                "--port",
                str(port),
                "--no-access-log",
                "--log-level",
                "info",
            ]
        )
    logs = workspace_path(work / "logs", work, stage=f"process.{name}.logs")
    stdout_path = workspace_path(logs / f"{name}.stdout.log", logs, stage=f"process.{name}.stdout")
    stderr_path = workspace_path(logs / f"{name}.stderr.log", logs, stage=f"process.{name}.stderr")
    stdout_file = stdout_path.open("xb")
    stderr_file = stderr_path.open("xb")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        stdout_file.close()
        stderr_file.close()
        raise AcceptanceFailure(f"process.{name}", "start_failed") from None
    return ManagedProcess(name, process, stdout_file, stderr_file)


def stop_owned_process(item: ManagedProcess) -> None:
    """Stop only the exact Popen handle supplied by this runner."""

    if item.process.poll() is None:
        item.process.terminate()
        try:
            item.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            item.process.kill()
            item.process.wait(timeout=5)
    if not item.stdout_file.closed:
        item.stdout_file.close()
    if not item.stderr_file.closed:
        item.stderr_file.close()


def wait_health(url: str, process: ManagedProcess, *, stage: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.process.poll() is not None:
            raise AcceptanceFailure(stage, "process_exited")
        try:
            response = httpx.get(url, timeout=1, follow_redirects=False, trust_env=False)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.15)
    raise AcceptanceFailure(stage, "health_timeout")


def auth_headers(
    token: str, request_id: str, *, idempotency_key: str | None = None
) -> dict[str, str]:
    result = {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}
    if idempotency_key is not None:
        result["Idempotency-Key"] = idempotency_key
    return result


def login(
    client: httpx.Client,
    *,
    email: str,
    password: str,
    request_id: str,
) -> str:
    response = client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
        headers={"X-Request-ID": request_id},
    )
    require(response.status_code == 200, "http.login", "status")
    payload = response.json()
    token = payload.get("access_token") if isinstance(payload, dict) else None
    require(isinstance(token, str) and len(token) > 40, "http.login", "token_missing")
    return token


def expect_error(
    response: httpx.Response,
    *,
    status: int,
    code: str,
    request_id: str,
    stage: str,
) -> dict[str, object]:
    require(response.status_code == status, stage, "status")
    payload = response.json()
    valid = (
        isinstance(payload, dict)
        and payload.get("code") == code
        and payload.get("request_id") == request_id
        and isinstance(payload.get("trace_id"), str)
        and bool(payload.get("trace_id"))
    )
    require(valid, stage, "error_contract")
    return {"status": status, "code": code, "request_id_preserved": True}


def upload_and_scan(
    client: httpx.Client,
    token: str,
    *,
    filename: str,
    text: str,
    marker: str,
) -> str:
    upload = client.post(
        "/files",
        files={"file": (filename, text.encode("utf-8"), "text/plain")},
        headers=auth_headers(token, f"prd074-upload-{marker}"),
    )
    require(upload.status_code == 201, f"http.upload.{marker}", "status")
    payload = upload.json()
    file_id = payload.get("file", {}).get("file_id") if isinstance(payload, dict) else None
    require(
        isinstance(file_id, str) and re.fullmatch(r"file_[0-9a-f]{32}", file_id) is not None,
        f"http.upload.{marker}",
        "file_id",
    )
    scan = client.post(
        f"/files/{file_id}/security-scan",
        headers=auth_headers(token, f"prd074-scan-{marker}"),
    )
    require(scan.status_code == 200, f"http.scan.{marker}", "status")
    report = scan.json().get("report", {})
    require(
        report.get("model_access_status") == "allowed"
        and report.get("coverage_status") == "complete",
        f"http.scan.{marker}",
        "not_allowed",
    )
    return file_id


def create_batch_job(
    client: httpx.Client,
    token: str,
    graph: RuntimeGraph,
    *,
    file_id: str,
    name: str,
    marker: str,
    priority: str = "normal",
) -> str:
    request_id = f"prd074-create-{marker}"
    response = client.post(
        "/batch-jobs",
        json={
            "name": name,
            "project_id": graph.project_id,
            "environment_id": graph.environment_id,
            "model_alias_id": graph.model_alias_id,
            "operation": "summarize",
            "file_ids": [file_id],
            "priority": priority,
            "max_duration_seconds": 300,
            "max_attempts": 2,
            "max_output_tokens": 128,
            "output_language": "zh-CN",
        },
        headers=auth_headers(
            token,
            request_id,
            idempotency_key=f"74000000-0000-4000-8000-{int(marker):012d}",
        ),
    )
    require(response.status_code == 201, f"http.create.{marker}", "status")
    require(
        response.headers.get("cache-control") == "no-store, private",
        f"http.create.{marker}",
        "cache",
    )
    payload = response.json()
    job = payload.get("job", {}) if isinstance(payload, dict) else {}
    job_id = job.get("batch_job_id")
    require(
        isinstance(job_id, str)
        and re.fullmatch(r"bjob_[0-9a-f]{32}", job_id) is not None
        and job.get("status") == "queued"
        and payload.get("request_id") == request_id,
        f"http.create.{marker}",
        "contract",
    )
    return job_id


def wait_database(
    graph: RuntimeGraph,
    query: str,
    parameters: tuple[object, ...],
    predicate: Any,
    *,
    stage: str,
    timeout: float = 45,
) -> sqlite3.Row:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = connect_database(graph.database_url)
        try:
            row = connection.execute(query, parameters).fetchone()
            if row is not None and predicate(row):
                return row
        finally:
            connection.close()
        time.sleep(0.15)
    raise AcceptanceFailure(stage, "timeout")


def job_status(graph: RuntimeGraph, job_id: str) -> str | None:
    connection = connect_database(graph.database_url)
    try:
        row = connection.execute(
            "SELECT status FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
            (graph.tenant_id, job_id),
        ).fetchone()
        return str(row[0]) if row is not None else None
    finally:
        connection.close()


def revoke_developer_batch_permission(graph: RuntimeGraph) -> None:
    """Revoke developer batch.write and advance the authoritative role matrix revision/hash."""

    connection = connect_database(graph.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        role = connection.execute(
            "SELECT role_id,permission_revision FROM roles WHERE tenant_id=? AND code='developer'",
            (graph.tenant_id,),
        ).fetchone()
        permission = connection.execute(
            "SELECT permission_id FROM permissions WHERE tenant_id=? AND code='batch.write'",
            (graph.tenant_id,),
        ).fetchone()
        require(role is not None and permission is not None, "permission.revoke", "missing")
        deleted = connection.execute(
            "DELETE FROM role_permissions WHERE tenant_id=? AND role_id=? AND permission_id=?",
            (graph.tenant_id, role["role_id"], permission["permission_id"]),
        )
        require(deleted.rowcount == 1, "permission.revoke", "not_changed")
        remaining = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT permission.code FROM role_permissions relation
                JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE relation.tenant_id=? AND relation.role_id=?
                """,
                (graph.tenant_id, role["role_id"]),
            ).fetchall()
        }
        connection.execute(
            """
            UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=?
            WHERE tenant_id=? AND role_id=?
            """,
            (
                int(role["permission_revision"]) + 1,
                permission_matrix_sha256(remaining),
                utc_now(),
                graph.tenant_id,
                role["role_id"],
            ),
        )
        connection.commit()
    except (sqlite3.Error, AcceptanceFailure):
        connection.rollback()
        raise
    finally:
        connection.close()


@dataclass(slots=True)
class RealtimeObservation:
    status: int | None = None
    contract_valid: bool = False
    failure: str | None = None


def start_realtime_request(
    graph: RuntimeGraph,
    secrets_: RuntimeSecrets,
) -> tuple[threading.Thread, RealtimeObservation]:
    """Start one public Gateway request whose provider response is held by the stub."""

    observation = RealtimeObservation()

    def invoke() -> None:
        try:
            with httpx.Client(
                base_url=f"http://{LOOPBACK_HOST}:{GATEWAY_PORT}",
                timeout=httpx.Timeout(55, connect=3),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": graph.model_alias,
                        "messages": [{"role": "user", "content": secrets_.realtime_text}],
                        "stream": False,
                    },
                    headers={
                        "Authorization": f"Bearer {secrets_.virtual_key}",
                        "X-Request-ID": "prd074-realtime-priority",
                    },
                )
                observation.status = response.status_code
                payload = response.json()
                choices = payload.get("choices") if isinstance(payload, dict) else None
                message = (
                    choices[0].get("message") if isinstance(choices, list) and choices else None
                )
                observation.contract_valid = (
                    response.status_code == 200
                    and isinstance(message, dict)
                    and message.get("content") == secrets_.realtime_answer
                )
                del payload, choices, message
        except (httpx.HTTPError, ValueError, TypeError, IndexError):
            observation.failure = "request_failed"

    thread = threading.Thread(target=invoke, name="prd074-realtime-request", daemon=True)
    thread.start()
    return thread, observation


def create_webhook_endpoint(
    client: httpx.Client,
    admin_token: str,
    endpoint_url: str,
    stub: WebhookStub,
) -> str:
    response = client.post(
        "/webhooks",
        json={
            "name": "PRD074 runtime receiver",
            "endpoint_url": endpoint_url,
            "event_types": ["batch_job.created", "batch_job.completed"],
            "status": "active",
            "timeout_ms": 2000,
        },
        headers=auth_headers(admin_token, "prd074-webhook-create"),
    )
    require(response.status_code == 200, "http.webhook_create", "status")
    payload = response.json()
    signing_secret = payload.get("signing_secret") if isinstance(payload, dict) else None
    require(
        isinstance(signing_secret, str) and len(signing_secret) > 32,
        "http.webhook_create",
        "secret",
    )
    stub.set_signing_secret(signing_secret)
    return signing_secret


def wait_webhook(
    stub: WebhookStub,
    predicate: Any,
    *,
    stage: str,
    timeout: float = 30,
) -> dict[str, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = stub.snapshot()
        if predicate(snapshot):
            return snapshot
        time.sleep(0.15)
    raise AcceptanceFailure(stage, "timeout")


def main_result_contract(
    client: httpx.Client,
    admin_token: str,
    main_job_id: str,
    secrets_: RuntimeSecrets,
) -> dict[str, object]:
    tasks = client.get(
        f"/batch-jobs/{main_job_id}/tasks",
        headers=auth_headers(admin_token, "prd074-main-tasks"),
    )
    require(tasks.status_code == 200, "http.main_tasks", "status")
    task_values = tasks.json().get("tasks", [])
    require(
        isinstance(task_values, list)
        and len(task_values) == 1
        and task_values[0].get("status") == "succeeded"
        and task_values[0].get("result_available") is True,
        "http.main_tasks",
        "contract",
    )
    task_id = task_values[0].get("batch_task_id")
    result = client.get(
        f"/batch-jobs/{main_job_id}/tasks/{task_id}/result",
        headers=auth_headers(admin_token, "prd074-main-result"),
    )
    require(result.status_code == 200, "http.main_result", "status")
    require(
        result.headers.get("cache-control") == "no-store, private",
        "http.main_result",
        "cache",
    )
    payload = result.json()
    require(
        payload.get("assistant") == secrets_.assistant_text
        and isinstance(payload.get("request_id"), str)
        and isinstance(payload.get("trace_id"), str),
        "http.main_result",
        "contract",
    )
    del payload
    return {
        "tasks_status": tasks.status_code,
        "result_status": result.status_code,
        "result_matched_in_memory": True,
        "cache_control": "no-store, private",
    }


def busy_recovery_facts(graph: RuntimeGraph, job_id: str) -> dict[str, object]:
    connection = connect_database(graph.database_url)
    try:
        task = connection.execute(
            "SELECT batch_task_id,current_attempt_id,status,attempt_count,lease_generation "
            "FROM batch_tasks WHERE tenant_id=? AND batch_job_id=?",
            (graph.tenant_id, job_id),
        ).fetchone()
        require(task is not None, "database.busy", "task_missing")
        attempt = connection.execute(
            "SELECT batch_attempt_id,request_id,trace_id,status,execution_outcome "
            "FROM batch_attempts WHERE tenant_id=? AND batch_job_id=?",
            (graph.tenant_id, job_id),
        ).fetchone()
        grants = connection.execute(
            "SELECT status FROM batch_execution_grants WHERE tenant_id=? AND batch_job_id=?",
            (graph.tenant_id, job_id),
        ).fetchall()
        require(
            attempt is not None
            and task["status"] == "leased"
            and int(task["attempt_count"]) == 0
            and task["current_attempt_id"] == attempt["batch_attempt_id"]
            and attempt["status"] == "prepared"
            and attempt["execution_outcome"] == "not_invoked"
            and len(grants) >= 2
            and all(row["status"] in {"issued", "revoked"} for row in grants)
            and not any(row["status"] == "consumed" for row in grants),
            "database.busy",
            "grant_or_attempt_changed",
        )
        return {
            "attempt_count": 0,
            "provider_not_invoked": True,
            "grant_consumed": False,
            "grant_rotations": len(grants),
            "prepared_attempt_preserved": True,
        }
    finally:
        connection.close()


def owner_chain_facts(graph: RuntimeGraph, job_id: str) -> dict[str, object]:
    """Cross-check the successful owner chain without returning any business identifiers."""

    connection = connect_database(graph.database_url)
    try:
        job = connection.execute(
            "SELECT * FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
            (graph.tenant_id, job_id),
        ).fetchone()
        task = connection.execute(
            "SELECT * FROM batch_tasks WHERE tenant_id=? AND batch_job_id=?",
            (graph.tenant_id, job_id),
        ).fetchone()
        attempts = connection.execute(
            "SELECT * FROM batch_attempts WHERE tenant_id=? AND batch_job_id=? ORDER BY attempt_no",
            (graph.tenant_id, job_id),
        ).fetchall()
        require(
            job is not None and task is not None and len(attempts) == 1,
            "database.owner",
            "batch_rows",
        )
        attempt = attempts[0]
        grants = connection.execute(
            "SELECT * FROM batch_execution_grants WHERE tenant_id=? AND batch_attempt_id=?",
            (graph.tenant_id, attempt["batch_attempt_id"]),
        ).fetchall()
        allocations = connection.execute(
            "SELECT * FROM batch_budget_allocations WHERE tenant_id=? AND batch_attempt_id=?",
            (graph.tenant_id, attempt["batch_attempt_id"]),
        ).fetchall()
        request_log = connection.execute(
            "SELECT * FROM request_logs WHERE tenant_id=? AND request_id=?",
            (graph.tenant_id, attempt["request_id"]),
        ).fetchone()
        routing = connection.execute(
            "SELECT * FROM routing_attempts WHERE tenant_id=? AND request_id=?",
            (graph.tenant_id, attempt["request_id"]),
        ).fetchall()
        usage = connection.execute(
            """
            SELECT usage.routing_attempt_id,routing.trace_id AS routing_trace_id,
                   routing.status AS routing_status,
                   routing.usage_status AS routing_usage_status
            FROM usage_ledger usage
            JOIN routing_attempts routing
              ON routing.tenant_id=usage.tenant_id
             AND routing.request_id=usage.request_id
             AND routing.routing_attempt_id=usage.routing_attempt_id
            WHERE usage.tenant_id=? AND usage.request_id=?
            """,
            (graph.tenant_id, attempt["request_id"]),
        ).fetchone()
        finops = connection.execute(
            "SELECT * FROM finops_ledger_entries WHERE tenant_id=? AND source_request_id=?",
            (graph.tenant_id, attempt["request_id"]),
        ).fetchall()
        owner_ok = (
            job["status"] == "completed"
            and task["status"] == "succeeded"
            and task["current_attempt_id"] == attempt["batch_attempt_id"]
            and attempt["status"] == "provider_succeeded"
            and attempt["execution_outcome"] == "succeeded"
            and attempt["result_state"] == "available"
            and request_log is not None
            and request_log["trace_id"] == attempt["trace_id"]
            and len(routing) >= 1
            and all(row["trace_id"] == attempt["trace_id"] for row in routing)
            and usage is not None
            and usage["routing_trace_id"] == attempt["trace_id"]
            and usage["routing_status"] == "succeeded"
            and usage["routing_usage_status"] == "recorded"
            and len(finops) >= 1
            and all(row["source_trace_id"] == attempt["trace_id"] for row in finops)
            and len(allocations) >= 1
            and all(
                row["status"] == "posted" and row["charge_state"] == "posted" for row in allocations
            )
            and any(row["status"] == "consumed" for row in grants)
        )
        require(owner_ok, "database.owner", "chain_mismatch")
        return {
            "owner_chain_complete": True,
            "attempts": len(attempts),
            "grants": len(grants),
            "allocations": len(allocations),
            "request_logs": 1,
            "routing_attempts": len(routing),
            "usage_rows": 1,
            "finops_rows": len(finops),
            "posted_cost_nonzero": float(str(job["posted_cost_cny"])) > 0,
        }
    finally:
        connection.close()


def database_summary(
    graph: RuntimeGraph,
    *,
    main_job_id: str,
    cancelled_job_id: str,
    outbox_job_id: str,
    revoked_job_id: str,
) -> dict[str, object]:
    connection = connect_database(graph.database_url)
    try:
        migration = connection.execute(
            "SELECT COUNT(*),MAX(version) FROM schema_migrations WHERE status='applied'"
        ).fetchone()
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        statuses = {
            str(row["batch_job_id"]): str(row["status"])
            for row in connection.execute(
                "SELECT batch_job_id,status FROM batch_jobs WHERE tenant_id=?",
                (graph.tenant_id,),
            ).fetchall()
        }
        cancelled_attempts = int(
            connection.execute(
                "SELECT COUNT(*) FROM batch_attempts WHERE tenant_id=? AND batch_job_id=?",
                (graph.tenant_id, cancelled_job_id),
            ).fetchone()[0]
        )
        revoked = connection.execute(
            "SELECT status,failure_code,attempt_count FROM batch_tasks "
            "WHERE tenant_id=? AND batch_job_id=?",
            (graph.tenant_id, revoked_job_id),
        ).fetchone()
        completed_outbox = connection.execute(
            """
            SELECT outbox.dispatch_status,outbox.dispatch_attempts
            FROM batch_job_event_outbox outbox
            JOIN batch_job_events event
              ON event.tenant_id=outbox.tenant_id
             AND event.batch_job_event_id=outbox.batch_job_event_id
            WHERE event.tenant_id=? AND event.batch_job_id=?
              AND event.event_type='batch_job.completed'
            """,
            (graph.tenant_id, outbox_job_id),
        ).fetchone()
        require(
            migration is not None
            and tuple(migration) == MIGRATION_CONTRACT
            and integrity == "ok"
            and foreign_key_violations == 0
            and statuses.get(main_job_id) == "completed"
            and statuses.get(cancelled_job_id) == "cancelled"
            and statuses.get(outbox_job_id) == "completed"
            and statuses.get(revoked_job_id) == "failed"
            and cancelled_attempts == 0
            and revoked is not None
            and revoked["failure_code"] == "batch.authorization_revoked"
            and int(revoked["attempt_count"]) == 0
            and completed_outbox is not None
            and completed_outbox["dispatch_status"] == "dispatched",
            "database.summary",
            "contract",
        )
        return {
            "migration": {"count": 67, "highest": "0067", "catalog_revision": 10},
            "integrity_check": "ok",
            "foreign_key_violations": 0,
            "jobs": {
                "completed": sum(value == "completed" for value in statuses.values()),
                "cancelled": sum(value == "cancelled" for value in statuses.values()),
                "failed": sum(value == "failed" for value in statuses.values()),
            },
            "queued_cancel_provider_calls": 0,
            "revoked_attempt_count": 0,
            "revoked_failure_code": "batch.authorization_revoked",
            "recovered_completed_outbox": True,
        }
    finally:
        connection.close()


PROCESS_NAMES = (
    "gateway",
    "control-initial",
    "batch-worker-initial",
    "batch-worker-recovery",
    "batch-worker-outbox",
    "control-recovery",
    "batch-worker-revoked",
)


def exercise_runtime(
    work: Path,
    graph: RuntimeGraph,
    environment: dict[str, str],
    secrets_: RuntimeSecrets,
    provider: ProviderStub,
    webhook: WebhookStub,
    webhook_url: str,
    processes: OwnedProcessSet,
) -> tuple[dict[str, object], tuple[str, ...], str]:
    """Exercise busy admission, owned-worker recovery, outbox recovery, cancel, and revocation."""

    gateway = processes.add(
        start_process(
            "gateway",
            "uvicorn",
            app="metahub_gateway.app:app",
            port=GATEWAY_PORT,
            work=work,
            environment=environment,
        )
    )
    wait_health(
        f"http://{LOOPBACK_HOST}:{GATEWAY_PORT}/health",
        gateway,
        stage="health.gateway",
    )
    control = processes.add(
        start_process(
            "control-initial",
            "uvicorn",
            app="metahub_control.app:app",
            port=CONTROL_PORT,
            work=work,
            environment=environment,
        )
    )
    wait_health(
        f"http://{LOOPBACK_HOST}:{CONTROL_PORT}/health",
        control,
        stage="health.control_initial",
    )

    with httpx.Client(
        base_url=f"http://{LOOPBACK_HOST}:{CONTROL_PORT}",
        timeout=httpx.Timeout(25, connect=3),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        admin_token = login(
            client,
            email="admin@metahub.local",
            password=secrets_.admin_password,
            request_id="prd074-admin-login",
        )
        developer_token = login(
            client,
            email=graph.developer_email,
            password=secrets_.developer_password,
            request_id="prd074-developer-login",
        )
        webhook_secret = create_webhook_endpoint(client, admin_token, webhook_url, webhook)
        file_ids = tuple(
            upload_and_scan(
                client,
                developer_token,
                filename=secrets_.file_names[index],
                text=secrets_.file_texts[index],
                marker=str(index + 1),
            )
            for index in range(4)
        )
        main_job_id = create_batch_job(
            client,
            developer_token,
            graph,
            file_id=file_ids[0],
            name=secrets_.job_names[0],
            marker="1",
            priority="high",
        )
        cancelled_job_id = create_batch_job(
            client,
            developer_token,
            graph,
            file_id=file_ids[1],
            name=secrets_.job_names[1],
            marker="2",
            priority="normal",
        )
        cancelled = client.post(
            f"/batch-jobs/{cancelled_job_id}/cancel",
            headers=auth_headers(developer_token, "prd074-cancel-queued"),
        )
        require(cancelled.status_code == 200, "http.cancel", "status")
        require(
            cancelled.json().get("job", {}).get("status") == "cancelled", "http.cancel", "contract"
        )

        realtime_thread, realtime = start_realtime_request(graph, secrets_)
        require(provider.realtime_seen.wait(timeout=20), "runtime.realtime", "provider_not_reached")
        worker_initial = processes.add(
            start_process(
                "batch-worker-initial",
                "metahub_control.batch_jobs.worker_main",
                work=work,
                environment=environment,
            )
        )
        wait_database(
            graph,
            """
            SELECT task.status,task.attempt_count,attempt.status AS attempt_status,
                   (SELECT COUNT(*) FROM batch_execution_grants grant
                    WHERE grant.tenant_id=task.tenant_id
                      AND grant.batch_job_id=task.batch_job_id) AS grant_count
            FROM batch_tasks task
            JOIN batch_attempts attempt
              ON attempt.tenant_id=task.tenant_id
             AND attempt.batch_attempt_id=task.current_attempt_id
            WHERE task.tenant_id=? AND task.batch_job_id=?
            """,
            (graph.tenant_id, main_job_id),
            lambda row: (
                row["status"] == "leased"
                and int(row["attempt_count"]) == 0
                and row["attempt_status"] == "prepared"
                and int(row["grant_count"]) >= 2
            ),
            stage="runtime.busy",
            timeout=35,
        )
        require(provider.snapshot()["batch_calls"] == 0, "runtime.busy", "provider_called")
        busy = busy_recovery_facts(graph, main_job_id)
        processes.stop(worker_initial)

        provider.release_realtime.set()
        realtime_thread.join(timeout=25)
        require(not realtime_thread.is_alive(), "runtime.realtime", "thread_stuck")
        require(
            realtime.status == 200 and realtime.contract_valid and realtime.failure is None,
            "runtime.realtime",
            "response_contract",
        )
        lease_row = wait_database(
            graph,
            "SELECT lease_expires_at FROM batch_tasks WHERE tenant_id=? AND batch_job_id=?",
            (graph.tenant_id, main_job_id),
            lambda row: row["lease_expires_at"] is not None,
            stage="runtime.lease",
        )
        expires = datetime.fromisoformat(str(lease_row["lease_expires_at"]).replace("Z", "+00:00"))
        remaining = (expires - datetime.now(UTC)).total_seconds() + 1.25
        if remaining > 0:
            time.sleep(min(remaining, 35))

        worker_recovery = processes.add(
            start_process(
                "batch-worker-recovery",
                "metahub_control.batch_jobs.worker_main",
                work=work,
                environment=environment,
            )
        )
        wait_database(
            graph,
            "SELECT status FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
            (graph.tenant_id, main_job_id),
            lambda row: row["status"] == "completed",
            stage="runtime.worker_recovery",
            timeout=45,
        )
        processes.stop(worker_recovery)
        result_contract = main_result_contract(
            client,
            admin_token,
            main_job_id,
            secrets_,
        )
        owner = owner_chain_facts(graph, main_job_id)

        outbox_job_id = create_batch_job(
            client,
            developer_token,
            graph,
            file_id=file_ids[2],
            name=secrets_.job_names[2],
            marker="3",
            priority="normal",
        )
        before_outage = webhook.snapshot()["completed"]
        processes.stop(control)
        worker_outbox = processes.add(
            start_process(
                "batch-worker-outbox",
                "metahub_control.batch_jobs.worker_main",
                work=work,
                environment=environment,
            )
        )
        wait_database(
            graph,
            "SELECT status FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
            (graph.tenant_id, outbox_job_id),
            lambda row: row["status"] == "completed",
            stage="runtime.control_outage_completion",
            timeout=45,
        )
        processes.stop(worker_outbox)
        wait_database(
            graph,
            """
            SELECT outbox.dispatch_status
            FROM batch_job_event_outbox outbox
            JOIN batch_job_events event
              ON event.tenant_id=outbox.tenant_id
             AND event.batch_job_event_id=outbox.batch_job_event_id
            WHERE event.tenant_id=? AND event.batch_job_id=?
              AND event.event_type='batch_job.completed'
            """,
            (graph.tenant_id, outbox_job_id),
            lambda row: row["dispatch_status"] == "pending",
            stage="runtime.outbox_pending",
        )
        require(
            webhook.snapshot()["completed"] == before_outage,
            "runtime.control_outage",
            "delivered_while_stopped",
        )

        control_recovery = processes.add(
            start_process(
                "control-recovery",
                "uvicorn",
                app="metahub_control.app:app",
                port=CONTROL_PORT,
                work=work,
                environment=environment,
            )
        )
        wait_health(
            f"http://{LOOPBACK_HOST}:{CONTROL_PORT}/health",
            control_recovery,
            stage="health.control_recovery",
        )
        wait_webhook(
            webhook,
            lambda snapshot: snapshot["completed"] > before_outage,
            stage="runtime.outbox_recovery",
            timeout=35,
        )

        revoked_job_id = create_batch_job(
            client,
            developer_token,
            graph,
            file_id=file_ids[3],
            name=secrets_.job_names[3],
            marker="4",
            priority="low",
        )
        batch_calls_before_revoke = provider.snapshot()["batch_calls"]
        revoke_developer_batch_permission(graph)
        worker_revoked = processes.add(
            start_process(
                "batch-worker-revoked",
                "metahub_control.batch_jobs.worker_main",
                work=work,
                environment=environment,
            )
        )
        wait_database(
            graph,
            """
            SELECT job.status,task.failure_code,task.attempt_count
            FROM batch_jobs job JOIN batch_tasks task
              ON task.tenant_id=job.tenant_id AND task.batch_job_id=job.batch_job_id
            WHERE job.tenant_id=? AND job.batch_job_id=?
            """,
            (graph.tenant_id, revoked_job_id),
            lambda row: (
                row["status"] == "failed"
                and row["failure_code"] == "batch.authorization_revoked"
                and int(row["attempt_count"]) == 0
            ),
            stage="runtime.authorization_revoked",
            timeout=30,
        )
        processes.stop(worker_revoked)
        require(
            provider.snapshot()["batch_calls"] == batch_calls_before_revoke,
            "runtime.authorization_revoked",
            "provider_called",
        )
        wait_webhook(
            webhook,
            lambda snapshot: snapshot["created"] >= 4 and snapshot["completed"] >= 4,
            stage="runtime.webhooks",
            timeout=35,
        )
        summary = database_summary(
            graph,
            main_job_id=main_job_id,
            cancelled_job_id=cancelled_job_id,
            outbox_job_id=outbox_job_id,
            revoked_job_id=revoked_job_id,
        )
        webhook_fact = webhook.snapshot()
        require(
            webhook_fact["invalid"] == 0
            and webhook_fact["signature_failures"] == 0
            and webhook_fact["forbidden_field_hits"] == 0,
            "runtime.webhooks",
            "receiver_contract",
        )
        provider_fact = provider.snapshot()
        require(
            provider_fact["realtime_calls"] == 1
            and provider_fact["batch_calls"] == 2
            and provider_fact["invalid_calls"] == 0,
            "runtime.provider",
            "call_count",
        )
        return (
            {
                "http": {
                    "upload_and_security_scan_count": 4,
                    "queued_cancel_status": 200,
                    **result_contract,
                },
                "realtime_priority": {
                    "realtime_status": 200,
                    "realtime_never_waited_for_batch": True,
                    **busy,
                },
                "worker_recovery": {
                    "owned_worker_stopped": True,
                    "expired_lease_reaped": True,
                    "same_prepared_attempt_recovered": True,
                    "provider_duplicate_calls": 0,
                },
                "owner_chain": owner,
                "outbox_recovery": {
                    "source_committed_while_control_stopped": True,
                    "not_delivered_during_outage": True,
                    "control_lifespan_recovered_delivery": True,
                    "active_drain_invoked": False,
                },
                "authorization": {
                    "revoked_failure_code": "batch.authorization_revoked",
                    "provider_calls_after_revoke": 0,
                },
                "provider_stub": provider_fact,
                "webhook_stub": webhook_fact,
                "database": summary,
            },
            (admin_token, developer_token),
            webhook_secret,
        )


def runtime_service_log_paths(work: Path) -> tuple[Path, ...]:
    logs = workspace_path(work / "logs", work, stage="privacy.logs")
    return tuple(
        logs / f"{name}.{stream}.log" for name in PROCESS_NAMES for stream in ("stdout", "stderr")
    )


def sensitive_encodings(value: str) -> tuple[bytes, ...]:
    require(len(value) >= 12, "privacy.value", "too_short")
    return tuple(value.encode(encoding) for encoding in ("utf-8", "utf-16-le", "utf-16-be"))


def _safe_scan_file(path: Path, *, stage: str, maximum: int = 128 * 1024 * 1024) -> bytes:
    require(path.exists(), stage, "required_file_missing")
    require(path.is_file() and not path.is_symlink(), stage, "required_file_invalid")
    size = path.stat().st_size
    require(0 <= size <= maximum, stage, "file_too_large")
    return path.read_bytes()


def verify_private_storage(
    database: Path,
    storage: Path,
    private_values: tuple[str, ...],
    *,
    log_paths: tuple[Path, ...],
    extra_targets: tuple[Path, ...] = (),
) -> dict[str, int]:
    """Scan database, encrypted objects, spool, every required log, and final evidence."""

    encodings = tuple(encoded for value in private_values for encoded in sensitive_encodings(value))
    targets: list[tuple[Path, str]] = [(database, "privacy.database")]
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists():
            targets.append((sidecar, "privacy.database"))
    for root in (storage, storage.parent / "spool"):
        require(root.is_dir() and not root.is_symlink(), "privacy.storage", "root_invalid")
        for path in root.rglob("*"):
            require(not path.is_symlink(), "privacy.storage", "link_invalid")
            if path.is_file():
                targets.append((path, "privacy.storage"))
    for path in log_paths:
        targets.append((path, "privacy.logs"))
    for path in extra_targets:
        if path.exists():
            targets.append((path, "privacy.evidence"))
    scanned = 0
    for path, stage in targets:
        body = _safe_scan_file(path, stage=stage)
        if any(encoded and encoded in body for encoded in encodings):
            raise AcceptanceFailure("privacy", "plaintext_persisted")
        scanned += 1
    return {
        "scanned_files": scanned,
        "service_log_files_scanned": len(log_paths),
        "plaintext_hits": 0,
        "encodings_per_value": 3,
    }


_FORBIDDEN_EVIDENCE_KEYS = {
    "password",
    "access_token",
    "internal_batch_token",
    "internal_chat_token",
    "provider_key",
    "virtual_key",
    "signing_secret",
    "prompt",
    "response",
    "assistant",
    "content",
    "ciphertext",
    "lease_token",
    "grant",
    "endpoint_url",
    "database_url",
}


def assert_safe_evidence(value: object, private_values: tuple[str, ...]) -> None:
    """Reject secret-shaped keys and any private value in the recursive evidence projection."""

    encoded_private = tuple(item.encode("utf-8") for item in private_values)

    def walk(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = str(key).lower()
                if normalized in _FORBIDDEN_EVIDENCE_KEYS or normalized.endswith(
                    ("_password", "_access_token", "_provider_key", "_signing_secret")
                ):
                    raise AcceptanceFailure("evidence", "forbidden_key")
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            body = item.encode("utf-8")
            if any(secret in body for secret in encoded_private):
                raise AcceptanceFailure("evidence", "private_value")

    walk(value)


def write_evidence(
    artifact: Path,
    payload: dict[str, object],
    private_values: tuple[str, ...],
) -> Path:
    assert_safe_evidence(payload, private_values)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    target = workspace_path(artifact / "runtime-acceptance.json", artifact, stage="evidence.path")
    temporary = workspace_path(
        artifact / ".runtime-acceptance.json.tmp", artifact, stage="evidence.temp"
    )
    require(not target.exists() and not temporary.exists(), "evidence", "already_exists")
    with temporary.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    digest = hashlib.sha256(encoded).hexdigest()
    checksum = workspace_path(artifact / "SHA256SUMS", artifact, stage="evidence.checksum")
    with checksum.open("x", encoding="ascii", newline="\n") as stream:
        stream.write(f"{digest}  runtime-acceptance.json\n")
        stream.flush()
        os.fsync(stream.fileno())
    return target


def run(run_id: str | None = None) -> Path:
    """Run the complete self-contained acceptance and return the evidence path."""

    actual_run_id = safe_run_id(run_id)
    work, artifact = create_run_directories(actual_run_id)
    require(PYTHON.resolve().is_relative_to(ROOT.resolve()), "config.python", "outside_workspace")
    assert_fixed_ports_available()
    secrets_ = RuntimeSecrets.create()
    provider = ProviderStub(secrets_)
    webhook = WebhookStub()
    processes = OwnedProcessSet()
    session_tokens: tuple[str, ...] = ()
    webhook_secret: str | None = None
    try:
        provider_url = provider.start()
        webhook_url = webhook.start()
        graph = prepare_database(work, provider_url, secrets_)
        environment = service_environment(work, graph, provider_url, secrets_)
        facts, session_tokens, webhook_secret = exercise_runtime(
            work,
            graph,
            environment,
            secrets_,
            provider,
            webhook,
            webhook_url,
            processes,
        )
    finally:
        provider.release_realtime.set()
        processes.close()
        webhook.stop()
        provider.stop()
    private_values = secrets_.private_values(
        session_tokens=session_tokens,
        webhook_secret=webhook_secret,
    )
    privacy = verify_private_storage(
        graph.database,
        work / "storage",
        private_values,
        log_paths=runtime_service_log_paths(work),
    )
    payload: dict[str, object] = {
        "prd": "PRD-074",
        "status": "passed",
        "run_id": actual_run_id,
        "executed_at": utc_now(),
        "fixed_loopback_ports": {
            "gateway": GATEWAY_PORT,
            "control": CONTROL_PORT,
            "provider_stub": PROVIDER_PORT,
            "webhook_stub": WEBHOOK_PORT,
        },
        "processes": {
            "gateway": "owned_and_stopped",
            "control_initial": "owned_and_stopped",
            "control_recovery": "owned_and_stopped",
            "worker_initial": "owned_and_stopped",
            "worker_recovery": "owned_and_stopped",
            "worker_outbox": "owned_and_stopped",
            "worker_revoked": "owned_and_stopped",
        },
        **facts,
        "privacy": privacy,
        "all_108_prds_complete": False,
    }
    target = write_evidence(artifact, payload, private_values)
    final_privacy = verify_private_storage(
        graph.database,
        work / "storage",
        private_values,
        log_paths=runtime_service_log_paths(work),
        extra_targets=(target, artifact / "SHA256SUMS"),
    )
    require(final_privacy["plaintext_hits"] == 0, "privacy.final", "plaintext_persisted")
    return target


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run PRD-074 isolated runtime acceptance")
    parser.add_argument("--run-id", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        target = run(parse_arguments(argv).run_id)
    except AcceptanceFailure as exc:
        print(f"PRD-074 runtime acceptance failed: {exc.stage}:{exc.code}", file=sys.stderr)
        return 1
    except Exception:
        print("PRD-074 runtime acceptance failed: runtime:unexpected", file=sys.stderr)
        return 1
    print(str(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
