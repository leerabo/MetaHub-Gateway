"""PRD-064 Provider Adapter 契约框架专项测试。

测试只使用显式内存夹具和 loopback mock，不访问公网、数据库、Redis 或真实 Provider。
pytest ``--basetemp`` 只允许位于仓库 `.tmp` 或 `.artifacts`；正式报告仍映射并写入仓库
`.artifacts`。测试不会生成 patch 中间文件，也不会把合成 Key、Prompt、响应或 Endpoint
写入报告与终端输出。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from metahub_gateway.provider_contracts import cli as cli_module
from metahub_gateway.provider_contracts import matrix as matrix_module
from metahub_gateway.provider_contracts import report as report_module
from metahub_gateway.provider_contracts import runner as runner_module
from metahub_gateway.provider_contracts.fixtures import (
    CONTRACT_FIXTURES,
    SENSITIVE_CANARY,
)
from metahub_gateway.provider_contracts.matrix import (
    ContractMatrixError,
    build_contract_matrix,
    validate_fixture_coverage,
    validate_result_coverage,
)
from metahub_gateway.provider_contracts.models import ContractRunReport
from metahub_gateway.provider_contracts.report import (
    canonical_report_bytes,
    report_hash,
    validate_report,
    write_report_atomic,
)
from metahub_gateway.provider_contracts.runner import (
    contract_source_revision_hash,
    run_merge_gate_sync,
)
from metahub_gateway.providers import factory as factory_module
from metahub_shared.runtime.provider_adapter_spec import provider_adapter_specs

# 从测试文件位置稳定定位仓库；只用于读取 CI 接线及验证沙箱临时根和制品输出归属。
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
_FAILURE_CODE_BY_KIND = {
    "request": "adapter.contract_request_failed",
    "non_stream": "adapter.contract_non_stream_failed",
    "stream": "adapter.contract_stream_failed",
    "error": "adapter.contract_error_mapping_failed",
    "usage": "adapter.contract_usage_failed",
}


@pytest.fixture(scope="module")
def merge_gate_report() -> ContractRunReport:
    """完整执行一次 103 条动态矩阵，并在本模块其余测试中复用不可变报告。"""

    return run_merge_gate_sync(
        tenant_id="ci-contract",
        request_id="req_prd064_pytest",
        trace_id="trc_prd064_pytest",
        source_revision_hash=contract_source_revision_hash(),
    )


def _artifact_target(tmp_path: Path, name: str = "report.json") -> Path:
    """校验工作区内 pytest 临时根，并返回生产路径门禁认可的报告路径。

    参数：pytest 分配的用例目录与报告文件名。返回：工作区 `.artifacts` 下的隔离路径。
    约束：basetemp 只接受工作区内 `.tmp`/`.artifacts`；链接逃逸或其他目录必须在写入前
    失败。`.tmp` 只提供沙箱内的用例隔离标识，不能扩大正式报告允许写入的目录范围。
    """

    workspace_root = WORKSPACE_ROOT.resolve(strict=True)
    resolved = tmp_path.resolve()
    try:
        relative = resolved.relative_to(workspace_root)
    # resolve 后越出工作区通常意味着系统临时目录或链接逃逸，必须在任何写入前失败。
    except ValueError as exc:
        raise AssertionError(
            "PRD-064 tests require a workspace-local .tmp or .artifacts basetemp"
        ) from exc
    if not relative.parts or relative.parts[0].casefold() not in {
        ".tmp",
        ".artifacts",
    }:
        # 工作区内的源码、数据或其他隐藏目录也不是 pytest 写入根，不能只凭“未越出仓库”放行。
        raise AssertionError(
            "PRD-064 tests require a workspace-local .tmp or .artifacts basetemp"
        )
    if relative.parts[0].casefold() == ".artifacts":
        return resolved / name

    # 沙箱测试统一把 pytest 临时目录放在 `.tmp`，而生产报告只能写入 `.artifacts`。这里只
    # 使用已校验的相对用例标识建立隔离目录，不允许调用方把任意绝对路径带入正式制品根。
    artifact_root = (workspace_root / ".artifacts").resolve(strict=False)
    try:
        artifact_root.relative_to(workspace_root)
    # `.artifacts` 若是指向工作区外的链接，也不能借测试辅助逻辑绕过生产安全边界。
    except ValueError as exc:
        raise AssertionError("PRD-064 artifact root must remain in workspace") from exc
    artifact_dir = (
        artifact_root
        / "pytest-prd064"
        / "provider-adapter-contracts"
        / Path(*relative.parts[1:])
    ).resolve(strict=False)
    try:
        artifact_dir.relative_to(artifact_root)
    # 即使未来隔离标识生成规则变化，也必须再次证明最终目录仍属于正式制品根。
    except ValueError as exc:
        raise AssertionError("PRD-064 artifact path must remain in .artifacts") from exc
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir / name


def test_artifact_target_rejects_workspace_external_basetemp() -> None:
    """路径权限：工作区外 basetemp 必须在创建正式报告前被辅助门禁拒绝。"""

    with pytest.raises(AssertionError, match="workspace-local"):
        _artifact_target(WORKSPACE_ROOT.parent / "prd064-outside")


def _failed_report(report: ContractRunReport) -> ContractRunReport:
    """把首个已通过 case 转成结构一致的固定失败报告，用于退出码和 schema 测试。"""

    first = report.cases[0]
    failure_code = _FAILURE_CODE_BY_KIND[first.case_kind]
    failed_case = replace(
        first,
        status="failed",
        failure_code=failure_code,
        evidence_hash=hashlib.sha256(b"prd064-fixed-failure-evidence").hexdigest(),
    )
    return replace(
        report,
        status="failed",
        passed_cases=report.passed_cases - 1,
        failed_cases=1,
        failure_codes=(failure_code,),
        cases=(failed_case, *report.cases[1:]),
    )


def _run_cli_with_matrix_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    callback: Callable[..., ContractRunReport],
) -> tuple[int, str, str]:
    """注入一个纯内存矩阵失败并返回 CLI 退出码及安全输出。"""

    monkeypatch.setattr(cli_module, "run_merge_gate_sync", callback)
    monkeypatch.setattr(cli_module, "contract_source_revision_hash", lambda: "a" * 64)
    exit_code = cli_module.main(
        [
            "--workspace-root",
            str(WORKSPACE_ROOT),
            "--output",
            str(_artifact_target(tmp_path)),
        ]
    )
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def test_matrix_is_dynamic_exact_set_for_all_specs_and_auth_types() -> None:
    """主流程：十个显式夹具按 auth 加九类固定行为动态形成当前 103 条唯一 case。"""

    specs = provider_adapter_specs()
    matrix = build_contract_matrix()
    expected_count = sum(len(spec.allowed_auth_types) + 9 for spec in specs)

    assert expected_count == 103
    assert len(matrix) == expected_count
    assert len({case.case_key for case in matrix}) == expected_count
    assert set(CONTRACT_FIXTURES) == {spec.contract_case_id for spec in specs}
    assert {fixture.adapter_name for fixture in CONTRACT_FIXTURES.values()} == set(
        factory_module._ADAPTER_TYPES
    )


def test_matrix_stamps_explicit_target_tenant_without_changing_case_keys() -> None:
    """租户流程：部署租户只改变报告归属，不能改变冻结 exact-set 或复用夹具租户。"""

    synthetic = build_contract_matrix()
    target = build_contract_matrix(tenant_id="default")

    assert {case.tenant_id for case in synthetic} == {"ci-contract"}
    assert {case.tenant_id for case in target} == {"default"}
    assert [case.case_key for case in target] == [case.case_key for case in synthetic]


def test_missing_fixture_fails_before_network_start() -> None:
    """异常流程：删除任一显式夹具必须以固定 missing code 阻断，不能生成默认夹具。"""

    fixtures = dict(CONTRACT_FIXTURES)
    fixtures.pop(next(iter(fixtures)))

    with pytest.raises(ContractMatrixError) as caught:
        build_contract_matrix(fixtures)

    assert caught.value.code == "adapter.contract_fixture_missing"


def test_phantom_spec_without_fixture_fails_closed() -> None:
    """异常流程：内存新增伪 spec 且未提交夹具时，exact-set 门禁必须立即失败。"""

    specs = provider_adapter_specs()
    phantom = replace(
        specs[0],
        spec_id="adapter-spec-future-v1",
        provider_type="future",
        contract_case_id="prd063_future_minimum_contract",
    )

    with pytest.raises(ContractMatrixError) as caught:
        validate_fixture_coverage(specs=(*specs, phantom))

    assert caught.value.code == "adapter.contract_fixture_missing"


def test_factory_family_drift_fails_exact_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """兼容流程：生产工厂新增 Adapter family 却没有 spec/fixture 时不能被现有矩阵放行。"""

    monkeypatch.setitem(factory_module._ADAPTER_TYPES, "future_adapter", lambda: None)

    with pytest.raises(ContractMatrixError) as caught:
        validate_fixture_coverage()

    assert caught.value.code == "adapter.contract_coverage_mismatch"


def test_duplicate_result_key_fails_exact_set() -> None:
    """异常流程：即使所有预期 key 都出现，额外重复结果仍必须阻断合并。"""

    matrix = build_contract_matrix()
    actual = [case.case_key for case in matrix]
    actual.append(actual[0])

    with pytest.raises(ContractMatrixError) as caught:
        validate_result_coverage(matrix, actual)

    assert caught.value.code == "adapter.contract_coverage_mismatch"


def test_runner_rejects_nonstandard_trace_before_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """链路流程：非 trc_ trace 必须在构造矩阵和启动 loopback 前以固定错误失败。"""

    def reject_matrix(*_args: object, **_kwargs: object) -> object:
        """若非法链路 ID 仍进入矩阵阶段，就明确暴露验证顺序回归。"""

        # 该哨兵只验证顺序，不包含请求、租户或 Provider 自由文本。
        raise AssertionError("invalid trace must fail before matrix construction")

    monkeypatch.setattr(runner_module, "build_contract_matrix", reject_matrix)
    with pytest.raises(ValueError) as caught:
        run_merge_gate_sync(
            tenant_id="ci-contract",
            request_id="req_prd064_invalid_trace",
            trace_id="trace_wrong_prefix",
            source_revision_hash="a" * 64,
        )

    assert str(caught.value) == "adapter.contract_report_invalid"


def test_full_runner_covers_success_stream_errors_and_usage(
    merge_gate_report: ContractRunReport,
) -> None:
    """主流程：完整 runner 覆盖非流式、两类 SSE、四类错误和三类 usage 且 103/103 通过。"""

    validate_report(merge_gate_report)
    assert merge_gate_report.status == "passed"
    assert merge_gate_report.coverage_complete is True
    assert merge_gate_report.total_cases == 103
    assert merge_gate_report.passed_cases == 103
    assert merge_gate_report.failed_cases == 0
    assert merge_gate_report.failure_codes == ()
    assert {case.provider_type for case in merge_gate_report.cases} == {
        spec.provider_type for spec in provider_adapter_specs()
    }
    assert {(case.case_kind, case.variant) for case in merge_gate_report.cases} >= {
        ("non_stream", "success"),
        ("stream", "success"),
        ("error", "auth_401"),
        ("error", "rate_limit_429"),
        ("error", "server_5xx"),
        ("error", "timeout"),
        ("usage", "standard"),
        ("usage", "missing"),
        ("usage", "invalid"),
    }
    for case in merge_gate_report.cases:
        expected_id = "acc_" + hashlib.sha256(
            f"{merge_gate_report.run_id}:{case.case_key}".encode()
        ).hexdigest()[:32]
        assert case.adapter_contract_case_id == expected_id


def test_expected_warning_filter_preserves_any_contract_drift(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """日志流程：只压制精确合成 warning，错误码漂移记录必须保留供 CI 诊断。"""

    logger = logging.getLogger("metahub.provider_adapter")
    caplog.set_level(logging.WARNING, logger="metahub.provider_adapter")
    common = {
        "request_id": "req_prd064_filter",
        "trace_id": "trc_prd064_filter",
        "tenant_id": "ci-contract",
    }
    with runner_module._suppress_expected_adapter_error_log(
        request_id="req_prd064_filter",
        trace_id="trc_prd064_filter",
        http_status=401,
        error_code="gateway.upstream_auth_failed",
    ):
        logger.warning(
            "provider_adapter.error",
            extra={
                **common,
                "payload": {
                    "http_status": 401,
                    "error_code": "gateway.upstream_auth_failed",
                },
            },
        )
        logger.warning(
            "provider_adapter.error",
            extra={
                **common,
                "payload": {
                    "http_status": 401,
                    "error_code": "gateway.upstream_error",
                },
            },
        )

    records = [record for record in caplog.records if record.name == logger.name]
    assert len(records) == 1
    assert records[0].payload["error_code"] == "gateway.upstream_error"


def test_report_is_canonical_private_and_hash_stable(
    merge_gate_report: ContractRunReport,
) -> None:
    """安全流程：canonical 报告仅含白名单事实，不泄露 Key、Prompt、响应或 loopback URL。"""

    encoded = canonical_report_bytes(merge_gate_report)
    decoded = json.loads(encoded)

    assert encoded == canonical_report_bytes(merge_gate_report)
    assert report_hash(merge_gate_report) == hashlib.sha256(encoded).hexdigest()
    assert decoded == merge_gate_report.to_wire()
    for forbidden in (
        SENSITIVE_CANARY.encode(),
        b"contract-input",
        b"contract-ok",
        b"Authorization",
        b"127.0.0.1",
        b"http://",
    ):
        assert forbidden not in encoded


def test_report_validator_rejects_counts_status_codes_and_case_identity(
    merge_gate_report: ContractRunReport,
) -> None:
    """异常流程：计数、状态、failure codes、trace 前缀或 case ID 漂移均用固定错误拒绝。"""

    first = merge_gate_report.cases[0]
    invalid_reports = (
        replace(merge_gate_report, total_cases=merge_gate_report.total_cases + 1),
        replace(merge_gate_report, status="failed"),
        replace(
            merge_gate_report,
            failure_codes=("adapter.contract_usage_failed",),
        ),
        replace(merge_gate_report, trace_id="trace_wrong_prefix"),
        replace(
            merge_gate_report,
            cases=(
                replace(first, adapter_contract_case_id="acc_" + "0" * 32),
                *merge_gate_report.cases[1:],
            ),
        ),
        replace(
            merge_gate_report,
            cases=(replace(first, status="skipped"), *merge_gate_report.cases[1:]),
        ),
    )

    for invalid in invalid_reports:
        with pytest.raises(ValueError) as caught:
            canonical_report_bytes(invalid)
        assert str(caught.value) == "adapter.contract_report_invalid"

    # 合法 failed 报告仍可 canonical 化，证明 validator 不是把所有失败结果一律拒绝。
    assert json.loads(canonical_report_bytes(_failed_report(merge_gate_report)))[
        "status"
    ] == "failed"


@pytest.mark.parametrize("target_name", ["factory.py", "generic_http.py"])
def test_source_revision_hash_includes_both_factory_and_generic_adapter(
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
) -> None:
    """制品身份：生产工厂或 Generic Adapter 任一源码变化都必须改变 revision hash。"""

    baseline = contract_source_revision_hash()
    original = Path.read_bytes
    touched = False

    def altered(path: Path) -> bytes:
        """只给目标文件的内存读取结果追加固定字节，不修改正式源码。"""

        nonlocal touched
        content = original(path)
        if path.name == target_name:
            touched = True
            return content + b"\n# prd064 in-memory revision probe\n"
        return content

    monkeypatch.setattr(Path, "read_bytes", altered)
    changed = contract_source_revision_hash()

    assert touched is True
    assert changed != baseline
    assert len(changed) == 64


def test_atomic_report_write_replaces_complete_document(
    merge_gate_report: ContractRunReport,
    tmp_path: Path,
) -> None:
    """落盘流程：目标只出现完整 canonical JSON，且成功后不遗留 pending 文件。"""

    target = _artifact_target(tmp_path)
    written = write_report_atomic(
        merge_gate_report,
        target,
        workspace_root=WORKSPACE_ROOT,
    )

    assert written == target.resolve()
    assert written.read_bytes() == canonical_report_bytes(merge_gate_report) + b"\n"
    assert list(written.parent.glob(".*.pending")) == []


def test_atomic_report_failure_preserves_previous_document(
    merge_gate_report: ContractRunReport,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常流程：原子替换失败时保留旧证据并清理本次私有 pending，不产生半份报告。"""

    target = _artifact_target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"previous-safe-report\n")

    def reject_replace(_source: object, _target: object) -> None:
        """模拟同目录原子替换失败，不访问或修改其他路径。"""

        # 固定异常消息不带 source/target，确保 CLI 归一测试不会泄露机器路径。
        raise OSError("synthetic atomic replace failure")

    monkeypatch.setattr(report_module.os, "replace", reject_replace)
    with pytest.raises(OSError):
        write_report_atomic(
            merge_gate_report,
            target,
            workspace_root=WORKSPACE_ROOT,
        )

    assert target.read_bytes() == b"previous-safe-report\n"
    assert list(target.parent.glob(".*.pending")) == []


def test_report_writer_rejects_path_outside_workspace_artifacts(
    merge_gate_report: ContractRunReport,
) -> None:
    """权限流程：即使目标仍在仓库内，只要不属于 `.artifacts` 就不得创建报告。"""

    forbidden = WORKSPACE_ROOT / "gateway-data-plane" / "prd064-forbidden-report.json"
    with pytest.raises(ValueError) as caught:
        write_report_atomic(
            merge_gate_report,
            forbidden,
            workspace_root=WORKSPACE_ROOT,
        )

    assert str(caught.value) == "adapter.contract_report_path_invalid"
    assert not forbidden.exists()


def test_cli_defaults_use_standard_ids_and_return_zero(
    merge_gate_report: ContractRunReport,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """命令主流程：默认生成 req_/trc_ 身份、原子写报告并只输出安全摘要。"""

    captured_kwargs: dict[str, object] = {}

    def return_report(**kwargs: object) -> ContractRunReport:
        """记录 CLI 参数并让不可变成功报告使用同一对链路 ID。"""

        captured_kwargs.update(kwargs)
        return replace(
            merge_gate_report,
            request_id=str(kwargs["request_id"]),
            trace_id=str(kwargs["trace_id"]),
        )

    monkeypatch.setattr(cli_module, "run_merge_gate_sync", return_report)
    monkeypatch.setattr(cli_module, "contract_source_revision_hash", lambda: "a" * 64)
    target = _artifact_target(tmp_path)
    exit_code = cli_module.main(
        [
            "--workspace-root",
            str(WORKSPACE_ROOT),
            "--output",
            str(target),
        ]
    )
    captured = capsys.readouterr()
    summary = json.loads(captured.out)

    assert exit_code == 0
    assert str(captured_kwargs["request_id"]).startswith("req_")
    assert str(captured_kwargs["trace_id"]).startswith("trc_")
    assert captured.err == ""
    assert summary["status"] == "passed"
    assert summary["total_cases"] == 103
    assert summary["report_path"].startswith(".artifacts/")
    assert SENSITIVE_CANARY not in captured.out


def test_cli_returns_one_for_valid_failed_contract(
    merge_gate_report: ContractRunReport,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """命令异常流程：合法失败报告必须先落盘，再以退出码 1 阻断合并。"""

    failed = _failed_report(merge_gate_report)
    monkeypatch.setattr(cli_module, "run_merge_gate_sync", lambda **_kwargs: failed)
    monkeypatch.setattr(cli_module, "contract_source_revision_hash", lambda: "a" * 64)
    target = _artifact_target(tmp_path)
    exit_code = cli_module.main(
        [
            "--workspace-root",
            str(WORKSPACE_ROOT),
            "--output",
            str(target),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert target.exists()
    assert json.loads(captured.out)["status"] == "failed"
    assert captured.err == ""


def test_cli_missing_fixture_returns_two_without_sensitive_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """命令负例：缺失显式夹具映射为退出码 2，stderr 只包含固定 code。"""

    fixtures = dict(CONTRACT_FIXTURES)
    fixtures.pop(next(iter(fixtures)))

    def run_missing(**kwargs: object) -> ContractRunReport:
        """使用缺夹具注册表调用真实 runner，并在启动网络前触发矩阵错误。"""

        return run_merge_gate_sync(**kwargs, fixtures=fixtures)

    exit_code, stdout, stderr = _run_cli_with_matrix_failure(
        monkeypatch,
        capsys,
        tmp_path,
        run_missing,
    )

    assert exit_code == 2
    assert stdout == ""
    assert stderr.strip() == "adapter.contract_fixture_missing"


def test_cli_phantom_spec_returns_two_without_source_edit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """命令负例：仅内存新增伪 spec 时以退出码 2 阻断，不修改或复制正式源码。"""

    specs = provider_adapter_specs()
    phantom = replace(
        specs[0],
        spec_id="adapter-spec-future-v1",
        provider_type="future",
        contract_case_id="prd063_future_minimum_contract",
    )
    monkeypatch.setattr(matrix_module, "provider_adapter_specs", lambda: (*specs, phantom))

    exit_code, stdout, stderr = _run_cli_with_matrix_failure(
        monkeypatch,
        capsys,
        tmp_path,
        run_merge_gate_sync,
    )

    assert exit_code == 2
    assert stdout == ""
    assert stderr.strip() == "adapter.contract_fixture_missing"


def test_cli_duplicate_result_returns_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """命令负例：重复结果 key 的 exact-set 错误必须映射为结构错误退出码 2。"""

    def run_duplicate(**_kwargs: object) -> ContractRunReport:
        """在内存构造重复实际结果，复用生产覆盖 validator 触发固定错误。"""

        matrix = build_contract_matrix()
        keys = [case.case_key for case in matrix]
        validate_result_coverage(matrix, (*keys, keys[0]))
        # validator 理应在上一行抛出；走到这里表示重复结果被错误放行。
        raise AssertionError("duplicate result must fail")

    exit_code, stdout, stderr = _run_cli_with_matrix_failure(
        monkeypatch,
        capsys,
        tmp_path,
        run_duplicate,
    )

    assert exit_code == 2
    assert stdout == ""
    assert stderr.strip() == "adapter.contract_coverage_mismatch"


def test_cli_probe_requires_explicit_configuration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """边界流程：M1 未配置真实探测时返回 3，不能把 mock 或任意 URL 当作真实 Provider。"""

    exit_code = cli_module.main(["--mode", "gateway_probe"])
    captured = capsys.readouterr()

    assert exit_code == 3
    assert captured.out == ""
    assert captured.err.strip() == "adapter.contract_probe_config_required"


def test_ci_scripts_wire_independent_gate_before_full_pytest() -> None:
    """CI 流程：独立 PowerShell wrapper 必须由全量 pytest 前的 check 门禁显式调用。"""

    wrapper = (
        WORKSPACE_ROOT / "scripts" / "ci" / "provider-adapter-contract-test.ps1"
    ).read_text(encoding="utf-8")
    check = (WORKSPACE_ROOT / "scripts" / "ci" / "check.ps1").read_text(
        encoding="utf-8"
    )

    assert "metahub_gateway.provider_contracts" in wrapper
    assert "--tenant-id" in wrapper
    assert "exit $ContractExitCode" in wrapper
    invocation = check.index("provider-adapter-contract-test.ps1")
    full_pytest = check.index("Invoke-CheckedNative $Python -m pytest")
    assert invocation < full_pytest
    assert "$ProviderContractArguments = @(" in check
    assert (
        "Invoke-CheckedNative -FilePath $CurrentPowerShell "
        "-Arguments $ProviderContractArguments"
    ) in check
