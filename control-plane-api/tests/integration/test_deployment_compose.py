"""测试职责：验证 PRD-028 单机 Docker Compose 部署包的结构、安全边界和 CLI 异常路径。

维护边界：这些测试不依赖 Docker daemon；真实容器启动由可选 smoke 覆盖，静态和 dry-run 必须先保证不会泄露 secret。
来源文件：control-plane-api/tests/integration/test_deployment_compose.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


# 部署测试需要稳定定位仓库根目录和 PowerShell 可执行文件，避免依赖调用者的工作目录。
ROOT = Path(__file__).resolve().parents[3]
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def run_powershell(args: list[str], *, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
    """执行部署 PowerShell 脚本，并把 stdout/stderr 保留给断言。

    部署 CLI 必须能被交付流水线稳定调用；测试只跑 dry-run 和错误路径，避免误启动或删除容器。
    """

    if POWERSHELL is None:
        pytest.skip("PowerShell is required for PRD-028 deployment CLI tests.")
    completed = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if expect_success and completed.returncode != 0:
        # PowerShell 失败时直接带 stderr 终止测试，避免后续 JSON 解析掩盖真正的 CLI 错误。
        raise AssertionError(f"PowerShell command failed: {completed.stderr}")
    return completed


def ps_single_quote(value: Path) -> str:
    """把 Windows 路径安全放进 PowerShell 单引号字符串。

    测试需要把临时 env 路径传给模块函数；这里只做单引号转义，不拼接任何用户可控命令片段。
    """

    return str(value).replace("'", "''")


def test_compose_file_defines_core_services_and_optional_infra() -> None:
    """验证 compose 文件包含默认可运行服务和 compose-full 可选基础设施。

    默认 profile 不能把应用切到 PostgreSQL；当前运行时仍只支持 sqlite://，否则一条命令启动会失败。
    """

    compose = (ROOT / "infra/compose/docker-compose.yml").read_text(encoding="utf-8")

    assert "gateway:" in compose
    assert "control-api:" in compose
    assert "console:" in compose
    assert 'profiles: ["compose-full"]' in compose
    assert "postgres:" in compose
    assert "redis:" in compose
    assert "minio:" in compose
    assert "healthcheck:" in compose
    assert (
        "PYTHONPATH: /app/shared-sdk/python:/app/gateway-data-plane/src:"
        "/app/control-plane-api/src"
    ) in compose
    assert "sqlite:///./.data/compose/metahub.db" not in compose
    assert "METAHUB_DATABASE_URL" not in compose


def test_deployment_profiles_keep_sqlite_default_and_mark_optional_services() -> None:
    """验证 DeploymentProfile JSON 能表达默认 SQLite 和完整基础设施 profile。

    Profile 属于部署配置对象，不进入业务库；测试确保 profile 不引入 tenant_id 之外的新业务表假设。
    """

    local_profile = json.loads(
        (ROOT / "infra/compose/profiles/local-sqlite.json").read_text(encoding="utf-8")
    )
    full_profile = json.loads(
        (ROOT / "infra/compose/profiles/compose-full.json").read_text(encoding="utf-8")
    )

    assert local_profile["profile_id"] == "local-sqlite"
    assert local_profile["database_mode"] == "sqlite"
    assert local_profile["services"]["postgres"]["enabled"] is False
    assert full_profile["profile_id"] == "compose-full"
    assert full_profile["database_mode"] == "sqlite"
    assert full_profile["services"]["postgres"]["enabled"] is True
    assert full_profile["services"]["minio"]["enabled"] is True


def test_env_template_uses_placeholders_and_no_real_default_secret() -> None:
    """验证 env 模板只有占位符，不包含真实 Provider Key、管理员固定密码或弱默认密钥。

    install 脚本负责生成本地 secret；模板文件进入版本库时必须保持不可用的占位状态。
    """

    env_template = (ROOT / "infra/compose/.env.compose.example").read_text(encoding="utf-8")

    assert "replace-with-generated-admin-password" in env_template
    assert "replace-with-generated-auth-token-secret" in env_template
    assert "MetaHubLocal!2026" not in env_template
    assert "sk-" not in env_template
    assert "METAHUB_DATABASE_URL=sqlite:///./.data/compose/metahub.db" in env_template
    assert "METAHUB_POSTGRES_PASSWORD=replace-with-generated-postgres-password" in env_template


def test_install_dry_run_outputs_request_id_and_does_not_create_env(tmp_path: Path) -> None:
    """验证 install dry-run 可解析且不会生成真实 env 或打印 secret。

    该路径用于冷启动前的交付预检，必须只输出计划步骤、端口和 request_id。
    """

    env_path = tmp_path / ".env.compose"
    completed = run_powershell(
        [
            "-File",
            "scripts/deploy/install.ps1",
            "-DryRun",
            "-EnvFile",
            str(env_path),
        ]
    )
    payload = json.loads(completed.stdout)

    assert payload["status"] == "ok"
    assert payload["mode"] == "dry_run"
    assert payload["request_id"].startswith("deploy_")
    assert payload["ports"]["gateway"] == "8000"
    assert not env_path.exists()
    assert "admin_password" not in completed.stdout
    assert "replace-with-generated" not in completed.stdout


def test_stop_remove_volumes_requires_explicit_confirmation() -> None:
    """验证删除 volume 必须显式二次确认。

    默认停止必须保留 SQLite/PostgreSQL/Redis/MinIO 数据卷，防止重启后数据丢失。
    """

    completed = run_powershell(
        ["-File", "scripts/deploy/stop.ps1", "-DryRun", "-RemoveVolumes"],
        expect_success=False,
    )
    payload = json.loads(completed.stderr)

    assert completed.returncode == 1
    assert payload["code"] == "deployment.profile_invalid"
    assert payload["request_id"].startswith("deploy_")
    assert "ConfirmRemoveVolumes" in payload["message"]


def test_env_generation_replaces_placeholders_without_printing_non_admin_secret(tmp_path: Path) -> None:
    """验证 env 初始化会替换占位符，且 stdout 只允许一次性返回管理员密码。

    Provider Key、PostgreSQL 密码、MinIO Secret 和 Token Secret 必须只写入本地 env，不进入 CLI 输出。
    """

    env_path = tmp_path / ".env.compose"
    module = ROOT / "scripts/deploy/lib/MetaHub.Deploy.psm1"
    command = (
        f"Import-Module '{ps_single_quote(module)}' -Force; "
        f"$result = Initialize-MetaHubEnvFile -EnvFile '{ps_single_quote(env_path)}' "
        "-RequestId 'deploy_test'; "
        "$result | ConvertTo-Json -Compress"
    )
    completed = run_powershell(["-Command", command])
    payload = json.loads(completed.stdout)
    generated_env = env_path.read_text(encoding="utf-8-sig")

    assert payload["created"] is True
    assert payload["admin_password"].startswith("MetaHub-")
    assert "replace-with-generated" not in generated_env
    assert "replace-with-generated" not in completed.stdout
    assert "METAHUB_AUTH_TOKEN_SECRET=" in generated_env
    assert "METAHUB_POSTGRES_PASSWORD=" in generated_env
    assert "METAHUB_MINIO_SECRET_KEY=" in generated_env
    assert "METAHUB_POSTGRES_PASSWORD=" not in completed.stdout
    assert "METAHUB_MINIO_SECRET_KEY=" not in completed.stdout


def test_dockerfiles_reference_existing_entrypoints_and_runtime_requirements() -> None:
    """验证 Dockerfile 使用现有服务入口和必要运行时依赖。

    PRD-028 不能另造启动路径；容器命令必须复用 dev 脚本已经验证过的 Python module entrypoint。
    """

    python_dockerfile = (ROOT / "infra/docker/python-service.Dockerfile").read_text(
        encoding="utf-8"
    )
    console_dockerfile = (ROOT / "infra/docker/console.Dockerfile").read_text(encoding="utf-8")
    requirements = (ROOT / "infra/docker/requirements.runtime.txt").read_text(encoding="utf-8")

    assert "metahub_gateway.main" in python_dockerfile
    assert "shared-sdk/python" in python_dockerfile
    assert "control-plane-api/src" in python_dockerfile
    assert "node" in console_dockerfile
    assert "scripts/dev-server.mjs" in console_dockerfile
    assert "fastapi" in requirements
    assert "uvicorn" in requirements
    assert "cryptography" in requirements
