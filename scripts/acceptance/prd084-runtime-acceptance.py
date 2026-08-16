"""Reproducible real-process runtime acceptance for PRD-084."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
for source in (
    ROOT / "shared-sdk" / "python",
    ROOT / "gateway-data-plane" / "src",
    ROOT / "control-plane-api" / "src",
):
    sys.path.insert(0, str(source))

from metahub_shared.db import apply_migrations, apply_seed_data, connect_database  # noqa: E402
from metahub_shared.runtime.webhooks import verify_webhook_signature  # noqa: E402

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
WORK_ROOT = ROOT / ".tmp" / "prd084-runtime-live"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd084-runtime"
ADMIN_PASSWORD = "Prd084-Admin-Aa1!" + secrets.token_urlsafe(24)
AUTH_SECRET = secrets.token_urlsafe(48)
ENCRYPTION_SECRET = secrets.token_urlsafe(48)
PROVIDER_CREDENTIAL = "prd084-provider-" + secrets.token_urlsafe(32)
NON_STREAM_INPUT = "prd084-input-" + secrets.token_urlsafe(18)
STREAM_INPUT = "prd084-stream-" + secrets.token_urlsafe(18)
FAILURE_INPUT = "prd084-failure-" + secrets.token_urlsafe(18)
DLP_MARKER = "prd084-block-" + secrets.token_urlsafe(18)
DLP_INPUT = "prefix-" + DLP_MARKER + "-suffix"
BUDGET_INPUT = "prd084-budget-" + secrets.token_urlsafe(18)
NON_STREAM_OUTPUT = "prd084-output-" + secrets.token_urlsafe(18)
STREAM_OUTPUT = "prd084-stream-output-" + secrets.token_urlsafe(18)
FAILURE_OUTPUT = "prd084-upstream-failure-" + secrets.token_urlsafe(18)
RUN_ID = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "url",
        "endpoint_url",
        "authorization",
        "token",
        "plain_value",
        "prompt",
        "response",
        "content",
        "payload",
        "secret",
        "database_url",
    }
)


class AcceptanceFailure(RuntimeError):
    def __init__(self, stage: str, code: str) -> None:
        self.stage = stage
        self.code = code
        super().__init__(f"{stage}:{code}")


def require(condition: bool, stage: str, code: str) -> None:
    if not condition:
        raise AcceptanceFailure(stage, code)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def serialize_safe_evidence(evidence: dict[str, Any], sensitive_values: tuple[str, ...]) -> str:
    def walk(value: object) -> None:
        if isinstance(value, dict):
            keys = {str(key).casefold() for key in value}
            require(
                not FORBIDDEN_EVIDENCE_KEYS.intersection(keys),
                "evidence",
                "forbidden_key",
            )
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(evidence)
    serialized = json.dumps(evidence, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    for value in sensitive_values:
        require(not value or value not in serialized, "evidence", "sensitive_canary")
    return serialized


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


class OpenAIStub:
    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.models_calls = 0
        self.chat_calls = 0
        self.stream_calls = 0
        self.failure_calls = 0

    def start(self) -> tuple[str, int]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/v1/models" or self.headers.get("Authorization") != (
                    f"Bearer {PROVIDER_CREDENTIAL}"
                ):
                    self._json(401, {"error": {"message": "unauthorized"}})
                    return
                owner.models_calls += 1
                self._json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {"id": "served-chat", "object": "model"},
                            {"id": "served-embed", "object": "model"},
                        ],
                    },
                )

            def do_POST(self) -> None:  # noqa: N802
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    require(0 < length <= 512 * 1024, "stub.request", "length")
                    body = self.rfile.read(length)
                    payload = json.loads(body.decode("utf-8"))
                    require(isinstance(payload, dict), "stub.request", "shape")
                    require(
                        self.path == "/v1/chat/completions",
                        "stub.request",
                        "path",
                    )
                    require(
                        self.headers.get("Authorization")
                        == f"Bearer {PROVIDER_CREDENTIAL}",
                        "stub.request",
                        "auth",
                    )
                    messages = payload.get("messages")
                    require(isinstance(messages, list) and messages, "stub.request", "messages")
                    content = messages[-1].get("content")
                    require(isinstance(content, str), "stub.request", "content")
                except (AcceptanceFailure, UnicodeError, ValueError, json.JSONDecodeError):
                    self._json(400, {"error": {"message": "invalid"}})
                    return
                owner.chat_calls += 1
                if content == FAILURE_INPUT:
                    owner.failure_calls += 1
                    self._json(503, {"error": {"message": FAILURE_OUTPUT, "type": "server_error"}})
                    return
                if payload.get("stream") is True:
                    owner.stream_calls += 1
                    first = {
                        "id": "chatcmpl-prd084-stream",
                        "object": "chat.completion.chunk",
                        "choices": [
                            {"index": 0, "delta": {"content": STREAM_OUTPUT}, "finish_reason": None}
                        ],
                    }
                    final = {
                        "id": "chatcmpl-prd084-stream",
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                    }
                    wire = (
                        "data: "
                        + json.dumps(first, separators=(",", ":"))
                        + "\n\ndata: "
                        + json.dumps(final, separators=(",", ":"))
                        + "\n\ndata: [DONE]\n\n"
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(wire)))
                    self.end_headers()
                    self.wfile.write(wire)
                    return
                self._json(
                    200,
                    {
                        "id": "chatcmpl-prd084",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": NON_STREAM_OUTPUT},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
                    },
                )

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                wire = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{port}/v1", port

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


class WebhookReceiver:
    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.received: list[tuple[str, bytes]] = []

    def start(self) -> tuple[str, int]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/events" or not 0 < length <= 64 * 1024:
                    self.send_response(400)
                    self.end_headers()
                    return
                body = self.rfile.read(length)
                owner.received.append((self.headers.get("X-MetaHub-Signature", ""), body))
                self.send_response(204)
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{port}/events", port

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


@dataclass
class OwnedProcess:
    name: str
    process: subprocess.Popen[bytes]
    stdout: Any
    stderr: Any


def start_process(name: str, module: str, port: int, environment: dict[str, str]) -> OwnedProcess:
    stdout = (WORK_ROOT / "logs" / f"{name}.stdout.log").open("wb")
    stderr = (WORK_ROOT / "logs" / f"{name}.stderr.log").open("wb")
    process = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            module,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
            "--no-proxy-headers",
        ],
        cwd=str(ROOT),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return OwnedProcess(name, process, stdout, stderr)


def stop_process(item: OwnedProcess) -> None:
    if item.process.poll() is None:
        item.process.terminate()
        try:
            item.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            item.process.kill()
            item.process.wait(timeout=5)
    item.stdout.close()
    item.stderr.close()


def wait_health(base_url: str, item: OwnedProcess) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if item.process.poll() is not None:
            raise AcceptanceFailure(f"service.{item.name}", "process_exited")
        try:
            if httpx.get(base_url + "/health", timeout=1, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AcceptanceFailure(f"service.{item.name}", "health_timeout")


def headers(credential: str | None, label: str) -> tuple[dict[str, str], str]:
    request_id = f"req_prd084_runtime_{label}"
    result = {"X-Request-ID": request_id, "X-Trace-ID": f"trc_prd084_runtime_{label}"}
    if credential is not None:
        result["Authorization"] = f"Bearer {credential}"
    return result, request_id


def json_payload(response: httpx.Response, expected: int, stage: str) -> dict[str, Any]:
    require(response.status_code == expected, stage, f"status_{response.status_code}")
    parsed = response.json()
    require(isinstance(parsed, dict), stage, "body_shape")
    return parsed


def transient_control_call(
    operation: Callable[[], httpx.Response],
    *,
    retry_codes: frozenset[str] = frozenset({"rbac.unavailable"}),
) -> httpx.Response:
    """Retry only the public fail-closed code produced by transient SQLite writer contention."""

    for attempt in range(6):
        response = operation()
        if response.status_code != 503:
            return response
        try:
            code = response.json().get("code")
        except (ValueError, AttributeError):
            return response
        if code not in retry_codes or attempt == 5:
            return response
        time.sleep(1.5)
    raise AssertionError("bounded retry loop is exhaustive")


def service_environment(
    database_url: str,
    control_port: int,
    gateway_port: int,
    stub_port: int,
) -> dict[str, str]:
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "OS",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(
                str(path)
                for path in (
                    ROOT / "shared-sdk" / "python",
                    ROOT / "gateway-data-plane" / "src",
                    ROOT / "control-plane-api" / "src",
                )
            ),
            "TEMP": str(WORK_ROOT / "temp"),
            "TMP": str(WORK_ROOT / "temp"),
            "METAHUB_ENV": "dev",
            "METAHUB_DATABASE_URL": database_url,
            "METAHUB_SEED_ADMIN_PASSWORD": ADMIN_PASSWORD,
            "METAHUB_AUTH_TOKEN_SECRET": AUTH_SECRET,
            "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": ENCRYPTION_SECRET,
            "METAHUB_GATEWAY_API_BASE_URL": f"http://127.0.0.1:{gateway_port}",
            "METAHUB_CONTROL_API_BASE_URL": f"http://127.0.0.1:{control_port}",
            "METAHUB_ALLOWED_ORIGINS": "http://localhost:3000",
            "METAHUB_LOCAL_ENDPOINT_ALLOWED_PORTS": str(stub_port),
            "METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK": "true",
            "METAHUB_LOCAL_ENDPOINT_CONNECT_TIMEOUT_SECONDS": "3",
            "METAHUB_LOCAL_ENDPOINT_OVERALL_TIMEOUT_SECONDS": "15",
            "METAHUB_PROVIDER_HEALTH_ENABLED": "false",
            "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED": "false",
            "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED": "false",
            "METAHUB_USAGE_EXPORT_ENABLED": "false",
            "METAHUB_REQUEST_EXPLANATION_EVENT_ENABLED": "false",
            "METAHUB_REQUEST_LOG_QUERY_EVENT_ENABLED": "false",
            "METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED": "false",
            "METAHUB_APPROVAL_OUTBOX_ENABLED": "false",
            "METAHUB_APPROVAL_WRITEBACK_ENABLED": "false",
            "METAHUB_FINOPS_USAGE_SPOOL_POLL_INTERVAL_SECONDS": "60",
            "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_POLL_INTERVAL_SECONDS": "60",
            "METAHUB_FINOPS_USAGE_SPOOL_ROOT": str(WORK_ROOT / "spool"),
            "METAHUB_LOG_LEVEL": "INFO",
        }
    )
    return environment


def prepare_database() -> tuple[str, str, dict[str, Any]]:
    database = WORK_ROOT / "runtime.db"
    database_url = f"sqlite:///{database.as_posix()}"
    seed_environment = {
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_SEED_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "METAHUB_AUTH_TOKEN_SECRET": AUTH_SECRET,
        "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": ENCRYPTION_SECRET,
    }
    previous = {key: os.environ.get(key) for key in seed_environment}
    os.environ.update(seed_environment)
    try:
        migration = apply_migrations(database_url)
        seed = apply_seed_data(database_url)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    require(len(migration.applied) == 77, "database.migration", "count")
    require(migration.applied[-1] == "0077", "database.migration", "version")
    return database_url, seed.tenant_id, {
        "count": len(migration.applied),
        "latest": migration.applied[-1],
    }


def wait_local_event_settlement(database_url: str, expected: int) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with closing(connect_database(database_url)) as connection:
                source = connection.execute(
                    """
                    SELECT COUNT(*) FROM local_model_call_event_outbox
                    WHERE status='sent'
                    """
                ).fetchone()[0]
                deliveries = connection.execute(
                    """
                    SELECT COUNT(*) FROM webhook_deliveries
                    WHERE event_type='local_model.called' AND status='success'
                    """
                ).fetchone()[0]
            if int(source) == expected and int(deliveries) == expected:
                return
        except sqlite3.Error:
            pass
        time.sleep(0.2)
    raise AcceptanceFailure("webhook", "settlement_timeout")


def scan_files(paths: list[Path], values: list[str]) -> int:
    total = 0
    needles = [value.encode("utf-8") for value in values if value]
    for path in paths:
        try:
            if path.exists() and path.is_file():
                data = path.read_bytes()
                total += sum(data.count(needle) for needle in needles)
        except OSError as error:
            raise AcceptanceFailure("privacy", "file_read_failed") from error
    return total


def source_hashes() -> dict[str, str]:
    files = {
        "migration": ROOT / "infra" / "migrations" / "0077_local_model_endpoint_access.sql",
        "control": (
            ROOT
            / "control-plane-api"
            / "src"
            / "metahub_control"
            / "local_endpoints"
            / "service.py"
        ),
        "gateway": (
            ROOT
            / "gateway-data-plane"
            / "src"
            / "metahub_gateway"
            / "api"
            / "openai_compatible.py"
        ),
        "events": ROOT / "gateway-data-plane" / "src" / "metahub_gateway" / "local_model_events.py",
        "acceptance": ROOT / "scripts" / "acceptance" / "prd084-runtime-acceptance.py",
    }
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}


def publish_evidence(
    serialized: str,
    *,
    artifact_root: Path = ARTIFACT_ROOT,
    run_id: str = RUN_ID,
) -> tuple[Path, str]:
    artifact_root.mkdir(parents=True, exist_ok=True)
    target = artifact_root / f"{run_id}.json"
    pending = artifact_root / f".{run_id}.pending.json"
    pending.write_bytes(serialized.encode("utf-8"))
    os.replace(pending, target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = artifact_root / f"{run_id}.sha256"
    pending_manifest = artifact_root / f".{run_id}.pending.sha256"
    pending_manifest.write_bytes(f"{digest}  {target.name}\n".encode("ascii"))
    os.replace(pending_manifest, manifest)
    return target, digest


def release_check(ports: list[int]) -> bool:
    for port in ports:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
                candidate.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def remove_tree(path: Path) -> None:
    for _ in range(20):
        try:
            if path.exists():
                shutil.rmtree(path)
            return
        except OSError:
            time.sleep(0.25)
    raise AcceptanceFailure("cleanup", "work_tree_locked")


def main() -> int:
    require(PYTHON.is_file(), "preflight", "python_missing")
    remove_tree(WORK_ROOT)
    (WORK_ROOT / "logs").mkdir(parents=True)
    (WORK_ROOT / "temp").mkdir(parents=True)
    (WORK_ROOT / "spool").mkdir(parents=True)
    stub = OpenAIStub()
    receiver = WebhookReceiver()
    processes: list[OwnedProcess] = []
    control_process: OwnedProcess | None = None
    gateway_process: OwnedProcess | None = None
    stub_url = ""
    receiver_url = ""
    signing_secret = ""
    virtual_key = ""
    evidence: dict[str, Any] | None = None
    ports: list[int] = []
    try:
        stub_url, stub_port = stub.start()
        receiver_url, receiver_port = receiver.start()
        control_port = free_port()
        gateway_port = free_port()
        ports = [stub_port, receiver_port, control_port, gateway_port]
        require(len(set(ports)) == 4, "preflight", "port_collision")
        database_url, tenant_id, migration = prepare_database()
        environment = service_environment(database_url, control_port, gateway_port, stub_port)
        control_process = start_process(
            "control-config", "metahub_control.app:app", control_port, environment
        )
        processes.append(control_process)
        control_base = f"http://127.0.0.1:{control_port}"
        wait_health(control_base, control_process)
        gateway_base = f"http://127.0.0.1:{gateway_port}"
        # Let one-shot startup recovery transactions finish before API acceptance begins.
        time.sleep(10)

        with (
            httpx.Client(base_url=control_base, timeout=20, trust_env=False) as control,
            httpx.Client(base_url=gateway_base, timeout=180, trust_env=False) as gateway,
        ):
            login_headers, _ = headers(None, "login")
            login = json_payload(
                transient_control_call(
                    lambda: control.post(
                        "/auth/login",
                        headers=login_headers,
                        json={
                            "tenant_slug": "default",
                            "email": "admin@metahub.local",
                            "password": ADMIN_PASSWORD,
                        },
                    )
                ),
                200,
                "control.login",
            )
            admin_token = str(login.get("access_token", ""))
            require(len(admin_token) > 40, "control.login", "credential_missing")

            webhook_headers, _ = headers(admin_token, "webhook_create")
            webhook = json_payload(
                transient_control_call(
                    lambda: control.post(
                        "/webhooks",
                        headers=webhook_headers,
                        json={
                            "name": "PRD-084 runtime receiver",
                            "endpoint_url": receiver_url,
                            "event_types": ["local_model.called"],
                            "status": "active",
                            "timeout_ms": 3000,
                            "format": "json",
                        },
                    )
                ),
                200,
                "control.webhook_create",
            )
            signing_secret = str(webhook.get("signing_secret", ""))
            require(signing_secret.startswith("whsec_"), "control.webhook_create", "secret_missing")

            create_headers, _ = headers(admin_token, "aggregate_create")
            create_response = transient_control_call(
                lambda: control.post(
                    "/local-endpoints",
                    headers=create_headers,
                    json={
                        "provider_code": "prd084_runtime_local",
                        "name": "PRD-084 runtime local",
                        "engine_kind": "vllm",
                        "base_url": stub_url,
                        "auth_type": "bearer",
                        "bearer_token": PROVIDER_CREDENTIAL,
                        "initial_model": {
                            "internal_model_id": "prd084-runtime-chat",
                            "provider_model_id": "served-chat",
                            "display_name": "PRD-084 runtime chat",
                            "capabilities": {"chat": True, "stream": True},
                            "context_window": 32768,
                            "input_price_per_1k": "1",
                            "output_price_per_1k": "2",
                            "currency": "CNY",
                        },
                    },
                ),
                retry_codes=frozenset(
                    {"rbac.unavailable", "local_endpoint.unavailable"}
                ),
            )
            require(
                stub_url not in create_response.text,
                "control.aggregate_create",
                "location_exposed",
            )
            require(
                PROVIDER_CREDENTIAL not in create_response.text,
                "control.aggregate_create",
                "credential_exposed",
            )
            created = json_payload(create_response, 201, "control.aggregate_create")
            aggregate = created.get("local_endpoint")
            require(isinstance(aggregate, dict), "control.aggregate_create", "shape")
            aggregate_id = str(aggregate.get("local_endpoint_id", ""))
            require(aggregate_id.startswith("lme_"), "control.aggregate_create", "id")
            initial_model = aggregate.get("initial_model", {})
            require(isinstance(initial_model, dict), "control.aggregate_create", "model_shape")
            model_alias = str(initial_model.get("internal_model_id", ""))
            model_id = str(initial_model.get("model_id", ""))
            require(model_alias == "prd084-runtime-chat", "control.aggregate_create", "model")
            require(bool(model_id), "control.aggregate_create", "model_id")

            list_headers, _ = headers(admin_token, "aggregate_list")
            listed = json_payload(
                transient_control_call(
                    lambda: control.get("/local-endpoints", headers=list_headers)
                ),
                200,
                "control.aggregate_list",
            )
            require(
                stub_url not in json.dumps(listed),
                "control.aggregate_list",
                "location_exposed",
            )

            probe_headers, _ = headers(admin_token, "aggregate_probe")
            probed = json_payload(
                transient_control_call(
                    lambda: control.post(
                        f"/local-endpoints/{aggregate_id}/test",
                        headers=probe_headers,
                        json={},
                    )
                ),
                200,
                "control.aggregate_probe",
            )
            require(
                probed.get("test_result", {}).get("status") == "success",
                "control.aggregate_probe",
                "result",
            )
            require(
                int(probed.get("test_result", {}).get("discovered_model_count", 0)) == 2,
                "control.aggregate_probe",
                "catalog",
            )

            price_headers, _ = headers(admin_token, "price_create")
            effective_from = (datetime.now(UTC) - timedelta(days=1)).replace(
                microsecond=0
            ).isoformat().replace("+00:00", "Z")
            price = json_payload(
                transient_control_call(
                    lambda: control.post(
                        "/model-prices",
                        headers=price_headers,
                        json={
                            "model_id": model_id,
                            "input_price_per_1k": "1",
                            "output_price_per_1k": "2",
                            "currency": "CNY",
                            "effective_from": effective_from,
                            "price_schema_version": "prd084-runtime-price-v1",
                        },
                    )
                ),
                201,
                "control.price_create",
            )
            require(
                price.get("price_plan", {}).get("price_schema_version")
                == "prd084-runtime-price-v1",
                "control.price_create",
                "version",
            )

            key_headers, _ = headers(admin_token, "key_create")
            key_payload = json_payload(
                transient_control_call(
                    lambda: control.post(
                        "/virtual-keys",
                        headers=key_headers,
                        json={
                            "name": "PRD-084 runtime key",
                            "environment": "dev",
                            "description": "PRD-084 runtime acceptance",
                        },
                    )
                ),
                201,
                "control.key_create",
            )
            virtual_key = str(key_payload.get("plain_value", ""))
            virtual_key_id = str(key_payload.get("virtual_key", {}).get("virtual_key_id", ""))
            require(virtual_key.startswith("mhv_"), "control.key_create", "credential_missing")

            stop_process(control_process)
            processes.remove(control_process)
            control_process = None
            gateway_process = start_process(
                "gateway-calls", "metahub_gateway.app:app", gateway_port, environment
            )
            processes.append(gateway_process)
            wait_health(gateway_base, gateway_process)
            time.sleep(10)

            request_ids: dict[str, str] = {}
            chat_headers, request_ids["chat"] = headers(virtual_key, "chat")
            chat = gateway.post(
                "/v1/chat/completions",
                headers=chat_headers,
                json={
                    "model": model_alias,
                    "messages": [{"role": "user", "content": NON_STREAM_INPUT}],
                    "stream": False,
                },
            )
            require(chat.status_code == 200, "gateway.chat", f"status_{chat.status_code}")
            require(NON_STREAM_OUTPUT in chat.text, "gateway.chat", "wire")

            stream_headers, request_ids["stream"] = headers(virtual_key, "stream")
            with gateway.stream(
                "POST",
                "/v1/chat/completions",
                headers=stream_headers,
                json={
                    "model": model_alias,
                    "messages": [{"role": "user", "content": STREAM_INPUT}],
                    "stream": True,
                },
            ) as stream_response:
                require(
                    stream_response.status_code == 200,
                    "gateway.stream",
                    f"status_{stream_response.status_code}",
                )
                stream_wire = b"".join(stream_response.iter_bytes())
            require(STREAM_OUTPUT.encode() in stream_wire, "gateway.stream", "wire")
            require(b"data: [DONE]" in stream_wire, "gateway.stream", "terminal")

            failure_headers, request_ids["failure"] = headers(virtual_key, "failure")
            failure = gateway.post(
                "/v1/chat/completions",
                headers=failure_headers,
                json={
                    "model": model_alias,
                    "messages": [{"role": "user", "content": FAILURE_INPUT}],
                    "stream": False,
                },
            )
            require(failure.status_code in {502, 503, 504}, "gateway.failure", "status")
            failure_payload = failure.json()
            failure_code = str(failure_payload.get("code", ""))
            require(failure_code.startswith("gateway."), "gateway.failure", "code")
            require(FAILURE_OUTPUT not in failure.text, "gateway.failure", "upstream_body_exposed")

            delivery_deadline = time.monotonic() + 60
            while time.monotonic() < delivery_deadline and len(receiver.received) < 3:
                time.sleep(0.2)
            require(len(receiver.received) == 3, "webhook", "delivery_count")
            wait_local_event_settlement(database_url, 3)
            stop_process(gateway_process)
            processes.remove(gateway_process)
            gateway_process = None
            control_process = start_process(
                "control-policy", "metahub_control.app:app", control_port, environment
            )
            processes.append(control_process)
            wait_health(control_base, control_process)
            time.sleep(10)

            dlp_headers, _ = headers(admin_token, "dlp_create")
            dlp = json_payload(
                transient_control_call(
                    lambda: control.post(
                        "/dlp/rules",
                        headers=dlp_headers,
                        json={
                            "name": "PRD-084 runtime input block",
                            "rule_type": "keyword",
                            "pattern": DLP_MARKER,
                            "action": "block",
                            "scan_scope": "input",
                            "status": "active",
                            "priority": 1,
                        },
                    )
                ),
                200,
                "control.dlp_create",
            )
            require(
                str(dlp.get("dlp_rule", {}).get("dlp_rule_id", "")).startswith("dlp_"),
                "control.dlp_create",
                "id",
            )

            budget_headers, _ = headers(admin_token, "budget_create")
            budget = json_payload(
                transient_control_call(
                    lambda: control.post(
                        "/budgets",
                        headers=budget_headers,
                        json={
                            "scope_type": "virtual_key",
                            "scope_id": virtual_key_id,
                            "environment": "dev",
                            "name": "PRD-084 runtime exhausted budget",
                            "daily_budget_cny": "0.000001",
                            "warning_threshold_percent": 80,
                            "exhausted_action": "block",
                            "status": "active",
                        },
                    ),
                    retry_codes=frozenset(
                        {"rbac.unavailable", "budget.status_unavailable"}
                    ),
                ),
                201,
                "control.budget_create",
            )
            require(
                str(budget.get("budget_rule", {}).get("budget_rule_id", "")),
                "control.budget_create",
                "id",
            )
            stop_process(control_process)
            processes.remove(control_process)
            control_process = None
            gateway_process = start_process(
                "gateway-policy", "metahub_gateway.app:app", gateway_port, environment
            )
            processes.append(gateway_process)
            wait_health(gateway_base, gateway_process)
            time.sleep(10)

            before_dlp = stub.chat_calls
            dlp_call_headers, request_ids["dlp"] = headers(virtual_key, "dlp")
            dlp_call = gateway.post(
                "/v1/chat/completions",
                headers=dlp_call_headers,
                json={
                    "model": model_alias,
                    "messages": [{"role": "user", "content": DLP_INPUT}],
                    "stream": False,
                },
            )
            dlp_error = json_payload(dlp_call, 403, "gateway.dlp")
            require(dlp_error.get("code") == "gateway.dlp_blocked", "gateway.dlp", "code")
            require(stub.chat_calls == before_dlp, "gateway.dlp", "upstream_called")

            before_budget = stub.chat_calls
            budget_call_headers, request_ids["budget"] = headers(virtual_key, "budget")
            budget_call = gateway.post(
                "/v1/chat/completions",
                headers=budget_call_headers,
                json={
                    "model": model_alias,
                    "messages": [{"role": "user", "content": BUDGET_INPUT}],
                    "stream": False,
                },
            )
            budget_error = json_payload(budget_call, 402, "gateway.budget")
            require(
                budget_error.get("code") == "gateway.budget_exhausted",
                "gateway.budget",
                "code",
            )
            require(stub.chat_calls == before_budget, "gateway.budget", "upstream_called")
            stop_process(gateway_process)
            processes.remove(gateway_process)
            gateway_process = None

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and len(receiver.received) < 3:
            time.sleep(0.2)
        require(len(receiver.received) == 3, "webhook", "delivery_count")
        webhook_events = []
        for signature, body in receiver.received:
            require(
                verify_webhook_signature(signature, signing_secret, body),
                "webhook",
                "signature",
            )
            parsed = json.loads(body)
            require(parsed.get("event_type") == "local_model.called", "webhook", "event_type")
            webhook_events.append(str(parsed.get("event_id", "")))
        require(len(set(webhook_events)) == 3, "webhook", "event_id_duplicate")

        with closing(connect_database(database_url)) as connection:
            logs = connection.execute(
                """
                SELECT request_id,status,error_code,dlp_status,budget_status,routing_attempt_count
                FROM request_logs WHERE request_id IN(?,?,?,?,?) ORDER BY request_id
                """,
                tuple(request_ids.values()),
            ).fetchall()
            attempts = connection.execute(
                """
                SELECT status,provider_call_status,usage_status FROM routing_attempts
                WHERE request_id IN(?,?,?) ORDER BY request_id
                """,
                (request_ids["chat"], request_ids["stream"], request_ids["failure"]),
            ).fetchall()
            usage = connection.execute(
                """
                SELECT COUNT(*) AS rows,COALESCE(SUM(total_tokens),0) AS tokens,
                       COALESCE(SUM(cost_cny),0) AS cost
                FROM usage_ledger WHERE request_id IN(?,?)
                """,
                (request_ids["chat"], request_ids["stream"]),
            ).fetchone()
            local_events = connection.execute(
                "SELECT outcome,usage_status FROM local_model_call_events ORDER BY completed_at"
            ).fetchall()
            source_outbox = connection.execute(
                "SELECT status,COUNT(*) AS count FROM local_model_call_event_outbox GROUP BY status"
            ).fetchall()
            deliveries = connection.execute(
                """
                SELECT status,COUNT(*) AS count FROM webhook_deliveries
                WHERE event_type='local_model.called' GROUP BY status
                """
            ).fetchall()
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())

        require(len(logs) == 5, "database.logs", "count")
        logs_by_request_id = {str(row["request_id"]): row for row in logs}
        require(
            set(logs_by_request_id) == set(request_ids.values()),
            "database.logs",
            "request_ids",
        )
        require(
            all(
                logs_by_request_id[request_ids[label]]["status"] == "success"
                and logs_by_request_id[request_ids[label]]["error_code"] is None
                for label in ("chat", "stream")
            ),
            "database.logs",
            "success_terminal",
        )
        require(
            logs_by_request_id[request_ids["failure"]]["status"] == "failed"
            and logs_by_request_id[request_ids["failure"]]["error_code"] == failure_code,
            "database.logs",
            "upstream_failure_terminal",
        )
        require(
            logs_by_request_id[request_ids["dlp"]]["status"] == "failed"
            and logs_by_request_id[request_ids["dlp"]]["error_code"]
            == "gateway.dlp_blocked"
            and logs_by_request_id[request_ids["dlp"]]["dlp_status"] == "blocked",
            "database.logs",
            "dlp_terminal",
        )
        require(
            logs_by_request_id[request_ids["budget"]]["status"] == "failed"
            and logs_by_request_id[request_ids["budget"]]["error_code"]
            == "gateway.budget_exhausted"
            and logs_by_request_id[request_ids["budget"]]["budget_status"] == "exhausted",
            "database.logs",
            "budget_terminal",
        )
        require(len(attempts) == 3, "database.attempts", "count")
        require(
            sum(1 for row in attempts if row["provider_call_status"] == "called") == 3,
            "database.attempts",
            "call_fence",
        )
        require(int(usage["rows"]) == 2, "database.usage", "rows")
        require(int(usage["tokens"]) == 20, "database.usage", "tokens")
        require(float(usage["cost"]) > 0, "database.usage", "cost")
        require(len(local_events) == 3, "database.events", "count")
        require(
            {row["outcome"] for row in local_events} == {"succeeded", "failed"},
            "database.events",
            "outcome",
        )
        require(
            [(row["status"], int(row["count"])) for row in source_outbox] == [("sent", 3)],
            "database.source_outbox",
            "terminal",
        )
        require(
            [(row["status"], int(row["count"])) for row in deliveries] == [("success", 3)],
            "database.deliveries",
            "terminal",
        )
        require(integrity == "ok" and foreign_keys == 0, "database.integrity", "failed")

        sensitive_values = [
            ADMIN_PASSWORD,
            AUTH_SECRET,
            ENCRYPTION_SECRET,
            PROVIDER_CREDENTIAL,
            virtual_key,
            NON_STREAM_INPUT,
            STREAM_INPUT,
            FAILURE_INPUT,
            DLP_INPUT,
            BUDGET_INPUT,
            NON_STREAM_OUTPUT,
            STREAM_OUTPUT,
            FAILURE_OUTPUT,
            stub_url,
            receiver_url,
        ]
        log_files = list((WORK_ROOT / "logs").glob("*.log"))
        log_hits = scan_files(log_files, sensitive_values)
        database_path = WORK_ROOT / "runtime.db"
        database_files = [
            path
            for path in (
                database_path,
                Path(str(database_path) + "-wal"),
                Path(str(database_path) + "-shm"),
            )
            if path.exists()
        ]
        database_hits = scan_files(
            database_files,
            [
                ADMIN_PASSWORD,
                AUTH_SECRET,
                ENCRYPTION_SECRET,
                PROVIDER_CREDENTIAL,
                virtual_key,
                NON_STREAM_INPUT,
                STREAM_INPUT,
                FAILURE_INPUT,
                DLP_INPUT,
                BUDGET_INPUT,
                NON_STREAM_OUTPUT,
                STREAM_OUTPUT,
                FAILURE_OUTPUT,
            ],
        )
        require(log_hits == 0, "privacy.logs", "sensitive_canary")
        require(database_hits == 0, "privacy.database", "sensitive_canary")

        evidence = {
            "schema_version": "1.0",
            "prd_id": "PRD-084",
            "status": "passed",
            "generated_at": utc_now(),
            "all_108_prds_complete": False,
            "migration": migration,
            "services": {
                "control_http": True,
                "gateway_http": True,
                "openai_compatible_stub": True,
                "signed_webhook_receiver": True,
            },
            "workflow": {
                "aggregate_created": True,
                "catalog_probe_succeeded": True,
                "non_stream_succeeded": True,
                "stream_succeeded": True,
                "upstream_failure_tracked": True,
                "dlp_blocked_before_upstream": True,
                "budget_blocked_before_upstream": True,
            },
            "traceability": {
                "request_log_rows": len(logs),
                "succeeded_request_rows": sum(1 for row in logs if row["status"] == "success"),
                "failed_request_rows": sum(1 for row in logs if row["status"] == "failed"),
                "routing_attempt_rows": len(attempts),
                "called_attempt_rows": sum(
                    1 for row in attempts if row["provider_call_status"] == "called"
                ),
                "usage_rows": int(usage["rows"]),
                "usage_total_tokens": int(usage["tokens"]),
                "positive_cost": float(usage["cost"]) > 0,
                "local_call_event_rows": len(local_events),
                "source_outbox_sent_rows": 3,
                "signed_event_rows": len(receiver.received),
                "failure_code": failure_code,
                "dlp_code": "gateway.dlp_blocked",
                "budget_code": "gateway.budget_exhausted",
            },
            "database": {
                "integrity": integrity,
                "foreign_key_violations": foreign_keys,
                "nonterminal_source_rows": 0,
                "nonterminal_delivery_rows": 0,
            },
            "privacy": {
                "service_log_canary_hits": log_hits,
                "database_canary_hits": database_hits,
                "formal_evidence_canary_hits": 0,
            },
            "source_provenance": source_hashes(),
        }
    finally:
        for item in reversed(processes):
            stop_process(item)
        receiver.stop()
        stub.stop()

    require(evidence is not None, "evidence", "missing")
    released = release_check(ports)
    require(released, "cleanup", "ports_busy")
    remove_tree(WORK_ROOT)
    evidence["cleanup"] = {
        "owned_processes_remaining": 0,
        "owned_ports_released": True,
        "runtime_tree_removed": True,
    }
    serialized = serialize_safe_evidence(
        evidence,
        (
            ADMIN_PASSWORD,
            AUTH_SECRET,
            ENCRYPTION_SECRET,
            PROVIDER_CREDENTIAL,
            virtual_key,
            NON_STREAM_INPUT,
            STREAM_INPUT,
            FAILURE_INPUT,
            DLP_INPUT,
            BUDGET_INPUT,
            NON_STREAM_OUTPUT,
            STREAM_OUTPUT,
            FAILURE_OUTPUT,
            stub_url,
            receiver_url,
        ),
    )
    target, digest = publish_evidence(serialized)
    print(
        json.dumps(
            {
                "status": "passed",
                "artifact": str(target.relative_to(ROOT)),
                "sha256": digest,
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AcceptanceFailure as exc:
        print(json.dumps({"status": "failed", "stage": exc.stage, "code": exc.code}))
        raise SystemExit(1) from None
