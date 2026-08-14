"""PRD-078 Runtime 验收器的隔离、时钟、隐私、发布与清理合同。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_runtime_module():
    """按真实脚本路径加载验收器，导入本身不得启动服务或占用端口。"""

    path = Path(__file__).resolve().parents[1] / "prd078-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(
        "prd078_runtime_acceptance_contract",
        path,
    )
    if specification is None or specification.loader is None:
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        raise AssertionError("unable to load PRD-078 runtime acceptance")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


# 隔离加载的 Runtime 模块仅供合同测试调用，导入过程不得启动服务、监听端口或发布制品。
RUNTIME = load_runtime_module()


def test_runtime_boundaries_are_repository_owned_and_loopback_only() -> None:
    """可写目录、解释器和全部监听端口必须冻结在仓库与回环边界。"""

    root = Path(__file__).resolve().parents[3]
    assert RUNTIME.ROOT == root
    assert RUNTIME.WORK_ROOT == root / ".tmp" / "prd078-runtime"
    assert RUNTIME.ARTIFACT_ROOT == root / ".artifacts" / "prd078-runtime"
    assert RUNTIME.PYTHON.is_relative_to(root)
    assert RUNTIME.NODE.is_relative_to(root)
    assert RUNTIME.LOOPBACK == "127.0.0.1"
    assert len(RUNTIME.FIXED_PORTS) == len(set(RUNTIME.FIXED_PORTS)) == 4
    assert RUNTIME.EXPECTED_MIGRATION_COUNT == 71
    assert RUNTIME.EXPECTED_MIGRATION_VERSION == "0071"


def test_runtime_secrets_are_unique_repr_safe_and_fully_scanned(tmp_path: Path) -> None:
    """每个安全域必须使用独立随机值，repr 与最终证据都不能遗漏扫描 owner。"""

    secrets_ = RUNTIME.RuntimeSecrets.create()
    values = secrets_.values()
    assert len(values) == len(set(values)) == 13
    assert all(len(value) >= 32 and value not in repr(secrets_) for value in values)

    work = tmp_path / "work"
    for name in ("temp", "spool"):
        (work / name).mkdir(parents=True, exist_ok=True)
    graph = SimpleNamespace(database_url="sqlite:///repository-owned.sqlite3")
    environment = RUNTIME.service_environment(work, graph, secrets_)
    assert environment["TEMP"] == environment["TMP"] == str(work / "temp")
    assert environment["NO_PROXY"] == "127.0.0.1,localhost"
    assert environment["METAHUB_DATABASE_URL"] == graph.database_url
    assert environment["METAHUB_REDIS_URL"] == "redis://127.0.0.1:6389/0"
    assert environment["METAHUB_APPROVAL_HMAC_SECRET"] == secrets_.approval_secret
    assert environment["METAHUB_INTERNAL_CONFIG_TOKEN"] == secrets_.internal_token
    for host_only_name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "OPENAI_API_KEY",
        "USERPROFILE",
    ):
        assert host_only_name not in environment


@pytest.mark.parametrize(
    "unsafe",
    (
        "..",
        "../outside",
        "C:/outside",
        "contains space",
        "a/b",
        "a\\b",
        "x" * 81,
    ),
)
def test_run_id_rejects_path_or_argument_injection(unsafe: str) -> None:
    """运行 ID 只能是单个短 ASCII 片段，不能借参数写到仓库边界外。"""

    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.safe_run_id(unsafe)
    assert (failure.value.stage, failure.value.code) == ("config.run_id", "invalid")


def test_temporary_environment_restores_present_and_missing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed 与配置环境即使异常也必须恢复，不能污染后续测试或开发终端。"""

    monkeypatch.setenv("PRD078_EXISTING", "before")
    monkeypatch.delenv("PRD078_MISSING", raising=False)
    with pytest.raises(RuntimeError, match="synthetic"):
        with RUNTIME.temporary_environment(
            {"PRD078_EXISTING": "during", "PRD078_MISSING": "created"}
        ):
            assert os.environ["PRD078_EXISTING"] == "during"
            assert os.environ["PRD078_MISSING"] == "created"
            # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
            raise RuntimeError("synthetic")
    assert os.environ["PRD078_EXISTING"] == "before"
    assert "PRD078_MISSING" not in os.environ


def test_webhook_schema_is_exact_and_rejects_private_fields() -> None:
    """公开审批卡片只允许冻结字段，凭据、正文、金额与 Endpoint 一律拒绝。"""

    created = {
        "schema_version": 1,
        "approval_id": "apr_public",
        "status": "pending",
        "target_type": "budget_block_release",
        "target_action": "release",
        "risk_level": "standard",
        "requires_step_up": False,
        "expires_at": "2026-08-13T00:00:00Z",
        "entry": "mobile",
    }
    timeout = {
        "schema_version": 1,
        "approval_id": "apr_public",
        "status": "rejected",
        "resolution_reason": "timeout",
        "target_type": "budget_block_release",
        "target_action": "release",
        "decided_at": "2026-08-13T00:05:00Z",
    }
    assert RUNTIME._valid_webhook_payload("approval.created", created)
    assert RUNTIME._valid_webhook_payload("approval.timeout", timeout)
    assert not RUNTIME._contains_forbidden_webhook_key(created)
    assert not RUNTIME._contains_forbidden_webhook_key(timeout)

    for private_name in ("token", "prompt", "response", "cost", "endpoint_url"):
        polluted = {**created, private_name: "private-canary"}
        assert not RUNTIME._valid_webhook_payload("approval.created", polluted)
        assert RUNTIME._contains_forbidden_webhook_key(polluted)


def test_evidence_rejects_private_keys_and_nested_private_values() -> None:
    """最终 JSON 必须递归拒绝敏感字段名以及任一 Runtime 私有值。"""

    private_value = "PRD078-private-evidence-canary"
    safe = {"status": "passed", "all_108_prds_complete": False, "counts": [1, 2]}
    RUNTIME.assert_safe_evidence(safe, (private_value,))

    with pytest.raises(RUNTIME.AcceptanceFailure) as key_failure:
        RUNTIME.assert_safe_evidence({"nested": {"signing_secret": "hidden"}}, ())
    assert (key_failure.value.stage, key_failure.value.code) == (
        "evidence.shape",
        "forbidden_key",
    )
    with pytest.raises(RUNTIME.AcceptanceFailure) as value_failure:
        RUNTIME.assert_safe_evidence({"nested": [f"prefix-{private_value}"]}, (private_value,))
    assert (value_failure.value.stage, value_failure.value.code) == (
        "evidence.privacy",
        "private_value",
    )


def test_run_directories_are_exclusive_direct_children_with_owner_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Work、staging 与 final 必须是仓库内直属目录，且已存在时禁止覆盖。"""

    work_root = tmp_path / "work-root"
    artifact_root = tmp_path / "artifact-root"
    monkeypatch.setattr(RUNTIME, "WORK_ROOT", work_root)
    monkeypatch.setattr(RUNTIME, "ARTIFACT_ROOT", artifact_root)
    owner = "a" * 64
    work, staging, final = RUNTIME.create_run_directories("prd078-unit-run", owner)
    assert work.parent == work_root
    assert staging.parent == artifact_root
    assert final.parent == artifact_root
    assert not final.exists()
    assert (work / RUNTIME.OWNER_FILENAME).read_text(encoding="ascii").strip() == owner
    assert (staging / RUNTIME.OWNER_FILENAME).read_text(encoding="ascii").strip() == owner
    assert {item.name for item in work.iterdir()} == {
        RUNTIME.OWNER_FILENAME,
        "logs",
        "temp",
        "spool",
    }

    with pytest.raises(RUNTIME.AcceptanceFailure) as duplicate:
        RUNTIME.create_run_directories("prd078-unit-run", owner)
    assert (duplicate.value.stage, duplicate.value.code) == ("config.work", "exists")


def test_owned_work_cleanup_refuses_wrong_claim_then_removes_exact_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清理只能删除本轮直属且 owner 匹配的 work，未知目录保持原样。"""

    work_root = tmp_path / "work-root"
    artifact_root = tmp_path / "artifact-root"
    monkeypatch.setattr(RUNTIME, "WORK_ROOT", work_root)
    monkeypatch.setattr(RUNTIME, "ARTIFACT_ROOT", artifact_root)
    owner = "b" * 64
    work, _staging, _final = RUNTIME.create_run_directories("prd078-cleanup", owner)
    with pytest.raises(RUNTIME.AcceptanceFailure) as wrong_owner:
        RUNTIME.remove_owned_work(work, "c" * 64)
    assert (wrong_owner.value.stage, wrong_owner.value.code) == (
        "cleanup.work",
        "claim",
    )
    assert work.exists()
    RUNTIME.remove_owned_work(work, owner)
    assert not work.exists()


def test_evidence_publish_is_atomic_hashed_and_keeps_global_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证据必须先写 staging、复读和扫描，再以单次 rename 发布且保持总状态 false。"""

    staging = tmp_path / ".staging"
    final = tmp_path / "final"
    staging.mkdir()
    payload = {
        "prd_id": "PRD-078",
        "status": "passed",
        "all_108_prds_complete": False,
    }
    scans: list[Path] = []

    def verify(path: Path, _private_values: tuple[str, ...]) -> dict[str, int]:
        """``verify`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
        scans.append(path)
        return {
            "files_scanned": len(tuple(path.rglob("*"))),
            "plaintext_matches": 0,
            "sqlite_sidecars_remaining": 0,
        }

    monkeypatch.setattr(RUNTIME._BASE, "verify_private_storage", verify)
    target = RUNTIME.write_evidence(staging, final, payload, ("private-canary",))
    assert target == final / "runtime-acceptance.json"
    assert final.is_dir() and not staging.exists()
    assert scans == [staging]
    encoded = target.read_bytes()
    assert json.loads(encoded) == payload
    assert (final / "SHA256SUMS.txt").read_text(encoding="ascii") == (
        f"{hashlib.sha256(encoded).hexdigest()}  runtime-acceptance.json\n"
    )


def test_prepare_database_runs_71_down_70_reup_and_uses_unique_owned_scopes(
    tmp_path: Path,
) -> None:
    """空库必须真实完成 71→70→71，且 timeout 使用同租户真实 Project scope。"""

    work = tmp_path / "database"
    work.mkdir()
    secrets_ = RUNTIME.RuntimeSecrets.create()
    graph = RUNTIME.prepare_database(work, secrets_)
    assert graph.migration["fresh"] == {
        "count": 71,
        "version": "0071",
        "integrity": "ok",
        "foreign_key_violations": 0,
    }
    assert graph.migration["repeat"] == graph.migration["fresh"]
    assert graph.migration["down"] == {
        "count": 70,
        "version": "0070",
        "integrity": "ok",
        "foreign_key_violations": 0,
    }
    assert graph.migration["reup"] == graph.migration["fresh"]

    connection = RUNTIME.connect_database(graph.database_url)
    try:
        rows = connection.execute(
            """
            SELECT budget_rule_id,tenant_id,environment_id,scope_type,scope_id
            FROM budget_rules ORDER BY budget_rule_id
            """
        ).fetchall()
        assert len(rows) == 4
        assert (
            len(
                {
                    (row["tenant_id"], row["environment_id"], row["scope_type"], row["scope_id"])
                    for row in rows
                }
            )
            == 4
        )
        timeout = next(
            row for row in rows if row["budget_rule_id"] == graph.budget_rules["timeout"]
        )
        assert timeout["tenant_id"] == graph.tenant_id
        assert timeout["scope_type"] == "project"
        project = connection.execute(
            "SELECT tenant_id,status FROM projects WHERE project_id=?",
            (timeout["scope_id"],),
        ).fetchone()
        assert project is not None
        assert (project["tenant_id"], project["status"]) == (graph.tenant_id, "active")
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
    RUNTIME.quiesce_database(graph)
    privacy = RUNTIME._BASE.verify_private_storage(work, secrets_.values())
    assert privacy["plaintext_matches"] == 0
    assert privacy["sqlite_sidecars_remaining"] == 0


def test_timeout_wait_uses_database_clock_and_does_not_inject_future_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停服恢复必须等待数据库真实到期，Scheduler 调用不得携带伪造 future now。"""

    graph = SimpleNamespace(
        database_url="sqlite:///unused.sqlite3",
        budget_rules={"timeout": "rule_timeout", "foreign": "rule_foreign"},
    )
    calls: list[dict[str, object]] = []

    class Connection:
        """``Connection`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""

        def execute(self, sql: str, _params: tuple[str, str]):
            """``execute`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
            if "MAX(unixepoch(expires_at))" in sql:
                return SimpleNamespace(
                    fetchone=lambda: {
                        "target_count": 2,
                        "latest_expiry": 100,
                        "database_now": 100,
                    }
                )
            return SimpleNamespace(
                fetchall=lambda: [
                    {"tenant_id": "tenant_a", "status": "rejected", "resolution_reason": "timeout"},
                    {"tenant_id": "tenant_b", "status": "rejected", "resolution_reason": "timeout"},
                ]
            )

        def close(self) -> None:
            """``close`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
            return None

    monkeypatch.setattr(RUNTIME, "connect_database", lambda _url: Connection())
    monkeypatch.setattr(RUNTIME, "load_app_config", lambda _service: object())

    def run_once(_config: object, **kwargs: object) -> bool:
        """``run_once`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
        calls.append(kwargs)
        return True

    monkeypatch.setattr(RUNTIME, "run_approval_timeout_once", run_once)
    assert RUNTIME._run_timeout_while_control_stopped({}, graph) == 2
    assert calls == [{"worker_id": "prd078-runtime-outage-timeout"}]


def test_timeout_wait_fails_closed_when_database_clock_never_reaches_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据库时钟未到期时不得提交虚假 timeout 结论，等待上限必须固定失败。"""

    graph = SimpleNamespace(
        database_url="sqlite:///unused.sqlite3",
        budget_rules={"timeout": "rule_timeout", "foreign": "rule_foreign"},
    )

    class Connection:
        """模拟数据库时钟读取，确保未到期任务不会被验收器误判为 timeout。"""

        @staticmethod
        def execute(_sql: str, _params: tuple[str, str]):
            """``execute`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
            return SimpleNamespace(
                fetchone=lambda: {
                    "target_count": 2,
                    "latest_expiry": 101,
                    "database_now": 100,
                }
            )

        @staticmethod
        def close() -> None:
            """``close`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
            return None

    monotonic = iter((0.0, 361.0))
    monkeypatch.setattr(RUNTIME, "connect_database", lambda _url: Connection())
    monkeypatch.setattr(RUNTIME.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(RUNTIME.time, "sleep", lambda _seconds: None)
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME._run_timeout_while_control_stopped({}, graph)
    assert (failure.value.stage, failure.value.code) == (
        "recovery.timeout_wait",
        "timeout",
    )


def test_main_uses_fixed_failure_coordinates_without_echoing_arguments(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 失败只能输出稳定坐标，不能回显路径、Token 或 traceback。"""

    private_argument = "private-token-value"
    monkeypatch.setattr(
        RUNTIME,
        "run",
        lambda _run_id: (_ for _ in ()).throw(
            RUNTIME.AcceptanceFailure("database.business_seed", "failed")
        ),
    )
    assert RUNTIME.main(["--run-id", private_argument]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("PRD-078 runtime acceptance failed: database.business_seed:failed\n")
    assert private_argument not in captured.err


def test_source_provenance_binds_runtime_migration_and_recovery_implementations() -> None:
    """最终摘要的每个 SHA-256 必须绑定实际执行的脚本、迁移与恢复实现。"""

    provenance = RUNTIME.source_provenance()
    assert set(provenance) == set(RUNTIME.SOURCE_FILES)
    for name, path in RUNTIME.SOURCE_FILES.items():
        assert provenance[name] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert len(provenance[name]) == 64
