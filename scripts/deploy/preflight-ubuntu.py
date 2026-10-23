#!/usr/bin/env python3
"""Read-only Ubuntu host preflight for a MetaHub public trial.

The command deliberately does not create state, start containers, change a
firewall, or open a listener.  It emits a redacted JSON report so an operator
can attach it to the deployment handoff without exposing credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any


MIN_CPUS = 2
MIN_MEMORY_BYTES = 4 * 1024**3
MIN_DISK_BYTES = 40 * 1024**3
INTERNAL_PORTS = {5432, 8000, 8001, 8446}


def command(*args: str, timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 127, "", type(exc).__name__
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def parse_listeners(value: str) -> list[dict[str, Any]]:
    """Parse the stable columns emitted by ``ss -ltnH`` without credentials."""
    listeners: list[dict[str, Any]] = []
    for line in value.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        local = fields[3].rsplit(":", 1)
        if len(local) != 2:
            continue
        host, port = local
        try:
            port_number = int(port)
        except ValueError:
            continue
        listeners.append({"address": host, "port": port_number})
    return listeners


def check(
    checks: list[dict[str, Any]], name: str, status: str, detail: str, action: str = ""
) -> None:
    checks.append({"name": name, "status": status, "detail": detail, "action": action})


def read_memory_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    except OSError:
        pass
    return values


def run_preflight(domain: str | None, state: str | None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    system = platform.system()
    os_release = read_os_release()
    if system == "Linux" and os_release.get("ID") == "ubuntu":
        check(checks, "ubuntu", "pass", f"Ubuntu {os_release.get('VERSION_ID', 'unknown')}")
    elif system != "Linux":
        check(checks, "ubuntu", "fail", f"当前系统为 {system}，目标必须是 Ubuntu Linux", "在目标 Ubuntu 主机执行此脚本")
    else:
        check(checks, "ubuntu", "fail", f"发行版为 {os_release.get('ID', 'unknown')}，目标必须是 Ubuntu", "在目标 Ubuntu 主机执行此脚本")

    machine = platform.machine().lower()
    architecture = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if architecture:
        check(checks, "architecture", "pass", architecture)
    else:
        check(checks, "architecture", "fail", f"不支持的架构：{machine}", "使用 x86_64 或 arm64 主机")

    cpus = os.cpu_count() or 0
    memory = read_memory_bytes()
    disk = shutil.disk_usage("/").free
    resource_status = "pass" if cpus >= MIN_CPUS and (memory or 0) >= MIN_MEMORY_BYTES and disk >= MIN_DISK_BYTES else "warn"
    memory_gib = f"{memory / 1024**3:.2f} GiB" if memory else "unknown"
    check(
        checks,
        "resources",
        resource_status,
        f"CPU {cpus}, RAM {memory_gib}, 可用磁盘 {disk / 1024**3:.2f} GiB",
        "建议至少 2 vCPU、4 GiB RAM、40 GiB 可用磁盘" if resource_status == "warn" else "",
    )

    docker_path = shutil.which("docker")
    if not docker_path:
        check(checks, "docker", "warn", "未找到 docker 命令", "先按安装清单安装 Docker Engine 和 Compose 插件")
    else:
        docker_rc, docker_version, _ = command("docker", "version", "--format", "{{.Server.Version}}")
        compose_rc, compose_version, _ = command("docker", "compose", "version", "--short")
        if docker_rc == 0 and compose_rc == 0:
            check(checks, "docker", "pass", f"Docker {docker_version}; Compose {compose_version}")
        else:
            check(checks, "docker", "warn", "Docker 或 Compose 服务端不可用", "确认 Docker daemon 已启动并允许当前用户访问")

    if domain:
        try:
            addresses = sorted({item[4][0] for item in socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)})
        except socket.gaierror:
            addresses = []
        if addresses:
            check(checks, "dns", "pass", f"{domain} 解析到 {', '.join(addresses)}")
        else:
            check(checks, "dns", "warn", f"{domain} 当前无法解析", "先将域名解析到目标主机，再执行公开 HTTPS 验收")
    else:
        check(checks, "dns", "warn", "未提供域名", "部署前必须提供绑定域名")

    timedatectl_rc, ntp, _ = command("timedatectl", "show", "-p", "NTPSynchronized", "--value")
    if timedatectl_rc == 0 and ntp.lower() == "yes":
        check(checks, "time", "pass", "NTP 已同步")
    else:
        check(checks, "time", "warn", "无法确认 NTP 同步状态", "启用 systemd-timesyncd 或同等时间同步服务")

    ufw_rc, ufw, _ = command("ufw", "status")
    if ufw_rc == 0 and "Status: active" in ufw:
        check(checks, "firewall", "pass", "ufw active")
    else:
        nft_rc, _, _ = command("nft", "list", "ruleset")
        check(checks, "firewall", "pass" if nft_rc == 0 else "warn", "检测到 nftables 规则" if nft_rc == 0 else "无法确认主机防火墙", "只允许 SSH、HTTP/HTTPS，禁止数据库和内部 API 公网访问" if nft_rc != 0 else "")

    ss_rc, ss_output, _ = command("ss", "-ltnH")
    if ss_rc == 0:
        exposed = [item for item in parse_listeners(ss_output) if item["port"] in INTERNAL_PORTS and item["address"] not in {"127.0.0.1", "::1", "localhost"}]
        if exposed:
            detail = ", ".join(f"{item['address']}:{item['port']}" for item in exposed)
            check(checks, "internal-listeners", "fail", f"内部端口暴露：{detail}", "关闭公网绑定；数据库和控制/网关 API 只能在私网或容器网络中监听")
        else:
            check(checks, "internal-listeners", "pass", "未发现公网绑定的数据库或内部 API 端口")
    else:
        check(checks, "internal-listeners", "warn", "无法读取监听端口", "安装 iproute2 后重新执行")

    if state:
        state_path = Path(state).expanduser()
        if state_path.exists():
            check(checks, "state-path", "fail", f"state 目录已存在：{state_path}", "首次安装使用新的 state 目录；已有数据走副本迁移流程")
        else:
            check(checks, "state-path", "pass", f"state 目录不存在：{state_path}")

    failed = sum(item["status"] == "fail" for item in checks)
    warnings = sum(item["status"] == "warn" for item in checks)
    return {
        "schema": "metahub.ubuntu-preflight.v1",
        "status": "fail" if failed else ("warn" if warnings else "pass"),
        "read_only": True,
        "domain": domain or None,
        "checks": checks,
        "summary": {"failed": failed, "warnings": warnings, "passed": len(checks) - failed - warnings},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only MetaHub Ubuntu deployment preflight")
    parser.add_argument("--domain", help="public hostname to resolve")
    parser.add_argument("--state", help="new state directory expected to be absent")
    parser.add_argument("--report", type=Path, help="optional JSON report path")
    args = parser.parse_args(argv)
    report = run_preflight(args.domain, args.state)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    print(encoded, end="")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    return 2 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
