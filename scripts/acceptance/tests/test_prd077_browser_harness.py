"""验证 PRD-077 Browser harness 的资源所有权、隐私 seed 与失败清理合同。

测试只使用仓库内临时目录和固定故障，不启动未受控外部网络，也不输出私有值。
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

# 模块路径与 owner nonce 固定在仓库测试边界，不能由被测浏览器输入覆盖。
ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "scripts" / "acceptance" / "prd077-browser-harness.py"


def _load_harness():
    """按稳定模块名加载带连字符的验收器，导入阶段不启动任何服务。"""

    name = "_metahub_prd077_browser_harness_tests"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    specification = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


# 模块实例与 owner nonce 只服务本轮资源归属验证，不能接受浏览器输入覆盖。
HARNESS = _load_harness()
OWNER_NONCE = "ab" * 32


def _secrets(answer_canary: str = "12345.678901"):
    """构造只存在测试内存中的定长私有值，失败断言不输出其正文。"""

    private_values = tuple(f"prd077-private-{index:02d}-" + "x" * 24 for index in range(13))
    # 私有值只封装到 repr-safe secrets 对象，测试失败不得打印字段正文。
    return HARNESS.BrowserSecrets(*private_values, answer_canary)


def test_browser_secrets_from_environment_owns_all_private_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Node 传入的十四个固定环境键必须全部进入 repr-safe 扫描 owner。"""

    required_service_keys = {
        "PRD077_BROWSER_FILE_ENCRYPTION_SECRET",
        "PRD077_BROWSER_BATCH_ENCRYPTION_SECRET",
        "PRD077_BROWSER_PROMPT_TEMPLATE_ENCRYPTION_SECRET",
        "PRD077_BROWSER_CACHE_AFFINITY_HMAC_SECRET",
    }
    if len(HARNESS.PRIVATE_ENV_KEYS) != 14 or not required_service_keys.issubset(
        HARNESS.PRIVATE_ENV_KEYS
    ):
        pytest.fail("browser private environment contract incomplete")
    for index, name in enumerate(HARNESS.PRIVATE_ENV_KEYS):
        value = (
            "12345.678901"
            if name == "PRD077_BROWSER_ANSWER_CANARY"
            else f"prd077-environment-{index:02d}-" + "x" * 24
        )
        monkeypatch.setenv(name, value)
    secrets_ = HARNESS.BrowserSecrets.from_environment()
    scan_values = secrets_.scan_values()
    if len(scan_values) != 14 or any(
        os.environ[name] not in scan_values for name in HARNESS.PRIVATE_ENV_KEYS
    ):
        pytest.fail("browser private environment owner missing")
    # 强类型 Secret 的 repr 只能显示字段形态，任何私有值回显都视为验收器泄漏。
    if any(value in repr(secrets_) for value in scan_values):
        pytest.fail("browser private environment repr exposure")


@pytest.mark.parametrize(
    "field_name",
    (
        "file_encryption_secret",
        "batch_encryption_secret",
        "prompt_template_encryption_secret",
        "cache_affinity_hmac_secret",
    ),
)
def test_browser_service_secret_maps_to_runtime_and_leak_fails_closed(
    tmp_path: Path,
    field_name: str,
) -> None:
    """Browser 启动的四个服务 Secret 必须进入 Runtime owner 和最终扫描集。"""

    secrets_ = _secrets()
    runtime_secrets = secrets_.runtime()
    private_value = getattr(secrets_, field_name)
    # Browser owner 到 Runtime owner 的字段映射必须保持同一私有值，不能遗漏服务配置。
    if getattr(runtime_secrets, field_name) != private_value:
        pytest.fail("browser service secret runtime mapping missing")
    scan_values = secrets_.scan_values()
    if private_value not in scan_values or len(scan_values) != 14:
        pytest.fail("browser service secret privacy owner missing")
    # 两层 Secret 对象都必须阻断 repr 明文，避免失败断言把配置写入测试输出。
    if private_value in repr(secrets_) or private_value in repr(runtime_secrets):
        pytest.fail("browser service secret repr exposure")

    (tmp_path / "browser-service.log").write_bytes(private_value.encode("utf-16-be"))
    with pytest.raises(HARNESS.AcceptanceFailure) as failure:
        HARNESS.RUNTIME.verify_private_storage(tmp_path, scan_values)
    assert (failure.value.stage, failure.value.code) == (
        "privacy.scan",
        "plaintext_found",
    )


class _Response:
    """提供 harness 撤权流程所需的最小 HTTP 响应与安全头表面。"""

    def __init__(self, status_code: int, payload: dict[str, object], headers=None) -> None:
        """冻结状态、公开 payload 与安全响应头，不保存请求凭据。"""

        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> dict[str, object]:
        """返回预先冻结的公开测试信封。"""

        return self._payload


def test_revoke_uses_role_api_and_finance_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """撤权必须经 admin CAS API 完成，并以 finance Session 证明真实后端 403。"""

    original_codes = ["admin_assistant.query", "billing.usage.read", "dashboard.read"]
    replacement = ["admin_assistant.query", "dashboard.read"]
    calls: list[tuple[str, str]] = []

    class Client:
        """拒绝任何未冻结路径，并只记录方法与路径而不记录凭据或正文。"""

        def __init__(self, **_kwargs) -> None:
            """接受生产客户端构造参数但不保存其中可能存在的敏感配置。"""

            pass

        def __enter__(self):
            """返回当前内存客户端，模拟受控上下文管理器。"""

            return self

        def __exit__(self, *_args) -> None:
            """结束模拟会话且不吞并被测逻辑抛出的异常。"""

            return None

        def get(self, path: str, **_kwargs):
            """返回冻结角色矩阵，只记录不含凭据的 HTTP 方法与路径。"""

            calls.append(("GET", path))
            return _Response(
                200,
                {
                    "roles": [
                        {
                            "role_id": "role_finance",
                            "code": "finance_admin",
                            "permission_revision": 13,
                            "permission_codes": original_codes,
                        }
                    ]
                },
            )

        def patch(self, path: str, *, json: dict[str, object], **_kwargs):
            """验证精确 revision CAS body，并返回撤权后的冻结角色。"""

            calls.append(("PATCH", path))
            assert json == {"expected_revision": 13, "permission_codes": replacement}
            return _Response(
                200,
                {
                    "changed": True,
                    "role": {
                        "code": "finance_admin",
                        "permission_codes": replacement,
                    },
                },
            )

        def post(self, path: str, **_kwargs):
            """模拟 finance 撤权后的统一后端 403 与私有响应头。"""

            calls.append(("POST", path))
            return _Response(
                403,
                {
                    "code": "auth.permission_denied",
                    "request_id": "req_prd077_browser_backend_denied",
                    "trace_id": "trc_prd077_browser_backend_denied",
                },
                {
                    "x-request-id": "req_prd077_browser_backend_denied",
                    "x-trace-id": "trc_prd077_browser_backend_denied",
                    "cache-control": "no-store, private",
                    "pragma": "no-cache",
                    "vary": "Authorization, Origin",
                },
            )

    def login(_client, _slug, email, _password, _suffix):
        """按固定测试身份返回不同内存 Token，不读取密码正文。"""

        return (
            "token-prd077-admin-session"
            if email == "admin@metahub.local"
            else "token-prd077-finance-session"
        )

    monkeypatch.setattr(HARNESS.httpx, "Client", Client)
    monkeypatch.setattr(HARNESS.RUNTIME, "login", login)
    harness = HARNESS.BrowserHarness("prd077-harness-api-01", _secrets(), OWNER_NONCE)
    harness.graph = SimpleNamespace(database_url="sqlite:///unused", tenant_id="tenant")

    harness.revoke_admin_assistant()

    assert calls == [
        ("GET", "/permissions"),
        ("PATCH", "/roles/role_finance/permissions"),
        ("POST", "/admin-assistant/query"),
    ]
    assert harness.revoked is True
    assert harness.base_permission_retained is True
    assert harness.backend_denied is True
    assert len(harness.runtime_tokens) == 2


def test_quiesce_stops_owned_services_once_and_keeps_harness_alive() -> None:
    """稳定点只关闭本轮服务 owner；重复控制请求必须幂等。"""

    events: list[str] = []
    started = threading.Event()
    release = threading.Event()
    harness = HARNESS.BrowserHarness("prd077-harness-quiesce-01", _secrets(), OWNER_NONCE)

    def close_processes() -> None:
        """模拟精确关闭本轮直接子进程，并用事件闩锁放大并发幂等窗口。"""

        events.append("processes")
        started.set()
        assert release.wait(timeout=2)

    harness.processes = SimpleNamespace(close=close_processes)

    class Job:
        """记录 Windows Job 必须先排空进程树、再关闭唯一句柄的调用顺序。"""

        def terminate_and_wait(self, timeout_seconds: float) -> int:
            """校验排空预算并记录 Job 进程树已在关闭句柄前归零。"""

            assert timeout_seconds == HARNESS.OWNER_DRAIN_TIMEOUT_SECONDS
            events.append("job_drain")
            return 0

        def close(self) -> None:
            """记录 Job 句柄关闭动作，供测试断言完整 owner 回收顺序。"""

            events.append("job_close")

    harness.process_job = Job()

    def quiesce_database(*, stage: str) -> None:
        """断言所有服务 owner 已排空后才允许把 SQLite 标记为静止。"""

        assert stage == "quiesce.sqlite"
        assert harness.service_owners_drained is True
        events.append("database")
        harness.database_quiesced = True

    harness._quiesce_database = quiesce_database

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(harness.quiesce_services)
        assert started.wait(timeout=2)
        second = executor.submit(harness.quiesce_services)
        release.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert events == ["processes", "job_drain", "job_close", "database"]
    assert harness.process_job is None
    assert harness.quiesced is True
    assert harness.owned_processes_remaining == 0
    assert harness.stop_event.is_set() is False


def _prepare_cleanup_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    run_id: str,
):
    """把 harness 的清理边界限定到当前仓库内 pytest basetemp。"""

    work_root = tmp_path / "work"
    artifact_root = tmp_path / "artifact"
    work_root.mkdir()
    artifact_root.mkdir()
    monkeypatch.setattr(HARNESS, "WORK_ROOT", work_root)
    monkeypatch.setattr(HARNESS, "ARTIFACT_ROOT", artifact_root)
    harness = HARNESS.BrowserHarness(run_id, _secrets(), OWNER_NONCE)
    harness.work.mkdir()
    harness.work_created = True
    harness.staging.mkdir()
    harness.staging_created = True
    harness.work_identity = HARNESS._capture_directory_identity(
        harness.work,
        work_root,
        stage="test.work_identity",
    )
    harness.staging_identity = HARNESS._capture_directory_identity(
        harness.staging,
        artifact_root,
        stage="test.staging_identity",
    )
    HARNESS._write_owner_claim(harness.work, harness.owner_claim_digest)
    HARNESS._write_owner_claim(harness.staging, harness.owner_claim_digest)
    database = harness.work / "business.sqlite3"
    sqlite3.connect(database).close()
    harness.database_identity = HARNESS.RUNTIME.regular_file_identity(
        database,
        stage="test.database_identity",
    )
    harness.graph = SimpleNamespace(
        database=database,
        database_url=f"sqlite:///{database.as_posix()}",
    )

    def quiesce_database(*, stage: str) -> None:
        """为清理夹具模拟已验证的 SQLite 静止点，并检查 owner 排空前置条件。"""

        assert stage in {"cleanup.sqlite", "quiesce.sqlite"}
        assert harness.service_owners_drained is True
        harness.database_quiesced = True

    monkeypatch.setattr(harness, "_quiesce_database", quiesce_database)

    def remove_owned(
        path: Path,
        boundary: Path,
        *,
        stage: str,
        expected_identity: tuple[int, int],
        owner_claim: str,
        claim_filename: str,
        retry_attempts: int,
        retry_delay_seconds: float,
    ) -> None:
        """仅删除测试刚创建的精确子目录，并验证调用方传入预期边界。"""

        assert path.parent == boundary
        assert stage in {
            "cleanup.work",
            "cleanup.staging",
            "cleanup.artifact",
        }
        assert expected_identity in {
            (harness.work_identity.device, harness.work_identity.inode),
            (harness.staging_identity.device, harness.staging_identity.inode),
        }
        assert owner_claim == harness.owner_claim_digest
        assert claim_filename == HARNESS.OWNER_CLAIM_FILENAME
        assert retry_attempts == HARNESS.CLEANUP_RETRY_ATTEMPTS
        assert retry_delay_seconds == HARNESS.CLEANUP_RETRY_DELAY_SECONDS
        shutil.rmtree(path)

    monkeypatch.setattr(HARNESS.RUNTIME, "remove_owned_directory", remove_owned)
    monkeypatch.setattr(
        HARNESS.RUNTIME,
        "database_health",
        lambda *_args, **_kwargs: {
            "latest_migration": "0070",
            "migration_count": 70,
            "integrity_check": "ok",
            "foreign_key_violations": 0,
        },
    )
    return harness


def test_cleanup_refuses_passed_summary_without_revoke_and_backend_denial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """单独 stop 不得把未执行动态撤权的 Browser 运行伪装为通过。"""

    harness = _prepare_cleanup_harness(
        monkeypatch,
        tmp_path,
        "prd077-harness-missing-01",
    )
    harness.quiesced = True
    monkeypatch.setattr(
        HARNESS.RUNTIME,
        "verify_private_storage",
        lambda *_args, **_kwargs: pytest.fail("privacy scan must follow revoke gate"),
    )

    with pytest.raises(HARNESS.AcceptanceFailure) as captured:
        harness.cleanup(success=True)

    assert (captured.value.stage, captured.value.code) == ("cleanup.revoke", "missing")
    assert not harness.work.exists()
    assert not harness.staging.exists()
    assert not harness.final_artifact.exists()


def test_cleanup_refuses_passed_summary_without_quiesce(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """未先停止服务 owner 时不得扫描活目录或发布 passed 摘要。"""

    harness = _prepare_cleanup_harness(
        monkeypatch,
        tmp_path,
        "prd077-harness-missing-quiesce-01",
    )
    harness.revoked = True
    harness.base_permission_retained = True
    harness.backend_denied = True
    monkeypatch.setattr(
        HARNESS.RUNTIME,
        "verify_private_storage",
        lambda *_args, **_kwargs: pytest.fail("privacy scan must follow quiesce gate"),
    )

    with pytest.raises(HARNESS.AcceptanceFailure) as captured:
        harness.cleanup(success=True)

    assert (captured.value.stage, captured.value.code) == ("cleanup.quiesce", "missing")
    assert not harness.work.exists()
    assert not harness.staging.exists()
    assert not harness.final_artifact.exists()


def test_cleanup_preserves_fixed_validation_failure_after_owned_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证失败必须保留固定坐标，同时仍删除 work 与 staging。"""

    harness = _prepare_cleanup_harness(
        monkeypatch,
        tmp_path,
        "prd077-harness-validation-failure-01",
    )
    harness.revoked = True
    harness.base_permission_retained = True
    harness.backend_denied = True
    harness.quiesced = True

    def fail_privacy(*_args, **_kwargs):
        """注入固定隐私扫描失败，验证清理保留首个标准错误坐标。"""

        raise HARNESS.AcceptanceFailure("cleanup.sqlite_sidecars", "remaining")

    monkeypatch.setattr(HARNESS.RUNTIME, "verify_private_storage", fail_privacy)
    with pytest.raises(HARNESS.AcceptanceFailure) as captured:
        harness.cleanup(success=True)

    assert (captured.value.stage, captured.value.code) == (
        "cleanup.sqlite_sidecars",
        "remaining",
    )
    assert not harness.work.exists()
    assert not harness.staging.exists()


def test_runtime_database_health_uses_browser_summary_schema(tmp_path: Path) -> None:
    """真实 Runtime helper 的字段必须与 Browser runtime-summary 合同一致。"""

    database = tmp_path / "health.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,status TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO schema_migrations(version,status) VALUES(?, 'applied')",
            [(f"{index:04d}",) for index in range(1, 71)],
        )
        connection.commit()
    finally:
        connection.close()
    health = HARNESS.RUNTIME.database_health(
        f"sqlite:///{database.as_posix()}",
        head="0070",
        count=70,
        stage="test.database",
    )
    assert health == {
        "migration_count": 70,
        "latest_migration": "0070",
        "integrity_check": "ok",
        "foreign_key_violations": 0,
    }
    assert HARNESS.CLEANUP_RETRY_ATTEMPTS == 150
    assert HARNESS.CLEANUP_RETRY_DELAY_SECONDS == 0.2


def test_answer_canary_uses_numeric_columns_without_normalized_plaintext(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证金额 canary 保持规范精度，同时 normalized Usage 不保存其明文。"""

    answer_canary = "12345.678901"
    harness = HARNESS.BrowserHarness(
        "prd077-answer-canary-01",
        _secrets(answer_canary),
        OWNER_NONCE,
    )
    harness.work = tmp_path / "work"
    calls: list[tuple[Path, object, str, bool]] = []
    expected = object()

    def seed(
        work: Path,
        secrets: object,
        *,
        usage_cost_cny: str,
        include_normalized_cost: bool,
    ) -> object:
        """截获 seed 参数，证明高金额仅进入受控数值列而不进入规范化正文。"""

        calls.append((work, secrets, usage_cost_cny, include_normalized_cost))
        return expected

    monkeypatch.setattr(HARNESS.RUNTIME, "seed_business_database", seed)
    runtime_secrets = object()
    assert harness._seed_business_graph(runtime_secrets) is expected
    assert calls == [(harness.work, runtime_secrets, answer_canary, False)]


def test_prepare_preserves_preexisting_same_run_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """同 run-id 冲突必须失败且不得删除任何进入本轮前已存在的目录。"""

    work_root = tmp_path / "work"
    artifact_root = tmp_path / "artifact"
    work_root.mkdir()
    artifact_root.mkdir()
    monkeypatch.setattr(HARNESS, "WORK_ROOT", work_root)
    monkeypatch.setattr(HARNESS, "ARTIFACT_ROOT", artifact_root)
    harness = HARNESS.BrowserHarness("prd077-existing-01", _secrets(), OWNER_NONCE)
    harness.staging.mkdir()
    marker = harness.staging / "existing.txt"
    marker.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(harness, "_assert_paths_and_ports", lambda: None)

    with pytest.raises(HARNESS.AcceptanceFailure):
        harness.prepare()

    assert marker.read_text(encoding="utf-8") == "existing"
    assert not harness.work.exists()
    assert not harness.final_artifact.exists()


def test_cleanup_propagates_owned_directory_removal_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """删除失败必须显式失败，不能留下伪造的 stopped/passed 结果。"""

    harness = _prepare_cleanup_harness(
        monkeypatch,
        tmp_path,
        "prd077-cleanup-failure-01",
    )

    def fail_remove(*_args, **_kwargs) -> None:
        """注入固定删除失败，验证 harness 不会发布伪成功终态。"""

        # 固定异常不包含 owner 路径或任何浏览器私有值。
        raise OSError("injected")

    monkeypatch.setattr(HARNESS.RUNTIME, "remove_owned_directory", fail_remove)
    with pytest.raises(HARNESS.AcceptanceFailure) as captured:
        harness.cleanup(success=False)

    assert (captured.value.stage, captured.value.code) == ("cleanup.work", "failed")
    assert harness.work.exists()
    assert harness.staging.exists()


def test_cleanup_retries_transient_owned_directory_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Windows 短暂锁只重试已隔离的 owner，不再解析公开路径。"""

    remove_owned_directory = HARNESS.RUNTIME.remove_owned_directory
    harness = _prepare_cleanup_harness(
        monkeypatch,
        tmp_path,
        "prd077-cleanup-retry-01",
    )
    original_rmtree = HARNESS.RUNTIME.shutil.rmtree
    attempts: list[Path] = []

    def transient_rmtree(path: Path) -> None:
        """首次隔离目录删除模拟短锁，随后允许同一隔离目录重试。"""

        attempts.append(path)
        assert path.name.startswith(HARNESS.RUNTIME.OWNER_QUARANTINE_PREFIX)
        if path.parent == harness.work.parent:
            assert not os.path.lexists(harness.work)
        else:
            assert path.parent == harness.staging.parent
            assert not os.path.lexists(harness.staging)
        if len(attempts) == 1:
            # 首次隔离目录删除注入短锁，验证有界重试始终指向同一 owner 身份。
            raise OSError("injected")
        original_rmtree(path)

    monkeypatch.setattr(
        HARNESS.RUNTIME,
        "remove_owned_directory",
        remove_owned_directory,
    )
    monkeypatch.setattr(HARNESS.RUNTIME.shutil, "rmtree", transient_rmtree)
    monkeypatch.setattr(HARNESS.RUNTIME.time, "sleep", lambda _seconds: None)
    harness.cleanup(success=False)

    assert len(attempts) == 3
    assert attempts[0] == attempts[1]
    assert attempts[2] != attempts[0]
    assert not harness.work.exists()
    assert not harness.staging.exists()


def test_cleanup_preserves_replaced_same_run_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """取得所有权后同名目录被替换时必须拒绝递归删除新 owner。"""

    harness = _prepare_cleanup_harness(
        monkeypatch,
        tmp_path,
        "prd077-cleanup-replaced-01",
    )
    shutil.rmtree(harness.work)
    harness.work.mkdir()
    marker = harness.work / "replacement.txt"
    marker.write_text("replacement", encoding="utf-8")

    with pytest.raises(HARNESS.AcceptanceFailure) as captured:
        harness.cleanup(success=False)

    assert (captured.value.stage, captured.value.code) == (
        "cleanup.work",
        "owner_identity",
    )
    assert marker.read_text(encoding="utf-8") == "replacement"


def test_cleanup_rejects_replacement_that_copies_exact_owner_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """复制精确 claim 也不能复制独占创建时保存的目录 identity。"""

    harness = _prepare_cleanup_harness(
        monkeypatch,
        tmp_path,
        "prd077-cleanup-copied-claim-01",
    )
    claim = (harness.work / HARNESS.OWNER_CLAIM_FILENAME).read_bytes()
    shutil.rmtree(harness.work)
    harness.work.mkdir()
    (harness.work / HARNESS.OWNER_CLAIM_FILENAME).write_bytes(claim)
    marker = harness.work / "replacement.txt"
    marker.write_text("replacement", encoding="utf-8")

    with pytest.raises(HARNESS.AcceptanceFailure) as captured:
        harness.cleanup(success=False)

    assert (captured.value.stage, captured.value.code) == (
        "cleanup.work",
        "owner_identity",
    )
    assert marker.read_text(encoding="utf-8") == "replacement"
    assert not harness.staging.exists()


def test_cleanup_rechecks_identity_before_every_delete_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """隔离后公开路径即使出现 copied-claim，重试也只能删除 quarantine。"""

    remove_owned_directory = HARNESS.RUNTIME.remove_owned_directory
    harness = _prepare_cleanup_harness(
        monkeypatch,
        tmp_path,
        "prd077-cleanup-identity-retry-01",
    )
    claim = (harness.work / HARNESS.OWNER_CLAIM_FILENAME).read_bytes()
    original_rmtree = HARNESS.RUNTIME.shutil.rmtree
    attempts: list[Path] = []

    def replace_then_retry(quarantine: Path) -> None:
        """首次删除失败时创建 copied-claim 替代目录，验证重试不会误删公开路径。"""

        attempts.append(quarantine)
        assert quarantine.name.startswith(HARNESS.RUNTIME.OWNER_QUARANTINE_PREFIX)
        if quarantine.parent == harness.work.parent and not os.path.lexists(harness.work):
            harness.work.mkdir()
            (harness.work / HARNESS.OWNER_CLAIM_FILENAME).write_bytes(claim)
            (harness.work / "replacement.txt").write_text(
                "replacement",
                encoding="utf-8",
            )
            # 注入短暂删除失败，让下一轮重试面对已出现的同名替代 generation。
            raise OSError("injected")
        original_rmtree(quarantine)

    monkeypatch.setattr(
        HARNESS.RUNTIME,
        "remove_owned_directory",
        remove_owned_directory,
    )
    monkeypatch.setattr(HARNESS.RUNTIME.shutil, "rmtree", replace_then_retry)
    monkeypatch.setattr(HARNESS.RUNTIME.time, "sleep", lambda _seconds: None)

    with pytest.raises(HARNESS.AcceptanceFailure) as captured:
        harness.cleanup(success=False)

    assert (captured.value.stage, captured.value.code) == (
        "cleanup.work",
        "filesystem",
    )
    work_attempts = [path for path in attempts if path.parent == harness.work.parent]
    assert len(work_attempts) == 2
    assert work_attempts[0] == work_attempts[1]
    assert all(path != harness.work for path in attempts)
    assert (harness.work / "replacement.txt").read_text("utf-8") == "replacement"
    assert not harness.staging.exists()


def test_broken_directory_symlink_is_rejected_before_resolve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """broken link 的 exists 为假也不能被误判为空路径并接管。"""

    parent = tmp_path / "parent"
    parent.mkdir()
    candidate = parent / "broken"
    junction_target: Path | None = None
    try:
        candidate.symlink_to(parent / "missing", target_is_directory=True)
    # 主机不能创建符号链接时才降级构造 junction，仍保持 broken reparse 测试语义。
    except OSError:
        # Windows 未开启开发者模式时创建符号链接需要额外权限；junction 不需要，
        # 先连向真实空目录再删除目标，即得到同样 exists=False 的 broken reparse。
        junction_target = parent / "junction-target"
        junction_target.mkdir()
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(candidate), str(junction_target)],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if created.returncode != 0:
            pytest.skip("host cannot create a local reparse test fixture")
        junction_target.rmdir()
    monkeypatch.setattr(HARNESS, "ROOT", tmp_path)

    try:
        with pytest.raises(HARNESS.AcceptanceFailure) as captured:
            HARNESS._lstat_direct_child(candidate, parent, stage="test.reparse")
    finally:
        if candidate.is_symlink():
            candidate.unlink()
        else:
            os.rmdir(candidate)

    assert (captured.value.stage, captured.value.code) == ("test.reparse", "reparse")


def test_cleanup_preserves_work_when_direct_process_is_still_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """直接 Popen 未退出时禁止数据库验证、隐私扫描和 work 删除。"""

    harness = _prepare_cleanup_harness(
        monkeypatch,
        tmp_path,
        "prd077-cleanup-live-process-01",
    )

    class LiveProcess:
        """模拟始终存活的直接子进程，证明 owner 未排空时 work 必须保留。"""

        def poll(self):
            """持续返回未退出状态，阻止清理流程宣称本轮进程数已经归零。"""

            return None

    harness.direct_processes.append(LiveProcess())
    monkeypatch.setattr(HARNESS, "OWNER_DRAIN_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(HARNESS, "OWNER_DRAIN_POLL_SECONDS", 0.0001)
    monkeypatch.setattr(
        HARNESS.RUNTIME,
        "verify_private_storage",
        lambda *_args, **_kwargs: pytest.fail("live work must not be scanned"),
    )

    with pytest.raises(HARNESS.AcceptanceFailure) as captured:
        harness.cleanup(success=False)

    assert (captured.value.stage, captured.value.code) == ("cleanup.owners", "active")
    assert harness.owned_processes_remaining == 1
    assert harness.work.exists()
    assert not harness.staging.exists()


def test_cleanup_does_not_claim_zero_after_process_close_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """close 抛错时即使没有已知 Popen，也不能发布或删除可能活写的 work。"""

    harness = _prepare_cleanup_harness(
        monkeypatch,
        tmp_path,
        "prd077-cleanup-process-close-failure-01",
    )

    def fail_close() -> None:
        """模拟直接进程集合关闭失败，验证未知 owner 状态不会被写成零。"""

        # 关闭异常意味着仍可能存在活写 owner，清理必须保留 work 并拒绝发布证据。
        raise OSError("injected")

    harness.processes = SimpleNamespace(close=fail_close)

    with pytest.raises(HARNESS.AcceptanceFailure) as captured:
        harness.cleanup(success=False)

    assert (captured.value.stage, captured.value.code) == ("cleanup.owners", "failed")
    assert harness.owned_processes_remaining is None
    assert harness.work.exists()
    assert not harness.staging.exists()


def test_cleanup_preserves_work_when_job_close_is_unproven(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Job 已测得零但句柄 close 失败时，终态仍不得声称 owner 已完整释放。"""

    harness = _prepare_cleanup_harness(
        monkeypatch,
        tmp_path,
        "prd077-cleanup-job-close-failure-01",
    )

    class FailingJob:
        """模拟进程数已归零但唯一 Job 句柄无法关闭的未证明释放状态。"""

        def terminate_and_wait(self, _timeout_seconds: float) -> int:
            """返回已排空的进程计数，隔离后续句柄关闭失败这一单独边界。"""

            return 0

        def close(self) -> None:
            """注入 Job 句柄关闭失败，验证句柄会保留供下一次清理重试。"""

            # 唯一句柄未关闭即不能证明 owner 生命周期结束，最终结果必须失败关闭。
            raise OSError("injected")

    harness.process_job = FailingJob()

    with pytest.raises(HARNESS.AcceptanceFailure) as captured:
        harness.cleanup(success=False)

    assert (captured.value.stage, captured.value.code) == ("cleanup.owners", "failed")
    assert harness.owned_processes_remaining is None
    assert harness.work.exists()
    assert not harness.staging.exists()


@pytest.mark.parametrize("failure_point", ("create", "start"))
def test_control_thread_start_failure_closes_bound_server_without_shutdown_or_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_point: str,
) -> None:
    """未启动的 control thread 不能调用 shutdown/join，仍要继续 owner-safe cleanup。"""

    harness = _prepare_cleanup_harness(
        monkeypatch,
        tmp_path,
        f"prd077-control-thread-{failure_point}-01",
    )
    events: list[str] = []

    class Server:
        """模拟已绑定但控制线程未启动的 HTTP 服务，记录仅允许的 socket 关闭动作。"""

        def __init__(self, *_args, **_kwargs) -> None:
            """记录控制服务已完成构造，但尚未进入任何服务线程。"""

            events.append("server_create")

        def serve_forever(self) -> None:
            """拒绝未启动线程意外进入服务循环，保护夹具的生命周期前提。"""

            pytest.fail("unstarted server must not serve")

        def shutdown(self) -> None:
            """拒绝对未启动服务调用可能死锁的 shutdown，要求直接关闭 socket。"""

            pytest.fail("unstarted server must not receive shutdown")

        def server_close(self) -> None:
            """记录未启动服务允许执行的唯一 socket 资源关闭动作。"""

            events.append("server_close")

    class StartFailingThread:
        """模拟构造成功但启动失败的控制线程，确保 cleanup 不会错误 join。"""

        ident = None

        def __init__(self, *_args, **_kwargs) -> None:
            """记录线程对象已构造，同时保持 ident 为空表示从未成功启动。"""

            events.append("thread_create")

        def start(self) -> None:
            """在启动点注入固定失败，验证后续只关闭已绑定的服务 socket。"""

            events.append("thread_start")
            # 启动失败必须保持 ident 为空，清理流程不得调用 shutdown 或 join。
            raise RuntimeError("injected")

        def join(self, *_args, **_kwargs) -> None:
            """拒绝 join 从未启动的线程，避免测试掩盖生产清理中的死锁风险。"""

            pytest.fail("unstarted thread must not be joined")

    monkeypatch.setattr(HARNESS, "ThreadingHTTPServer", Server)
    if failure_point == "create":
        monkeypatch.setattr(
            HARNESS.threading,
            "Thread",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected")),
        )
        expected_code = "thread_create"
    else:
        monkeypatch.setattr(HARNESS.threading, "Thread", StartFailingThread)
        expected_code = "thread_start"

    with pytest.raises(HARNESS.AcceptanceFailure) as captured:
        harness._start_control_server()
    assert (captured.value.stage, captured.value.code) == (
        "harness.control",
        expected_code,
    )

    harness.cleanup(success=False)

    assert events[-1] == "server_close"
    assert "thread_start" in events if failure_point == "start" else True
    assert not harness.work.exists()
    assert not harness.staging.exists()


def test_real_wal_is_checkpointed_switched_to_delete_and_has_no_sidecars(
    tmp_path: Path,
) -> None:
    """真实 WAL 数据库必须经 TRUNCATE checkpoint 与 DELETE 模式正规收尾。"""

    database = tmp_path / "real-wal.sqlite3"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE sample(value INTEGER NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES(1)")
        connection.commit()
        assert Path(f"{database}-wal").exists()
        assert Path(f"{database}-shm").exists()
    finally:
        connection.close()

    HARNESS._quiesce_sqlite_database(
        database,
        stage="test.sqlite",
        timeout_seconds=2.0,
    )

    probe = sqlite3.connect(database)
    try:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert probe.execute("SELECT value FROM sample").fetchone()[0] == 1
    finally:
        probe.close()
    assert HARNESS._sidecars_remaining(database) == 0


def test_live_sqlite_writer_blocks_quiesce_without_deleting_sidecars(
    tmp_path: Path,
) -> None:
    """活写连接必须固定失败并保留 WAL；helper 不能把 sidecar 当垃圾盲删。"""

    database = tmp_path / "live-wal.sqlite3"
    holder = sqlite3.connect(database)
    try:
        assert holder.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        holder.execute("CREATE TABLE sample(value INTEGER NOT NULL)")
        holder.commit()
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO sample(value) VALUES(1)")
        assert HARNESS._sidecars_remaining(database) >= 2

        with pytest.raises(HARNESS.AcceptanceFailure) as captured:
            HARNESS._quiesce_sqlite_database(
                database,
                stage="test.sqlite_live",
                timeout_seconds=0.05,
            )

        assert captured.value.stage == "test.sqlite_live"
        assert captured.value.code in {"busy", "journal_mode", "sidecars"}
        assert HARNESS._sidecars_remaining(database) >= 2
    finally:
        holder.rollback()
        holder.close()
    HARNESS._quiesce_sqlite_database(
        database,
        stage="test.sqlite_cleanup",
        timeout_seconds=2.0,
    )


def test_cleanup_preserves_work_when_sqlite_cannot_be_quiesced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """服务 owner 已归零但数据库仍活写时，只清 staging 并保留整个 work。"""

    harness = _prepare_cleanup_harness(
        monkeypatch,
        tmp_path,
        "prd077-cleanup-live-sqlite-01",
    )
    monkeypatch.setattr(
        harness,
        "_quiesce_database",
        HARNESS.BrowserHarness._quiesce_database.__get__(harness),
    )
    monkeypatch.setattr(HARNESS, "SQLITE_QUIESCE_TIMEOUT_SECONDS", 0.05)
    holder = sqlite3.connect(harness.graph.database)
    try:
        assert holder.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        holder.execute("CREATE TABLE live_write(value INTEGER NOT NULL)")
        holder.commit()
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO live_write(value) VALUES(1)")

        with pytest.raises(HARNESS.AcceptanceFailure) as captured:
            harness.cleanup(success=False)

        assert captured.value.stage == "cleanup.sqlite"
        assert harness.work.exists()
        assert not harness.staging.exists()
    finally:
        holder.rollback()
        holder.close()


def test_job_close_failure_retains_handle_for_cleanup_retry() -> None:
    """CloseHandle 失败不能丢失唯一 Job handle，否则后续无法重试释放。"""

    class Kernel32:
        """模拟 Windows CloseHandle 返回失败，检查 Job 保留唯一可重试句柄。"""

        def CloseHandle(self, _handle) -> int:
            """返回 Windows 失败码而不销毁传入句柄，供后续清理再次尝试。"""

            return 0

    job = object.__new__(HARNESS.WindowsKillOnCloseJob)
    job._kernel32 = Kernel32()
    job._handle = 123
    job._accounting_type = None
    job._last_active_processes = 0

    with pytest.raises(HARNESS.AcceptanceFailure) as captured:
        job.close()

    assert (captured.value.stage, captured.value.code) == ("process.job", "close")
    assert job._handle == 123


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_windows_job_reports_and_drains_real_active_process_count() -> None:
    """正常 quiesce 通过 QueryInformationJobObject 观察非零并有界归零。"""

    job = HARNESS.WindowsKillOnCloseJob()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        job.assign(child)
        assert job.active_processes() >= 1
        assert job.terminate_and_wait(5.0) == 0
        assert child.wait(timeout=5) != 0
        assert job.active_processes() == 0
        job.close()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if job._handle is not None:
            job.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_windows_job_kills_child_when_owner_hard_exits() -> None:
    """harness 父进程不执行 finally 也必须由 Job Object 回收精确后代。"""

    helper = r"""
import importlib.util
import os
import subprocess
import sys

specification = importlib.util.spec_from_file_location("_prd077_job_crash_helper", sys.argv[1])
module = importlib.util.module_from_spec(specification)
sys.modules[specification.name] = module
specification.loader.exec_module(module)
job = module.WindowsKillOnCloseJob()
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
)
job.assign(child)
print(child.pid, flush=True)
sys.stdin.buffer.read(1)
os._exit(0)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", helper, str(MODULE_PATH)],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    process_handle = None
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline().strip())
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        process_handle = kernel32.OpenProcess(0x00100000 | 0x0001, False, child_pid)
        assert process_handle
        assert parent.stdin is not None
        parent.stdin.close()
        assert parent.wait(timeout=10) == 0
        assert kernel32.WaitForSingleObject(process_handle, 10_000) == 0
    finally:
        if process_handle:
            kernel32.TerminateProcess(process_handle, 1)
            kernel32.CloseHandle(process_handle)
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)


def test_cleanup_scans_work_and_artifact_before_writing_passed_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """成功摘要只在双目录零明文、撤权和真实 403 全部成立后发布。"""

    harness = _prepare_cleanup_harness(
        monkeypatch,
        tmp_path,
        "prd077-harness-passed-01",
    )
    harness.revoked = True
    harness.base_permission_retained = True
    harness.backend_denied = True
    harness.quiesced = True
    harness.runtime_tokens.extend(("token-prd077-admin-session", "token-prd077-finance-session"))
    (harness.staging / "empty-desktop-1280x720.png").write_bytes(b"public-browser-evidence")
    scanned: list[Path] = []

    def verify(path: Path, values: tuple[str, ...]) -> dict[str, int]:
        """记录脱敏路径与值数量，不保存或输出任何私有值。"""

        scanned.append(path)
        assert len(values) == 16
        return {
            "files_scanned": sum(1 for item in path.rglob("*") if item.is_file()),
            "plaintext_matches": 0,
            "sqlite_sidecars_remaining": 0,
        }

    monkeypatch.setattr(HARNESS.RUNTIME, "verify_private_storage", verify)

    harness.cleanup(success=True)

    assert scanned == [harness.work, harness.staging, harness.staging]
    summary = json.loads((harness.staging / "runtime-summary.json").read_text("utf-8"))
    assert summary["status"] == "passed"
    assert summary["finance_source_permission_revoked"] is True
    assert summary["base_permission_retained"] is True
    assert summary["backend_denied_after_source_revoke"] is True
    assert summary["owned_processes_remaining"] == harness.owned_processes_remaining == 0
    assert summary["all_108_prds_complete"] is False
    assert not harness.work.exists()
    assert harness.staging.exists()
    assert not harness.final_artifact.exists()
