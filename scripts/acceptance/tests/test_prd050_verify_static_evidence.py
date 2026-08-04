"""验证 PRD-050 静态证据信任根的正向合同与关键失败关闭边界。

本文件只构造临时 workspace 和候选目录，不运行真实全量测试，也不修改开发数据库。单个测试
函数顺序覆盖完整成功证据、JSON 重复键、路径逃逸、源码闭集缩减、Node 统计缩减、XML 实体
声明、额外 XML 与 CLI 脱敏；独立 PowerShell 探针再覆盖归档重入和不可破坏发布边界。
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

# 验证器通过文件路径加载，避免带连字符的脚本文件名污染正常 Python 包导入合同。
VERIFIER_PATH = Path(__file__).resolve().parents[1] / "prd050-verify-static-evidence.py"
VERIFIER_SPEC = importlib.util.spec_from_file_location(
    "prd050_verify_static_evidence_contract",
    VERIFIER_PATH,
)
assert VERIFIER_SPEC is not None and VERIFIER_SPEC.loader is not None
# 动态模块对象只执行候选验证器的纯函数，不注册服务、不读取环境秘密或连接开发数据库。
VERIFIER = importlib.util.module_from_spec(VERIFIER_SPEC)
VERIFIER_SPEC.loader.exec_module(VERIFIER)

# 测试 fixture 独立锁定真实 Windows runner 字节，不能从被测 verifier 常量反向生成期望值。
PRODUCTION_QUALITY_OUTPUTS = {
    "console-lint": b"console lint ok (118 files)\n",
    "ruff": b"All checks passed!\n",
    "comments": b"comment coverage ok\r\n",
}

# 期望快照合同独立于被测 verifier，避免测试从错误常量反向生成后仍然自洽通过。
PRODUCTION_SNAPSHOT_CONTRACTS = {
    ".tmp/prd050-prepare-candidate.py": "prd050-prepare-candidate.py",
    ".tmp/prd050-assemble-candidate.py": "prd050-assemble-candidate.py",
    ".tmp/prd050-run-real-acceptance.ps1": "prd050-run-real-acceptance.ps1",
    ".tmp/prd050-final-closeout-audit.ps1": "prd050-final-closeout-audit.ps1",
    ".tmp/prd050-rerun-static-evidence.ps1": "prd050-rerun-static-evidence.ps1",
    ".tmp/prd050-generate-runner-process-contract.ps1": (
        "prd050-generate-runner-process-contract.ps1"
    ),
    ".tmp/prd050-launch-real-acceptance.ps1": "prd050-launch-real-acceptance.ps1",
    "scripts/acceptance/prd047-browser-acceptance.mjs": "prd047-browser-acceptance.mjs",
    "scripts/acceptance/prd048-runtime-acceptance.py": "prd048-runtime-acceptance.py",
    "scripts/acceptance/prd050-runtime-acceptance.py": "prd050-runtime-acceptance.py",
    "scripts/acceptance/prd050-browser-acceptance.mjs": "prd050-browser-acceptance.mjs",
    "scripts/acceptance/prd050-verify-static-evidence.py": (
        "prd050-verify-static-evidence.py"
    ),
}
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
STATIC_RUNNER_PATH = WORKSPACE_ROOT / ".tmp" / "prd050-rerun-static-evidence.ps1"
RUNTIME_RUNNER_PATH = WORKSPACE_ROOT / ".tmp" / "prd050-run-real-acceptance.ps1"
CLOSEOUT_PATH = WORKSPACE_ROOT / ".tmp" / "prd050-final-closeout-audit.ps1"
LAUNCHER_PATH = WORKSPACE_ROOT / ".tmp" / "prd050-launch-real-acceptance.ps1"
GENERATOR_PATH = WORKSPACE_ROOT / ".tmp" / "prd050-generate-runner-process-contract.ps1"
PREPARE_HELPER_PATH = WORKSPACE_ROOT / ".tmp" / "prd050-prepare-candidate.py"
ASSEMBLER_HELPER_PATH = WORKSPACE_ROOT / ".tmp" / "prd050-assemble-candidate.py"
COMMENT_CHECKER_PATH = WORKSPACE_ROOT / "scripts" / "ci" / "check-comments.py"


def _load_helper_module(path: Path, module_name: str) -> Any:
    """按真实文件路径加载冻结 helper，并注册模块以支持 dataclass 的类型解析。"""

    specification = importlib.util.spec_from_file_location(module_name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    return module


# 冻结 prepare helper 模块供同一测试项执行安全合同，不运行真实数据库迁移。
PREPARE_HELPER = _load_helper_module(
    PREPARE_HELPER_PATH,
    "prd050_prepare_candidate_contract",
)
# 冻结 assembler helper 模块供临时候选组装探针复用，不启动真实 verifier。
ASSEMBLER_HELPER = _load_helper_module(
    ASSEMBLER_HELPER_PATH,
    "prd050_assemble_candidate_contract",
)
# 注释检查器同样以纯函数模块加载，验证最终 static runner 不会扫描不可变证据中的源码副本。
COMMENT_CHECKER = _load_helper_module(
    COMMENT_CHECKER_PATH,
    "metahub_check_comments_contract",
)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """以稳定 UTF-8 JSON 写入临时 fixture，保留尾随换行以匹配生产 manifest。"""

    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _file_record(path: Path, relative: str) -> dict[str, Any]:
    """为临时证据生成与生产 schema 一致的相对路径、大小和 SHA-256 记录。"""

    data = path.read_bytes()
    return {
        "path": relative,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _node_output(test_count: int) -> bytes:
    """生成当前 Node spec reporter 的完整六字段成功汇总，用于独立语义校验。"""

    return (
        "✔ fixture case\n"
        f"ℹ tests {test_count}\n"
        "ℹ suites 0\n"
        f"ℹ pass {test_count}\n"
        "ℹ fail 0\n"
        "ℹ cancelled 0\n"
        "ℹ skipped 0\n"
        "ℹ todo 0\n"
        "ℹ duration_ms 1.0\n"
    ).encode()


def _junit_document(test_count: int) -> bytes:
    """生成无失败、错误和跳过节点的叶子 JUnit，且 testcase 数与 suite 属性一致。"""

    return (
        b'<testsuites><testsuite tests="'
        + str(test_count).encode("ascii")
        + b'" failures="0" errors="0" skipped="0" time="1.0">'
        + (b"<testcase/>" * test_count)
        + b"</testsuite></testsuites>"
    )


def _command_stdout(command_id: str) -> bytes:
    """返回某条固定命令的合规 stdout，质量门禁使用精确生产成功文本。"""

    if command_id in VERIFIER.NODE_TEST_CONTRACTS:
        return _node_output(VERIFIER.NODE_TEST_CONTRACTS[command_id])
    if command_id in PRODUCTION_QUALITY_OUTPUTS:
        return PRODUCTION_QUALITY_OUTPUTS[command_id]
    if command_id == "compileall":
        return b""
    return b"pytest ok\n"


def _build_fixture(workspace: Path) -> dict[str, Any]:
    """构造满足全部源码、命令、JUnit、摘要和十快照合同的独立候选目录。

    返回字典只保存测试修改所需的路径、generation 和已解析对象，不包含任何真实凭据或业务
    数据；每个故障用例使用独立 workspace，避免前一轮篡改影响后一轮定位。
    """

    generation = "20260723T120003"
    batch = workspace / "artifacts" / "prd050" / generation
    batch.mkdir(parents=True)
    source_records: list[dict[str, Any]] = []
    for relative in VERIFIER.SOURCE_PATH_ORDER:
        source_path = workspace.joinpath(*PurePosixPath(relative).parts)
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(("source:" + relative).encode("utf-8"))
        source_records.append(_file_record(source_path, relative))
    for source_relative, snapshot_name in VERIFIER.SNAPSHOT_CONTRACTS.items():
        source_path = workspace.joinpath(*PurePosixPath(source_relative).parts)
        (batch / snapshot_name).write_bytes(source_path.read_bytes())

    fingerprint = VERIFIER.compute_source_fingerprint(source_records)
    source_manifest = {
        "schema_version": 1,
        "status": "passed",
        "evidence_kind": "prd050_source_snapshot",
        "prd_id": "PRD-050",
        "generation": generation,
        "generated_at_utc": "2026-07-22T12:00:00.000Z",
        "file_count": len(source_records),
        "files_fingerprint_sha256": fingerprint,
        "files": source_records,
    }
    source_manifest_path = batch / "source-manifest.json"
    _write_json(source_manifest_path, source_manifest)

    commands: list[dict[str, Any]] = []
    for command_id in VERIFIER.COMMAND_IDS:
        stdout_path = batch / f"{command_id}.stdout.log"
        stderr_path = batch / f"{command_id}.stderr.log"
        exitcode_path = batch / f"{command_id}.exitcode"
        stdout_path.write_bytes(_command_stdout(command_id))
        stderr_path.write_bytes(b"")
        exitcode_path.write_bytes(b"0\n")
        junit = None
        if command_id in VERIFIER.JUNIT_CONTRACTS:
            junit_name, test_count = VERIFIER.JUNIT_CONTRACTS[command_id]
            junit_path = batch / junit_name
            junit_path.write_bytes(_junit_document(test_count))
            junit = {
                "file": _file_record(junit_path, junit_name),
                "summary": {
                    "suites": 1,
                    "tests": test_count,
                    "failures": 0,
                    "errors": 0,
                    "skipped": 0,
                    "time_seconds": 1.0,
                },
            }
        executable, arguments, working_directory = VERIFIER.expected_command_arguments(
            command_id,
            workspace,
            batch,
            generation,
        )
        commands.append(
            {
                "id": command_id,
                "status": "passed",
                "command": {
                    "executable": executable,
                    "arguments": list(arguments),
                    "working_directory": working_directory,
                },
                "started_at_utc": "2026-07-22T12:00:00.000Z",
                "finished_at_utc": "2026-07-22T12:00:00.001Z",
                "duration_ms": 1,
                "exit_code": 0,
                "stdout": _file_record(stdout_path, f"{command_id}.stdout.log"),
                "stderr": _file_record(stderr_path, f"{command_id}.stderr.log"),
                "exitcode_file": _file_record(exitcode_path, f"{command_id}.exitcode"),
                "junit": junit,
            }
        )

    python_tests = VERIFIER.JUNIT_CONTRACTS["python-full"][1]
    python_summary = {
        "status": "passed",
        "evidence_kind": "junit_derived_summary",
        "source_junit": "python-full-junit.xml",
        "tests": python_tests,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "time_seconds": 1.0,
        "exit_code": 0,
        "generated_at": "2026-07-22T12:00:01Z",
    }
    python_summary_path = batch / "python-full-summary.json"
    _write_json(python_summary_path, python_summary)
    static_manifest = {
        "schema_version": 1,
        "status": "passed",
        "evidence_kind": "prd050_static_one_shot",
        "prd_id": "PRD-050",
        "generation": generation,
        "generated_at_utc": "2026-07-22T12:00:02.000Z",
        "source_manifest": {
            **_file_record(source_manifest_path, "source-manifest.json"),
            "generation": generation,
            "file_count": len(source_records),
            "files_fingerprint_sha256": fingerprint,
        },
        "command_count": len(commands),
        "commands": commands,
        "junit_aggregate": {
            "files": len(VERIFIER.JUNIT_CONTRACTS),
            "tests": sum(value[1] for value in VERIFIER.JUNIT_CONTRACTS.values()),
            "failures": 0,
            "errors": 0,
            "skipped": 0,
        },
        "derived_artifacts": [
            _file_record(python_summary_path, "python-full-summary.json")
        ],
    }
    static_manifest_path = batch / "static-evidence-manifest.json"
    _write_json(static_manifest_path, static_manifest)
    return {
        "workspace": workspace,
        "batch": batch,
        "generation": generation,
        "source": source_manifest,
        "source_path": source_manifest_path,
        "static": static_manifest,
        "static_path": static_manifest_path,
    }


def _rewrite_source(fixture: dict[str, Any]) -> None:
    """重写变更后的 source manifest，并同步 static 中允许攻击者重算的交叉摘要。

    故障注入主动同步所有公开哈希，确保负向测试证明的是独立闭集校验，而不是偶然被旧摘要
    提前拦截。
    """

    source = fixture["source"]
    source["file_count"] = len(source["files"])
    source["files_fingerprint_sha256"] = VERIFIER.compute_source_fingerprint(source["files"])
    _write_json(fixture["source_path"], source)
    fixture["static"]["source_manifest"] = {
        **_file_record(fixture["source_path"], "source-manifest.json"),
        "generation": fixture["generation"],
        "file_count": source["file_count"],
        "files_fingerprint_sha256": source["files_fingerprint_sha256"],
    }
    _write_json(fixture["static_path"], fixture["static"])


def _rewrite_command_file(
    fixture: dict[str, Any],
    command_id: str,
    field: str,
    path: Path,
) -> None:
    """同步命令文件记录与 static manifest，证明语义门禁不依赖陈旧 SHA 失败。"""

    for command in fixture["static"]["commands"]:
        if command["id"] != command_id:
            continue
        if field == "junit":
            command["junit"]["file"] = _file_record(path, path.name)
        else:
            command[field] = _file_record(path, path.name)
        break
    _write_json(fixture["static_path"], fixture["static"])


def _expect_rejected(callback: Callable[[], Any], expected_code: str, label: str) -> None:
    """执行单个故障注入并断言固定错误码，使失败报告直接标出具体安全场景。"""

    with pytest.raises(VERIFIER.EvidenceRejected) as caught:
        callback()
    assert str(caught.value) == expected_code, label


def _helper_record(relative: str, data: bytes) -> dict[str, Any]:
    """为 helper fixture 生成与源码 manifest 相同的路径、长度和 SHA-256 记录。"""

    return {
        "path": relative,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _write_helper_bytes(path: Path, data: bytes) -> None:
    """创建 fixture 普通文件及其父目录；测试目录由 pytest 独占，不承载真实业务数据。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _build_assembler_helper_fixture(root: Path) -> SimpleNamespace:
    """构造 69 源文件、40 静态文件、冻结 assembler 与六文件 stage 的完整候选。"""

    generation = "20260723T120000"
    workspace = root / "workspace"
    batch = workspace / "artifacts" / "prd050" / generation
    stage = workspace / ".tmp" / f"prd050-stage-{generation}"
    failed = workspace / "artifacts" / "prd050" / "failed-attempts"
    for directory in (batch, stage, failed):
        directory.mkdir(parents=True)

    assembler_bytes = ASSEMBLER_HELPER_PATH.read_bytes()
    prepare_bytes = PREPARE_HELPER_PATH.read_bytes()
    source_data: dict[str, bytes] = {
        ASSEMBLER_HELPER.ASSEMBLER_SOURCE_RELATIVE: assembler_bytes,
        ASSEMBLER_HELPER.PREPARE_SOURCE_RELATIVE: prepare_bytes,
    }
    for relative in ASSEMBLER_HELPER.SNAPSHOT_CONTRACTS:
        source_data[relative] = ("snapshot:" + relative).encode("utf-8")
    filler_count = ASSEMBLER_HELPER.EXPECTED_SOURCE_FILE_COUNT - len(source_data)
    for index in range(filler_count):
        relative = f"fixture/source-{index:02d}.py"
        source_data[relative] = ("source:" + relative).encode("utf-8")
    assert len(source_data) == 69
    for relative, data in source_data.items():
        source_path = workspace.joinpath(*PurePosixPath(relative).parts)
        _write_helper_bytes(source_path, data)

    source_records = [
        _helper_record(relative, source_data[relative])
        for relative in sorted(source_data)
    ]
    source_manifest = {
        "schema_version": 1,
        "status": "passed",
        "evidence_kind": "prd050_source_snapshot",
        "prd_id": "PRD-050",
        "generation": generation,
        "file_count": 69,
        "files_fingerprint_sha256": "0" * 64,
        "files": source_records,
    }
    source_manifest_bytes = (
        json.dumps(source_manifest, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    for name in ASSEMBLER_HELPER.STATIC_FILE_NAMES:
        data = (
            source_manifest_bytes
            if name == ASSEMBLER_HELPER.SOURCE_MANIFEST_NAME
            else ("static:" + name).encode("utf-8")
        )
        _write_helper_bytes(batch / name, data)
    _write_helper_bytes(batch / ASSEMBLER_HELPER.ASSEMBLER_NAME, assembler_bytes)

    prepare_record = _helper_record(ASSEMBLER_HELPER.PREPARE_NAME, prepare_bytes)
    development_receipt = {
        "status": "passed",
        "evidence_kind": "prd050_prepare_candidate",
        "generation": generation,
        "stage_file_count": 6,
        "prepare_snapshot": prepare_record,
    }
    stage_data = {
        ASSEMBLER_HELPER.PREPARE_NAME: prepare_bytes,
        "source-pre-0040.db": b"source database",
        "source-pre-0040.json": b'{"status":"passed"}\n',
        "development-database-migration.json": (
            json.dumps(development_receipt, separators=(",", ":")) + "\n"
        ).encode("utf-8"),
        "metahub-prd050.db": b"acceptance database",
        "acceptance-database-initial.json": b'{"status":"passed"}\n',
    }
    for name, data in stage_data.items():
        _write_helper_bytes(stage / name, data)
    return SimpleNamespace(
        generation=generation,
        workspace=workspace,
        batch=batch,
        stage=stage,
        failed=failed,
    )


def _invoke_assembler_fixture(
    fixture: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    """在临时工作区调用真实 assembler 主流程，只替换昂贵的 verifier 子进程结果。"""

    arguments = SimpleNamespace(
        generation=fixture.generation,
        workspace=fixture.workspace,
        batch_dir=fixture.batch,
        stage_dir=fixture.stage,
    )
    verifier_receipt = (
        b'{"status":"passed","evidence_kind":'
        b'"prd050_static_evidence_verification"}\n'
    )
    with monkeypatch.context() as patch_context:
        patch_context.setattr(
            ASSEMBLER_HELPER,
            "PRODUCTION_WORKSPACE",
            fixture.workspace,
        )
        patch_context.setattr(
            ASSEMBLER_HELPER,
            "__file__",
            str(fixture.batch / ASSEMBLER_HELPER.ASSEMBLER_NAME),
        )
        patch_context.setattr(ASSEMBLER_HELPER, "parse_args", lambda: arguments)
        patch_context.setattr(
            ASSEMBLER_HELPER,
            "run_frozen_verifier",
            lambda *_args: verifier_receipt,
        )
        return ASSEMBLER_HELPER.main()


def test_static_evidence_trust_root_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """顺序验证完整成功合同、多类伪造证据和 CLI 敏感输入的失败关闭行为。"""

    assert VERIFIER.SNAPSHOT_CONTRACTS == PRODUCTION_SNAPSHOT_CONTRACTS
    positive = _build_fixture(tmp_path / "positive")
    result = VERIFIER.verify_all(
        str(positive["workspace"]),
        str(positive["batch"]),
        positive["generation"],
    )
    assert set(result) == {
        "status",
        "evidence_kind",
        "source_manifest_sha256",
        "static_manifest_sha256",
        "files_fingerprint_sha256",
        "source_file_count",
        "command_count",
        "junit_files",
        "junit_tests",
        "snapshot_count",
    }, "success JSON fields must remain stable"
    assert result["status"] == "passed", "positive fixture must pass"
    assert result["source_file_count"] == VERIFIER.EXPECTED_SOURCE_FILE_COUNT
    assert result["command_count"] == 11
    assert result["junit_files"] == 4
    assert result["junit_tests"] == VERIFIER.EXPECTED_JUNIT_TESTS
    assert result["snapshot_count"] == 12

    missing_browser_base = _build_fixture(tmp_path / "snapshot-browser-base-missing")
    (
        missing_browser_base["batch"]
        / PRODUCTION_SNAPSHOT_CONTRACTS["scripts/acceptance/prd047-browser-acceptance.mjs"]
    ).unlink()
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(missing_browser_base["workspace"]),
            str(missing_browser_base["batch"]),
            missing_browser_base["generation"],
        ),
        "prd050_static_evidence_snapshot_missing",
        "PRD-047 browser base executable snapshot missing",
    )

    missing_runtime_base = _build_fixture(tmp_path / "snapshot-runtime-base-missing")
    (
        missing_runtime_base["batch"]
        / PRODUCTION_SNAPSHOT_CONTRACTS["scripts/acceptance/prd048-runtime-acceptance.py"]
    ).unlink()
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(missing_runtime_base["workspace"]),
            str(missing_runtime_base["batch"]),
            missing_runtime_base["generation"],
        ),
        "prd050_static_evidence_snapshot_missing",
        "PRD-048 runtime base executable snapshot missing",
    )

    missing_runtime = _build_fixture(tmp_path / "snapshot-runtime-missing")
    (
        missing_runtime["batch"]
        / PRODUCTION_SNAPSHOT_CONTRACTS["scripts/acceptance/prd050-runtime-acceptance.py"]
    ).unlink()
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(missing_runtime["workspace"]),
            str(missing_runtime["batch"]),
            missing_runtime["generation"],
        ),
        "prd050_static_evidence_snapshot_missing",
        "runtime executable snapshot missing",
    )

    changed_browser = _build_fixture(tmp_path / "snapshot-browser-changed")
    (
        changed_browser["batch"]
        / PRODUCTION_SNAPSHOT_CONTRACTS["scripts/acceptance/prd050-browser-acceptance.mjs"]
    ).write_bytes(b"changed browser executable")
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(changed_browser["workspace"]),
            str(changed_browser["batch"]),
            changed_browser["generation"],
        ),
        "prd050_static_evidence_snapshot_mismatch",
        "browser executable snapshot changed",
    )

    duplicate = _build_fixture(tmp_path / "duplicate")
    source_text = duplicate["source_path"].read_text(encoding="utf-8")
    duplicate["source_path"].write_text(
        source_text.replace(
            '"status":"passed"',
            '"status":"passed","status":"passed"',
            1,
        ),
        encoding="utf-8",
    )
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(duplicate["workspace"]), str(duplicate["batch"]), duplicate["generation"]
        ),
        "prd050_static_evidence_json_duplicate_key",
        "duplicate JSON key",
    )

    escaped = _build_fixture(tmp_path / "escape")
    escaped["source"]["files"][0]["path"] = "../outside"
    _write_json(escaped["source_path"], escaped["source"])
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(escaped["workspace"]), str(escaped["batch"]), escaped["generation"]
        ),
        "prd050_static_evidence_source_file_invalid",
        "source path escape",
    )

    shrunk = _build_fixture(tmp_path / "source-shrink")
    shrunk["source"]["files"].pop()
    _rewrite_source(shrunk)
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(shrunk["workspace"]), str(shrunk["batch"]), shrunk["generation"]
        ),
        "prd050_static_evidence_source_contract_mismatch",
        "source closed set shrink",
    )

    duplicate_identity = _build_fixture(tmp_path / "source-identity")
    non_snapshot_paths = [
        relative
        for relative in VERIFIER.SOURCE_PATH_ORDER
        if relative not in VERIFIER.SNAPSHOT_CONTRACTS
    ]
    original_relative, duplicate_relative = non_snapshot_paths[:2]
    original_path = duplicate_identity["workspace"].joinpath(
        *PurePosixPath(original_relative).parts
    )
    duplicate_path = duplicate_identity["workspace"].joinpath(
        *PurePosixPath(duplicate_relative).parts
    )
    duplicate_path.unlink()
    os.link(original_path, duplicate_path)
    for record in duplicate_identity["source"]["files"]:
        if record["path"] == duplicate_relative:
            record.update(_file_record(duplicate_path, duplicate_relative))
            break
    _rewrite_source(duplicate_identity)
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(duplicate_identity["workspace"]),
            str(duplicate_identity["batch"]),
            duplicate_identity["generation"],
        ),
        "prd050_static_evidence_source_identity_duplicate",
        "two source paths share one filesystem identity",
    )

    stable_path = tmp_path / "fstat-drift.bin"
    stable_path.write_bytes(b"stable")
    original_fstat = VERIFIER.os.fstat
    fstat_calls = 0

    def _drifting_fstat(file_descriptor: int) -> Any:
        """在第二次句柄复核时伪造文件身份漂移，证明 before/after fstat 均参与门禁。"""

        nonlocal fstat_calls
        current = original_fstat(file_descriptor)
        fstat_calls += 1
        if fstat_calls != 2:
            return current
        return SimpleNamespace(
            st_dev=current.st_dev,
            st_ino=current.st_ino + 1,
            st_mode=current.st_mode,
            st_size=current.st_size,
            st_mtime_ns=current.st_mtime_ns,
        )

    with monkeypatch.context() as patch_context:
        patch_context.setattr(VERIFIER.os, "fstat", _drifting_fstat)
        _expect_rejected(
            lambda: VERIFIER.read_bytes_stable(
                stable_path,
                1024,
                "prd050_fixture_fstat_changed",
            ),
            "prd050_fixture_fstat_changed",
            "opened handle identity drift",
        )

    tap = _build_fixture(tmp_path / "tap-shrink")
    tap_path = tap["batch"] / "console-test.stdout.log"
    tap_path.write_bytes(_node_output(353))
    _rewrite_command_file(tap, "console-test", "stdout", tap_path)
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(tap["workspace"]), str(tap["batch"]), tap["generation"]
        ),
        "prd050_static_evidence_node_summary_mismatch",
        "Node TAP summary shrink",
    )

    # Browser 组合合同必须用恰少一条的反例锁住 71 条边界，避免只验证相差较大的
    # 陈旧计数仍能拒绝，却没有证明最新权威计数已真正生效。
    browser_tap = _build_fixture(tmp_path / "browser-tap-shrink")
    browser_tap_path = browser_tap["batch"] / "browser-unit.stdout.log"
    browser_tap_path.write_bytes(_node_output(70))
    _rewrite_command_file(
        browser_tap,
        "browser-unit",
        "stdout",
        browser_tap_path,
    )
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(browser_tap["workspace"]),
            str(browser_tap["batch"]),
            browser_tap["generation"],
        ),
        "prd050_static_evidence_node_summary_mismatch",
        "Browser-unit TAP summary one-short",
    )

    quality = _build_fixture(tmp_path / "quality-drift")
    quality_path = quality["batch"] / "console-lint.stdout.log"
    quality_path.write_bytes(b"console lint ok (117 files)\n")
    _rewrite_command_file(quality, "console-lint", "stdout", quality_path)
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(quality["workspace"]), str(quality["batch"]), quality["generation"]
        ),
        "prd050_static_evidence_quality_output_mismatch",
        "quality gate output drift",
    )

    stderr = _build_fixture(tmp_path / "stderr")
    stderr_path = stderr["batch"] / "comments.stderr.log"
    stderr_path.write_bytes(b"unexpected stderr\n")
    _rewrite_command_file(stderr, "comments", "stderr", stderr_path)
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(stderr["workspace"]), str(stderr["batch"]), stderr["generation"]
        ),
        "prd050_static_evidence_command_stderr_not_empty",
        "non-JUnit stderr must stay empty",
    )

    junit_stderr = _build_fixture(tmp_path / "junit-stderr")
    junit_stderr_path = junit_stderr["batch"] / "python-full.stderr.log"
    junit_stderr_path.write_bytes(b"unexpected pytest stderr\n")
    _rewrite_command_file(
        junit_stderr,
        "python-full",
        "stderr",
        junit_stderr_path,
    )
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(junit_stderr["workspace"]),
            str(junit_stderr["batch"]),
            junit_stderr["generation"],
        ),
        "prd050_static_evidence_command_stderr_not_empty",
        "JUnit stderr must stay empty",
    )

    compileall = _build_fixture(tmp_path / "compileall")
    compileall_path = compileall["batch"] / "compileall.stdout.log"
    compileall_path.write_bytes(b"unexpected compile output\n")
    _rewrite_command_file(compileall, "compileall", "stdout", compileall_path)
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(compileall["workspace"]),
            str(compileall["batch"]),
            compileall["generation"],
        ),
        "prd050_static_evidence_compileall_output_not_empty",
        "compileall stdout must stay empty",
    )

    dtd = _build_fixture(tmp_path / "dtd")
    junit_path = dtd["batch"] / "python-full-junit.xml"
    junit_path.write_bytes(
        (b" " * (1024 * 1024 + 1))
        + b'<!DOCTYPE testsuites [<!ENTITY hidden "value">]>'
        + junit_path.read_bytes()
    )
    _rewrite_command_file(dtd, "python-full", "junit", junit_path)
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(dtd["workspace"]), str(dtd["batch"]), dtd["generation"]
        ),
        "prd050_static_evidence_junit_unsafe_xml",
        "full document DTD scan",
    )

    extra_xml = _build_fixture(tmp_path / "extra-xml")
    browser_dir = extra_xml["batch"] / "browser"
    browser_dir.mkdir()
    (browser_dir / "unexpected.xml").write_text("<unexpected/>", encoding="utf-8")
    _expect_rejected(
        lambda: VERIFIER.verify_all(
            str(extra_xml["workspace"]),
            str(extra_xml["batch"]),
            extra_xml["generation"],
        ),
        "prd050_static_evidence_xml_set_invalid",
        "extra candidate XML",
    )

    secret = "SENSITIVE_WORKSPACE_SHOULD_NOT_ECHO"
    sanitized_environment = os.environ.copy()
    for name in VERIFIER.ADMIN_PASSWORD_ENVIRONMENT_NAMES:
        sanitized_environment.pop(name, None)
    completed = subprocess.run(
        [sys.executable, str(VERIFIER_PATH), "--workspace", secret],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=sanitized_environment,
    )
    assert completed.returncode == 1, "invalid CLI must fail"
    assert completed.stdout == "", "failed CLI must keep stdout empty"
    assert completed.stderr == "prd050_static_evidence_cli_invalid\n"
    assert secret not in completed.stdout + completed.stderr, "CLI must not echo sensitive input"

    for environment_name in VERIFIER.ADMIN_PASSWORD_ENVIRONMENT_NAMES:
        inherited_environment = sanitized_environment.copy()
        inherited_environment[environment_name] = secret
        inherited = subprocess.run(
            [sys.executable, str(VERIFIER_PATH)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=inherited_environment,
        )
        assert inherited.returncode == 1, "admin password environment must fail"
        assert inherited.stdout == "", "environment rejection must keep stdout empty"
        assert inherited.stderr == (
            "prd050_static_evidence_admin_password_environment_forbidden\n"
        )
        assert secret not in inherited.stdout + inherited.stderr

    # 两个独立 helper 的闭集算术必须由可执行常量而非文档文本锁定。
    assert len(ASSEMBLER_HELPER.STATIC_FILE_NAMES) == 40
    assert len(ASSEMBLER_HELPER.INITIAL_BATCH_NAMES) == 41
    assert len(ASSEMBLER_HELPER.ASSEMBLED_INPUT_NAMES) == 16
    assert len(ASSEMBLER_HELPER.PRE_VERIFICATION_NAMES) == 57
    assert len(ASSEMBLER_HELPER.FINAL_BATCH_NAMES) == 60
    assert ASSEMBLER_HELPER.EXPECTED_SOURCE_FILE_COUNT == 69

    assembled = _build_assembler_helper_fixture(tmp_path / "assembler-success")
    assert _invoke_assembler_fixture(assembled, monkeypatch) == 0
    assert {path.name for path in assembled.batch.iterdir()} == set(
        ASSEMBLER_HELPER.FINAL_BATCH_NAMES
    )
    assert len(list(assembled.batch.iterdir())) == 60
    assert (
        assembled.batch / "static-evidence-verification.stderr.log"
    ).read_bytes() == b""
    assert (
        assembled.batch / "static-evidence-verification.exitcode"
    ).read_bytes() == b"0\n"

    tampered = _build_assembler_helper_fixture(tmp_path / "assembler-tampered")
    (tampered.stage / ASSEMBLER_HELPER.PREPARE_NAME).write_bytes(b"tampered")
    with pytest.raises(ASSEMBLER_HELPER.AssemblerRejected) as caught_tampered:
        _invoke_assembler_fixture(tampered, monkeypatch)
    assert str(caught_tampered.value) == "prd050_assembler_prepare_receipt_mismatch"
    assert {path.name for path in tampered.batch.iterdir()} == set(
        ASSEMBLER_HELPER.INITIAL_BATCH_NAMES
    )

    archived = _build_assembler_helper_fixture(tmp_path / "assembler-archived")
    (archived.failed / f"{archived.generation}-crash").write_bytes(b"sentinel")
    with pytest.raises(ASSEMBLER_HELPER.AssemblerRejected) as caught_archived:
        _invoke_assembler_fixture(archived, monkeypatch)
    assert str(caught_archived.value) == (
        "prd050_assembler_generation_previously_failed"
    )

    # create-new 原语必须在旧目标存在时保留原字节，prepare 与 assembler 各自独立覆盖。
    collision_root = tmp_path / "helper-collisions"
    collision_root.mkdir()
    prepare_collision = collision_root / "prepare.bin"
    prepare_collision.write_bytes(b"old-prepare")
    with pytest.raises(PREPARE_HELPER.PrepareRejected) as caught_prepare_collision:
        PREPARE_HELPER.write_bytes_new(
            prepare_collision,
            b"new-prepare",
            "prd050_prepare_probe_collision",
        )
    assert str(caught_prepare_collision.value) == "prd050_prepare_probe_collision"
    assert prepare_collision.read_bytes() == b"old-prepare"
    assembler_collision = collision_root / "assembler.bin"
    assembler_collision.write_bytes(b"old-assembler")
    with pytest.raises(ASSEMBLER_HELPER.AssemblerRejected) as caught_assembler_collision:
        ASSEMBLER_HELPER.write_bytes_new(
            assembler_collision,
            b"new-assembler",
            "prd050_assembler_probe_collision",
        )
    assert str(caught_assembler_collision.value) == "prd050_assembler_probe_collision"
    assert assembler_collision.read_bytes() == b"old-assembler"

    # prepare 在任何数据库动作之前拒绝失败归档、活动 batch 与同名安全备份。
    prepare_workspace = tmp_path / "prepare-conflicts"
    prepare_failed = prepare_workspace / "artifacts" / "prd050" / "failed-attempts"
    prepare_backups = prepare_workspace / ".data" / "backups"
    prepare_failed.mkdir(parents=True)
    prepare_backups.mkdir(parents=True)
    prepare_generation = "20260723T120001"
    safety_backup = prepare_backups / f"backup-{prepare_generation}.db"
    with monkeypatch.context() as patch_context:
        patch_context.setattr(PREPARE_HELPER, "WORKSPACE", prepare_workspace)
        patch_context.setattr(PREPARE_HELPER, "FAILED_ATTEMPTS", prepare_failed)
        near_prefix = prepare_failed / f"{prepare_generation}0-near"
        near_prefix.mkdir()
        PREPARE_HELPER.scan_generation_conflicts(prepare_generation, safety_backup)
        archived_path = prepare_failed / f"{prepare_generation}-failed"
        archived_path.write_bytes(b"failed")
        with pytest.raises(PREPARE_HELPER.PrepareRejected) as caught_prepare_archive:
            PREPARE_HELPER.scan_generation_conflicts(prepare_generation, safety_backup)
        assert str(caught_prepare_archive.value) == (
            "prd050_prepare_generation_previously_failed"
        )
        archived_path.unlink()
        active_batch = prepare_workspace / "artifacts" / "prd050" / prepare_generation
        active_batch.mkdir()
        with pytest.raises(PREPARE_HELPER.PrepareRejected) as caught_active_batch:
            PREPARE_HELPER.scan_generation_conflicts(prepare_generation, safety_backup)
        assert str(caught_active_batch.value) == "prd050_prepare_active_batch_exists"
        active_batch.rmdir()
        safety_backup.write_bytes(b"old-backup")
        with pytest.raises(PREPARE_HELPER.PrepareRejected) as caught_backup:
            PREPARE_HELPER.scan_generation_conflicts(prepare_generation, safety_backup)
        assert str(caught_backup.value) == "prd050_prepare_safety_backup_exists"
        assert safety_backup.read_bytes() == b"old-backup"

    # prepare 的迁移原语只能接收 generation 临时候选，且共享开发库在正反路径均保持原字节。
    prepare_source = PREPARE_HELPER_PATH.read_text(encoding="utf-8-sig")
    assert "checkpoint_database(DEVELOPMENT_DATABASE)" not in prepare_source
    assert "restore_development_database" not in prepare_source
    assert "os.replace(temporary, DEVELOPMENT_DATABASE)" not in prepare_source
    assert prepare_source.count("rollback_last_migration(") == 1
    assert "sqlite_backup_new(development_connection, safety_backup)" in prepare_source
    assert "replay_candidate_database(\n            acceptance_database," in prepare_source
    assert '"migration_target": "generation_scoped_acceptance_clone"' in prepare_source
    assert '"development_source_unchanged": True' in prepare_source
    assert '"pre_reapply_sha256": pre_reapply_sha' in prepare_source
    assert '"pre_reapply_size_bytes": acceptance_initial_record.bytes' in prepare_source
    assert '"safety_backup_sha256": safety_record.sha256' in prepare_source
    # development/acceptance 两份 JSON 都必须锁定 up/down 原始迁移字节；receipt 再将
    # 两个摘要带到 runner，避免 UTF-8/换行归一化让被替换的 down 文件逃过验收。
    assert prepare_source.count(
        '"migration_file_raw_sha256": migration_raw_sha'
    ) == 2
    assert prepare_source.count(
        '"migration_down_file_raw_sha256": migration_down_raw_sha'
    ) == 2
    assert '"source_file": str(DEVELOPMENT_DATABASE)' in prepare_source
    assert '"source_sha256": development_source_sha' in prepare_source
    assert (
        '"method": "sqlite_backup_api_then_isolated_migration_replay"'
        in prepare_source
    )
    # stdout receipt 必须携带实际发布后的证据 JSON 与隔离重放文件摘要/大小，供
    # generation 脚本原子回填 runner/closeout 信任根；不能再按路径重新猜测字节。
    for receipt_contract in (
        "development_evidence_record = write_json_new(",
        "acceptance_evidence_record = write_json_new(",
        '"development_evidence_sha256": development_evidence_record.sha256',
        '"development_evidence_size_bytes": development_evidence_record.bytes',
        '"isolated_replay_post_sha256": post_reapply_sha',
        '"isolated_replay_post_size_bytes": len(post_bytes)',
        '"acceptance_database_size_bytes": acceptance_record.bytes',
        '"acceptance_evidence_sha256": acceptance_evidence_record.sha256',
        '"acceptance_evidence_size_bytes": acceptance_evidence_record.bytes',
        '"migration_raw_sha256": migration_raw_sha',
        '"migration_down_raw_sha256": migration_down_raw_sha',
    ):
        assert receipt_contract in prepare_source

    isolation_workspace = tmp_path / "prepare-isolation"
    isolation_temp = isolation_workspace / ".tmp"
    isolation_data = isolation_workspace / ".data"
    isolation_migrations = isolation_workspace / "migrations"
    isolation_temp.mkdir(parents=True)
    isolation_data.mkdir()
    isolation_migrations.mkdir()
    development_database = isolation_data / "development.db"
    candidate_database = isolation_temp / "candidate.db"
    development_database.write_bytes(b"development-must-remain-byte-identical")
    candidate_database.write_bytes(b"isolated-candidate")
    development_before = development_database.read_bytes()
    migration_targets: list[str] = []
    apply_call_count = 0
    checkpoint_targets: list[Path] = []

    def _fake_rollback(database_url: str, *, migrations_dir: Path) -> Any:
        """记录 down 目标并返回恰好回退 0040 的最小结果。"""

        assert migrations_dir == isolation_migrations
        migration_targets.append(database_url)
        return SimpleNamespace(rolled_back="0040")

    def _fake_apply(database_url: str, *, migrations_dir: Path) -> Any:
        """第一次模拟重放 0040，后两次模拟四十条迁移全部幂等跳过。"""

        nonlocal apply_call_count
        assert migrations_dir == isolation_migrations
        migration_targets.append(database_url)
        apply_call_count += 1
        if apply_call_count == 1:
            return SimpleNamespace(applied=["0040"], skipped=["old"] * 39, rolled_back=None)
        return SimpleNamespace(applied=[], skipped=["all"] * 40, rolled_back=None)

    def _fake_checkpoint(database: Path) -> None:
        """证明 checkpoint 同样只作用于候选路径，不触碰共享开发库。"""

        checkpoint_targets.append(database)

    with monkeypatch.context() as patch_context:
        patch_context.setattr(PREPARE_HELPER, "WORKSPACE", isolation_workspace)
        patch_context.setattr(
            PREPARE_HELPER,
            "DEVELOPMENT_DATABASE",
            development_database,
        )
        patch_context.setattr(PREPARE_HELPER, "MIGRATIONS", isolation_migrations)
        patch_context.setattr(PREPARE_HELPER, "rollback_last_migration", _fake_rollback)
        patch_context.setattr(PREPARE_HELPER, "apply_migrations", _fake_apply)
        patch_context.setattr(PREPARE_HELPER, "asdict", lambda value: vars(value).copy())
        patch_context.setattr(PREPARE_HELPER, "checkpoint_database", _fake_checkpoint)
        patch_context.setattr(
            PREPARE_HELPER,
            "database_facts",
            lambda database, checksum: {
                "database_path": database.name,
                "loader_checksum": checksum,
            },
        )
        replay_facts, replay_bytes = PREPARE_HELPER.replay_candidate_database(
            candidate_database,
            "frozen-loader-checksum",
        )
        with pytest.raises(PREPARE_HELPER.PrepareRejected) as caught_live_target:
            PREPARE_HELPER.replay_candidate_database(
                development_database,
                "frozen-loader-checksum",
            )
    expected_candidate_url = f"sqlite:///{candidate_database.as_posix()}"
    assert migration_targets == [expected_candidate_url] * 4
    assert checkpoint_targets == [candidate_database]
    assert replay_facts == {
        "rolled_back": "0040",
        "reapplied": ["0040"],
        "idempotent_reruns": 2,
        "database_path": "candidate.db",
        "loader_checksum": "frozen-loader-checksum",
    }
    assert replay_bytes == b"isolated-candidate"
    assert development_database.read_bytes() == development_before
    assert str(caught_live_target.value) == "prd050_prepare_replay_target_invalid"

    # 伪造第二次 fstat 身份漂移，证明 assembler 的来源 before/after 复核不是只比内存数据。
    drift_path = tmp_path / "assembler-source-drift.bin"
    drift_path.write_bytes(b"stable-source")
    with ASSEMBLER_HELPER.open_read_lease(
        drift_path,
        "prd050_assembler_source_drift_probe",
    ) as drift_lease:
        original_fstat = ASSEMBLER_HELPER.os.fstat
        drift_calls = 0

        def _assembler_drifting_fstat(file_descriptor: int) -> Any:
            """仅在第二次句柄复核中改变 inode，模拟校验后来源被替换。"""

            nonlocal drift_calls
            current = original_fstat(file_descriptor)
            drift_calls += 1
            if drift_calls != 2:
                return current
            return SimpleNamespace(
                st_dev=current.st_dev,
                st_ino=current.st_ino + 1,
                st_size=current.st_size,
                st_mtime_ns=current.st_mtime_ns,
            )

        with monkeypatch.context() as patch_context:
            patch_context.setattr(
                ASSEMBLER_HELPER.os,
                "fstat",
                _assembler_drifting_fstat,
            )
            with pytest.raises(ASSEMBLER_HELPER.AssemblerRejected) as caught_drift:
                ASSEMBLER_HELPER.read_lease_stable(
                    drift_lease,
                    1024,
                    "prd050_assembler_source_drift_probe",
                )
        assert str(caught_drift.value) == "prd050_assembler_source_drift_probe"

    # 无需创建真实 junction：把叶子 lstat 属性注入为 ReparsePoint，锁定逐组件拒绝分支。
    reparse_leaf = tmp_path / "assembler-reparse-probe"
    reparse_leaf.mkdir()
    original_lstat = ASSEMBLER_HELPER.os.lstat

    def _reparse_lstat(path: os.PathLike[str] | str) -> Any:
        """只为指定测试叶子增加 Win32 ReparsePoint 属性，其余组件返回真实 lstat。"""

        current = original_lstat(path)
        if ASSEMBLER_HELPER._same_path(Path(path), reparse_leaf):
            return SimpleNamespace(
                st_mode=current.st_mode,
                st_file_attributes=ASSEMBLER_HELPER.REPARSE_POINT_ATTRIBUTE,
            )
        return current

    with monkeypatch.context() as patch_context:
        patch_context.setattr(ASSEMBLER_HELPER.os, "lstat", _reparse_lstat)
        with pytest.raises(ASSEMBLER_HELPER.AssemblerRejected) as caught_reparse:
            ASSEMBLER_HELPER.assert_no_reparse_components(
                reparse_leaf,
                "prd050_assembler_reparse_probe",
            )
    assert str(caught_reparse.value) == "prd050_assembler_reparse_probe"

    # verifier 必须用参数数组且 shell=False；一行 CRLF 成功输出在内存中规范化为 LF。
    verifier_calls: dict[str, Any] = {}

    def _fake_verifier_run(arguments: list[str], **kwargs: Any) -> SimpleNamespace:
        """记录 verifier 调用参数并返回不含 stderr 的单行成功 receipt。"""

        verifier_calls["arguments"] = arguments
        verifier_calls["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stderr=b"",
            stdout=(
                b'{"status":"passed","evidence_kind":'
                b'"prd050_static_evidence_verification"}\r\n'
            ),
        )

    with monkeypatch.context() as patch_context:
        patch_context.setenv("METAHUB_ADMIN_PASSWORD", secret)
        patch_context.setattr(
            ASSEMBLER_HELPER.subprocess,
            "run",
            _fake_verifier_run,
        )
        normalized_receipt = ASSEMBLER_HELPER.run_frozen_verifier(
            tmp_path,
            tmp_path / "candidate",
            "20260723T120002",
        )
    assert normalized_receipt.endswith(b"\n") and b"\r" not in normalized_receipt
    assert verifier_calls["kwargs"]["shell"] is False
    assert isinstance(verifier_calls["arguments"], list)
    assert "METAHUB_ADMIN_PASSWORD" not in verifier_calls["kwargs"]["env"]
    assert secret not in repr(verifier_calls)


def _run_powershell_function_probe(
    tmp_path: Path,
    source_path: Path,
    function_names: tuple[str, ...],
    probe_source: str,
    environment: dict[str, str],
    *,
    standard_input: str | None = None,
    timeout_seconds: float = 60.0,
    inherit_raw_environment: bool = False,
) -> subprocess.CompletedProcess[str]:
    """用 PowerShell AST 提取指定生产函数并在隔离目录执行快速行为探针。

    只 dot-source 明确列出的函数定义，避免执行 static runner 或 generator 的全量主流程；测试
    参数通过环境变量传入且只包含临时路径和枚举值，不包含管理员口令或业务敏感信息；
    PATH 状态专项让子 PowerShell 原样继承 Windows 环境块，并在 harness 内设置这些非
    敏感参数；它不假定变体数量，也避免 Python 重建 env 改变实际键名或值。外层超时确保
    被测 PowerShell
    自身的 deadline 回归时，测试进程也不会无限等待。
    """

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    assert powershell is not None, "PowerShell is required for PRD-050 script contracts"
    quoted_names = ", ".join("'" + name.replace("'", "''") + "'" for name in function_names)
    assert all(
        "\x00" not in key and "\x00" not in value
        for key, value in environment.items()
    )
    harness_environment_assignments = (
        tuple(
            "[Environment]::SetEnvironmentVariable("
            + "'"
            + key.replace("'", "''")
            + "', '"
            + value.replace("'", "''")
            + "')"
            for key, value in environment.items()
        )
        if inherit_raw_environment
        else ()
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    harness = tmp_path / "function-probe.ps1"
    harness.write_text(
        "\n".join(
            (
                "param([Parameter(Mandatory = $true)][string]$SourcePath)",
                "Set-StrictMode -Version Latest",
                "$ErrorActionPreference = 'Stop'",
                *harness_environment_assignments,
                "$Tokens = $null",
                "$ParseErrors = $null",
                "$Ast = [Management.Automation.Language.Parser]::ParseFile(",
                "    $SourcePath, [ref]$Tokens, [ref]$ParseErrors",
                ")",
                "if (@($ParseErrors).Count -ne 0) { throw 'prd050_probe_source_parse_failed' }",
                f"$RequiredNames = @({quoted_names})",
                "$Definitions = @($Ast.FindAll(",
                "    { param($Node)",
                "        $Node -is [Management.Automation.Language.FunctionDefinitionAst]",
                "    },",
                "    $true",
                "))",
                "$Selected = foreach ($Name in $RequiredNames) {",
                "    $Matches = @($Definitions | Where-Object { $_.Name -ceq $Name })",
                "    if ($Matches.Count -ne 1) { throw 'prd050_probe_function_set_invalid' }",
                "    $Matches[0]",
                "}",
                "$FunctionText = ($Selected | Sort-Object { $_.Extent.StartOffset } |",
                "    ForEach-Object { $_.Extent.Text }) -join [Environment]::NewLine",
                ". ([scriptblock]::Create($FunctionText))",
                probe_source,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    child_environment = None
    if not inherit_raw_environment:
        child_environment = os.environ.copy()
        child_environment.update(environment)
    return subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            "-SourcePath",
            str(source_path),
        ],
        check=False,
        capture_output=True,
        input=standard_input,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_environment,
        timeout=timeout_seconds,
    )


def _extract_powershell_single_quoted_here_string(
    source_path: Path,
    variable_name: str,
) -> str:
    """提取指定 PowerShell 变量唯一的单引号 here-string 原始正文。

    迁移重放探针必须执行生产 closeout 内嵌的真实 Python，而不能在测试中复制一份容易
    漂移的替身。匹配同时冻结变量名、行首赋值和 here-string 边界；缺失或重复都立即
    失败，避免测试悄悄选中错误代码块。
    """

    source = source_path.read_text(encoding="utf-8-sig")
    pattern = re.compile(
        rf"(?ms)^\${re.escape(variable_name)}\s*=\s*@'\r?\n"
        rf"(?P<body>.*?)\r?\n'@\r?$"
    )
    matches = list(pattern.finditer(source))
    assert len(matches) == 1, f"{variable_name} here-string must be unique"
    return matches[0].group("body") + "\n"


# Launcher provenance 探针持有真实 FileShare.Read 租约，并在释放前后尝试写入、删除和
# sidecar 注入；只输出稳定状态，避免 Windows IOException 把临时绝对路径带入测试日志。
_LAUNCHER_PROVENANCE_LEASE_PROBE = r"""
$ProbeRoot = $env:PRD050_PROBE_ROOT
New-Item -ItemType Directory -Path $ProbeRoot -Force | Out-Null
$DevelopmentPath = Join-Path $ProbeRoot 'development.db'
$SafetyPath = Join-Path $ProbeRoot 'safety.db'
[IO.File]::WriteAllText($DevelopmentPath, 'development', [Text.Encoding]::UTF8)
[IO.File]::WriteAllText($SafetyPath, 'safety', [Text.Encoding]::UTF8)
$script:LauncherLeases = New-Object System.Collections.ArrayList
$script:LauncherLeaseByPath = @{}
$ExpectedDevelopmentSourcePath = $DevelopmentPath
$ExpectedSafetyBackupPath = $SafetyPath
$WriteBlocked = $false
$DeleteBlocked = $false
$Stable = $false
$SidecarCode = ''
$MissingPathCode = ''
$ReleasedWriteSucceeded = $false
try {
    $DevelopmentLease = Add-LauncherFrozenLease $DevelopmentPath
    $SafetyLease = Add-LauncherFrozenLease $SafetyPath
    $DuplicateLease = Add-LauncherFrozenLease (
        $DevelopmentPath
    ) $DevelopmentLease.Length $DevelopmentLease.Sha256
    try {
        $Writer = [IO.File]::Open(
            $DevelopmentPath,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Write,
            [IO.FileShare]::ReadWrite
        )
        $Writer.Dispose()
    }
    catch [IO.IOException] {
        $WriteBlocked = $true
    }
    try {
        [IO.File]::Delete($SafetyPath)
    }
    catch [IO.IOException] {
        $DeleteBlocked = $true
    }
    Assert-AllLauncherLeasesUnchanged
    $Stable = $true
    $SidecarPath = $DevelopmentPath + '-wal'
    [IO.File]::WriteAllText($SidecarPath, 'sidecar', [Text.Encoding]::UTF8)
    try {
        Assert-LauncherProvenanceSidecarsAbsent
    }
    catch {
        $SidecarCode = [string]$_.Exception.Message
    }
    finally {
        Remove-Item -LiteralPath $SidecarPath -Force -ErrorAction SilentlyContinue
    }
    try {
        Assert-LauncherNoReparsePoint (Join-Path $ProbeRoot 'missing.db')
    }
    catch {
        $MissingPathCode = [string]$_.Exception.Message
    }
}
finally {
    for (
        $Index = $script:LauncherLeases.Count - 1;
        $Index -ge 0;
        $Index -= 1
    ) {
        $script:LauncherLeases[$Index].Stream.Dispose()
    }
    $script:LauncherLeaseByPath.Clear()
    $script:LauncherLeases.Clear()
}
try {
    $ReleasedWriter = [IO.File]::Open(
        $DevelopmentPath,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    $ReleasedWriter.Dispose()
    $ReleasedWriteSucceeded = $true
}
catch {
    $ReleasedWriteSucceeded = $false
}
[pscustomobject][ordered]@{
    write_blocked = $WriteBlocked
    delete_blocked = $DeleteBlocked
    stable_while_leased = $Stable
    duplicate_lease_reused = [object]::ReferenceEquals(
        $DevelopmentLease,
        $DuplicateLease
    )
    sidecar_code = $SidecarCode
    missing_path_code = $MissingPathCode
    lease_count_after_cleanup = $script:LauncherLeases.Count
    released_write_succeeded = $ReleasedWriteSucceeded
} | ConvertTo-Json -Compress
""".strip()


def test_launcher_holds_provenance_leases_across_acceptance(tmp_path: Path) -> None:
    """验证 launcher 长租约阻止外部改写，并把路径和 sidecar 异常折叠为固定码。"""

    completed = _run_powershell_function_probe(
        tmp_path / "launcher-provenance-lease",
        LAUNCHER_PATH,
        (
            "Assert-LauncherCondition",
            "Assert-LauncherNoReparsePoint",
            "Get-LauncherLeaseDigest",
            "Add-LauncherFrozenLease",
            "Assert-LauncherProvenanceSidecarsAbsent",
            "Assert-AllLauncherLeasesUnchanged",
        ),
        _LAUNCHER_PROVENANCE_LEASE_PROBE,
        {"PRD050_PROBE_ROOT": str(tmp_path / "launcher-provenance-lease" / "data")},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    observed = json.loads(completed.stdout)
    assert observed == {
        "write_blocked": True,
        "delete_blocked": True,
        "stable_while_leased": True,
        "duplicate_lease_reused": True,
        "sidecar_code": "prd050_launcher_provenance_sidecar_present",
        "missing_path_code": "prd050_launcher_reparse_point_check_failed",
        "lease_count_after_cleanup": 0,
        "released_write_succeeded": True,
    }


# 两组原生 helper 探针必须比较调用前后的真实 PATH 环境，而不能假定宿主一定同时提供
# Path 和 PATH。Ordinal 字典保留每个键的实际大小写；完整值只在子 PowerShell
# 内存中逐项比较，JSON 只返回计数和布尔结论，避免把宿主路径写入测试日志。
_PATH_ENVIRONMENT_SNAPSHOT_HELPERS = r"""
function Get-PathEnvironmentSnapshot {
    $Snapshot = [Collections.Generic.Dictionary[string,string]]::new(
        [StringComparer]::Ordinal
    )
    foreach ($Entry in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
        $Name = [string]$Entry.Key
        if (-not [string]::Equals(
            $Name,
            'Path',
            [StringComparison]::OrdinalIgnoreCase
        )) {
            continue
        }
        if ($Snapshot.ContainsKey($Name)) {
            throw 'prd050_probe_path_variant_duplicate'
        }
        $Snapshot.Add($Name, [string]$Entry.Value)
    }
    return ,$Snapshot
}

function Test-PathEnvironmentSnapshot {
    param(
        [Parameter(Mandatory = $true)]$Before,
        [Parameter(Mandatory = $true)]$After
    )

    if ($Before.Count -ne $After.Count) {
        return $false
    }
    foreach ($Name in $Before.Keys) {
        if (
            -not $After.ContainsKey($Name) -or
            -not [string]::Equals(
                [string]$Before[$Name],
                [string]$After[$Name],
                [StringComparison]::Ordinal
            )
        ) {
            return $false
        }
    }
    return $true
}
""".strip()


# Runner 静态 verifier 探针构造最小但完整的 manifest/hash 信任链，让生产函数经
# 有界 ProcessStartInfo helper 真正执行 Python；失败脚本故意写 stderr 并退出 7。
_RUNNER_STATIC_VERIFIER_NATIVE_PROBE = (
    _PATH_ENVIRONMENT_SNAPSHOT_HELPERS
    + "\n"
    + r"""
$ProbeRoot = $env:PRD050_PROBE_ROOT
$ProbeCase = $env:PRD050_PROBE_CASE
$Generation = '20260723T120007'
$Workspace = Join-Path $ProbeRoot 'workspace'
$Batch = Join-Path $Workspace (Join-Path 'artifacts\prd050' $Generation)
$WorkspaceVerifierDirectory = Join-Path $Workspace 'scripts\acceptance'
New-Item -ItemType Directory -Path $Batch -Force | Out-Null
New-Item -ItemType Directory -Path $WorkspaceVerifierDirectory -Force | Out-Null
$script:WorkspaceRoot = (Resolve-Path -LiteralPath $Workspace).Path
$script:ResolvedBatchDir = (Resolve-Path -LiteralPath $Batch).Path
$script:Generation = $Generation
$script:Python = $env:PRD050_PROBE_PYTHON
$script:Utf8NoBom = New-Object Text.UTF8Encoding($false)
# 故意提供会写 BOM 的宿主编码，验证生产 helper 在 Start 窗口自行去 BOM 后原样恢复。
[Console]::InputEncoding = New-Object Text.UTF8Encoding($true, $true)
$CandidateVerifierPath = Join-Path $Batch 'prd050-verify-static-evidence.py'
$WorkspaceVerifierPath = Join-Path (
    $WorkspaceVerifierDirectory
) 'prd050-verify-static-evidence.py'
if ($ProbeCase -ceq 'success-defined') {
    $VerifierCode = @'
from pathlib import Path
import sys

# 完整 verifier 同样必须从重定向 stdin 立即读到 EOF，宿主编码不得注入 BOM。
if sys.stdin.buffer.read(1) != b"":
    raise SystemExit(9)
batch = Path(sys.argv[sys.argv.index("--batch") + 1])
print((batch / "static-evidence-verification.json").read_text(encoding="utf-8").strip())
'@
}
else {
    $VerifierCode = @'
import sys

sys.stderr.write("PRD050_RUNNER_NATIVE_STDERR_SENTINEL\n")
raise SystemExit(7)
'@
}
[IO.File]::WriteAllText(
    $CandidateVerifierPath,
    $VerifierCode + [Environment]::NewLine,
    $script:Utf8NoBom
)
[IO.File]::WriteAllText(
    $WorkspaceVerifierPath,
    $VerifierCode + [Environment]::NewLine,
    $script:Utf8NoBom
)
$VerifierItem = Get-Item -LiteralPath $CandidateVerifierPath -Force
$VerifierSha256 = (
    Get-FileHash -Algorithm SHA256 -LiteralPath $CandidateVerifierPath
).Hash.ToLowerInvariant()
$SourceManifestPath = Join-Path $Batch 'source-manifest.json'
$SourceManifest = [ordered]@{
    files = @(
        [ordered]@{
            path = 'scripts/acceptance/prd050-verify-static-evidence.py'
            bytes = [int64]$VerifierItem.Length
            sha256 = $VerifierSha256
        }
    )
}
[IO.File]::WriteAllText(
    $SourceManifestPath,
    (($SourceManifest | ConvertTo-Json -Depth 10 -Compress) + [Environment]::NewLine),
    $script:Utf8NoBom
)
$StaticManifestPath = Join-Path $Batch 'static-evidence-manifest.json'
[IO.File]::WriteAllText(
    $StaticManifestPath,
    "static-manifest" + [Environment]::NewLine,
    $script:Utf8NoBom
)
$Baseline = [ordered]@{
    status = 'passed'
    evidence_kind = 'prd050_static_evidence_verification'
    command_count = 11
    junit_files = 4
    junit_tests = 977
    snapshot_count = 12
    source_file_count = 69
    source_manifest_sha256 = (
        Get-FileHash -Algorithm SHA256 -LiteralPath $SourceManifestPath
    ).Hash.ToLowerInvariant()
    static_manifest_sha256 = (
        Get-FileHash -Algorithm SHA256 -LiteralPath $StaticManifestPath
    ).Hash.ToLowerInvariant()
    files_fingerprint_sha256 = "0".PadLeft(64, [char]48)
}
$BaselinePath = Join-Path $Batch 'static-evidence-verification.json'
[IO.File]::WriteAllText(
    $BaselinePath,
    (($Baseline | ConvertTo-Json -Compress) + [Environment]::NewLine),
    $script:Utf8NoBom
)
$PathEnvironmentBefore = Get-PathEnvironmentSnapshot
$InitiallyDefined = $ProbeCase -cne 'failure-undefined'
if ($InitiallyDefined) {
    $global:LASTEXITCODE = 417
}
else {
    Remove-Variable -Name LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue
}
$ErrorActionPreference = 'Stop'
$ErrorCode = ''
try {
    Invoke-StaticEvidenceVerification 'probe-output.json'
}
catch {
    $ErrorCode = [string]$_.Exception.Message
}
$RestoredDefined = Test-Path -LiteralPath Variable:global:LASTEXITCODE
$RestoredValue = if ($RestoredDefined) {
    [int]$global:LASTEXITCODE
}
else {
    0
}
$PathEnvironmentAfter = Get-PathEnvironmentSnapshot
$PathEnvironmentPreserved = Test-PathEnvironmentSnapshot (
    $PathEnvironmentBefore
) $PathEnvironmentAfter
[pscustomobject][ordered]@{
    case = $ProbeCase
    error_code = $ErrorCode
    error_action_preference = [string]$ErrorActionPreference
    last_exitcode_defined_after = $RestoredDefined
    last_exitcode_value_after = $RestoredValue
    path_variant_count_before = [int]$PathEnvironmentBefore.Count
    path_variant_count_after = [int]$PathEnvironmentAfter.Count
    path_variants_preserved = [bool]$PathEnvironmentPreserved
    output_exists = Test-Path -LiteralPath (Join-Path $Batch 'probe-output.json')
    console_input_encoding_restored = (
        [BitConverter]::ToString([Console]::InputEncoding.GetPreamble()) -ceq 'EF-BB-BF'
    )
} | ConvertTo-Json -Compress
""".strip()
)


def test_runner_static_verifier_uses_bounded_capture_without_global_state_changes(
    tmp_path: Path,
) -> None:
    """验证 runner 有界捕获双流，且不改写 EAP、LASTEXITCODE 或 Path/PATH。"""

    cases = (
        ("success-defined", "", True, 417, True),
        (
            "failure-defined",
            "prd050_runner_static_verifier_failed",
            True,
            417,
            False,
        ),
        (
            "failure-undefined",
            "prd050_runner_static_verifier_failed",
            False,
            0,
            False,
        ),
    )
    for (
        case_name,
        expected_error,
        expected_defined,
        expected_value,
        output_exists,
    ) in cases:
        case_root = tmp_path / f"runner-native-{case_name}"
        completed = _run_powershell_function_probe(
            case_root,
            RUNTIME_RUNNER_PATH,
            (
                "Assert-Condition",
                "Assert-NoReparsePoint",
                "Assert-InBatch",
                "Write-Utf8NoBom",
                "ConvertTo-WindowsCommandLineArgument",
                "Invoke-BoundedRunnerStaticVerifier",
                "Invoke-StaticEvidenceVerification",
            ),
            _RUNNER_STATIC_VERIFIER_NATIVE_PROBE,
            {
                "PRD050_PROBE_ROOT": str(case_root / "fixture"),
                "PRD050_PROBE_CASE": case_name,
                "PRD050_PROBE_PYTHON": str(Path(sys.executable).resolve()),
            },
            inherit_raw_environment=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stderr == ""
        assert "PRD050_RUNNER_NATIVE_STDERR_SENTINEL" not in completed.stdout
        assert "PRD050_RUNNER_NATIVE_STDERR_SENTINEL" not in completed.stderr
        assert str(case_root) not in completed.stdout
        assert str(case_root) not in completed.stderr
        observed = json.loads(completed.stdout)
        path_variant_count_before = observed.pop("path_variant_count_before")
        assert path_variant_count_before >= 1
        assert observed.pop("path_variant_count_after") == path_variant_count_before
        assert observed.pop("path_variants_preserved") is True
        assert observed == {
            "case": case_name,
            "error_code": expected_error,
            "error_action_preference": "Stop",
            "last_exitcode_defined_after": expected_defined,
            "last_exitcode_value_after": expected_value,
            "output_exists": output_exists,
            "console_input_encoding_restored": True,
        }


# Runner 与 launcher 的两个有界 helper 使用同一行为探针。挂起脚本先把 PID 写入 batch，
# helper 返回后再查进程表，从而直接证明 timeout 不只是返回固定码，还完成了 kill/观察。
_BOUNDED_STATIC_VERIFIER_HELPER_PROBE = (
    _PATH_ENVIRONMENT_SNAPSHOT_HELPERS
    + "\n"
    + r"""
$ProbeRoot = $env:PRD050_PROBE_ROOT
$ProbeCase = $env:PRD050_PROBE_CASE
$ProbeKind = $env:PRD050_PROBE_KIND
$Workspace = Join-Path $ProbeRoot 'workspace with space'
$Batch = Join-Path $Workspace 'batch'
New-Item -ItemType Directory -Path $Batch -Force | Out-Null
$VerifierPath = Join-Path $Batch 'bounded verifier.py'
$PidPath = Join-Path $Batch 'child.pid'
$Utf8NoBom = New-Object Text.UTF8Encoding($false)
# 两个 bounded helper 都必须抵抗宿主 BOM，且调用结束后不能永久改变控制台编码。
[Console]::InputEncoding = New-Object Text.UTF8Encoding($true, $true)
foreach ($SecretName in @(
    'METAHUB_PRD050_ADMIN_PASSWORD',
    'METAHUB_ADMIN_PASSWORD',
    'METAHUB_PRD050_PASSWORD_VERIFY'
)) {
    Remove-Item -LiteralPath ("Env:" + $SecretName) -ErrorAction SilentlyContinue
}
switch ($ProbeCase) {
    'success' {
        $VerifierCode = @'
import os
import sys

secret_names = (
    "METAHUB_PRD050_ADMIN_PASSWORD",
    "METAHUB_ADMIN_PASSWORD",
    "METAHUB_PRD050_PASSWORD_VERIFY",
)
# 有界 helper 必须关闭重定向 stdin，并继承入口已经净化的环境。
if sys.stdin.buffer.read(1) != b"" or any(name in os.environ for name in secret_names):
    raise SystemExit(9)
print("prd050-bounded-helper-ok")
'@
    }
    'stderr-nonzero' {
        $VerifierCode = @'
import sys

sys.stdout.write("PRD050_BOUNDED_STDOUT_SENTINEL\n")
sys.stderr.write("PRD050_BOUNDED_STDERR_SENTINEL\n")
raise SystemExit(7)
'@
    }
    'timeout' {
        $VerifierCode = @'
import os
import sys
import time
from pathlib import Path

batch = Path(sys.argv[sys.argv.index("--batch") + 1])
pid_path = batch / "child.pid"
pid_path.write_text(str(os.getpid()), encoding="ascii")
time.sleep(60)
'@
    }
    'startup-failure' {
        $VerifierCode = 'raise SystemExit(0)'
    }
    default {
        throw 'prd050_bounded_helper_probe_case_invalid'
    }
}
[IO.File]::WriteAllText(
    $VerifierPath,
    $VerifierCode + [Environment]::NewLine,
    $Utf8NoBom
)
$PythonPath = $env:PRD050_PROBE_PYTHON
if ($ProbeCase -ceq 'startup-failure') {
    $PythonPath = Join-Path $ProbeRoot 'missing-python.exe'
}
if ($ProbeKind -ceq 'runner') {
    $script:Python = $PythonPath
}
# 2 秒只为 WinPS5 高负载下的 Python 冷启动和 PID 落盘保留握手裕量；生产默认
# 30 秒 deadline 不变，且休眠 60 秒的探针仍必然进入 timeout/kill 分支。
$TimeoutMilliseconds = if ($ProbeCase -ceq 'timeout') { 2000 } else { 5000 }
$PathEnvironmentBefore = Get-PathEnvironmentSnapshot
$Invocation = $null
$ErrorCode = ''
$Stopwatch = [Diagnostics.Stopwatch]::StartNew()
try {
    try {
        if ($ProbeKind -ceq 'runner') {
            $Invocation = Invoke-BoundedRunnerStaticVerifier (
                $VerifierPath
            ) $Workspace $Batch '20260723T120008' $TimeoutMilliseconds
        }
        elseif ($ProbeKind -ceq 'launcher') {
            $Invocation = Invoke-BoundedLauncherStaticVerifier (
                $PythonPath
            ) $VerifierPath $Workspace $Batch '20260723T120008' $TimeoutMilliseconds
        }
        else {
            throw 'prd050_bounded_helper_probe_kind_invalid'
        }
    }
    catch {
        $ErrorCode = [string]$_.Exception.Message
    }
}
finally {
    $Stopwatch.Stop()
}
$ExitCode = -1
$Stdout = ''
$Stderr = ''
if ($null -ne $Invocation) {
    $ExitCode = [int]$Invocation.ExitCode
    $Stdout = [string]$Invocation.Stdout
    $Stderr = [string]$Invocation.Stderr
}
$ExpectedContractCode = if ($ProbeKind -ceq 'runner') {
    'prd050_runner_static_verifier_failed'
}
else {
    'prd050_launcher_static_evidence_invalid'
}
$ContractErrorCode = $ErrorCode
$NormalizedStdout = $Stdout.Replace(
    ([string][char]13 + [char]10),
    [string][char]10
)
if (
    [string]::IsNullOrEmpty($ContractErrorCode) -and
    (
        $ExitCode -ne 0 -or
        $Stderr -cne '' -or
        $NormalizedStdout -cne ('prd050-bounded-helper-ok' + [char]10)
    )
) {
    $ContractErrorCode = $ExpectedContractCode
}
$ChildPidRecorded = Test-Path -LiteralPath $PidPath -PathType Leaf
$ChildProcessAlive = $false
if ($ChildPidRecorded) {
    try {
        $ChildPid = [int][IO.File]::ReadAllText($PidPath, [Text.Encoding]::ASCII)
        $ChildProcessAlive = $null -ne (
            Get-Process -Id $ChildPid -ErrorAction SilentlyContinue
        )
    }
    catch {
        $ChildProcessAlive = $false
    }
}
$PathEnvironmentAfter = Get-PathEnvironmentSnapshot
$PathEnvironmentPreserved = Test-PathEnvironmentSnapshot (
    $PathEnvironmentBefore
) $PathEnvironmentAfter
[pscustomobject][ordered]@{
    kind = $ProbeKind
    case = $ProbeCase
    error_code = $ErrorCode
    contract_error_code = $ContractErrorCode
    exit_code = $ExitCode
    success_output_valid = (
        $NormalizedStdout -ceq ('prd050-bounded-helper-ok' + [char]10) -and
        $Stderr -ceq ''
    )
    diagnostics_captured = (
        $Stdout.Contains('PRD050_BOUNDED_STDOUT_SENTINEL') -and
        $Stderr.Contains('PRD050_BOUNDED_STDERR_SENTINEL')
    )
    elapsed_milliseconds = [int]$Stopwatch.ElapsedMilliseconds
    child_pid_recorded = $ChildPidRecorded
    child_process_alive = $ChildProcessAlive
    path_variant_count_before = [int]$PathEnvironmentBefore.Count
    path_variant_count_after = [int]$PathEnvironmentAfter.Count
    path_variants_preserved = [bool]$PathEnvironmentPreserved
    console_input_encoding_restored = (
        [BitConverter]::ToString([Console]::InputEncoding.GetPreamble()) -ceq 'EF-BB-BF'
    )
} | ConvertTo-Json -Compress
""".strip()
)


def test_bounded_static_verifier_helpers_kill_and_capture_without_leaks(
    tmp_path: Path,
) -> None:
    """实测两个 verifier helper 的 EOF、双流、超时终止、固定码和 Path/PATH 合同。"""

    cases = (
        ("success", ""),
        ("stderr-nonzero", "contract"),
        ("timeout", "timeout"),
        ("startup-failure", "process_invalid"),
    )
    for script_kind in ("runner", "launcher"):
        source_path = (
            RUNTIME_RUNNER_PATH if script_kind == "runner" else LAUNCHER_PATH
        )
        function_names = (
            (
                "Assert-Condition",
                "ConvertTo-WindowsCommandLineArgument",
                "Invoke-BoundedRunnerStaticVerifier",
            )
            if script_kind == "runner"
            else (
                "Assert-LauncherCondition",
                "ConvertTo-LauncherWindowsArgument",
                "Invoke-BoundedLauncherStaticVerifier",
            )
        )
        source = source_path.read_text(encoding="utf-8-sig")
        # $Info.Environment 前缀同时覆盖 Environment 与 EnvironmentVariables，
        # 防止生产 helper 通过大小写不敏感字典重建并合并宿主 PATH 变体。
        assert "$Info.Environment" not in source
        assert ".ReadToEndAsync()" in source
        for case_name, expected_error_suffix in cases:
            case_root = tmp_path / f"{script_kind}-bounded-{case_name}"
            completed = _run_powershell_function_probe(
                case_root,
                source_path,
                function_names,
                _BOUNDED_STATIC_VERIFIER_HELPER_PROBE,
                {
                    "PRD050_PROBE_ROOT": str(case_root / "fixture"),
                    "PRD050_PROBE_CASE": case_name,
                    "PRD050_PROBE_KIND": script_kind,
                    "PRD050_PROBE_PYTHON": str(Path(sys.executable).resolve()),
                },
                timeout_seconds=15.0,
                inherit_raw_environment=True,
            )
            assert completed.returncode == 0, completed.stderr
            assert completed.stderr == ""
            for forbidden in (
                "PRD050_BOUNDED_STDOUT_SENTINEL",
                "PRD050_BOUNDED_STDERR_SENTINEL",
                str(case_root),
            ):
                assert forbidden not in completed.stdout
                assert forbidden not in completed.stderr
            observed = json.loads(completed.stdout)
            assert observed["kind"] == script_kind
            assert observed["case"] == case_name
            # 单键和双键宿主都合法；合同要求 helper 调用前后实际键名和值逐项不变。
            path_variant_count_before = observed["path_variant_count_before"]
            assert path_variant_count_before >= 1
            assert observed["path_variant_count_after"] == path_variant_count_before
            assert observed["path_variants_preserved"] is True
            assert observed["console_input_encoding_restored"] is True
            assert observed["child_process_alive"] is False
            assert 0 <= observed["elapsed_milliseconds"] < 5_000

            prefix = f"prd050_{script_kind}_static_verifier_"
            if case_name == "success":
                assert observed["error_code"] == ""
                assert observed["contract_error_code"] == ""
                assert observed["exit_code"] == 0
                assert observed["success_output_valid"] is True
            elif case_name == "stderr-nonzero":
                expected_contract = (
                    "prd050_runner_static_verifier_failed"
                    if script_kind == "runner"
                    else "prd050_launcher_static_evidence_invalid"
                )
                assert observed["error_code"] == ""
                assert observed["contract_error_code"] == expected_contract
                assert observed["exit_code"] == 7
                assert observed["diagnostics_captured"] is True
            else:
                assert observed["error_code"] == prefix + expected_error_suffix
                assert observed["contract_error_code"] == prefix + expected_error_suffix
                assert observed["exit_code"] == -1
                if case_name == "timeout":
                    assert observed["child_pid_recorded"] is True
                    assert 1_800 <= observed["elapsed_milliseconds"] < 5_000


# 归档守卫探针构造精确 generation、隐藏项和大小写变体，确保 one-shot 候选不能
# 在已有归档碰撞时被原地复用；使用原始字符串是为保持嵌入 PowerShell 的字面语义。
_ARCHIVE_GUARD_PROBE = r"""
$ArchiveRoot = $env:PRD050_PROBE_ROOT
$Generation = '20260723T120004'
New-Item -ItemType Directory -Path $ArchiveRoot -Force | Out-Null
switch ($env:PRD050_PROBE_CASE) {
    'exact-directory' {
        New-Item -ItemType Directory -Path (Join-Path $ArchiveRoot $Generation) | Out-Null
    }
    'hidden-file' {
        $Entry = Join-Path $ArchiveRoot ($Generation + '-hidden')
        [IO.File]::WriteAllText($Entry, 'sentinel')
        [IO.File]::SetAttributes($Entry, [IO.FileAttributes]::Hidden)
    }
    'regular-file' {
        [IO.File]::WriteAllText(
            (Join-Path $ArchiveRoot ($Generation + '-regular')),
            'sentinel'
        )
    }
    'empty-directory' {
        New-Item -ItemType Directory -Path (
            Join-Path $ArchiveRoot ($Generation + '-empty')
        ) | Out-Null
    }
    'reparse-directory' {
        $Target = Join-Path ([IO.Path]::GetDirectoryName($ArchiveRoot)) 'junction-target'
        New-Item -ItemType Directory -Path $Target -Force | Out-Null
        New-Item -ItemType Junction -Path (
            Join-Path $ArchiveRoot ($Generation + '-junction')
        ) -Target $Target | Out-Null
    }
    'near-prefix' {
        New-Item -ItemType Directory -Path (
            Join-Path $ArchiveRoot ($Generation + '0-reason')
        ) | Out-Null
    }
    'case-variant' {
        New-Item -ItemType Directory -Path (
            Join-Path $ArchiveRoot ($Generation.ToUpperInvariant() + '-reason')
        ) | Out-Null
    }
    'fresh' {}
    default { throw 'prd050_probe_archive_case_invalid' }
}
try {
    Assert-GenerationNotArchived `
        -ArchiveRoot $ArchiveRoot `
        -GenerationValue $Generation
    Write-Output 'passed'
}
catch {
    Write-Output ([string]$_.Exception.Message)
}
""".strip()


@pytest.mark.parametrize(
    ("case_name", "expected_output"),
    (
        ("fresh", "passed"),
        ("near-prefix", "passed"),
        ("case-variant", "prd050_static_runner_generation_previously_failed"),
        ("exact-directory", "prd050_static_runner_generation_previously_failed"),
        ("hidden-file", "prd050_static_runner_generation_previously_failed"),
        ("regular-file", "prd050_static_runner_generation_previously_failed"),
        ("empty-directory", "prd050_static_runner_generation_previously_failed"),
        ("reparse-directory", "prd050_static_runner_generation_previously_failed"),
    ),
)
def test_static_runner_rejects_archived_generation_shapes(
    tmp_path: Path,
    case_name: str,
    expected_output: str,
) -> None:
    """验证失败 generation 的精确前缀、隐藏对象和各类目录都在写证据前被拒绝。"""

    probe_root = tmp_path / case_name / "failed-attempts"
    completed = _run_powershell_function_probe(
        tmp_path / case_name,
        STATIC_RUNNER_PATH,
        (
            "Assert-Condition",
            "Assert-NoReparsePoint",
            "Assert-GenerationNotArchived",
        ),
        _ARCHIVE_GUARD_PROBE,
        {
            "PRD050_PROBE_ROOT": str(probe_root),
            "PRD050_PROBE_CASE": case_name,
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout.strip() == expected_output


# 静态源清单探针通过 PowerShell AST 读取赋值而非宽松文本搜索，防止注释或相似变量
# 伪造 69 个冻结源文件与 12 个快照的闭集合同。
_STATIC_SOURCE_ASSIGNMENT_PROBE = r"""
$Assignments = @($Ast.FindAll(
    {
        param($Node)
        $Node -is [Management.Automation.Language.AssignmentStatementAst]
    },
    $true
))
$SelectedAssignments = @{}
foreach ($VariableName in @('RuffTargets', 'SourceRelativePaths')) {
    $ExpectedLeft = '$' + $VariableName
    $Matches = @($Assignments | Where-Object {
        $_.Left.Extent.Text -ceq $ExpectedLeft
    })
    if ($Matches.Count -ne 1) {
        throw 'prd050_probe_source_assignment_invalid'
    }
    $SelectedAssignments[$VariableName] = $Matches[0]
}
. ([scriptblock]::Create($SelectedAssignments['RuffTargets'].Extent.Text))
. ([scriptblock]::Create($SelectedAssignments['SourceRelativePaths'].Extent.Text))
[pscustomobject][ordered]@{
    ruff_targets = @($RuffTargets)
    source_paths = @($SourceRelativePaths)
} | ConvertTo-Json -Depth 4 -Compress
""".strip()


# 生成器只会 dot-source runner 的冻结函数闭集；此探针用 PowerShell AST 递归展开所有
# 本地函数调用，避免只检查几个已知名字后再次漏掉间接依赖。
_GENERATOR_REQUIRED_FUNCTION_CLOSURE_PROBE = r"""
$GeneratorPath = $env:PRD050_PROBE_GENERATOR
$RunnerPath = $env:PRD050_PROBE_RUNNER
$CloseoutPath = $env:PRD050_PROBE_CLOSEOUT
$GeneratorTokens = $null
$GeneratorErrors = $null
$GeneratorAst = [Management.Automation.Language.Parser]::ParseFile(
    $GeneratorPath,
    [ref]$GeneratorTokens,
    [ref]$GeneratorErrors
)
if (@($GeneratorErrors).Count -ne 0) {
    throw 'prd050_probe_generator_parse_failed'
}
$RequiredAssignments = @($GeneratorAst.FindAll(
    {
        param($Node)
        $Node -is [Management.Automation.Language.AssignmentStatementAst] -and
        $Node.Left.Extent.Text -ceq '$RequiredFunctions'
    },
    $true
))
if ($RequiredAssignments.Count -ne 1) {
    throw 'prd050_probe_required_assignment_invalid'
}
. ([scriptblock]::Create($RequiredAssignments[0].Extent.Text))
$RequiredNames = @($RequiredFunctions | ForEach-Object { [string]$_ })
$RequiredSet = [Collections.Generic.HashSet[string]]::new(
    [StringComparer]::Ordinal
)
foreach ($Name in $RequiredNames) {
    if (-not $RequiredSet.Add($Name)) {
        throw 'prd050_probe_required_function_duplicate'
    }
}

$RunnerTokens = $null
$RunnerErrors = $null
$RunnerAst = [Management.Automation.Language.Parser]::ParseFile(
    $RunnerPath,
    [ref]$RunnerTokens,
    [ref]$RunnerErrors
)
if (@($RunnerErrors).Count -ne 0) {
    throw 'prd050_probe_runner_parse_failed'
}
$RunnerRequiredAssignments = @($RunnerAst.FindAll(
    {
        param($Node)
        $Node -is [Management.Automation.Language.AssignmentStatementAst] -and
        $Node.Left.Extent.Text -ceq '$RequiredExtractedFunctions'
    },
    $true
))
if ($RunnerRequiredAssignments.Count -ne 1) {
    throw 'prd050_probe_runner_required_assignment_invalid'
}
. ([scriptblock]::Create($RunnerRequiredAssignments[0].Extent.Text))
$RunnerRequiredNames = @(
    $RequiredExtractedFunctions | ForEach-Object { [string]$_ }
)

$CloseoutTokens = $null
$CloseoutErrors = $null
$CloseoutAst = [Management.Automation.Language.Parser]::ParseFile(
    $CloseoutPath,
    [ref]$CloseoutTokens,
    [ref]$CloseoutErrors
)
if (@($CloseoutErrors).Count -ne 0) {
    throw 'prd050_probe_closeout_parse_failed'
}
$CloseoutRequiredAssignments = @($CloseoutAst.FindAll(
    {
        param($Node)
        $Node -is [Management.Automation.Language.AssignmentStatementAst] -and
        $Node.Left.Extent.Text -ceq '$RunnerContractRequiredFunctions'
    },
    $true
))
if ($CloseoutRequiredAssignments.Count -ne 1) {
    throw 'prd050_probe_closeout_required_assignment_invalid'
}
. ([scriptblock]::Create($CloseoutRequiredAssignments[0].Extent.Text))
$CloseoutRequiredNames = @(
    $RunnerContractRequiredFunctions | ForEach-Object { [string]$_ }
)

$Definitions = @($RunnerAst.FindAll(
    {
        param($Node)
        $Node -is [Management.Automation.Language.FunctionDefinitionAst]
    },
    $true
))
$DefinitionByName = [Collections.Generic.Dictionary[string, object]]::new(
    [StringComparer]::Ordinal
)
foreach ($Definition in $Definitions) {
    if ($DefinitionByName.ContainsKey([string]$Definition.Name)) {
        throw 'prd050_probe_runner_function_duplicate'
    }
    $DefinitionByName.Add([string]$Definition.Name, $Definition)
}

$Closure = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
$Queue = [Collections.Generic.Queue[string]]::new()
foreach ($Name in $RequiredNames) {
    [void]$Closure.Add($Name)
    $Queue.Enqueue($Name)
}
while ($Queue.Count -gt 0) {
    $Name = $Queue.Dequeue()
    if (-not $DefinitionByName.ContainsKey($Name)) {
        throw 'prd050_probe_required_function_missing'
    }
    $Definition = $DefinitionByName[$Name]
    $Commands = @($Definition.Body.FindAll(
        {
            param($Node)
            $Node -is [Management.Automation.Language.CommandAst]
        },
        $true
    ))
    foreach ($Command in $Commands) {
        $Callee = [string]$Command.GetCommandName()
        if (
            -not [string]::IsNullOrWhiteSpace($Callee) -and
            $DefinitionByName.ContainsKey($Callee) -and
            $Closure.Add($Callee)
        ) {
            $Queue.Enqueue($Callee)
        }
    }
}
$Missing = @(
    $Closure | Where-Object { -not $RequiredSet.Contains([string]$_) } | Sort-Object
)
[pscustomobject][ordered]@{
    required = @($RequiredNames | Sort-Object)
    runner_required = @($RunnerRequiredNames | Sort-Object)
    closeout_required = @($CloseoutRequiredNames | Sort-Object)
    closure = @($Closure | Sort-Object)
    missing = $Missing
} | ConvertTo-Json -Depth 4 -Compress
""".strip()


def test_static_runner_source_contract_matches_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 static runner 的 69 个源码输入与 verifier 独立闭集完全一致。"""

    comment_scope = tmp_path / "artifacts" / "checkout"
    # 注释门禁只从正式源码根进入扫描；测试文件必须位于 ``scripts``，才能同时证明
    # 当前源码被纳入、其内部 artifacts/.artifacts 冻结副本在下钻前被剪枝。
    active_source = comment_scope / "scripts" / "active.py"
    archived_source = comment_scope / "scripts" / "artifacts" / "failed-attempt.py"
    hidden_archived_source = comment_scope / "scripts" / ".artifacts" / "snapshot.py"
    active_source.parent.mkdir(parents=True)
    archived_source.parent.mkdir(parents=True)
    hidden_archived_source.parent.mkdir(parents=True)
    active_source.write_text(
        '"""当前源码用于验证注释检查器只扫描需要维护的项目代码。"""\n',
        encoding="utf-8",
    )
    archived_source.write_text("def archived():\n    return 1\n", encoding="utf-8")
    hidden_archived_source.write_text("def snapshot():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(COMMENT_CHECKER, "ROOT", comment_scope)
    # 失败批次必须保留原始字节，但冻结副本不能被当成当前源码重复执行注释门禁。
    assert COMMENT_CHECKER.iter_code_files() == [active_source]

    completed = _run_powershell_function_probe(
        tmp_path / "static-source-contract",
        STATIC_RUNNER_PATH,
        ("Assert-Condition",),
        _STATIC_SOURCE_ASSIGNMENT_PROBE,
        {},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    observed = json.loads(completed.stdout)
    assert tuple(observed["ruff_targets"]) == VERIFIER.RUFF_TARGETS
    assert frozenset(observed["source_paths"]) == VERIFIER.SOURCE_PATH_CONTRACT
    assert len(observed["source_paths"]) == VERIFIER.EXPECTED_SOURCE_FILE_COUNT == 69
    # PAGE-073 请求日志测试承载租户隔离和 DOM 隐私门禁，必须由独立字面量断言锁入闭集，
    # 防止 verifier 与 static runner 同时遗漏后仍相互比较通过。
    assert "console-web/tests/unit/cacheAffinityRequestLogs.test.mjs" in observed[
        "source_paths"
    ]

    generator_source = GENERATOR_PATH.read_text(encoding="utf-8")
    assert generator_source.count("[int]$StaticReceipt.snapshot_count -eq 12") == 1
    closure_completed = _run_powershell_function_probe(
        tmp_path / "generator-function-closure",
        GENERATOR_PATH,
        ("Assert-GeneratorCondition",),
        _GENERATOR_REQUIRED_FUNCTION_CLOSURE_PROBE,
        {
            "PRD050_PROBE_GENERATOR": str(GENERATOR_PATH),
            "PRD050_PROBE_RUNNER": str(RUNTIME_RUNNER_PATH),
            "PRD050_PROBE_CLOSEOUT": str(CLOSEOUT_PATH),
        },
    )
    assert closure_completed.returncode == 0, closure_completed.stderr
    assert closure_completed.stderr == ""
    closure_observed = json.loads(closure_completed.stdout)
    expected_required_functions = sorted(
        (
            "Add-OwnedProcessIdentity",
            "Assert-Condition",
            "Assert-InBatch",
            "Assert-NoReparsePoint",
            "ConvertTo-CmdToken",
            "Get-ProcessIdentity",
            "Initialize-Prd050NativeProcessHost",
            "Invoke-CapturedCommand",
            "New-CapturedCommand",
            "New-OwnedPendingCapturePath",
            "Open-OwnedPendingCaptureLease",
            "Publish-OwnedPendingCapture",
            "Register-OwnedDescendants",
            "Start-OwnedCmdWrapper",
            "Stop-ExactProcess",
            "Stop-OwnedJobLease",
            "Test-ProcessIdentity",
            "Wait-DirectChildIdentity",
            "Wait-StartedProcessIdentityVisible",
            "Write-JsonAtomic",
            "Write-Utf8NoBom",
        )
    )
    assert closure_observed == {
        "required": expected_required_functions,
        "runner_required": expected_required_functions,
        "closeout_required": expected_required_functions,
        "closure": expected_required_functions,
        "missing": [],
    }
    # cmd 在 Windows 上可能先于 Python 父进程退出。父进程启动两个后代后仍要求 3，
    # 才能跨该差异证明恢复后的 Job 确实包含后代，而不是把门槛降为两个根进程。
    tree_code_block = generator_source.split("$TreeCode =", 1)[1].split(
        "$ObservedCode =", 1
    )[0]
    assert tree_code_block.count("subprocess.Popen(") == 2
    assert "'{0}-child-a'" in tree_code_block
    assert "'{0}-child-b'" in tree_code_block
    assert generator_source.count("-MinimumActiveProcesses 3") == 1


# Runner 字节码探针覆盖大小写无关的 __pycache__ 和 .pyc 前后门禁，确保冻结 Runtime
# 的动态导入不会在候选根产生未纳入 manifest 的目录项。
_RUNNER_BYTECODE_GUARD_PROBE = r"""
$CandidateRoot = $env:PRD050_PROBE_ROOT
New-Item -ItemType Directory -Path $CandidateRoot -Force | Out-Null
$script:ResolvedBatchDir = (Resolve-Path -LiteralPath $CandidateRoot).Path
switch ($env:PRD050_PROBE_CASE) {
    'clean' {}
    'pycache' {
        New-Item -ItemType Directory -Path (
            Join-Path $CandidateRoot '__PYCACHE__'
        ) | Out-Null
    }
    'pyc' {
        [IO.File]::WriteAllBytes(
            (Join-Path $CandidateRoot 'runtime.PYC'),
            [byte[]]@(1, 2, 3)
        )
    }
    default { throw 'prd050_probe_bytecode_case_invalid' }
}
try {
    Assert-NoCandidatePythonBytecodeArtifacts 'bytecode_artifact_rejected'
    Write-Output 'passed'
}
catch {
    Write-Output ([string]$_.Exception.Message)
}
""".strip()


# 匿名 stdin 与捕获 lease 探针在子进程内随机生成 sentinel：它不进入 harness、argv 或环境，
# producer 必须读到一行后依靠 EOF 退出，父句柄则必须在发布前阻止 pending 被删除或替换。
_RUNNER_STDIN_AND_CAPTURE_PROBE = r"""
$ProbeRoot = $env:PRD050_PROBE_ROOT
New-Item -ItemType Directory -Path $ProbeRoot -Force | Out-Null
$script:ResolvedBatchDir = (Resolve-Path -LiteralPath $ProbeRoot).Path
$script:Utf8NoBom = New-Object Text.UTF8Encoding($false)
$script:ComSpec = [IO.Path]::Combine([Environment]::SystemDirectory, 'cmd.exe')
Initialize-Prd050NativeProcessHost
$Sentinel = 'prd050-stdin-' + [Guid]::NewGuid().ToString('N')
$PythonCode = (
    "import hashlib,sys;line=sys.stdin.buffer.readline();extra=sys.stdin.buffer.read();" +
    "payload=line[:-1];valid=line.endswith(b'\n') and not extra;" +
    "print(hashlib.sha256(payload).hexdigest() if valid else 'invalid');" +
    "raise SystemExit(0 if valid else 9)"
)
$Arguments = @('-c', $PythonCode)
$StdoutFinal = Join-Path $ProbeRoot 'probe.stdout.log'
$StderrFinal = Join-Path $ProbeRoot 'probe.stderr.log'
$StdoutPending = New-OwnedPendingCapturePath $StdoutFinal 'stdout'
$StderrPending = New-OwnedPendingCapturePath $StderrFinal 'stderr'
$Command = New-CapturedCommand (
    $env:PRD050_PROBE_PYTHON
) $Arguments $StdoutPending $StderrPending
$EnvironmentContainsSentinel = @(
    Get-ChildItem Env: | Where-Object { [string]$_.Value -ceq $Sentinel }
).Count -ne 0
$OwnedIdentities = New-Object Collections.ArrayList
$OwnedJobs = New-Object Collections.ArrayList
$OwnedCaptures = New-Object Collections.ArrayList
$CommandRoots = New-Object Collections.ArrayList
$Started = $null
$DeleteBlocked = $false
try {
    $Started = Start-OwnedCmdWrapper `
        -Executable $env:PRD050_PROBE_PYTHON `
        -Arguments $Arguments `
        -WorkingDirectory $ProbeRoot `
        -StdoutPath $StdoutPending `
        -StderrPath $StderrPending `
        -OwnedIdentities $OwnedIdentities `
        -OwnedJobLeases $OwnedJobs `
        -CommandRootIdentities $CommandRoots `
        -OwnedCaptureLeases $OwnedCaptures `
        -StandardInputLine $Sentinel
    try {
        Remove-Item -LiteralPath $StdoutPending -Force -ErrorAction Stop
    }
    catch {
        $DeleteBlocked = $true
    }
    if (-not $Started.Lease.WaitForExit(10000)) {
        throw 'prd050_probe_stdin_eof_timeout'
    }
    $ExitCode = [int]$Started.Lease.GetExitCode()
    Publish-OwnedPendingCapture $Started.StdoutCaptureLease $StdoutFinal
    Publish-OwnedPendingCapture $Started.StderrCaptureLease $StderrFinal
    $Stdout = [IO.File]::ReadAllText($StdoutFinal, [Text.Encoding]::UTF8)
    $Stderr = [IO.File]::ReadAllText($StderrFinal, [Text.Encoding]::UTF8)
    $Bytes = [Text.Encoding]::UTF8.GetBytes($Sentinel)
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $ExpectedDigest = [BitConverter]::ToString(
            $Hasher.ComputeHash($Bytes)
        ).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $Hasher.Dispose()
        [Array]::Clear($Bytes, 0, $Bytes.Length)
    }
    [pscustomobject][ordered]@{
        exit_code = $ExitCode
        delete_blocked = $DeleteBlocked
        command_clean = $Command.IndexOf($Sentinel, [StringComparison]::Ordinal) -lt 0
        environment_clean = -not $EnvironmentContainsSentinel
        stdout_matches_digest = $Stdout.Trim() -ceq $ExpectedDigest
        stdout_secret_free = $Stdout.IndexOf($Sentinel, [StringComparison]::Ordinal) -lt 0
        stderr_secret_free = $Stderr.IndexOf($Sentinel, [StringComparison]::Ordinal) -lt 0
        stderr_empty = $Stderr.Length -eq 0
        pending_removed = (
            -not (Test-Path -LiteralPath $StdoutPending) -and
            -not (Test-Path -LiteralPath $StderrPending)
        )
    } | ConvertTo-Json -Compress
}
finally {
    if ($null -ne $Started -and -not [bool]$Started.Lease.IsDisposed) {
        $Started.Lease.Dispose()
    }
    foreach ($CaptureLease in $OwnedCaptures) {
        if ($null -ne $CaptureLease.Stream) {
            $CaptureLease.Stream.Dispose()
            $CaptureLease.Stream = $null
        }
    }
    $Sentinel = $null
}
""".strip()


# 数据库绑定探针从外层测试 stdin 接收随机密码，再调用生产函数；这样密码既不进入测试
# harness、命令参数和环境，也能真实覆盖 ProcessStartInfo 的 stdin/stdout/stderr 合同。
_RUNNER_PASSWORD_HASH_PROBE = r"""
$script:Python = $env:PRD050_PROBE_PYTHON
[Console]::InputEncoding = New-Object Text.UTF8Encoding($true, $true)
$ProbeCase = $env:PRD050_PROBE_CASE
if ([string]::IsNullOrEmpty($ProbeCase)) {
    $ProbeCase = 'success'
}
$Password = [Console]::In.ReadLine()
# 正常及最大长度口令需要执行真实 PBKDF2；高负载机器允许使用生产默认值一半的
# 十五秒预算。专门的 timeout 向量会在下方覆盖为 200ms，不放宽故障门禁。
$TimeoutMilliseconds = 15000
switch ($ProbeCase) {
    'success' {}
    'empty' {
        $Password = ''
    }
    'carriage-return' {
        $Password += [char]13
    }
    'line-feed' {
        $Password += [char]10
    }
    'carriage-line-feed' {
        $Password += [string][char]13 + [string][char]10
    }
    'isolated-surrogate' {
        $Password = [string][char]0xD800
    }
    'max-ascii' {
        $Password = ''.PadLeft(32767, [char]97)
    }
    'over-ascii' {
        $Password = ''.PadLeft(32768, [char]97)
    }
    'max-multibyte' {
        $Password = ''.PadLeft(10922, [char]0x5BC6) + 'a'
    }
    'over-multibyte' {
        $Password = ''.PadLeft(10923, [char]0x5BC6)
    }
    'wrong-password' {
        $Password += '-wrong'
    }
    'startup-failure' {}
    'timeout' {
        $TimeoutMilliseconds = 200
    }
    default {
        throw 'prd050_runner_password_probe_case_invalid'
    }
}
$SecretEnvironmentNames = @(
    'METAHUB_PRD050_ADMIN_PASSWORD',
    'METAHUB_ADMIN_PASSWORD',
    'METAHUB_PRD050_PASSWORD_VERIFY'
)
# 生产入口在构造 ProcessStartInfo 前已清除兼容口令变量；行为探针从同一净化边界开始，
# 并验证调用结束后它们仍保持不存在，而不是依赖 PS5 环境字典的大小写不敏感复制。
foreach ($EnvironmentName in $SecretEnvironmentNames) {
    Remove-Item -LiteralPath ("Env:" + $EnvironmentName) -ErrorAction SilentlyContinue
}
$Digest = ''
$ErrorCode = ''
$ParentEnvironmentPreserved = $false
$Stopwatch = [Diagnostics.Stopwatch]::StartNew()
try {
    try {
        $PasswordParameters = @{
            DatabasePath = $env:PRD050_PROBE_DATABASE
            Password = $Password
            TimeoutMilliseconds = $TimeoutMilliseconds
        }
        $Digest = Get-AdminPasswordHashDigest @PasswordParameters
    }
    catch {
        $ErrorCode = [string]$_.Exception.Message
    }
    $ParentEnvironmentPreserved = @(
        $SecretEnvironmentNames | Where-Object {
            $null -eq [Environment]::GetEnvironmentVariable($_)
        }
    ).Count -eq $SecretEnvironmentNames.Count
}
finally {
    $Password = $null
    foreach ($EnvironmentName in $SecretEnvironmentNames) {
        [Environment]::SetEnvironmentVariable($EnvironmentName, $null)
    }
}
$Stopwatch.Stop()
[pscustomobject][ordered]@{
    case = $ProbeCase
    digest = $Digest
    error_code = $ErrorCode
    elapsed_milliseconds = [long]$Stopwatch.ElapsedMilliseconds
    parent_environment_preserved = $ParentEnvironmentPreserved
    console_input_encoding_restored = (
        [BitConverter]::ToString([Console]::InputEncoding.GetPreamble()) -ceq 'EF-BB-BF'
    )
} | ConvertTo-Json -Compress
""".strip()


def test_runtime_runner_rejects_candidate_python_bytecode(tmp_path: Path) -> None:
    """验证字节码守卫、匿名口令 stdin、捕获 lease 及调用方环境恢复合同。"""

    for case_name, expected_output in (
        ("clean", "passed"),
        ("pycache", "bytecode_artifact_rejected"),
        ("pyc", "bytecode_artifact_rejected"),
    ):
        case_root = tmp_path / case_name
        completed = _run_powershell_function_probe(
            case_root,
            RUNTIME_RUNNER_PATH,
            (
                "Assert-Condition",
                "Assert-NoReparsePoint",
                "Assert-InBatch",
                "Assert-NoCandidatePythonBytecodeArtifacts",
            ),
            _RUNNER_BYTECODE_GUARD_PROBE,
            {
                "PRD050_PROBE_ROOT": str(case_root / "candidate"),
                "PRD050_PROBE_CASE": case_name,
            },
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stderr == ""
        assert completed.stdout.strip() == expected_output

    python_executable = Path(sys.executable).resolve()
    capture_root = tmp_path / "stdin-capture"
    captured = _run_powershell_function_probe(
        capture_root,
        RUNTIME_RUNNER_PATH,
        (
            "Assert-Condition",
            "Assert-NoReparsePoint",
            "Assert-InBatch",
            "New-OwnedPendingCapturePath",
            "Open-OwnedPendingCaptureLease",
            "Publish-OwnedPendingCapture",
            "ConvertTo-CmdToken",
            "New-CapturedCommand",
            "Initialize-Prd050NativeProcessHost",
            "Add-OwnedProcessIdentity",
            "Start-OwnedCmdWrapper",
        ),
        _RUNNER_STDIN_AND_CAPTURE_PROBE,
        {
            "PRD050_PROBE_ROOT": str(capture_root / "evidence"),
            "PRD050_PROBE_PYTHON": str(python_executable),
        },
    )
    assert captured.returncode == 0, captured.stderr
    assert captured.stderr == ""
    capture_result = json.loads(captured.stdout)
    assert capture_result == {
        "exit_code": 0,
        "delete_blocked": True,
        "command_clean": True,
        "environment_clean": True,
        "stdout_matches_digest": True,
        "stdout_secret_free": True,
        "stderr_secret_free": True,
        "stderr_empty": True,
        "pending_removed": True,
    }

    password_seed = "Prd050!" + secrets.token_hex(20) + "\u5bc6\u7801"
    salt = secrets.token_bytes(16)
    rounds = 200_000
    password_digest = hashlib.pbkdf2_hmac(
        "sha256", password_seed.encode("utf-8"), salt, rounds
    )
    stored_hash = "pbkdf2_sha256${}${}${}".format(
        rounds,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(password_digest).decode("ascii"),
    )
    database_path = tmp_path / "password-binding.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE tenants (
                tenant_id TEXT PRIMARY KEY,
                slug TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                email TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                password_hash TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO tenants VALUES (?, ?, ?)",
            ("tenant-default", "default", "active"),
        )
        connection.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
            (
                "user-admin",
                "tenant-default",
                "admin@metahub.local",
                "admin",
                "active",
                stored_hash,
            ),
        )
    expected_binding_digest = hashlib.sha256(
        b"PRD-050/admin-password-hash/v1\0" + stored_hash.encode("utf-8")
    ).hexdigest()

    def bind_database_password(bound_password: str) -> str:
        """更新隔离库 PBKDF2 绑定，并返回 closeout 使用的域分离摘要。"""

        bound_digest = hashlib.pbkdf2_hmac(
            "sha256", bound_password.encode("utf-8"), salt, rounds
        )
        bound_hash = "$".join(
            (
                "pbkdf2_sha256",
                str(rounds),
                base64.urlsafe_b64encode(salt).decode("ascii"),
                base64.urlsafe_b64encode(bound_digest).decode("ascii"),
            )
        )
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE users SET password_hash=? WHERE user_id='user-admin'",
                (bound_hash,),
            )
        return hashlib.sha256(
            b"PRD-050/admin-password-hash/v1\0" + bound_hash.encode("utf-8")
        ).hexdigest()

    def run_hash_case(
        case_name: str,
        python_path: Path = python_executable,
    ) -> dict[str, object]:
        """运行真实 ProcessStartInfo 口令向量，并禁止外层探针无限等待。"""

        case_root = tmp_path / f"password-hash-{case_name}"
        hash_probe = _run_powershell_function_probe(
            case_root,
            RUNTIME_RUNNER_PATH,
            (
                "Assert-Condition",
                "ConvertTo-WindowsCommandLineArgument",
                "Get-AdminPasswordHashDigest",
            ),
            _RUNNER_PASSWORD_HASH_PROBE,
            {
                "PRD050_PROBE_PYTHON": str(python_path),
                "PRD050_PROBE_DATABASE": str(database_path),
                "PRD050_PROBE_CASE": case_name,
            },
            standard_input=password_seed + "\n",
                timeout_seconds=30.0,
        )
        assert hash_probe.returncode == 0, hash_probe.stderr
        assert hash_probe.stderr == ""
        assert password_seed not in hash_probe.stdout
        assert password_seed not in hash_probe.stderr
        observed = json.loads(hash_probe.stdout)
        assert observed["parent_environment_preserved"] is True
        assert observed["console_input_encoding_restored"] is True
        return observed

    success_result = run_hash_case("success")
    assert success_result["case"] == "success"
    assert success_result["digest"] == expected_binding_digest, success_result
    assert success_result["error_code"] == ""
    assert 0 <= success_result["elapsed_milliseconds"] < 25_000

    accepted_passwords = {
        "max-ascii": "a" * 32767,
        "max-multibyte": "\u5bc6" * 10922 + "a",
    }
    for case_name, accepted_password in accepted_passwords.items():
        expected_digest = bind_database_password(accepted_password)
        accepted_result = run_hash_case(case_name)
        assert accepted_result["case"] == case_name
        assert accepted_result["digest"] == expected_digest, accepted_result
        assert accepted_result["error_code"] == ""
        assert 0 <= accepted_result["elapsed_milliseconds"] < 25_000

    bind_database_password(password_seed)
    for case_name in (
        "empty",
        "carriage-return",
        "line-feed",
        "carriage-line-feed",
        "isolated-surrogate",
        "over-ascii",
        "over-multibyte",
        "wrong-password",
    ):
        rejected_result = run_hash_case(case_name)
        assert rejected_result["case"] == case_name
        assert rejected_result["digest"] == ""
        assert rejected_result["error_code"] == (
            "prd050_runner_admin_password_binding_failed"
        )
        assert 0 <= rejected_result["elapsed_milliseconds"] < 25_000

    startup_result = run_hash_case(
        "startup-failure",
        tmp_path / "missing-prd050-python.exe",
    )
    assert startup_result["case"] == "startup-failure"
    assert startup_result["digest"] == ""
    assert startup_result["error_code"] == "prd050_runner_admin_password_binding_failed"
    assert 0 <= startup_result["elapsed_milliseconds"] < 5_000

    # 独占锁让真实 Python verifier 在只读 SELECT 处等待；200ms 内部 deadline 必须
    # 主动终止它并观察异步任务，外层 15s timeout 只是防止回归时拖住整个 pytest。
    lock_connection = sqlite3.connect(database_path, isolation_level=None)
    try:
        lock_connection.execute("BEGIN EXCLUSIVE")
        timeout_result = run_hash_case("timeout")
    finally:
        lock_connection.rollback()
        lock_connection.close()
    assert timeout_result["case"] == "timeout"
    assert timeout_result["digest"] == ""
    assert timeout_result["error_code"] == "prd050_runner_admin_password_binding_failed"
    assert 100 <= timeout_result["elapsed_milliseconds"] < 5_000
    password_seed = ""

    runner_source = RUNTIME_RUNNER_PATH.read_text(encoding="utf-8-sig")
    environment_assignment = '$env:PYTHONDONTWRITEBYTECODE = "1"'
    pre_runtime_verifier = (
        'Invoke-StaticEvidenceVerification "static-evidence-pre-runtime.json"'
    )
    assert runner_source.count(environment_assignment) == 1
    assert runner_source.index(environment_assignment) < runner_source.index(
        pre_runtime_verifier
    )
    assert runner_source.count("Assert-NoCandidatePythonBytecodeArtifacts (") == 2
    assert '$env:METAHUB_PRD050_ADMIN_PASSWORD =' not in runner_source
    assert '$env:METAHUB_PRD050_PASSWORD_VERIFY =' not in runner_source
    assert runner_source.count('"--password-stdin"') == 2
    assert runner_source.count("-StandardInputLine $AdminPassword") == 2
    assert runner_source.count('"--generation", $Generation') == 2
    assert '"--suffix", $RuntimeSuffix' in runner_source
    assert '$RuntimeSuffix = "final" + $Generation.Substring(9, 6)' in runner_source
    # 口令 verifier 与静态证据 verifier 各自使用一套有界 ProcessStartInfo 管道。
    assert runner_source.count("RedirectStandardInput = $true") == 2
    assert runner_source.count("RedirectStandardOutput = $true") == 2
    assert runner_source.count("RedirectStandardError = $true") == 2
    assert runner_source.count(".ReadToEndAsync()") == 4
    assert "$PasswordByteCount -le 32767" in runner_source
    assert "$InputWriteTask = $Process.StandardInput.BaseStream.WriteAsync(" in (
        runner_source
    )
    assert "$InputFlushTask = $Process.StandardInput.BaseStream.FlushAsync()" in (
        runner_source
    )
    assert "[ValidateRange(100, 30000)][int]$TimeoutMilliseconds" in runner_source
    assert "NativeMethods.CreatePipe(" in runner_source
    assert "NativeMethods.SetHandleInformation(" in runner_source
    assert "[IO.FileShare]::ReadWrite" in runner_source
    assert "Open-OwnedPendingCaptureLease" in runner_source
    assert "prd050_runner_environment_restore_failed" in runner_source
    assert '"sqlite_backup_api_then_isolated_migration_replay"' in runner_source
    assert '[string]$AcceptanceEvidence.generation -eq $Generation' in runner_source
    for migration_evidence_contract in (
        "$DevelopmentEvidence.migration_target",
        "$DevelopmentEvidence.development_source_file",
        "$DevelopmentEvidence.development_source_sha256",
        "$DevelopmentEvidence.development_source_unchanged",
        "$DevelopmentEvidence.safety_backup_file",
        "$DevelopmentEvidence.safety_backup_sha256",
        "$DevelopmentEvidence.safety_backup_size_bytes",
        "$DevelopmentEvidence.pre_reapply_sha256",
        "$DevelopmentEvidence.pre_reapply_size_bytes",
        "$AcceptanceEvidence.pre_reapply_sha256",
        "$AcceptanceEvidence.pre_reapply_size_bytes",
        "$ExpectedSafetyBackupRelativePath",
        "$ExpectedSafetyBackupSha256",
        "$ExpectedPreReapplySha256",
    ):
        assert migration_evidence_contract in runner_source
    for provenance_contract in (
        "$ExpectedDevelopmentEvidenceSha256",
        "$DevelopmentSourceLease = [IO.File]::Open(",
        "$SafetyBackupLease = [IO.File]::Open(",
        "prd050_runner_prepare_provenance_lease_failed",
        "prd050_runner_prepare_provenance_read_failed",
        "prd050_runner_development_evidence_hash_mismatch",
        "prd050_runner_provenance_database_sidecar_present_after_acceptance",
        "prd050_runner_provenance_changed_during_acceptance",
        'foreach ($LeaseVariableName in @("SafetyBackupLease", "DevelopmentSourceLease"))',
        "prd050_runner_reparse_point_check_failed",
    ):
        assert provenance_contract in runner_source
    assert runner_source.count("[IO.FileShare]::Read") >= 2
    assert "$Info.EnvironmentVariables" not in runner_source
    assert "function Invoke-BoundedRunnerStaticVerifier" in runner_source
    assert "& $script:Python @VerifierArguments 2>&1" not in runner_source
    assert "prd050_runner_static_verifier_timeout" in runner_source
    assert "prd050_runner_static_verifier_input_encoding_restore_failed" in (
        runner_source
    )
    assert runner_source.count(
        "[Console]::InputEncoding = $PreviousConsoleInputEncoding"
    ) >= 2
    assert "[Console]::InputEncoding = $StrictStdinUtf8" in runner_source
    assert "[Console]::InputEncoding = $Utf8Strict" in runner_source

    launcher_source = LAUNCHER_PATH.read_text(encoding="utf-8-sig")
    for launcher_contract in (
        "$ExpectedDevelopmentSourceSizeBytes",
        "$ExpectedSafetyBackupSizeBytes",
        "function Assert-LauncherProvenanceSidecarsAbsent",
        "prd050_launcher_provenance_sidecar_check_failed",
        "prd050_launcher_reparse_point_check_failed",
        "prd050_launcher_runner_failed",
        "prd050_launcher_closeout_failed",
        "prd050_launcher_cleanup_failed",
        "function Invoke-BoundedLauncherStaticVerifier",
        "prd050_launcher_static_verifier_timeout",
        "prd050_launcher_static_verifier_input_encoding_restore_failed",
    ):
        assert launcher_contract in launcher_source
    assert launcher_source.count("[IO.FileShare]::Read") >= 1
    assert "$Info.EnvironmentVariables" not in launcher_source
    assert "[Console]::InputEncoding = $StrictStdinUtf8" in launcher_source
    assert (
        "[Console]::InputEncoding = $PreviousConsoleInputEncoding"
        in launcher_source
    )
    assert "$StaticOutput = @(& $Python @VerifierArguments 2>&1)" not in launcher_source
    assert launcher_source.count("Assert-LauncherProvenanceSidecarsAbsent") >= 5
    development_lease_call = (
        "[void](Add-LauncherFrozenLease (\n"
        "        $ExpectedDevelopmentSourcePath"
    )
    runner_call = "& $RunnerPath -AdminPasswordSecure $SecurePassword"
    closeout_call = (
        "& $CloseoutPath -BatchDir $BatchDir -AdminPasswordSecure $SecurePassword"
    )
    final_lease_check = (
        "# closeout 返回后再复核一次，覆盖调用期间已存在共享写句柄的极端情形"
    )
    assert launcher_source.index(development_lease_call) < launcher_source.index(runner_call)
    assert launcher_source.index(runner_call) < launcher_source.index(closeout_call)
    assert launcher_source.index(closeout_call) < launcher_source.index(final_lease_check)
    browser_source = WORKSPACE_ROOT / "scripts" / "acceptance" / "prd050-browser-acceptance.mjs"
    browser_sha256 = hashlib.sha256(browser_source.read_bytes()).hexdigest()
    assert browser_sha256 == "9ea86689c5144888e6c22ddec6fa00122da1a890683b51d53690826b5a3ce19e"
    assert f'$ExpectedBrowserAcceptanceSha256 = "{browser_sha256}"' in runner_source
    closeout_source = CLOSEOUT_PATH.read_text(encoding="utf-8-sig")
    assert f'$ExpectedBrowserAcceptanceSha256 = "{browser_sha256}"' in closeout_source
    assert '"prepare_snapshot": prepare_snapshot' in closeout_source
    assert '"evidence_kind": "prd050_prepare_candidate"' in closeout_source
    assert (
        '"method": "sqlite_backup_api_then_isolated_migration_replay"'
        in closeout_source
    )
    assert '"generation": generation' in closeout_source
    for service_environment_name in (
        "METAHUB_ENV",
        "METAHUB_LOG_LEVEL",
        "METAHUB_DATABASE_URL",
        "METAHUB_ALLOWED_ORIGINS",
        "METAHUB_GATEWAY_API_BASE_URL",
        "METAHUB_CONTROL_API_BASE_URL",
        "METAHUB_PROVIDER_HEALTH_ENABLED",
        "METAHUB_USAGE_EXPORT_ENABLED",
        "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED",
        "METAHUB_CONSOLE_PORT",
        "METAHUB_AUTH_TOKEN_SECRET",
        "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET",
        "METAHUB_CACHE_AFFINITY_HMAC_SECRET",
        "METAHUB_INTERNAL_CONFIG_TOKEN",
        "METAHUB_CHROME_PATH",
    ):
        assert runner_source.count(f'"{service_environment_name}"') >= 1
        assert f"$env:{service_environment_name} =" in runner_source


# 无 stdin 探针复用生产 ProcessStartInfo 封装，覆盖正常 EOF、双流诊断、超时、启动失败
# 和真实迁移脚本 argv 边界。探针只输出稳定布尔值，绝不把捕获的子进程诊断二次泄露。
_CLOSEOUT_NO_STDIN_AND_MIGRATION_ARGV_PROBE = r"""
$ProbeRoot = $env:PRD050_PROBE_ROOT
$ProbeCase = $env:PRD050_PROBE_CASE
New-Item -ItemType Directory -Path $ProbeRoot -Force | Out-Null
$script:MigrationReplayTempRoot = (Resolve-Path -LiteralPath $ProbeRoot).Path
$script:WorkspaceRoot = $script:MigrationReplayTempRoot
$script:Utf8NoBom = New-Object Text.UTF8Encoding($false)
$script:Python = $env:PRD050_PROBE_PYTHON
# closeout helper 必须让无 stdin 子进程看到真实 EOF，而不是宿主 UTF-8 BOM。
[Console]::InputEncoding = New-Object Text.UTF8Encoding($true, $true)
$SecretEnvironmentNames = @(
    'METAHUB_PRD050_ADMIN_PASSWORD',
    'METAHUB_ADMIN_PASSWORD',
    'METAHUB_PRD050_PASSWORD_VERIFY'
)
# 与生产 closeout 入口保持一致：三种兼容口令变量在启动任何 Python 前都必须不存在。
foreach ($EnvironmentName in $SecretEnvironmentNames) {
    Remove-Item -LiteralPath ("Env:" + $EnvironmentName) -ErrorAction SilentlyContinue
}
$RunnerCode = @'
from __future__ import annotations

import os
import sys

secret_names = (
    "METAHUB_PRD050_ADMIN_PASSWORD",
    "METAHUB_ADMIN_PASSWORD",
    "METAHUB_PRD050_PASSWORD_VERIFY",
)
# 无输入 runner 必须从重定向管道立即读到 EOF，不能继承外层 PowerShell 终端。
if sys.stdin.buffer.read(1) != b"":
    raise SystemExit(8)
# 子进程独立复核入口净化合同，防止测试只相信父进程变量观察结果而形成自洽。
if any(name in os.environ for name in secret_names):
    raise SystemExit(9)
print("prd050-no-stdin-ok")
'@
$Arguments = @('probe')
$Prefix = 'prd050-migration-replay'
$TimeoutMilliseconds = 5000
switch ($ProbeCase) {
    'success' {}
    'child-diagnostic' {
        $RunnerCode = @'
import sys

sys.stdout.write("PRD050_NO_STDIN_STDOUT_SENTINEL\n")
sys.stderr.write("PRD050_NO_STDIN_STDERR_SENTINEL\n")
raise SystemExit(7)
'@
    }
    'timeout' {
        $TimeoutMilliseconds = 200
        $RunnerCode = @'
import time

time.sleep(60)
'@
    }
    'startup-failure' {
        $script:Python = Join-Path $ProbeRoot 'missing-python.exe'
    }
    'migration-argv-27' {
        $RunnerCode = [IO.File]::ReadAllText(
            $env:PRD050_PROBE_MIGRATION_CODE,
            [Text.Encoding]::UTF8
        )
        $Arguments = @(0..26 | ForEach-Object { "argument-$_" })
    }
    'migration-argv-28' {
        $RunnerCode = [IO.File]::ReadAllText(
            $env:PRD050_PROBE_MIGRATION_CODE,
            [Text.Encoding]::UTF8
        )
        $Arguments = @(0..27 | ForEach-Object { "argument-$_" })
        foreach ($NumericIndex in @(10, 14, 17, 19)) {
            $Arguments[$NumericIndex] = '1'
        }
    }
    'migration-argv-29' {
        $RunnerCode = [IO.File]::ReadAllText(
            $env:PRD050_PROBE_MIGRATION_CODE,
            [Text.Encoding]::UTF8
        )
        $Arguments = @(0..28 | ForEach-Object { "argument-$_" })
    }
    default {
        throw 'prd050_no_stdin_probe_case_invalid'
    }
}
$Invocation = $null
$ErrorCode = ''
$Stopwatch = [Diagnostics.Stopwatch]::StartNew()
try {
    try {
        $RunnerParameters = @{
            Code = $RunnerCode
            Prefix = $Prefix
            Arguments = $Arguments
            TimeoutMilliseconds = $TimeoutMilliseconds
        }
        $Invocation = Invoke-TemporaryPythonRunner @RunnerParameters
    }
    catch {
        $ErrorCode = [string]$_.Exception.Message
    }
    $Stopwatch.Stop()
    $Captured = @()
    $ExitCode = -1
    if ($null -ne $Invocation) {
        $Captured = @($Invocation.output)
        $ExitCode = [int]$Invocation.exit_code
    }
    $CapturedText = $Captured -join [Environment]::NewLine
    $RemainingDirectories = @(
        Get-ChildItem -LiteralPath $ProbeRoot -Directory -Filter 'prd050-migration-replay-*'
    ).Count
    $ParentEnvironmentPreserved = @(
        $SecretEnvironmentNames | Where-Object {
            $null -eq [Environment]::GetEnvironmentVariable($_)
        }
    ).Count -eq $SecretEnvironmentNames.Count
    [pscustomobject][ordered]@{
        case = $ProbeCase
        error_code = $ErrorCode
        exit_code = $ExitCode
        success_output_valid = (
            $Captured.Count -eq 1 -and
            [string]$Captured[0] -ceq 'prd050-no-stdin-ok'
        )
        diagnostic_captured = (
            $Captured -ccontains 'PRD050_NO_STDIN_STDOUT_SENTINEL' -and
            $Captured -ccontains 'PRD050_NO_STDIN_STDERR_SENTINEL'
        )
        argument_count_rejected = $CapturedText.Contains(
            'prd050_closeout_migration_replay_argument_count_invalid'
        )
        root_rejected = $CapturedText.Contains(
            'prd050_closeout_migration_replay_root_invalid'
        )
        elapsed_milliseconds = [int]$Stopwatch.ElapsedMilliseconds
        temporary_directory_count = $RemainingDirectories
        parent_environment_preserved = $ParentEnvironmentPreserved
        console_input_encoding_restored = (
            [BitConverter]::ToString([Console]::InputEncoding.GetPreamble()) -ceq 'EF-BB-BF'
        )
    } | ConvertTo-Json -Compress
}
finally {
    foreach ($EnvironmentName in $SecretEnvironmentNames) {
        [Environment]::SetEnvironmentVariable($EnvironmentName, $null)
    }
}
""".strip()


# closeout 的正式 static verifier 使用独立 ProcessStartInfo；该探针以带 BOM 宿主编码
# 覆盖 EOF、双流、主进程超时以及主进程退出后由子进程继续持有输出管道的边界。
_CLOSEOUT_STATIC_VERIFIER_CAPTURE_PROBE = r"""
$ProbeRoot = $env:PRD050_PROBE_ROOT
$ProbeCase = $env:PRD050_PROBE_CASE
$Workspace = Join-Path $ProbeRoot 'workspace'
$Candidate = Join-Path $Workspace 'candidate'
New-Item -ItemType Directory -Path $Candidate -Force | Out-Null
$VerifierPath = Join-Path $Candidate 'static-verifier.py'
$PidPath = Join-Path $Candidate 'child.pid'
$Utf8NoBom = New-Object Text.UTF8Encoding($false)
[Console]::InputEncoding = New-Object Text.UTF8Encoding($true, $true)
foreach ($SecretName in @(
    'METAHUB_PRD050_ADMIN_PASSWORD',
    'METAHUB_ADMIN_PASSWORD',
    'METAHUB_PRD050_PASSWORD_VERIFY'
)) {
    Remove-Item -LiteralPath ("Env:" + $SecretName) -ErrorAction SilentlyContinue
}
$VerifierCode = ''
switch ($ProbeCase) {
    'success' {
        $VerifierCode = @'
import os
import sys

secret_names = (
    "METAHUB_PRD050_ADMIN_PASSWORD",
    "METAHUB_ADMIN_PASSWORD",
    "METAHUB_PRD050_PASSWORD_VERIFY",
)
# 无输入 closeout verifier 必须读到真实 EOF，并继承入口已净化的环境。
if sys.stdin.buffer.read(1) != b"" or any(name in os.environ for name in secret_names):
    raise SystemExit(9)
print("prd050-closeout-static-ok")
'@
    }
    'stderr-nonzero' {
        $VerifierCode = @'
import sys

sys.stdout.write("PRD050_CLOSEOUT_STATIC_STDOUT_SENTINEL" + chr(10))
sys.stderr.write("PRD050_CLOSEOUT_STATIC_STDERR_SENTINEL" + chr(10))
raise SystemExit(7)
'@
    }
    'timeout' {
        $VerifierCode = @'
import os
import sys
import time
from pathlib import Path

candidate = Path(sys.argv[sys.argv.index("--batch") + 1])
(candidate / "child.pid").write_text(str(os.getpid()), encoding="ascii")
time.sleep(60)
'@
    }
    'inherited-pipe' {
        $VerifierCode = @'
import subprocess
import sys
from pathlib import Path

candidate = Path(sys.argv[sys.argv.index("--batch") + 1])
# 子进程故意继承 stdout/stderr 后让 verifier 主进程退出；生产 helper 必须让双流等待
# 受同一 deadline 约束，并在有界 cleanup 中观察到继承者关闭管道。
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(2.0)"],
    close_fds=False,
)
(candidate / "child.pid").write_text(str(child.pid), encoding="ascii")
'@
    }
    'startup-failure' {
        $VerifierCode = 'raise SystemExit(0)'
    }
    default {
        throw 'prd050_closeout_static_probe_case_invalid'
    }
}
[IO.File]::WriteAllText(
    $VerifierPath,
    $VerifierCode + [Environment]::NewLine,
    $Utf8NoBom
)
$PythonPath = $env:PRD050_PROBE_PYTHON
if ($ProbeCase -ceq 'startup-failure') {
    $PythonPath = Join-Path $ProbeRoot 'missing-python.exe'
}
$TimeoutMilliseconds = if ($ProbeCase -ceq 'timeout') {
    1000
}
elseif ($ProbeCase -ceq 'inherited-pipe') {
    1000
}
else {
    5000
}
$Invocation = $null
$ErrorCode = ''
$Stopwatch = [Diagnostics.Stopwatch]::StartNew()
try {
    try {
        $StaticParameters = @{
            PythonPath = $PythonPath
            VerifierPath = $VerifierPath
            WorkspacePath = $Workspace
            CandidatePath = $Candidate
            Generation = '20260724T120001'
            TimeoutMilliseconds = $TimeoutMilliseconds
        }
        $Invocation = Invoke-StaticVerifierCaptured @StaticParameters
    }
    catch {
        $ErrorCode = [string]$_.Exception.Message
    }
}
finally {
    $Stopwatch.Stop()
}
$ExitCode = -1
$Stdout = ''
$Stderr = ''
if ($null -ne $Invocation) {
    $ExitCode = [int]$Invocation.exit_code
    $Stdout = [string]$Invocation.stdout
    $Stderr = [string]$Invocation.stderr
}
$ChildPidRecorded = Test-Path -LiteralPath $PidPath -PathType Leaf
$ChildProcessAlive = $false
if ($ChildPidRecorded) {
    try {
        $ChildPid = [int][IO.File]::ReadAllText($PidPath, [Text.Encoding]::ASCII)
        $ChildProcessAlive = $null -ne (
            Get-Process -Id $ChildPid -ErrorAction SilentlyContinue
        )
    }
    catch {
        $ChildProcessAlive = $false
    }
}
[pscustomobject][ordered]@{
    case = $ProbeCase
    error_code = $ErrorCode
    exit_code = $ExitCode
    success_output_valid = (
        $Stdout -ceq ("prd050-closeout-static-ok" + [Environment]::NewLine) -and
        $Stderr -ceq ''
    )
    diagnostics_captured = (
        $Stdout.Contains('PRD050_CLOSEOUT_STATIC_STDOUT_SENTINEL') -and
        $Stderr.Contains('PRD050_CLOSEOUT_STATIC_STDERR_SENTINEL')
    )
    elapsed_milliseconds = [int]$Stopwatch.ElapsedMilliseconds
    child_pid_recorded = $ChildPidRecorded
    child_process_alive = $ChildProcessAlive
    console_input_encoding_restored = (
        [BitConverter]::ToString([Console]::InputEncoding.GetPreamble()) -ceq 'EF-BB-BF'
    )
} | ConvertTo-Json -Compress
""".strip()


def test_closeout_no_stdin_runner_and_migration_argv_contract(tmp_path: Path) -> None:
    """验证无 stdin 子进程不会泄露诊断，并冻结迁移重放 29 项 sys.argv 合同。"""

    migration_code_path = tmp_path / "migration-replay-production.py"
    migration_code_path.write_text(
        _extract_powershell_single_quoted_here_string(
            CLOSEOUT_PATH,
            "MigrationReplayPython",
        ),
        encoding="utf-8",
        newline="\n",
    )

    def run_case(case_name: str) -> dict[str, object]:
        """执行一个真实 Windows PowerShell 5 行为向量并返回稳定 JSON 观察值。"""

        case_root = tmp_path / f"closeout-no-stdin-{case_name}"
        completed = _run_powershell_function_probe(
            case_root,
            CLOSEOUT_PATH,
            (
                "Assert-Condition",
                "Assert-NoReparsePoint",
                "ConvertTo-CloseoutWindowsArgument",
                "Invoke-TemporaryPythonRunner",
            ),
            _CLOSEOUT_NO_STDIN_AND_MIGRATION_ARGV_PROBE,
            {
                "PRD050_PROBE_ROOT": str(case_root / "runner-root"),
                "PRD050_PROBE_PYTHON": str(Path(sys.executable).resolve()),
                "PRD050_PROBE_CASE": case_name,
                "PRD050_PROBE_MIGRATION_CODE": str(migration_code_path),
            },
            timeout_seconds=15.0,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stderr == ""
        # 子进程输出中故意包含诊断哨兵和 pytest 临时绝对路径；生产封装只能把它们
        # 留在内存供固定码判断，探针 stdout/stderr 均不得出现原始诊断。
        for forbidden in (
            "PRD050_NO_STDIN_STDOUT_SENTINEL",
            "PRD050_NO_STDIN_STDERR_SENTINEL",
            str(case_root),
        ):
            assert forbidden not in completed.stdout
            assert forbidden not in completed.stderr
        observed = json.loads(completed.stdout)
        assert observed["case"] == case_name
        assert observed["temporary_directory_count"] == 0
        assert observed["parent_environment_preserved"] is True
        assert observed["console_input_encoding_restored"] is True
        assert 0 <= observed["elapsed_milliseconds"] < 5_000
        return observed

    success = run_case("success")
    assert success["error_code"] == ""
    assert success["exit_code"] == 0
    assert success["success_output_valid"] is True

    diagnostic = run_case("child-diagnostic")
    assert diagnostic["error_code"] == ""
    assert diagnostic["exit_code"] == 7
    assert diagnostic["diagnostic_captured"] is True

    timeout = run_case("timeout")
    assert timeout["error_code"] == "prd050_closeout_python_runner_timeout"
    assert timeout["exit_code"] == -1
    assert 100 <= timeout["elapsed_milliseconds"] < 5_000

    startup = run_case("startup-failure")
    assert startup["error_code"] == "prd050_closeout_python_runner_process_invalid"
    assert startup["exit_code"] == -1

    too_few = run_case("migration-argv-27")
    assert too_few["error_code"] == ""
    assert too_few["exit_code"] == 1
    assert too_few["argument_count_rejected"] is True
    assert too_few["root_rejected"] is False

    exact = run_case("migration-argv-28")
    assert exact["error_code"] == ""
    assert exact["exit_code"] == 1
    assert exact["argument_count_rejected"] is False
    assert exact["root_rejected"] is True

    too_many = run_case("migration-argv-29")
    assert too_many["error_code"] == ""
    assert too_many["exit_code"] == 1
    assert too_many["argument_count_rejected"] is True
    assert too_many["root_rejected"] is False

    # 第二个 closeout 子进程入口也必须使用有界 stdin/stdout/stderr 生命周期；将探针
    # 合并到既有测试，既提升行为覆盖，又不改变正式 Python/JUnit 冻结计数。
    def run_static_verifier_case(case_name: str) -> dict[str, object]:
        """执行 closeout static verifier 的真实子进程行为向量。

        参数：
            case_name：故障注入名称，限定为当前测试声明的成功、诊断、超时等向量。
        返回：
            仅含固定错误码、退出码和布尔观察值的字典，不返回原始子进程诊断。
        关键约束：
            外层十五秒上限只用于防止测试自身挂死；生产函数仍须用内部单调 deadline
            和五秒清理预算收口进程及双流任务，并恢复调用前的控制台输入编码。
        """

        case_root = tmp_path / f"closeout-static-{case_name}"
        completed = _run_powershell_function_probe(
            case_root,
            CLOSEOUT_PATH,
            (
                "Assert-Condition",
                "Invoke-StaticVerifierCaptured",
            ),
            _CLOSEOUT_STATIC_VERIFIER_CAPTURE_PROBE,
            {
                "PRD050_PROBE_ROOT": str(case_root / "fixture"),
                "PRD050_PROBE_CASE": case_name,
                "PRD050_PROBE_PYTHON": str(Path(sys.executable).resolve()),
            },
            timeout_seconds=15.0,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stderr == ""
        for forbidden in (
            "PRD050_CLOSEOUT_STATIC_STDOUT_SENTINEL",
            "PRD050_CLOSEOUT_STATIC_STDERR_SENTINEL",
            str(case_root),
        ):
            assert forbidden not in completed.stdout
            assert forbidden not in completed.stderr
        observed = json.loads(completed.stdout)
        assert observed["case"] == case_name
        assert observed["console_input_encoding_restored"] is True
        assert observed["child_process_alive"] is False
        assert 0 <= observed["elapsed_milliseconds"] < 5_000
        return observed

    static_success = run_static_verifier_case("success")
    assert static_success["error_code"] == ""
    assert static_success["exit_code"] == 0
    assert static_success["success_output_valid"] is True

    static_diagnostic = run_static_verifier_case("stderr-nonzero")
    assert static_diagnostic["error_code"] == ""
    assert static_diagnostic["exit_code"] == 7
    assert static_diagnostic["diagnostics_captured"] is True

    static_timeout = run_static_verifier_case("timeout")
    assert static_timeout["error_code"] == "prd050_closeout_static_verifier_timeout"
    assert static_timeout["exit_code"] == -1
    assert static_timeout["child_pid_recorded"] is True
    assert 1_000 <= static_timeout["elapsed_milliseconds"] < 5_000

    inherited_pipe = run_static_verifier_case("inherited-pipe")
    assert inherited_pipe["error_code"] == "prd050_closeout_static_verifier_timeout"
    assert inherited_pipe["exit_code"] == -1
    assert inherited_pipe["child_pid_recorded"] is True
    assert 1_000 <= inherited_pipe["elapsed_milliseconds"] < 5_000

    static_startup = run_static_verifier_case("startup-failure")
    assert static_startup["error_code"] == (
        "prd050_closeout_static_verifier_process_invalid"
    )
    assert static_startup["exit_code"] == -1

    closeout_source = CLOSEOUT_PATH.read_text(encoding="utf-8-sig")
    assert "& $script:Python $Runner @Arguments 2>&1" not in closeout_source
    assert "& $Python $MigrationReplayRunnerPath" not in closeout_source
    assert "$MigrationReplayRunnerPath" not in closeout_source
    assert closeout_source.count("New-Object System.Diagnostics.ProcessStartInfo") >= 1
    assert closeout_source.count("$Info.RedirectStandardInput = $true") == 2
    assert closeout_source.count("$Info.RedirectStandardOutput = $true") >= 1
    assert closeout_source.count("$Info.RedirectStandardError = $true") >= 1
    assert closeout_source.count(".ReadToEndAsync()") >= 2
    assert "$Info.EnvironmentVariables" not in closeout_source
    assert "prd050_closeout_reparse_point_check_failed" in closeout_source
    assert (
        "prd050_closeout_python_runner_input_encoding_restore_failed"
        in closeout_source
    )
    assert "prd050_closeout_static_verifier_cleanup_failed" in closeout_source
    assert (
        "prd050_closeout_static_verifier_input_encoding_restore_failed"
        in closeout_source
    )
    assert "$Completed = $Process.WaitForExit(120000)" not in closeout_source
    assert "[ValidateRange(100, 120000)][int]$TimeoutMilliseconds" in (
        closeout_source
    )
    assert '"sibling_backups_share_development_source": True' in closeout_source
    assert '"safety_backup_to_acceptance_source": True' not in closeout_source
    assert "$MigrationReplayArguments.Count -eq 28" in closeout_source
    assert '-Prefix "prd050-migration-replay"' in closeout_source
    assert "-TimeoutMilliseconds 120000" in closeout_source


# 口令探针从外层测试 stdin 接收随机值，再让生产 runner 通过匿名 stdin 传给子 Python。
# 子进程同时拒绝口令环境变量名、环境值和 argv 泄露，且只返回不可逆摘要；测试由此在
# 不把口令写入 harness、参数或环境的前提下覆盖 UTF-8、EOF、捕获和临时目录清理合同。
_CLOSEOUT_PASSWORD_STDIN_PROBE = r"""
$ProbeRoot = $env:PRD050_PROBE_ROOT
New-Item -ItemType Directory -Path $ProbeRoot -Force | Out-Null
$script:MigrationReplayTempRoot = (Resolve-Path -LiteralPath $ProbeRoot).Path
$script:WorkspaceRoot = $script:MigrationReplayTempRoot
$script:Python = $env:PRD050_PROBE_PYTHON
$script:Utf8NoBom = New-Object Text.UTF8Encoding($false)
[Console]::InputEncoding = New-Object Text.UTF8Encoding($false, $true)
$ProbeCase = $env:PRD050_PROBE_CASE
if ([string]::IsNullOrEmpty($ProbeCase)) {
    $ProbeCase = 'success'
}
$Password = [Console]::In.ReadLine()
$SecretEnvironmentNames = @(
    'METAHUB_PRD050_ADMIN_PASSWORD',
    'METAHUB_ADMIN_PASSWORD',
    'METAHUB_PRD050_PASSWORD_VERIFY'
)
# 入口净化是安全前置条件；探针仍由子 Python 独立断言三种变量均未被继承。
foreach ($EnvironmentName in $SecretEnvironmentNames) {
    Remove-Item -LiteralPath ("Env:" + $EnvironmentName) -ErrorAction SilentlyContinue
}
$RunnerCode = @'
from __future__ import annotations

import hashlib
import os
import sys

secret_names = (
    "METAHUB_PRD050_ADMIN_PASSWORD",
    "METAHUB_ADMIN_PASSWORD",
    "METAHUB_PRD050_PASSWORD_VERIFY",
)
line = sys.stdin.buffer.readline(32769)
extra = sys.stdin.buffer.read(1)
if not line.endswith(b"\n") or extra or len(line) > 32768:
    raise SystemExit(7)
payload = line[:-2] if line.endswith(b"\r\n") else line[:-1]
if b"\r" in payload or b"\n" in payload:
    raise SystemExit(7)
try:
    secret = payload.decode("utf-8", "strict")
# 非法 UTF-8 必须直接失败，不能让替换字符改变父子进程核对的口令字节。
except UnicodeDecodeError:
    raise SystemExit(7) from None
# 兼容口令变量只要仍存在就说明子进程环境清洗失效，即使变量值不是本次随机口令也拒绝。
if any(name in os.environ for name in secret_names):
    raise SystemExit(7)
# 同时扫描实际口令，证明它既不在匿名 stdin 之外的环境值中，也没有进入可枚举的 argv。
if (
    any(secret in value for value in os.environ.values())
    or any(secret in argument for argument in sys.argv)
):
    raise SystemExit(7)
print(hashlib.sha256(payload).hexdigest())
'@
$Arguments = @('probe')
$TimeoutMilliseconds = 5000
switch ($ProbeCase) {
    'success' {}
    'empty' {
        $Password = ''
    }
    'carriage-return' {
        $Password += "`r"
    }
    'line-feed' {
        $Password += "`n"
    }
    'isolated-surrogate' {
        $Password = [string][char]0xD800
    }
    'max-ascii' {
        $Password = ''.PadLeft(32767, [char]97)
    }
    'over-ascii' {
        $Password = ''.PadLeft(32768, [char]97)
    }
    'max-multibyte' {
        $Password = ''.PadLeft(10922, [char]0x5BC6) + 'a'
    }
    'over-multibyte' {
        $Password = ''.PadLeft(10923, [char]0x5BC6)
    }
    'child-nonzero' {
        $RunnerCode = @'
import sys

sys.stdin.buffer.read()
raise SystemExit(7)
'@
    }
    'timeout' {
        $Password = ''.PadLeft(32767, [char]97)
        $TimeoutMilliseconds = 200
        $RunnerCode = @'
import time

time.sleep(60)
'@
    }
    default {
        throw 'prd050_password_probe_case_invalid'
    }
}
$Invocation = $null
$ErrorCode = ''
try {
    try {
        $Invocation = Invoke-TemporaryPythonRunner `
            -Code $RunnerCode `
            -Prefix 'prd050-password-verify' `
            -Arguments $Arguments `
            -StandardInputLine $Password `
            -TimeoutMilliseconds $TimeoutMilliseconds
    }
    catch {
        $ErrorCode = [string]$_.Exception.Message
    }
    $ParentEnvironmentPreserved = @(
        $SecretEnvironmentNames | Where-Object {
            $null -eq [Environment]::GetEnvironmentVariable($_)
        }
    ).Count -eq $SecretEnvironmentNames.Count
    $RemainingDirectories = @(
        Get-ChildItem `
            -LiteralPath $ProbeRoot `
            -Directory `
            -Filter 'prd050-password-verify-*'
    ).Count
    $ExitCode = -1
    $Output = @()
    $TemporaryDirectoryRemoved = $RemainingDirectories -eq 0
    if ($null -ne $Invocation) {
        $ExitCode = [int]$Invocation.exit_code
        $Output = @($Invocation.output)
        $TemporaryDirectoryRemoved = [bool]$Invocation.temporary_directory_removed
    }
    $Password = $null
    [pscustomobject][ordered]@{
        case = $ProbeCase
        error_code = $ErrorCode
        exit_code = $ExitCode
        output = $Output
        temporary_directory_removed = $TemporaryDirectoryRemoved
        temporary_directory_count = $RemainingDirectories
        parent_environment_preserved = $ParentEnvironmentPreserved
    } | ConvertTo-Json -Compress
}
finally {
    $Password = $null
    foreach ($EnvironmentName in $SecretEnvironmentNames) {
        [Environment]::SetEnvironmentVariable($EnvironmentName, $null)
    }
}
""".strip()


# Closeout 探针同时冻结证据流锁、碰撞拒绝和不可覆盖发布，避免读取后证据漂移或
# 两个收口进程把同一 generation 的最终凭据相互覆盖。
_CLOSEOUT_LOCK_AND_PUBLISH_PROBE = r"""
$ProbeRoot = $env:PRD050_PROBE_ROOT
New-Item -ItemType Directory -Path $ProbeRoot -Force | Out-Null
$script:ResolvedBatchDir = (Resolve-Path -LiteralPath $ProbeRoot).Path
$script:LockedEvidenceStreams = New-Object System.Collections.ArrayList
$script:LockedEvidenceByFile = [Collections.Generic.Dictionary[string, object]]::new(
    [StringComparer]::Ordinal
)
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:OutputExistsCode = 'closeout_output_exists'
$InputPath = Join-Path $ProbeRoot 'input.txt'
$OutputPath = Join-Path $ProbeRoot 'output.txt'
[IO.File]::WriteAllText($InputPath, 'input', [Text.Encoding]::UTF8)
$WriteBlocked = $false
$DuplicateCode = ''
$ReentryCode = ''
$RecordStable = $false
try {
    $InputLease = Add-LockedEvidenceFile -Path $InputPath
    try {
        $Writer = [IO.File]::Open(
            $InputPath,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $Writer.Dispose()
    }
    catch [IO.IOException] {
        $WriteBlocked = $true
    }
    try {
        [void](Add-LockedEvidenceFile -Path $InputPath)
    }
    catch {
        $DuplicateCode = [string]$_.Exception.Message
    }
    Assert-LockedEvidenceInventory -Files @(
        (Get-Item -LiteralPath $InputPath -Force)
    ) -ExpectedCount 1 -Code 'locked_inventory_invalid'

    Write-Utf8TextAtomic -Path $OutputPath -Text 'first'
    try {
        Write-Utf8TextAtomic -Path $OutputPath -Text 'second'
    }
    catch {
        $ReentryCode = [string]$_.Exception.Message
    }
    $OutputLease = Add-LockedEvidenceFile -Path $OutputPath
    Assert-LockedEvidenceInventory -Files @(
        (Get-Item -LiteralPath $InputPath -Force),
        (Get-Item -LiteralPath $OutputPath -Force)
    ) -ExpectedCount 2 -Code 'locked_inventory_invalid'
    $FreshOutputRecord = Get-LockedFileRecord -Path $OutputLease.Path -Stream $OutputLease.Stream
    $RecordStable = (
        [long]$FreshOutputRecord.bytes -eq [long]$OutputLease.Record.bytes -and
        [string]$FreshOutputRecord.sha256 -ceq [string]$OutputLease.Record.sha256
    )
    $OutputText = [IO.File]::ReadAllText($OutputPath, [Text.Encoding]::UTF8)
}
finally {
    for ($Index = $script:LockedEvidenceStreams.Count - 1; $Index -ge 0; $Index -= 1) {
        $script:LockedEvidenceStreams[$Index].Dispose()
    }
}
$AfterRelease = [IO.File]::Open(
    $OutputPath,
    [IO.FileMode]::Open,
    [IO.FileAccess]::Write,
    [IO.FileShare]::None
)
$AfterRelease.Dispose()
[pscustomobject][ordered]@{
    write_blocked = $WriteBlocked
    duplicate_code = $DuplicateCode
    reentry_code = $ReentryCode
    output_text = $OutputText
    record_stable = $RecordStable
    lock_count = $script:LockedEvidenceByFile.Count
    release_reopen = $true
} | ConvertTo-Json -Compress
""".strip()


def test_closeout_locks_evidence_and_never_overwrites_outputs(tmp_path: Path) -> None:
    """验证 closeout 长期租约、闭集和 create-if-absent 发布的关键失败关闭合同。"""

    completed = _run_powershell_function_probe(
        tmp_path / "closeout-lock-publish",
        CLOSEOUT_PATH,
        (
            "Assert-Condition",
            "Assert-NoReparsePoint",
            "Assert-InBatch",
            "Write-Utf8TextAtomic",
            "Get-RelativeBatchPath",
            "Get-LockedFileRecord",
            "Add-LockedEvidenceFile",
            "Assert-LockedEvidenceInventory",
        ),
        _CLOSEOUT_LOCK_AND_PUBLISH_PROBE,
        {"PRD050_PROBE_ROOT": str(tmp_path / "closeout-lock-publish" / "evidence")},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    observed = json.loads(completed.stdout)
    assert observed == {
        "write_blocked": True,
        "duplicate_code": "prd050_closeout_locked_evidence_duplicate",
        "reentry_code": "closeout_output_exists",
        "output_text": "first",
        "record_stable": True,
        "lock_count": 2,
        "release_reopen": True,
    }

    password_seed = "Prd050Closeout!" + secrets.token_hex(20) + "\u5bc6\u7801"

    def run_password_case(case_name: str) -> dict[str, object]:
        """运行一个 stdin 边界向量，并保证外层 harness 也有独立超时。"""

        case_root = tmp_path / f"closeout-password-stdin-{case_name}"
        password_probe = _run_powershell_function_probe(
            case_root,
            CLOSEOUT_PATH,
            (
                "Assert-Condition",
                "Assert-NoReparsePoint",
                "ConvertTo-CloseoutWindowsArgument",
                "Invoke-TemporaryPythonRunner",
            ),
            _CLOSEOUT_PASSWORD_STDIN_PROBE,
            {
                "PRD050_PROBE_ROOT": str(case_root / "runner-root"),
                "PRD050_PROBE_PYTHON": str(Path(sys.executable).resolve()),
                "PRD050_PROBE_CASE": case_name,
            },
            standard_input=password_seed + "\n",
            timeout_seconds=15.0,
        )
        assert password_probe.returncode == 0, password_probe.stderr
        assert password_probe.stderr == ""
        assert password_seed not in password_probe.stdout
        assert password_seed not in password_probe.stderr
        return json.loads(password_probe.stdout)

    accepted_passwords = {
        "success": password_seed,
        "max-ascii": "a" * 32767,
        "max-multibyte": "\u5bc6" * 10922 + "a",
    }
    for case_name, accepted_password in accepted_passwords.items():
        expected_digest = hashlib.sha256(accepted_password.encode("utf-8")).hexdigest()
        password_result = run_password_case(case_name)
        assert accepted_password not in password_result["output"]
        assert password_result == {
            "case": case_name,
            "error_code": "",
            "exit_code": 0,
            "output": [expected_digest],
            "temporary_directory_removed": True,
            "temporary_directory_count": 0,
            "parent_environment_preserved": True,
        }

    for case_name in (
        "empty",
        "carriage-return",
        "line-feed",
        "isolated-surrogate",
        "over-ascii",
        "over-multibyte",
    ):
        assert run_password_case(case_name) == {
            "case": case_name,
            "error_code": "prd050_closeout_python_runner_stdin_invalid",
            "exit_code": -1,
            "output": [],
            "temporary_directory_removed": True,
            "temporary_directory_count": 0,
            "parent_environment_preserved": True,
        }

    assert run_password_case("child-nonzero") == {
        "case": "child-nonzero",
        "error_code": "",
        "exit_code": 7,
        "output": [],
        "temporary_directory_removed": True,
        "temporary_directory_count": 0,
        "parent_environment_preserved": True,
    }
    assert run_password_case("timeout") == {
        "case": "timeout",
        "error_code": "prd050_closeout_python_runner_timeout",
        "exit_code": -1,
        "output": [],
        "temporary_directory_removed": True,
        "temporary_directory_count": 0,
        "parent_environment_preserved": True,
    }
    password_seed = ""

    closeout_source = CLOSEOUT_PATH.read_text(encoding="utf-8-sig")
    assert '$env:METAHUB_PRD050_PASSWORD_VERIFY = $AdminPassword' not in closeout_source
    assert (
        'password = os.environ.pop("METAHUB_PRD050_PASSWORD_VERIFY", "")'
        not in closeout_source
    )
    assert closeout_source.count("-StandardInputLine $AdminPassword") == 1
    assert closeout_source.count("$Info.RedirectStandardInput = $true") == 2
    assert closeout_source.count(".BaseStream.WriteAsync(") == 1
    assert closeout_source.count(".BaseStream.FlushAsync()") == 1
    assert "[Diagnostics.Stopwatch]::StartNew()" in closeout_source
    assert "prd050_closeout_python_runner_timeout" in closeout_source
    assert "line = sys.stdin.buffer.readline(32769)" in closeout_source
    assert 'password_bytes.decode("utf-8", "strict")' in closeout_source
    assert "Get-ChildItem -LiteralPath $script:ResolvedBatchDir -Force -Recurse" not in (
        closeout_source
    )
    for expected_count in (127, 132, 136, 139):
        assert closeout_source.count(f"-ExpectedCount {expected_count}") == 1
    stage_inventory_counts = {
        "BaseCandidateExpectedFiles": 128,
        "InitialCloseoutExpectedFiles": 133,
        "PreDatabaseExpectedFiles": 137,
        "PreFinalExpectedFiles": 139,
        "FinalExpectedFiles": 140,
    }
    for variable_name, expected_count in stage_inventory_counts.items():
        expected_fragment = "$" + f"{variable_name}.Count -eq {expected_count}"
        assert expected_fragment in closeout_source
    assert "temporary_output_owned = False" in closeout_source
    assert "temporary_owned = False" in closeout_source
    assert closeout_source.count("$Published = $false") == 1


# Process-contract 生成器探针验证 JSON 与 exitcode 的 write-once 发布及异常清理，
# 防止重入覆盖上一轮合同或把半成品误交给正式 runner。
_GENERATOR_PUBLISH_PROBE = r"""
$ProbeRoot = $env:PRD050_PROBE_ROOT
New-Item -ItemType Directory -Path $ProbeRoot -Force | Out-Null
$ContractPath = Join-Path $ProbeRoot 'runner-process-contract.json'
$ExitcodePath = Join-Path $ProbeRoot 'runner-process-contract.exitcode'
$Document = [ordered]@{ status = 'passed'; generation = '20260723T120005' }
switch ($env:PRD050_PROBE_CASE) {
    'success' {}
    'final-reentry' {
        [IO.File]::WriteAllText($ContractPath, 'existing-contract')
        [IO.File]::WriteAllText($ExitcodePath, 'existing-exitcode')
    }
    'exitcode-reentry' {
        [IO.File]::WriteAllText($ExitcodePath, 'existing-exitcode')
    }
    'publish-collision' {
        [IO.File]::WriteAllText(
            (Join-Path $ProbeRoot '.prd050-runner-contract.publish.lock'),
            'existing-lock'
        )
    }
    'parent-mismatch' {
        $OtherRoot = Join-Path $ProbeRoot 'other'
        New-Item -ItemType Directory -Path $OtherRoot | Out-Null
        $ExitcodePath = Join-Path $OtherRoot 'runner-process-contract.exitcode'
    }
    default { throw 'prd050_probe_publish_case_invalid' }
}
try {
    Publish-RunnerContractEvidence `
        -ContractPath $ContractPath `
        -ExitcodePath $ExitcodePath `
        -ContractDocument $Document
    Write-Output 'published'
}
catch {
    Write-Output ([string]$_.Exception.Message)
}
""".strip()


@pytest.mark.parametrize(
    ("case_name", "expected_output"),
    (
        ("success", "published"),
        ("final-reentry", "runner_contract_output_already_exists"),
        ("exitcode-reentry", "runner_contract_output_already_exists"),
        ("publish-collision", "runner_contract_publish_collision"),
        ("parent-mismatch", "runner_contract_publish_path_invalid"),
    ),
)
def test_generator_publish_is_non_destructive(
    tmp_path: Path,
    case_name: str,
    expected_output: str,
) -> None:
    """验证成功发布、最终重入、互斥碰撞和路径错误均不删除既有正式证据。"""

    case_root = tmp_path / case_name
    completed = _run_powershell_function_probe(
        case_root,
        GENERATOR_PATH,
        (
            "Assert-GeneratorCondition",
            "Assert-GeneratorNoReparsePoint",
            "New-GeneratorOwnedUtf8File",
            "Publish-RunnerContractEvidence",
        ),
        _GENERATOR_PUBLISH_PROBE,
        {
            "PRD050_PROBE_ROOT": str(case_root / "evidence"),
            "PRD050_PROBE_CASE": case_name,
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout.strip() == expected_output

    evidence_root = case_root / "evidence"
    contract_path = evidence_root / "runner-process-contract.json"
    exitcode_path = evidence_root / "runner-process-contract.exitcode"
    lock_path = evidence_root / ".prd050-runner-contract.publish.lock"
    pending_paths = [path for path in evidence_root.iterdir() if ".pending." in path.name]
    assert pending_paths == [], "failed publication must clean only its owned temporary files"
    if case_name == "success":
        assert json.loads(contract_path.read_text(encoding="utf-8")) == {
            "status": "passed",
            "generation": "20260723T120005",
        }
        assert exitcode_path.read_text(encoding="utf-8").strip() == "0"
        assert not lock_path.exists(), "owned lock must use delete-on-close"
    elif case_name == "final-reentry":
        assert contract_path.read_text(encoding="utf-8") == "existing-contract"
        assert exitcode_path.read_text(encoding="utf-8") == "existing-exitcode"
    elif case_name == "exitcode-reentry":
        assert not contract_path.exists()
        assert exitcode_path.read_text(encoding="utf-8") == "existing-exitcode"
    elif case_name == "publish-collision":
        assert not contract_path.exists() and not exitcode_path.exists()
        assert lock_path.read_text(encoding="utf-8") == "existing-lock"
    else:
        assert not contract_path.exists() and not exitcode_path.exists()

    generator_source = GENERATOR_PATH.read_text(encoding="utf-8")
    # source_file_count 是冻结源码闭集的第二维门禁，必须在生成器中恰有一个严格整数断言；
    # 缺失或重复都可能让收缩后的 source receipt 被错误接受。
    source_count_guard = "[int]$StaticReceipt.source_file_count -eq 69"
    assert generator_source.count(source_count_guard) == 1
    assert "Remove-Item -LiteralPath $ContractPath" not in generator_source
    assert "Remove-Item -LiteralPath $ContractExitcodePath" not in generator_source
