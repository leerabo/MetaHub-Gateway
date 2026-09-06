"""Shipped Nginx policy: normalized retirement, surviving routes, and app rollback."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import yaml
from metahub_control.console_migration.manifest import LEGACY_ROUTE_MANIFEST

ROOT = Path(__file__).resolve().parents[3]
NGINX = os.getenv("METAHUB_TEST_NGINX_BINARY") or shutil.which("nginx")


def test_shipped_policy_matches_manifest_and_keeps_backend_unpublished():
    spec = importlib.util.spec_from_file_location(
        "control_ingress_generator", ROOT / "scripts/deploy/generate_control_ingress.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (ROOT / "infra/ingress/retired-console-routes.conf").read_text(
        encoding="ascii"
    ) == module.render_retired_routes()
    compose = yaml.safe_load((ROOT / "infra/compose/docker-compose.yml").read_text("utf-8"))
    services = compose["services"]
    config = (ROOT / "infra/ingress/nginx.conf").read_text("ascii")
    assert "error_log /dev/null crit;" in config
    assert "access_log off;" in config
    assert "ports" not in services["control-api"]
    ingress = services["control-ingress"]
    assert ingress["image"].endswith("/control-ingress:prd147-v1")
    assert "METAHUB_IMAGE_TAG" not in ingress["image"]
    assert ingress["read_only"] and ingress["cap_drop"] == ["ALL"]
    assert "env_file" not in ingress
    for profile in ("local-sqlite", "compose-full"):
        data = json.loads((ROOT / f"infra/compose/profiles/{profile}.json").read_text("utf-8"))
        assert data["services"]["control-api"]["ports"] == []
        assert data["services"]["control-ingress"]["port"] == 8001


class _RollbackBackend(BaseHTTPRequestHandler):
    """An adversarial old backend makes every retired request visibly succeed."""

    generation = "N-1"
    calls: list[str] = []

    def _handle(self):
        path = urlsplit(self.path).path
        self.calls.append(path)
        if path == "/upstream-error-probe":
            self.close_connection = True
            return
        unknown = path.startswith(("/__metahub_route_not_found__", "/unknown-"))
        payload = json.dumps(
            {"code": "infra.not_found"} if unknown else {"generation": self.generation}
        ).encode()
        self.send_response(404 if unknown else 200)
        self.send_header("Content-Type", "application/json")
        # Simulate an old backend incorrectly caching its unknown-route response.
        self.send_header("Cache-Control", "public, max-age=60")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_HEAD = do_POST = do_PATCH = do_PUT = do_DELETE = do_OPTIONS = _handle

    def log_message(self, _format, *_args):
        pass


@pytest.mark.skipif(not NGINX, reason="Set METAHUB_TEST_NGINX_BINARY for actual proxy tests")
def test_real_nginx_retirement_survives_old_current_and_rollback_backends(tmp_path):
    backend = ThreadingHTTPServer(("127.0.0.1", 0), _RollbackBackend)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    _RollbackBackend.calls = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = (ROOT / "infra/ingress/nginx.conf").read_text("ascii")
    config = config.replace("error_log /dev/null", f"error_log {os.devnull}")
    config = config.replace("worker_processes auto;", "worker_processes 1;")
    config = config.replace("/tmp/", tmp_path.as_posix() + "/")
    config = config.replace("/etc/nginx/retired-console-routes.conf", (
        ROOT / "infra/ingress/retired-console-routes.conf"
    ).as_posix())
    config = config.replace("control-api:8001", f"127.0.0.1:{backend.server_port}")
    config = config.replace("listen 8080;", f"listen 127.0.0.1:{port};")
    config_path = tmp_path / "nginx.conf"
    config_path.write_text(config, encoding="ascii")
    (tmp_path / "logs").mkdir()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    args = [str(NGINX), "-p", tmp_path.as_posix() + "/", "-c", config_path.as_posix()]
    subprocess.run([*args, "-t"], check=True, capture_output=True, creationflags=flags)
    process = subprocess.Popen(
        [*args, "-g", "daemon off;"], stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, creationflags=flags,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            deadline = time.monotonic() + 15
            while True:
                try:
                    assert client.get("/health").status_code == 200
                    break
                except httpx.ConnectError:
                    if time.monotonic() >= deadline or process.poll() is not None:
                        raise
                    time.sleep(0.05)
            for generation in ("N-1", "N", "rollback-N-1"):
                _RollbackBackend.generation = generation
                assert client.get("/health").json() == {"generation": generation}
                unknown = client.get("/unknown-reference")
                assert unknown.status_code == 404
                for method, template in LEGACY_ROUTE_MANIFEST:
                    path = re.sub(r"\{[^}]+\}", "retired-object", template)
                    for variant in (path, path + "/", path + "?probe=1"):
                        response = client.request(method, variant)
                        assert response.status_code == 404, (generation, method, variant)
                        assert response.json() == unknown.json()
                        assert response.headers["cache-control"] == "no-store"
                        assert _RollbackBackend.calls[-1] == "/__metahub_route_not_found__"
                for method in ("HEAD", "OPTIONS", "PUT", "DELETE"):
                    assert client.request(method, "/partners/profile").status_code == 404
                for path in ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"):
                    assert client.get(path).status_code == 404
                for alias in ("/%70artners/profile", "/partners%2Fprofile", "//partners//profile"):
                    assert client.get(f"http://127.0.0.1:{port}{alias}").status_code == 404
                for path in ("/branding/public", "/platform/console-migration/status",
                             "/workspace/navigation", "/health"):
                    response = client.get(path)
                    assert response.status_code == 200
                    assert response.json() == {"generation": generation}
                    assert response.headers["cache-control"] == "public, max-age=60"
            response = client.get("/upstream-error-probe?probe=must-not-enter-ingress-logs")
            assert response.status_code == 502
    finally:
        subprocess.run([*args, "-s", "quit"], capture_output=True, creationflags=flags)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        stderr = b""
        if process.stderr:
            stderr = process.stderr.read()
            process.stderr.close()
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=5)
        assert b"must-not-enter-ingress-logs" not in stderr
