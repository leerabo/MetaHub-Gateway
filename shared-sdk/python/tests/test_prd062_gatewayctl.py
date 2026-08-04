"""PRD-062 gatewayctl 网络、文件、脱敏与退出码专项测试。

测试临时文件只放在仓库 ``.artifacts/prd062-wrapper-final``，不使用 pytest 的系统临时目录。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from metahub_shared.gatewayctl import cli

# 专项测试产物使用明确仓库目录；每个用例使用随机子目录并在结束时清理。
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WRAPPER_ARTIFACT_ROOT = REPOSITORY_ROOT / ".artifacts" / "prd062-wrapper-final"
ARTIFACT_ROOT = WRAPPER_ARTIFACT_ROOT / "cli-tests"
TOKEN = "session-prd062-token-value"
# Windows 沙箱首次启动 PowerShell/Python 可能需要装载运行时；60 秒只放宽进程启动预算，
# 不改变 CLI 自身的网络超时或失败关闭语义，避免把环境冷启动误判为产品回归。
WRAPPER_PROCESS_TIMEOUT_SECONDS = 60


@pytest.fixture
def case_dir() -> Iterator[Path]:
    """创建并清理一个限定在 PRD-062 artifacts 根下的用例目录。"""

    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    path = ARTIFACT_ROOT / f"case-{uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class _ScriptedClient:
    """按 endpoint 动态构造响应的内存 ControlClient，用于验证 CLI 编排而不启动服务。"""

    def __init__(self, handlers: dict[str, Callable[[str], cli.HttpResponse]]) -> None:
        """保存端点处理器和实际调用顺序。"""

        self.handlers = handlers
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def post(self, endpoint: str, payload: dict[str, object], request_id: str) -> cli.HttpResponse:
        """记录安全调用摘要并返回本端点的动态响应。"""

        self.calls.append((endpoint, request_id, payload))
        return self.handlers[endpoint](request_id)


def _factory(client: _ScriptedClient, captured: dict[str, str] | None = None):
    """构造 run_cli 所需工厂，并可捕获规范 URL 与 Token 来源。"""

    def create(control_url: str, token: str) -> _ScriptedClient:
        """返回预建客户端且只在测试内记录入参。"""

        if captured is not None:
            captured["control_url"] = control_url
            captured["token"] = token
        return client

    return create


def _manifest() -> dict[str, object]:
    """构造最小且不含任何凭据字段的 ConfigExport v1。"""

    return {
        "schema_version": "metahub.config/v1",
        "source_tenant_id": "source-prd062",
        "exported_at": "2026-08-01T00:00:00Z",
        "providers": [],
        "models": [],
        "aliases": [],
        "virtual_keys": [],
    }


def _write_manifest(path: Path, payload: dict[str, object] | None = None) -> None:
    """把受控测试清单写入指定验收目录，保证用例不会接触仓库临时目录。"""

    path.write_text(json.dumps(payload or _manifest(), ensure_ascii=False), encoding="utf-8")


def _counts(*, generated: int = 0) -> dict[str, int]:
    """构造字段完整的导入计数对象。"""

    return {
        "providers_created": 0,
        "providers_unchanged": 0,
        "models_created": 0,
        "models_unchanged": 0,
        "aliases_created": 0,
        "aliases_unchanged": 0,
        "virtual_keys_created": generated,
        "virtual_keys_unchanged": 0,
        "generated_virtual_keys": generated,
        "disabled_virtual_keys": 0,
    }


def _report(
    request_id: str,
    status: str,
    *,
    generated: int = 0,
) -> dict[str, object]:
    """构造与服务端 ImportReport 精确一致的安全响应。"""

    invalid = status == "invalid"
    applied = status in {"applied", "replayed"}
    return {
        "status": status,
        "valid": not invalid,
        "applied": applied,
        "manifest_sha256": "a" * 64,
        "schema_version": "metahub.config/v1",
        "counts": _counts(generated=generated),
        "error_count": 1 if invalid else 0,
        "errors_truncated": False,
        "errors": (
            [
                {
                    "object_type": "provider",
                    "row": 1,
                    "field": "provider_code",
                    "code": "config_transfer.import_conflict",
                }
            ]
            if invalid
            else []
        ),
        "config_import_id": "import-prd062-001" if applied else None,
        "request_id": request_id,
        "trace_id": f"trc_{request_id[4:]}",
    }


def _import_response(
    request_id: str,
    status: str,
    *,
    generated: int = 0,
    keys: list[dict[str, object]] | None = None,
) -> cli.HttpResponse:
    """构造字段冻结的预检或应用响应，供两阶段命令编排测试复用。"""

    report = _report(request_id, status, generated=generated)
    return cli.HttpResponse(
        status=201 if status == "applied" else 200,
        payload={
            "report": report,
            "generated_virtual_keys": keys or [],
            "request_id": request_id,
            "trace_id": report["trace_id"],
        },
    )


def _export_response(request_id: str, payload: dict[str, object] | None = None) -> cli.HttpResponse:
    """构造字段精确且不含凭据的导出响应，验证客户端协议白名单。"""

    return cli.HttpResponse(
        status=200,
        payload={
            "config_export": payload or _manifest(),
            "config_export_id": "export-prd062-001",
            "manifest_sha256": "b" * 64,
            "request_id": request_id,
            "trace_id": f"trc_{request_id[4:]}",
        },
    )


def _run(
    arguments: list[str],
    client: _ScriptedClient,
    *,
    stdin_text: str = "",
    environ: dict[str, str] | None = None,
    captured: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """运行 CLI 并返回退出码及两个单行输出流。"""

    stdout = io.StringIO()
    stderr = io.StringIO()
    code = cli.run_cli(
        arguments,
        stdin=io.StringIO(stdin_text),
        stdout=stdout,
        stderr=stderr,
        environ=environ if environ is not None else {"METAHUB_CONTROL_TOKEN": TOKEN},
        client_factory=_factory(client, captured),
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_export_stdout_is_direct_importable_config_without_envelope_or_secret() -> None:
    """stdout 导出只包含 ConfigExport 本体，保持可直接作为 import 输入。"""

    client = _ScriptedClient({"config/export": _export_response})
    code, stdout, stderr = _run(["config", "export"], client)

    assert code == 0
    assert stderr == ""
    assert stdout.count("\n") == 1
    assert json.loads(stdout) == _manifest()
    assert TOKEN not in stdout
    assert [call[0] for call in client.calls] == ["config/export"]


def test_export_file_is_atomic_and_stdout_only_contains_safe_summary(case_dir: Path) -> None:
    """文件导出写 ConfigExport，命令摘要不回显路径、URL、Token 或配置正文。"""

    target = case_dir / "export.json"
    client = _ScriptedClient({"config/export": _export_response})
    code, stdout, stderr = _run(
        ["config", "export", "--output", str(target)],
        client,
    )

    assert code == 0
    assert stderr == ""
    assert json.loads(target.read_text(encoding="utf-8")) == _manifest()
    summary = json.loads(stdout)
    assert summary["status"] == "exported"
    assert summary["manifest_sha256"] == "b" * 64
    assert str(target) not in stdout
    assert _default_control_url_not_present(stdout)
    assert TOKEN not in stdout


def _default_control_url_not_present(value: str) -> bool:
    """断言辅助函数：普通输出不应携带 Control URL。"""

    return cli.DEFAULT_CONTROL_URL not in value


def test_export_existing_target_requires_force_before_network(case_dir: Path) -> None:
    """已存在目标默认在发网前拒绝，--force 才允许原子替换普通文件。"""

    target = case_dir / "export.json"
    target.write_text("old", encoding="utf-8")
    client = _ScriptedClient({"config/export": _export_response})

    rejected, stdout, stderr = _run(
        ["config", "export", "--output", str(target)],
        client,
    )
    assert rejected == 2
    assert stdout == ""
    assert json.loads(stderr)["code"] == "gatewayctl.output_exists"
    assert client.calls == []
    assert target.read_text(encoding="utf-8") == "old"

    accepted, _, accepted_stderr = _run(
        ["config", "export", "--output", str(target), "--force"],
        client,
    )
    assert accepted == 0
    assert accepted_stderr == ""
    assert json.loads(target.read_text(encoding="utf-8")) == _manifest()


def test_import_dry_run_stops_after_validation(case_dir: Path) -> None:
    """验证预演模式只调用服务端预检，并输出带校验请求标识的安全报告。"""

    source = case_dir / "manifest.json"
    _write_manifest(source)
    client = _ScriptedClient(
        {"config/import/validate": lambda request_id: _import_response(request_id, "validated")}
    )

    code, stdout, stderr = _run(
        ["config", "import", "--input", str(source), "--dry-run"],
        client,
    )

    assert code == 0
    assert stderr == ""
    summary = json.loads(stdout)
    assert summary["status"] == "validated"
    assert summary["validation_request_id"] == summary["request_id"]
    assert [call[0] for call in client.calls] == ["config/import/validate"]


def test_invalid_validation_returns_four_and_never_calls_apply(case_dir: Path) -> None:
    """服务端行级错误以退出码 4 返回完整脱敏报告，且不创建密钥文件或调用 apply。"""

    source = case_dir / "manifest.json"
    secret_target = case_dir / "keys.json"
    _write_manifest(source)
    client = _ScriptedClient(
        {"config/import/validate": lambda request_id: _import_response(request_id, "invalid")}
    )

    code, stdout, stderr = _run(
        [
            "config",
            "import",
            "--input",
            str(source),
            "--generated-keys-output",
            str(secret_target),
        ],
        client,
    )

    assert code == 4
    assert stderr == ""
    assert json.loads(stdout)["report"]["errors"][0]["code"] == "config_transfer.import_conflict"
    assert not secret_target.exists()
    assert [call[0] for call in client.calls] == ["config/import/validate"]


def test_apply_writes_plain_key_only_to_precreated_private_file(case_dir: Path) -> None:
    """apply 使用不同请求 ID，并确保明文 Key 只进入显式私有文件。"""

    source = case_dir / "manifest.json"
    secret_target = case_dir / "keys.json"
    _write_manifest(source)
    plain_value = "mhv_dev_once-only-secret-value"
    generated_key = {
        "virtual_key_id": "vk-prd062-001",
        "name": "delivery-key",
        "environment": "dev",
        "key_prefix": "mhv_dev_once",
        "plain_value": plain_value,
    }
    client = _ScriptedClient(
        {
            "config/import/validate": lambda request_id: _import_response(
                request_id,
                "validated",
                generated=1,
            ),
            "config/import": lambda request_id: _import_response(
                request_id,
                "applied",
                generated=1,
                keys=[generated_key],
            ),
        }
    )

    code, stdout, stderr = _run(
        [
            "config",
            "import",
            "--input",
            str(source),
            "--generated-keys-output",
            str(secret_target),
        ],
        client,
    )

    assert code == 0, stderr
    assert stderr == ""
    assert plain_value not in stdout
    assert str(secret_target) not in stdout
    assert TOKEN not in stdout
    secret = json.loads(secret_target.read_text(encoding="utf-8"))
    assert secret["generated_virtual_keys"] == [generated_key]
    assert secret["config_import_id"] == "import-prd062-001"
    assert [call[0] for call in client.calls] == ["config/import/validate", "config/import"]
    assert client.calls[0][1] != client.calls[1][1]
    if os.name != "nt":
        assert secret_target.stat().st_mode & 0o077 == 0


def test_generated_output_required_and_existing_target_block_apply(case_dir: Path) -> None:
    """预检预计生成 Key 时缺失或已存在密钥文件都必须在 apply 前失败。"""

    source = case_dir / "manifest.json"
    _write_manifest(source)
    existing = case_dir / "existing.json"
    existing.write_text("do-not-overwrite", encoding="utf-8")

    def validated(request_id: str) -> cli.HttpResponse:
        """返回预计生成一把新 Key 的预检结果。"""

        return _import_response(request_id, "validated", generated=1)

    missing_client = _ScriptedClient({"config/import/validate": validated})
    missing_code, _, missing_stderr = _run(
        ["config", "import", "--input", str(source)],
        missing_client,
    )
    assert missing_code == 2
    assert json.loads(missing_stderr)["code"] == "gatewayctl.generated_keys_output_required"
    assert [call[0] for call in missing_client.calls] == ["config/import/validate"]

    existing_client = _ScriptedClient({"config/import/validate": validated})
    existing_code, _, existing_stderr = _run(
        [
            "config",
            "import",
            "--input",
            str(source),
            "--generated-keys-output",
            str(existing),
        ],
        existing_client,
    )
    assert existing_code == 6
    assert json.loads(existing_stderr)["code"] == "gatewayctl.output_exists"
    assert existing.read_text(encoding="utf-8") == "do-not-overwrite"
    assert [call[0] for call in existing_client.calls] == ["config/import/validate"]


@pytest.mark.parametrize(
    "virtual_key_row",
    [
        {"name": "race-key", "environment": "dev", "status": " ACTIVE "},
        {"name": "race-key", "environment": "dev"},
    ],
    ids=["normalized-active", "default-active"],
)
def test_active_key_intent_reserves_secret_file_even_when_validate_reports_unchanged(
    case_dir: Path,
    virtual_key_row: dict[str, object],
) -> None:
    """显式或默认 active Key 都须在 apply 前预留明文承接文件。"""

    source = case_dir / "race-manifest.json"
    document = _manifest()
    document["virtual_keys"] = [virtual_key_row]
    _write_manifest(source, document)
    client = _ScriptedClient(
        {"config/import/validate": lambda request_id: _import_response(request_id, "validated")}
    )

    code, _, stderr = _run(["config", "import", "--input", str(source)], client)

    assert code == 2
    assert json.loads(stderr)["code"] == "gatewayctl.generated_keys_output_required"
    assert [call[0] for call in client.calls] == ["config/import/validate"]


def test_replay_does_not_require_or_write_plain_keys(case_dir: Path) -> None:
    """精确重试可保留历史 generated 计数，但响应和普通输出都不得重放明文。"""

    source = case_dir / "manifest.json"
    _write_manifest(source)
    client = _ScriptedClient(
        {
            "config/import/validate": lambda request_id: _import_response(request_id, "validated"),
            "config/import": lambda request_id: _import_response(
                request_id,
                "replayed",
                generated=1,
            ),
        }
    )

    code, stdout, stderr = _run(["config", "import", "--input", str(source)], client)

    assert code == 0
    assert stderr == ""
    assert json.loads(stdout)["status"] == "replayed"
    assert "plain_value" not in stdout


@pytest.mark.parametrize(
    "url,accepted",
    [
        ("http://localhost:8001", True),
        ("http://127.0.0.1:8001", True),
        ("http://[::1]:8001", True),
        ("https://control.example.com", True),
        ("http://control.example.com", False),
        ("https://user@control.example.com", False),
        ("https://control.example.com?tenant=other", False),
        ("file:///etc/config", False),
    ],
)
def test_control_url_requires_safe_origin_and_remote_https(url: str, accepted: bool) -> None:
    """Control URL 只允许无凭据/查询/fragment 的 HTTP(S)，远端必须 HTTPS。"""

    if accepted:
        base, origin = cli._normalize_control_url(url)
        assert base
        assert origin[0] in {"http", "https"}
    else:
        with pytest.raises(cli.CliFailure) as raised:
            cli._normalize_control_url(url)
        assert raised.value.exit_code == 2


def test_redirect_target_must_keep_frozen_origin() -> None:
    """同 origin 307/308 可继续，跨 scheme/host/port 或 query 目标均被拒绝。"""

    origin = ("https", "control.example.com", 443)
    assert cli._is_safe_redirect_target("https://control.example.com/config/import/", origin)
    assert not cli._is_safe_redirect_target("https://other.example.com/config/import", origin)
    assert not cli._is_safe_redirect_target("http://control.example.com/config/import", origin)
    assert not cli._is_safe_redirect_target("https://control.example.com:444/config/import", origin)
    assert not cli._is_safe_redirect_target("https://control.example.com/config/import?token=x", origin)


def test_redirect_handler_preserves_post_only_for_same_origin_307_308() -> None:
    """307/308 必须保留 POST 正文和认证头；其他状态及跨 origin 目标必须失败关闭。"""

    origin = ("https", "control.example.com", 443)
    handler = cli.FrozenOriginRedirectHandler(origin)
    request = cli.urllib.request.Request(
        "https://control.example.com/config/import",
        data=b"{}",
        method="POST",
        headers={"Authorization": "Bearer test-only", "Content-Type": "application/json"},
    )

    redirected = handler.redirect_request(
        request,
        io.BytesIO(),
        307,
        "Temporary Redirect",
        {},
        "/config/import/",
    )
    assert redirected is not None
    assert redirected.get_method() == "POST"
    assert redirected.data == b"{}"
    assert redirected.get_header("Authorization") == "Bearer test-only"
    assert redirected.full_url == "https://control.example.com/config/import/"

    with pytest.raises(cli.CliFailure):
        handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, "/other")
    with pytest.raises(cli.CliFailure):
        handler.redirect_request(
            request,
            io.BytesIO(),
            308,
            "Permanent Redirect",
            {},
            "https://other.example.com/config/import",
        )


def test_import_report_allows_folded_credential_field_but_rejects_unknown_field() -> None:
    """服务端固定 credential 字段可公开，攻击者控制的任意字段名仍不得进入 CLI 报告。"""

    request_id = "req_gatewayctl_report_test"
    credential_report = _report(request_id, "invalid")
    credential_report["errors"] = [
        {
            "object_type": "document",
            "row": 0,
            "field": "credential",
            "code": "config_transfer.import_sensitive_field",
        }
    ]
    validated = cli._validate_import_report(credential_report, request_id)
    assert validated["errors"][0]["field"] == "credential"

    credential_report["errors"][0]["field"] = "attacker_controlled_field"
    with pytest.raises(cli.CliFailure) as raised:
        cli._validate_import_report(credential_report, request_id)
    assert raised.value.code == "gatewayctl.response_invalid"


@pytest.mark.parametrize(
    ("error_count", "expected_length", "expected_truncated"),
    [
        (1, 1, False),
        (cli.MAX_RESPONSE_ERRORS, cli.MAX_RESPONSE_ERRORS, False),
        (cli.MAX_RESPONSE_ERRORS + 1, cli.MAX_RESPONSE_ERRORS, True),
    ],
)
def test_import_report_accepts_exact_error_truncation_boundaries(
    error_count: int,
    expected_length: int,
    expected_truncated: bool,
) -> None:
    """验证 200 条边界采用严格大于关系，并按错误总数精确裁剪返回明细。"""

    request_id = "req_gatewayctl_truncation_boundary"
    report = _report(request_id, "invalid")
    first_error = report["errors"][0]
    report["error_count"] = error_count
    report["errors"] = [dict(first_error) for _ in range(expected_length)]
    report["errors_truncated"] = expected_truncated

    validated = cli._validate_import_report(report, request_id)

    assert len(validated["errors"]) == min(error_count, cli.MAX_RESPONSE_ERRORS)
    assert validated["errors_truncated"] is (
        error_count > cli.MAX_RESPONSE_ERRORS
    )


def test_import_report_rejects_inconsistent_error_truncation_contract() -> None:
    """拒绝与错误总数不一致的截断标记或明细数量，避免 CLI 隐藏校验失败。"""

    request_id = "req_gatewayctl_truncation_invalid"
    report = _report(request_id, "invalid")
    first_error = report["errors"][0]
    report["error_count"] = cli.MAX_RESPONSE_ERRORS + 1
    report["errors"] = [dict(first_error) for _ in range(cli.MAX_RESPONSE_ERRORS)]
    report["errors_truncated"] = False
    with pytest.raises(cli.CliFailure) as wrong_flag:
        cli._validate_import_report(report, request_id)
    assert wrong_flag.value.code == "gatewayctl.response_invalid"

    report["errors_truncated"] = True
    report["errors"] = report["errors"][:-1]
    with pytest.raises(cli.CliFailure) as missing_item:
        cli._validate_import_report(report, request_id)
    assert missing_item.value.code == "gatewayctl.response_invalid"


def test_local_reader_rejects_duplicate_and_sensitive_keys_before_network(case_dir: Path) -> None:
    """重复键与嵌套 credential 字段在本地失败，正文和秘密不进入 stderr。"""

    duplicate = case_dir / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":"metahub.config/v1","schema_version":"other",'
        '"source_tenant_id":"source","exported_at":"2026-08-01T00:00:00Z",'
        '"providers":[],"models":[],"aliases":[],"virtual_keys":[]}',
        encoding="utf-8",
    )
    sensitive = case_dir / "sensitive.json"
    payload = _manifest()
    payload["providers"] = [{"provider_code": "p", "provider_key": "never-print-this"}]
    _write_manifest(sensitive, payload)
    camel_sensitive = case_dir / "camel-sensitive.json"
    camel_payload = _manifest()
    camel_payload["clientKey"] = "never-print-camel-secret"
    _write_manifest(camel_sensitive, camel_payload)
    surrogate = case_dir / "surrogate.json"
    surrogate.write_text(
        '{"schema_version":"metahub.config/v1","source_tenant_id":"\\ud800",'
        '"exported_at":"2026-08-01T00:00:00Z","providers":[],"models":[],'
        '"aliases":[],"virtual_keys":[]}',
        encoding="utf-8",
    )
    client = _ScriptedClient({})

    duplicate_code, _, duplicate_error = _run(
        ["config", "import", "--input", str(duplicate), "--dry-run"],
        client,
    )
    sensitive_code, _, sensitive_error = _run(
        ["config", "import", "--input", str(sensitive), "--dry-run"],
        client,
    )
    camel_code, _, camel_error = _run(
        ["config", "import", "--input", str(camel_sensitive), "--dry-run"],
        client,
    )
    surrogate_code, _, surrogate_error = _run(
        ["config", "import", "--input", str(surrogate), "--dry-run"],
        client,
    )

    assert duplicate_code == sensitive_code == camel_code == surrogate_code == 2
    assert "never-print-this" not in sensitive_error
    assert str(sensitive) not in sensitive_error
    assert json.loads(duplicate_error)["code"] == "gatewayctl.json_invalid"
    assert json.loads(sensitive_error)["code"] == "gatewayctl.sensitive_input_rejected"
    assert "never-print-camel-secret" not in camel_error
    assert json.loads(camel_error)["code"] == "gatewayctl.sensitive_input_rejected"
    assert json.loads(surrogate_error)["code"] == "gatewayctl.json_invalid"
    assert client.calls == []


def test_unknown_runtime_exception_is_folded_without_traceback_or_secret() -> None:
    """未知客户端异常统一退出 5，stderr 不得包含异常文本、Token 或 Python traceback。"""

    def broken_factory(_control_url: str, _token: str):
        """模拟标准库或替换传输层抛出未分类 RuntimeError。"""

        # 异常文本刻意包含秘密标记，用于证明最终边界不会回显原始运行时信息。
        raise RuntimeError("runtime-secret-must-not-leak")

    stdout = io.StringIO()
    stderr = io.StringIO()
    code = cli.run_cli(
        ["config", "export"],
        stdout=stdout,
        stderr=stderr,
        environ={"METAHUB_CONTROL_TOKEN": TOKEN},
        client_factory=broken_factory,
    )

    assert code == 5
    assert stdout.getvalue() == ""
    error_text = stderr.getvalue()
    assert json.loads(error_text)["code"] == "gatewayctl.failed"
    assert "runtime-secret-must-not-leak" not in error_text
    assert TOKEN not in error_text
    assert "Traceback" not in error_text


def test_token_stdin_wins_without_token_or_tenant_argv(case_dir: Path) -> None:
    """显式 --token-stdin 读取单行 Token，CLI 不提供 --token 或 tenant 参数。"""

    source = case_dir / "manifest.json"
    _write_manifest(source)
    captured: dict[str, str] = {}
    client = _ScriptedClient(
        {"config/import/validate": lambda request_id: _import_response(request_id, "validated")}
    )
    code, _, stderr = _run(
        ["config", "import", "--input", str(source), "--dry-run", "--token-stdin"],
        client,
        stdin_text="stdin-session-token\n",
        environ={"METAHUB_CONTROL_TOKEN": "ignored-env-token"},
        captured=captured,
    )
    assert code == 0
    assert stderr == ""
    assert captured["token"] == "stdin-session-token"

    rejected_client = _ScriptedClient({})
    rejected, _, rejected_error = _run(
        ["config", "export", "--token", "must-not-echo", "--tenant", "other"],
        rejected_client,
    )
    assert rejected == 2
    assert "must-not-echo" not in rejected_error
    assert "other" not in rejected_error
    assert rejected_client.calls == []


def test_malicious_export_protocol_never_writes_secret_or_target(case_dir: Path) -> None:
    """服务端导出响应含未知凭据字段时按协议失败，目标文件保持不存在。"""

    target = case_dir / "export.json"
    malicious = _manifest()
    malicious["provider_key"] = "server-secret"
    client = _ScriptedClient(
        {"config/export": lambda request_id: _export_response(request_id, malicious)}
    )

    code, stdout, stderr = _run(
        ["config", "export", "--output", str(target)],
        client,
    )

    assert code == 5
    assert stdout == ""
    assert not target.exists()
    assert "server-secret" not in stderr
    assert json.loads(stderr)["code"] == "gatewayctl.response_invalid"


def test_permission_failure_uses_exit_three_and_fixed_error_json() -> None:
    """后端权限失败映射退出码 3，错误流字段固定且不含自由文本。"""

    class _DeniedClient(_ScriptedClient):
        """始终返回权限拒绝的测试客户端。"""

        def post(self, endpoint: str, payload: dict[str, object], request_id: str) -> cli.HttpResponse:
            """抛出只含稳定权限错误码与链路 ID 的失败。"""

            del endpoint, payload
            raise cli.CliFailure(
                "auth.permission_denied",
                3,
                request_id=request_id,
                trace_id=f"trc_{request_id[4:]}",
            )

    code, stdout, stderr = _run(["config", "export"], _DeniedClient({}))
    assert code == 3
    assert stdout == ""
    error = json.loads(stderr)
    assert set(error) == {"status", "code", "request_id", "trace_id", "config_import_id"}
    assert error["code"] == "auth.permission_denied"


def test_gatewayctl_powershell_wrapper_resolves_source_from_script_directory() -> None:
    """包装脚本从 PSScriptRoot 解析源码并且不出现 Token、tenant 或客户目录扫描逻辑。"""

    wrapper = (REPOSITORY_ROOT / "scripts" / "gatewayctl.ps1").read_text(encoding="utf-8")
    lowered = wrapper.lower()
    assert "$psscriptroot" in lowered
    assert "metahub_shared.gatewayctl.cli" in wrapper
    assert "get-childitem" not in lowered
    assert "metahub_control_token" not in lowered
    assert "--token" not in lowered
    assert "--tenant" not in lowered


@pytest.mark.skipif(os.name != "nt", reason="PowerShell 包装脚本只在 Windows 环境执行")
def test_gatewayctl_powershell_wrapper_runs_outside_repository_without_pythonpath() -> None:
    """从仓库外目录真实启动包装脚本，证明源码根不依赖 cwd 或调用方 PYTHONPATH。"""

    outside = WRAPPER_ARTIFACT_ROOT / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["METAHUB_PYTHON"] = sys.executable
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-File",
            str(REPOSITORY_ROOT / "scripts" / "gatewayctl.ps1"),
            "--help",
        ],
        cwd=outside,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=WRAPPER_PROCESS_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage: gatewayctl" in result.stdout
    assert result.stderr == ""
    assert "ModuleNotFoundError" not in result.stdout


def test_gatewayctl_module_entrypoint_does_not_emit_duplicate_import_warning() -> None:
    """真实 ``python -m`` 启动不得因包级提前导入而污染机器可读输出。"""

    environment = os.environ.copy()
    source_root = str(REPOSITORY_ROOT / "shared-sdk" / "python")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (source_root, environment.get("PYTHONPATH", "")) if item
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "metahub_shared.gatewayctl.cli", "--help"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=WRAPPER_PROCESS_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "RuntimeWarning" not in result.stderr
    assert result.stderr == ""
