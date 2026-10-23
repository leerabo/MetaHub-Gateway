"""Read-only host preflight tests; no Docker, network, or host mutation."""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("ubuntu_preflight", ROOT / "scripts/deploy/preflight-ubuntu.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_parse_listeners_keeps_only_address_and_port():
    value = """LISTEN 0 4096 127.0.0.1:5432 0.0.0.0:*\nLISTEN 0 4096 0.0.0.0:8001 0.0.0.0:*\nLISTEN 0 4096 [::1]:443 [::]:*\n"""
    assert MODULE.parse_listeners(value) == [
        {"address": "127.0.0.1", "port": 5432},
        {"address": "0.0.0.0", "port": 8001},
        {"address": "[::1]", "port": 443},
    ]


def test_preflight_is_passable_with_private_internal_ports(monkeypatch):
    monkeypatch.setattr(MODULE.platform, "system", lambda: "Linux")
    monkeypatch.setattr(MODULE, "read_os_release", lambda: {"ID": "ubuntu", "VERSION_ID": "24.04"})
    monkeypatch.setattr(MODULE.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(MODULE.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(MODULE, "read_memory_bytes", lambda: 8 * 1024**3)
    monkeypatch.setattr(MODULE.shutil, "disk_usage", lambda _: type("Usage", (), {"free": 80 * 1024**3})())
    monkeypatch.setattr(MODULE.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(MODULE.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("203.0.113.10", 443))])

    def fake_command(*args, **kwargs):
        if args[:2] == ("docker", "version"):
            return 0, "27.0", ""
        if args[:2] == ("docker", "compose"):
            return 0, "2.29", ""
        if args[:2] == ("timedatectl", "show"):
            return 0, "yes", ""
        if args[:2] == ("ufw", "status"):
            return 0, "Status: active", ""
        if args[:2] == ("ss", "-ltnH"):
            return 0, "LISTEN 0 4096 127.0.0.1:5432 0.0.0.0:*\nLISTEN 0 4096 127.0.0.1:8001 0.0.0.0:*", ""
        return 127, "", ""

    monkeypatch.setattr(MODULE, "command", fake_command)
    report = MODULE.run_preflight("trial.example.com", "/tmp/metahub-state-new")
    assert report["status"] == "pass"
    assert report["read_only"] is True
    assert report["summary"] == {"failed": 0, "warnings": 0, "passed": 9}


def test_preflight_fails_when_internal_api_is_public(monkeypatch):
    monkeypatch.setattr(MODULE.platform, "system", lambda: "Linux")
    monkeypatch.setattr(MODULE, "read_os_release", lambda: {"ID": "ubuntu", "VERSION_ID": "24.04"})
    monkeypatch.setattr(MODULE.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(MODULE.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(MODULE, "read_memory_bytes", lambda: 8 * 1024**3)
    monkeypatch.setattr(MODULE.shutil, "disk_usage", lambda _: type("Usage", (), {"free": 80 * 1024**3})())
    monkeypatch.setattr(MODULE.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(MODULE.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("203.0.113.10", 443))])

    def fake_command(*args, **kwargs):
        if args[:2] == ("docker", "version") or args[:2] == ("docker", "compose"):
            return 0, "version", ""
        if args[:2] == ("timedatectl", "show"):
            return 0, "yes", ""
        if args[:2] == ("ufw", "status"):
            return 0, "Status: active", ""
        if args[:2] == ("ss", "-ltnH"):
            return 0, "LISTEN 0 4096 0.0.0.0:8001 0.0.0.0:*", ""
        return 127, "", ""

    monkeypatch.setattr(MODULE, "command", fake_command)
    report = MODULE.run_preflight("trial.example.com", "/tmp/metahub-state-new")
    assert report["status"] == "fail"
    internal = next(item for item in report["checks"] if item["name"] == "internal-listeners")
    assert internal["status"] == "fail"
    assert "0.0.0.0:8001" in internal["detail"]
