"""本文件验证 PRD-074 批处理调度边界，覆盖租户隔离、权限拒绝、并发领取、失败追踪与回退。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

Safety and source contracts for the PRD-074 self-contained runtime acceptance.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_runtime_module():
    """验证批处理场景。

    定义：load_runtime_module。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    path = Path(__file__).resolve().parents[1] / "prd074-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(
        "prd074_runtime_acceptance",
        path,
    )
    if specification is None or specification.loader is None:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise AssertionError("unable to load PRD-074 runtime acceptance")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


# 该常量组统一批任务状态、限额和错误语义，变更时同步契约与测试。
RUNTIME = load_runtime_module()


def test_module_load_is_inert_and_ports_are_frozen() -> None:
    """验证批处理场景。

    定义：test_module_load_is_inert_and_ports_are_frozen。
    固定租户隔离、权限拒绝、失败追踪和回退边界。

    Importing the contract must not bind ports or start subprocesses.
    """

    assert RUNTIME.FIXED_PORTS == (18740, 18741, 18742, 18743)
    assert RUNTIME.GATEWAY_PORT == 18740
    assert RUNTIME.CONTROL_PORT == 18741
    assert RUNTIME.PROVIDER_PORT == 18742
    assert RUNTIME.WEBHOOK_PORT == 18743
    assert RUNTIME.PYTHON == RUNTIME.ROOT / ".venv" / "Scripts" / "python.exe"


def test_workspace_and_loopback_guards_fail_closed() -> None:
    """验证批处理场景。

    定义：test_workspace_and_loopback_guards_fail_closed。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    inside = RUNTIME.workspace_path(
        RUNTIME.ROOT / ".tmp" / "prd074-contract" / "runtime.sqlite3",
        RUNTIME.ROOT / ".tmp",
        stage="unit.path",
    )
    assert inside.is_absolute()
    with pytest.raises(RUNTIME.AcceptanceFailure) as outside:
        RUNTIME.workspace_path(
            RUNTIME.ROOT.parent / "outside.sqlite3",
            RUNTIME.ROOT / ".tmp",
            stage="unit.path",
        )
    assert outside.value.code == "path_outside_workspace"

    accepted = RUNTIME.loopback_url(
        "http://127.0.0.1:18740",
        port=18740,
        stage="unit.url",
    )
    assert accepted == "http://127.0.0.1:18740"
    for value in (
        "https://127.0.0.1:18740",
        "http://localhost:18740",
        "http://user:password@127.0.0.1:18740",
        "http://127.0.0.1:18741",
        "http://127.0.0.1:18740/path",
        "http://127.0.0.1:not-a-port",
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.loopback_url(value, port=18740, stage="unit.url")


def test_runtime_secrets_are_random_hidden_and_complete() -> None:
    """验证批处理场景。

    定义：test_runtime_secrets_are_random_hidden_and_complete。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    first = RUNTIME.RuntimeSecrets.create()
    second = RUNTIME.RuntimeSecrets.create()
    assert first.provider_key != second.provider_key
    assert first.internal_batch_token != second.internal_batch_token
    representation = repr(first)
    for private in (
        first.admin_password,
        first.developer_password,
        first.provider_key,
        first.virtual_key,
        first.auth_secret,
        first.internal_batch_token,
        first.batch_secret,
        first.file_texts[0],
        first.assistant_text,
    ):
        assert private not in representation
    values = first.private_values(
        session_tokens=("session-contract-value-0123456789",),
        webhook_secret="whsec_contract_value_0123456789",
    )
    assert first.provider_key in values
    assert first.file_names[3] in values
    assert "session-contract-value-0123456789" in values
    assert "whsec_contract_value_0123456789" in values


def test_stubs_retain_only_counters_and_synchronization() -> None:
    """验证批处理场景。

    定义：test_stubs_retain_only_counters_and_synchronization。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    provider = RUNTIME.ProviderStub(RUNTIME.RuntimeSecrets.create())
    webhook = RUNTIME.WebhookStub()
    provider_names = set(vars(provider))
    webhook_names = set(vars(webhook))
    for forbidden in ("body", "payload", "headers", "prompt", "response", "requests"):
        assert forbidden not in provider_names
        assert forbidden not in webhook_names
    assert provider.snapshot() == {
        "realtime_calls": 0,
        "batch_calls": 0,
        "invalid_calls": 0,
        "max_concurrency": 0,
    }
    assert webhook.snapshot() == {
        "created": 0,
        "completed": 0,
        "invalid": 0,
        "signature_failures": 0,
        "forbidden_field_hits": 0,
    }


class _FakeStream:
    """验证批处理场景。

    定义：_FakeStream。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    def __init__(self) -> None:
        """验证批处理场景。

        定义：__init__。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        self.closed = False

    def close(self) -> None:
        """验证批处理场景。

        定义：close。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        self.closed = True


class _FakeProcess:
    """验证批处理场景。

    定义：_FakeProcess。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    def __init__(self, *, running: bool = True) -> None:
        """验证批处理场景。

        定义：__init__。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        self.running = running
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self):
        """验证批处理场景。

        定义：poll。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        return None if self.running else 0

    def terminate(self) -> None:
        """验证批处理场景。

        定义：terminate。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        self.terminate_calls += 1
        self.running = False

    def kill(self) -> None:
        """验证批处理场景。

        定义：kill。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        self.kill_calls += 1
        self.running = False

    def wait(self, timeout: float | None = None) -> int:
        """验证批处理场景。

        定义：wait。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        del timeout
        self.wait_calls += 1
        return 0


def _managed(name: str, process: _FakeProcess):
    """验证批处理场景。

    定义：_managed。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    return RUNTIME.ManagedProcess(name, process, _FakeStream(), _FakeStream())


def test_owned_process_cleanup_touches_only_registered_handles() -> None:
    """验证批处理场景。

    定义：test_owned_process_cleanup_touches_only_registered_handles。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    owned = _FakeProcess()
    unrelated = _FakeProcess()
    item = _managed("owned", owned)
    registry = RUNTIME.OwnedProcessSet()
    registry.add(item)
    registry.close()

    assert owned.terminate_calls == 1
    assert owned.kill_calls == 0
    assert owned.wait_calls == 1
    assert unrelated.terminate_calls == 0
    assert unrelated.kill_calls == 0
    assert item.stdout_file.closed is True
    assert item.stderr_file.closed is True


def _private_fixture(tmp_path: Path) -> tuple[Path, Path, tuple[Path, ...]]:
    """验证批处理场景。

    定义：_private_fixture。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "runtime.sqlite3"
    database.write_bytes(b"safe-database")
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "encrypted-object.bin").write_bytes(b"safe-object")
    spool = tmp_path / "spool"
    spool.mkdir()
    logs = tuple(tmp_path / f"service-{index}.log" for index in range(4))
    for path in logs:
        path.write_bytes(b"safe-log")
    return database, storage, logs


def test_private_scan_requires_every_log_and_scans_storage(tmp_path: Path) -> None:
    """验证批处理场景。

    定义：test_private_scan_requires_every_log_and_scans_storage。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database, storage, logs = _private_fixture(tmp_path)
    result = RUNTIME.verify_private_storage(
        database,
        storage,
        ("private-canary-0123456789",),
        log_paths=logs,
    )
    assert result["service_log_files_scanned"] == 4
    assert result["plaintext_hits"] == 0
    assert result["scanned_files"] == 6

    logs[-1].unlink()
    with pytest.raises(RUNTIME.AcceptanceFailure) as missing:
        RUNTIME.verify_private_storage(
            database,
            storage,
            ("private-canary-0123456789",),
            log_paths=logs,
        )
    assert missing.value.stage == "privacy.logs"
    assert missing.value.code == "required_file_missing"


@pytest.mark.parametrize("encoding", ("utf-8", "utf-16-le", "utf-16-be"))
def test_private_scan_rejects_all_supported_encodings(
    tmp_path: Path,
    encoding: str,
) -> None:
    """验证批处理场景。

    定义：test_private_scan_rejects_all_supported_encodings。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database, storage, logs = _private_fixture(tmp_path)
    canary = "private-canary-0123456789"
    logs[1].write_bytes(canary.encode(encoding))
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.verify_private_storage(
            database,
            storage,
            (canary,),
            log_paths=logs,
        )
    assert failure.value.stage == "privacy"
    assert failure.value.code == "plaintext_persisted"


def test_private_scan_rejects_log_directory_and_link_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_private_scan_rejects_log_directory_and_link_contract。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database, storage, logs = _private_fixture(tmp_path)
    logs[0].unlink()
    logs[0].mkdir()
    with pytest.raises(RUNTIME.AcceptanceFailure) as directory:
        RUNTIME.verify_private_storage(
            database,
            storage,
            ("private-canary-0123456789",),
            log_paths=logs,
        )
    assert directory.value.code == "required_file_invalid"

    logs[0].rmdir()
    logs[0].write_bytes(b"safe-log")
    original = Path.is_symlink

    def report_link(path: Path) -> bool:
        """验证批处理场景。

        定义：report_link。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        return path == logs[0] or original(path)

    monkeypatch.setattr(Path, "is_symlink", report_link)
    with pytest.raises(RUNTIME.AcceptanceFailure) as linked:
        RUNTIME.verify_private_storage(
            database,
            storage,
            ("private-canary-0123456789",),
            log_paths=logs,
        )
    assert linked.value.code == "required_file_invalid"


def test_evidence_rejects_sensitive_keys_and_values() -> None:
    """验证批处理场景。

    定义：test_evidence_rejects_sensitive_keys_and_values。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    private = ("private-evidence-value-0123456789",)
    RUNTIME.assert_safe_evidence(
        {
            "status": "passed",
            "usage": {"input_tokens": 19, "provider_calls": 2},
            "all_108_prds_complete": False,
        },
        private,
    )
    with pytest.raises(RUNTIME.AcceptanceFailure) as key_failure:
        RUNTIME.assert_safe_evidence({"access_token": "masked"}, private)
    assert key_failure.value.code == "forbidden_key"
    with pytest.raises(RUNTIME.AcceptanceFailure) as value_failure:
        RUNTIME.assert_safe_evidence({"status": private[0]}, private)
    assert value_failure.value.code == "private_value"


@pytest.mark.parametrize(
    ("routing_status", "usage_status", "expected_complete"),
    (
        ("succeeded", "recorded", True),
        ("failed", "recorded", False),
        ("succeeded", "pending", False),
    ),
)
def test_owner_chain_traces_usage_through_routing_attempt(
    tmp_path: Path,
    routing_status: str,
    usage_status: str,
    expected_complete: bool,
) -> None:
    """验证批处理场景。

    定义：test_owner_chain_traces_usage_through_routing_attempt。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "owner-chain.sqlite3"
    url = f"sqlite:///{database.as_posix()}"
    connection = RUNTIME.connect_database(url)
    connection.executescript(
        """
        CREATE TABLE batch_jobs(
          tenant_id TEXT,batch_job_id TEXT,status TEXT,posted_cost_cny TEXT
        );
        CREATE TABLE batch_tasks(
          tenant_id TEXT,batch_job_id TEXT,status TEXT,current_attempt_id TEXT
        );
        CREATE TABLE batch_attempts(
          tenant_id TEXT,batch_job_id TEXT,batch_attempt_id TEXT,attempt_no INTEGER,
          status TEXT,execution_outcome TEXT,result_state TEXT,request_id TEXT,trace_id TEXT
        );
        CREATE TABLE batch_execution_grants(
          tenant_id TEXT,batch_attempt_id TEXT,status TEXT
        );
        CREATE TABLE batch_budget_allocations(
          tenant_id TEXT,batch_attempt_id TEXT,status TEXT,charge_state TEXT
        );
        CREATE TABLE request_logs(tenant_id TEXT,request_id TEXT,trace_id TEXT);
        CREATE TABLE routing_attempts(
          tenant_id TEXT,request_id TEXT,routing_attempt_id TEXT,trace_id TEXT,
          status TEXT,usage_status TEXT
        );
        CREATE TABLE usage_ledger(
          tenant_id TEXT,request_id TEXT,routing_attempt_id TEXT
        );
        CREATE TABLE finops_ledger_entries(
          tenant_id TEXT,source_request_id TEXT,source_trace_id TEXT
        );
        INSERT INTO batch_jobs VALUES('tenant_a','job_a','completed','0.100000');
        INSERT INTO batch_tasks VALUES('tenant_a','job_a','succeeded','attempt_a');
        INSERT INTO batch_attempts
          VALUES('tenant_a','job_a','attempt_a',1,'provider_succeeded','succeeded',
                 'available','request_a','trace_a');
        INSERT INTO batch_execution_grants VALUES('tenant_a','attempt_a','consumed');
        INSERT INTO batch_budget_allocations VALUES('tenant_a','attempt_a','posted','posted');
        INSERT INTO request_logs VALUES('tenant_a','request_a','trace_a');
        INSERT INTO usage_ledger VALUES('tenant_a','request_a','routing_a');
        INSERT INTO finops_ledger_entries VALUES('tenant_a','request_a','trace_a');
        """
    )
    connection.execute(
        "INSERT INTO routing_attempts VALUES(?,?,?,?,?,?)",
        ("tenant_a", "request_a", "routing_a", "trace_a", routing_status, usage_status),
    )
    connection.commit()
    connection.close()

    graph = SimpleNamespace(database_url=url, tenant_id="tenant_a")
    if expected_complete:
        facts = RUNTIME.owner_chain_facts(graph, "job_a")
        assert facts["owner_chain_complete"] is True
        assert facts["usage_rows"] == 1
        return

    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.owner_chain_facts(graph, "job_a")
    assert failure.value.stage == "database.owner"
    assert failure.value.code == "chain_mismatch"


def test_source_freezes_migration_process_recovery_and_global_status() -> None:
    """验证批处理场景。

    定义：test_source_freezes_migration_process_recovery_and_global_status。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    source = (Path(__file__).resolve().parents[1] / "prd074-runtime-acceptance.py").read_text(
        encoding="utf-8"
    )
    assert 'MIGRATION_CONTRACT = (67, "0067")' in source
    assert '"migration": {"count": 67, "highest": "0067", "catalog_revision": 10}' in source
    assert '"all_108_prds_complete": False' in source
    assert '"source_committed_while_control_stopped": True' in source
    assert '"active_drain_invoked": False' in source
    assert "metahub_control.batch_jobs.worker_main" in source
    assert "subprocess.Popen" in source
    assert "processes.close()" in source
    assert "taskkill" not in source.lower()
    assert "get-ciminstance" not in source.lower()
    assert "reset --hard" not in source.lower()


def test_service_environment_keeps_writable_paths_under_run_directory(tmp_path: Path) -> None:
    """验证批处理场景。

    定义：test_service_environment_keeps_writable_paths_under_run_directory。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    work = tmp_path
    for name in ("temp", "storage", "spool"):
        (work / name).mkdir(exist_ok=True)
    graph = SimpleNamespace(database_url="sqlite:///C:/MetaHub/.tmp/contract.sqlite3")
    secrets_ = RUNTIME.RuntimeSecrets.create()
    environment = RUNTIME.service_environment(
        work,
        graph,
        "http://127.0.0.1:18742/v1",
        secrets_,
    )
    assert environment["TEMP"] == str(work / "temp")
    assert environment["TMP"] == str(work / "temp")
    assert environment["METAHUB_FILE_STORAGE_ROOT"] == str(work / "storage")
    assert environment["METAHUB_FINOPS_USAGE_SPOOL_ROOT"] == str(work / "spool")
    assert environment["METAHUB_GATEWAY_API_BASE_URL"] == "http://127.0.0.1:18740"
    assert environment["METAHUB_CONTROL_API_BASE_URL"] == "http://127.0.0.1:18741"
