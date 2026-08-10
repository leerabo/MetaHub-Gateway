"""PRD-075 Runtime 验收器的静止终态与隐私失败关闭测试。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


def load_runtime_module():
    """按真实脚本路径加载验收器，导入过程不得启动服务或占用端口。"""

    path = Path(__file__).resolve().parents[1] / "prd075-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(
        "prd075_runtime_acceptance_contract",
        path,
    )
    if specification is None or specification.loader is None:
        # 测试加载失败只暴露固定诊断，不拼接本机路径或运行时对象。
        raise AssertionError("unable to load PRD-075 runtime acceptance")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


# 复用同一惰性加载模块，确保各测试验证的固定端口和失败类型完全一致。
RUNTIME = load_runtime_module()


@pytest.mark.parametrize("sidecar_suffix", ("-wal", "-shm", "-journal"))
def test_private_storage_requires_zero_sqlite_sidecars(
    tmp_path: Path,
    sidecar_suffix: str,
) -> None:
    """停机后即使 sidecar 不含明文也必须拒绝，避免把活跃数据库误报为静止终态。"""

    private_value = "prd075-private-canary"
    (tmp_path / "runtime.sqlite3").write_bytes(b"closed database placeholder")

    clean = RUNTIME.verify_private_storage(tmp_path, (private_value,))
    assert clean == {
        "files_scanned": 1,
        "plaintext_matches": 0,
        "sqlite_sidecars_remaining": 0,
    }

    (tmp_path / f"runtime.sqlite3{sidecar_suffix}").write_bytes(b"non-secret sidecar")
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.verify_private_storage(tmp_path, (private_value,))
    assert failure.value.stage == "cleanup.sqlite_sidecars"
    assert failure.value.code == "remaining"


def test_private_storage_still_rejects_private_plaintext(tmp_path: Path) -> None:
    """新增 sidecar 门禁不能削弱既有 UTF-8/UTF-16 私有明文扫描。"""

    private_value = "prd075-private-canary"
    (tmp_path / "runtime.log").write_text(private_value, encoding="utf-8")

    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.verify_private_storage(tmp_path, (private_value,))
    assert failure.value.stage == "privacy.scan"
    assert failure.value.code == "plaintext_found"


def test_source_provenance_binds_current_runtime_and_migrations() -> None:
    """源码摘要必须精确对应脚本、迁移全集和 0068 up/down，且不得携带路径。"""

    provenance = RUNTIME.capture_source_provenance()
    assert set(provenance) == RUNTIME.SOURCE_PROVENANCE_NAMES
    for name, path in RUNTIME.source_provenance_files(RUNTIME.MIGRATIONS_DIR).items():
        assert provenance[name] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert len(provenance[name]) == 64
    migration_set = hashlib.sha256()
    for path in sorted(RUNTIME.MIGRATIONS_DIR.glob("*.sql"), key=lambda item: item.name):
        name = path.name.encode("utf-8")
        content = path.read_bytes()
        migration_set.update(len(name).to_bytes(4, "big"))
        migration_set.update(name)
        migration_set.update(len(content).to_bytes(8, "big"))
        migration_set.update(content)
    assert provenance["migration_set"] == migration_set.hexdigest()


def test_migration_snapshot_is_the_provenance_bound_execution_set(tmp_path: Path) -> None:
    """执行快照必须完整匹配仓库摘要，任一 SQL 后续漂移都会改变绑定。"""

    work = tmp_path / "work"
    work.mkdir()
    provenance = RUNTIME.capture_source_provenance()
    snapshot = RUNTIME.create_migration_snapshot(work, provenance)
    assert RUNTIME.capture_source_provenance(snapshot.directory) == provenance
    assert tuple(item.version for item in snapshot.up) == RUNTIME.MIGRATION_UP_VERSIONS
    assert snapshot.up[-1].source_sha256 == provenance["migration_0068_up"]
    assert snapshot.down_0068.source_sha256 == provenance["migration_0068_down"]
    assert snapshot.down_0068.execution_sha256 == hashlib.sha256(
        snapshot.down_0068_sql.encode("utf-8")
    ).hexdigest()

    up = snapshot.directory / "0068_model_ab_testing_quality_evaluation.sql"
    up.write_bytes(up.read_bytes() + b"\n-- synthetic drift\n")
    assert RUNTIME.capture_source_provenance(snapshot.directory) != provenance


def test_prepare_database_uses_one_frozen_snapshot_for_all_migration_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 prepare 必须让 fresh/repeat/down/re-up 消费同一个冻结清单实例。"""

    work = tmp_path / "prepare-work"
    work.mkdir()
    snapshot = RUNTIME.create_migration_snapshot(
        work,
        RUNTIME.capture_source_provenance(),
    )
    secrets_ = RUNTIME.RuntimeSecrets.create()
    calls: list[tuple[str, str, object]] = []
    original_apply = RUNTIME.apply_runtime_migrations
    original_rollback = RUNTIME.rollback_runtime_migration

    def tracked_apply(database_url: str, frozen, *, stage: str):
        """委托真实 up 入口，同时记录其阶段和冻结清单 identity。"""

        calls.append(("up", stage, frozen))
        return original_apply(database_url, frozen, stage=stage)

    def tracked_rollback(database_url: str, frozen, *, stage: str):
        """委托真实 down 入口，同时记录其阶段和冻结清单 identity。"""

        calls.append(("down", stage, frozen))
        return original_rollback(database_url, frozen, stage=stage)

    monkeypatch.setattr(RUNTIME, "apply_runtime_migrations", tracked_apply)
    monkeypatch.setattr(RUNTIME, "rollback_runtime_migration", tracked_rollback)
    for name in (
        "METAHUB_SEED_ADMIN_PASSWORD",
        "METAHUB_SEED_PROVIDER_API_KEY",
        "METAHUB_SEED_VIRTUAL_KEY",
        "METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL",
    ):
        monkeypatch.setenv(name, "prd075-test-restore-marker")

    graph = RUNTIME.prepare_database(
        work,
        secrets_,
        "http://127.0.0.1:18852/v1",
        "http://127.0.0.1:18853/v1",
        snapshot,
    )

    assert graph.migration_roundtrip["fresh_up"]["migration_count"] == 68
    assert [(kind, stage) for kind, stage, _frozen in calls] == [
        ("up", "database.migrate"),
        ("up", "database.migrate_repeat"),
        ("down", "database.migrate_down"),
        ("up", "database.migrate_reup"),
    ]
    assert all(frozen is snapshot for _kind, _stage, frozen in calls)
    RUNTIME.verify_migration_execution_checksums(
        graph.database_url,
        snapshot,
        stage="test.prepare.checksum",
    )


def test_evidence_records_provenance_and_rejects_invalid_shape(tmp_path: Path) -> None:
    """最终 JSON 和 manifest 绑定源码摘要，额外字段不能混入可提交证据。"""

    artifact = tmp_path / "artifact"
    provenance = RUNTIME.capture_source_provenance()
    evidence_path = RUNTIME.write_evidence(
        artifact,
        {},
        {
            "files_scanned": 0,
            "plaintext_matches": 0,
            "sqlite_sidecars_remaining": 0,
        },
        provenance,
        (),
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["source_provenance"] == {
        "algorithm": "sha256",
        **provenance,
    }
    manifest = (artifact / "SHA256SUMS.txt").read_text(encoding="ascii")
    assert manifest == (
        f"{hashlib.sha256(evidence_path.read_bytes()).hexdigest()}  "
        "runtime-acceptance.json\n"
    )

    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.write_evidence(
            artifact,
            {},
            {},
            {**provenance, "unexpected": "0" * 64},
            (),
        )
    assert failure.value.stage == "evidence.provenance"
    assert failure.value.code == "shape"

    invalid_digest = dict(provenance)
    invalid_digest["runtime_acceptance"] = None  # type: ignore[assignment]
    with pytest.raises(RUNTIME.AcceptanceFailure) as digest_failure:
        RUNTIME.validate_source_provenance(invalid_digest)
    assert digest_failure.value.stage == "evidence.provenance"
    assert digest_failure.value.code == "digest"

    with pytest.raises(RUNTIME.AcceptanceFailure) as reserved_failure:
        RUNTIME.write_evidence(
            artifact,
            {"source_provenance": {}},
            {},
            provenance,
            (),
        )
    assert reserved_failure.value.stage == "evidence.shape"
    assert reserved_failure.value.code == "reserved_key"


def test_provenance_read_failure_uses_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源码读取故障不能把绝对路径或原始 OSError 输出到验收终端。"""

    def fail_read(_path: Path) -> bytes:
        """模拟迁移文件在校验窗口内不可读。"""

        # 原始异常故意携带合成描述，用于确认转换后不会外泄。
        raise OSError("synthetic host path")

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.capture_source_provenance()
    assert failure.value.stage == "evidence.provenance"
    assert failure.value.code == "source_unavailable"


@pytest.mark.parametrize(
    ("drift_target", "expected_code"),
    (("source", "source_changed"), ("snapshot", "snapshot_changed")),
)
def test_run_fails_closed_when_source_or_snapshot_changes_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_target: str,
    expected_code: str,
) -> None:
    """run 必须在发布前复核 live source 与冻结快照，任一漂移都不能写 final。"""

    work = tmp_path / "run-work"
    work.mkdir()
    snapshot_directory = work / "migration-snapshot"
    snapshot_directory.mkdir()
    artifact = tmp_path / "run-artifact"
    base = {name: "0" * 64 for name in RUNTIME.SOURCE_PROVENANCE_NAMES}
    drifted = {**base, "migration_set": "1" * 64}
    entry = RUNTIME.MigrationManifestEntry(
        version="0068",
        name="model_ab_testing_quality_evaluation",
        filename="0068_model_ab_testing_quality_evaluation.sql",
        source_sha256="0" * 64,
        execution_sha256="0" * 64,
    )
    snapshot = RUNTIME.FrozenMigrationSnapshot(
        directory=snapshot_directory,
        migration_set_sha256="0" * 64,
        up=(entry,),
        up_sql=("",),
        down_0068=entry,
        down_0068_sql="",
    )
    live_reads = 0
    published: list[Path] = []

    def fake_capture(directory: Path | None = None):
        """分别模拟 live source 与 snapshot 在终态复核时发生漂移。"""

        nonlocal live_reads
        if directory is None:
            live_reads += 1
            if drift_target == "source" and live_reads > 1:
                return drifted
            return base
        return drifted if drift_target == "snapshot" else base

    class DummyService:
        """提供不占端口的 start/stop owner，供 run 生命周期真实收尾。"""

        def __init__(self, *args, **kwargs) -> None:
            """忽略只在真实 Stub 使用的本地参数。"""

        def start(self) -> str:
            """返回不会被 fake prepare 访问的回环地址。"""

            return "http://127.0.0.1:1"

        # Stub 生命周期结束时只记录空操作，不访问任何端口或文件。
        def stop(self) -> None:
            """模拟当前 Stub owner 已完整释放端口、线程与关联句柄。"""

    # 本测试没有真实子进程；独立 owner 仍用于验证 finally 会执行 close。
    class DummyProcesses:
        """模拟本次验收没有启动任何子进程时的空 owner 生命周期集合。"""

        # close 保持空操作，避免测试替身掩盖 source/snapshot 复核顺序。
        def close(self) -> None:
            """确认空 owner 收尾不访问进程、文件、端口或其它宿主资源。"""

    monkeypatch.setattr(RUNTIME, "capture_source_provenance", fake_capture)
    monkeypatch.setattr(RUNTIME, "assert_ports_available", lambda: None)
    monkeypatch.setattr(
        RUNTIME,
        "create_run_directories",
        lambda _run_id: (work, artifact),
    )
    monkeypatch.setattr(
        RUNTIME,
        "create_migration_snapshot",
        lambda _work, _provenance: snapshot,
    )
    monkeypatch.setattr(RUNTIME, "ProviderStub", DummyService)
    monkeypatch.setattr(RUNTIME, "WebhookStub", DummyService)
    monkeypatch.setattr(RUNTIME, "OwnedProcesses", DummyProcesses)
    monkeypatch.setattr(RUNTIME, "prepare_database", lambda *args: object())
    monkeypatch.setattr(RUNTIME, "service_environment", lambda *args: {})
    monkeypatch.setattr(RUNTIME, "exercise_runtime", lambda *args: ({}, ()))
    monkeypatch.setattr(
        RUNTIME,
        "verify_private_storage",
        lambda *_args: {
            "files_scanned": 0,
            "plaintext_matches": 0,
            "sqlite_sidecars_remaining": 0,
        },
    )

    def record_publish(target: Path, *_args):
        """若错误路径误发布则保留目标供断言。"""

        published.append(target)
        return target

    monkeypatch.setattr(RUNTIME, "write_evidence", record_publish)

    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.run("prd075-provenance-drift")
    assert failure.value.stage == "evidence.provenance"
    assert failure.value.code == expected_code
    assert published == []
    assert not artifact.exists()


@pytest.mark.parametrize(
    "loader_failure",
    (
        OSError("C:\\private-host\\migration.sql"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic invalid migration"),
    ),
    ids=("filesystem", "unicode"),
)
def test_migration_loader_dependency_failure_is_stable(
    monkeypatch: pytest.MonkeyPatch,
    loader_failure: Exception,
) -> None:
    """shared loader 的文件系统/Unicode 异常必须转换且不能携带 snapshot 路径。"""

    entry = RUNTIME.MigrationManifestEntry(
        version="0068",
        name="model_ab_testing_quality_evaluation",
        filename="0068_model_ab_testing_quality_evaluation.sql",
        source_sha256="0" * 64,
        execution_sha256="0" * 64,
    )
    snapshot = RUNTIME.FrozenMigrationSnapshot(
        directory=Path("synthetic-snapshot"),
        migration_set_sha256="0" * 64,
        up=(entry,),
        up_sql=("",),
        down_0068=entry,
        down_0068_sql="",
    )

    def fail_loader(*_args, **_kwargs):
        """模拟 Windows 锁或 ACL 造成的原始宿主路径异常。"""

        raise loader_failure

    monkeypatch.setattr(RUNTIME, "verify_snapshot_directory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(RUNTIME, "apply_migrations", fail_loader)
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.apply_runtime_migrations(
            "sqlite:///synthetic.sqlite3",
            snapshot,
            stage="database.synthetic",
        )
    assert failure.value.stage == "database.synthetic"
    assert failure.value.code == "failed"
    assert "private-host" not in str(failure.value)


def test_up_execution_text_is_supplied_by_frozen_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shared loader 必须执行冻结内存 SQL，不能重新打开同名但内容漂移的路径文件。"""

    migration_directory = tmp_path / "synthetic-migrations"
    migration_directory.mkdir()
    actual_sql = "CREATE TABLE actual_consumed(id INTEGER PRIMARY KEY);\n"
    expected_sql = "CREATE TABLE expected_manifest(id INTEGER PRIMARY KEY);\n"
    (migration_directory / "0001_synthetic.sql").write_text(actual_sql, encoding="utf-8")
    entry = RUNTIME.MigrationManifestEntry(
        version="0001",
        name="synthetic",
        filename="0001_synthetic.sql",
        source_sha256=hashlib.sha256(expected_sql.encode("utf-8")).hexdigest(),
        execution_sha256=hashlib.sha256(expected_sql.encode("utf-8")).hexdigest(),
    )
    snapshot = RUNTIME.FrozenMigrationSnapshot(
        directory=migration_directory,
        migration_set_sha256="0" * 64,
        up=(entry,),
        up_sql=(expected_sql,),
        down_0068=entry,
        down_0068_sql="",
    )
    database = tmp_path / "consumed.sqlite3"
    monkeypatch.setattr(RUNTIME, "verify_snapshot_directory", lambda *_args, **_kwargs: None)

    database_url = RUNTIME.database_url(database)
    result = RUNTIME.apply_runtime_migrations(
        database_url,
        snapshot,
        stage="database.synthetic",
    )
    assert result.applied == ["0001"]
    connection = RUNTIME.connect_database(database_url)
    try:
        # schema 只能出现冻结文本声明的表，路径中的替换文本必须保持零影响。
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "expected_manifest" in tables
    assert "actual_consumed" not in tables


def test_down_execution_text_is_supplied_by_frozen_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 shared rollback 只能从冻结内存适配器取得一次 0068 down 文本。"""

    down_sql = "SELECT 'frozen-down-contract';\n"
    checksum = hashlib.sha256(down_sql.encode("utf-8")).hexdigest()
    up_entry = RUNTIME.MigrationManifestEntry(
        version="0068",
        name="model_ab_testing_quality_evaluation",
        filename="0068_model_ab_testing_quality_evaluation.sql",
        source_sha256="0" * 64,
        execution_sha256="0" * 64,
    )
    down_entry = RUNTIME.MigrationManifestEntry(
        version="0068",
        name="model_ab_testing_quality_evaluation",
        filename="0068_model_ab_testing_quality_evaluation.down.sql",
        source_sha256="0" * 64,
        execution_sha256=checksum,
    )
    snapshot = RUNTIME.FrozenMigrationSnapshot(
        directory=Path("unreadable-synthetic-directory"),
        migration_set_sha256="0" * 64,
        up=(up_entry,),
        up_sql=("",),
        down_0068=down_entry,
        down_0068_sql=down_sql,
    )
    consumed: list[str] = []

    # 替身严格复刻 shared 当前的文件名拼接、exists 与单次 read_text 协议。
    def fake_shared_rollback(_database_url: str, *, migrations_dir):
        """复刻 shared 回滚入口对精确文件名、存在性和单次文本读取的消费协议。"""

        down_file = migrations_dir / (
            "0068_model_ab_testing_quality_evaluation.down.sql"
        )
        assert down_file.exists()
        consumed.append(down_file.read_text(encoding="utf-8"))
        return RUNTIME.RuntimeRollbackResult(rolled_back="0068", execution_sha256="unused")

    monkeypatch.setattr(RUNTIME, "verify_snapshot_directory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        RUNTIME,
        "verify_migration_execution_checksums",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(RUNTIME, "rollback_last_migration", fake_shared_rollback)

    result = RUNTIME.rollback_runtime_migration(
        "sqlite:///unused.sqlite3",
        snapshot,
        stage="database.migrate_down",
    )
    assert consumed == [down_sql]
    assert result.execution_sha256 == checksum


def test_stop_owned_process_closes_both_logs_after_terminate_and_kill_fail() -> None:
    """进程无法停止时仍须分别关闭 stdout/stderr，并只抛固定清理坐标。"""

    canary = "C:\\private-host\\owned-process-secret"
    process_calls: list[object] = []
    stream_calls: list[str] = []

    class FailingProcess:
        """模拟 terminate、kill 和两次有界 wait 均失败的精确 owner。"""

        def poll(self):
            """始终报告进程仍存活，迫使实现走完温和退出和强杀分支。"""

            process_calls.append("poll")
            return None

        def terminate(self) -> None:
            """携带私有 canary 的宿主失败不得进入最终异常。"""

            process_calls.append("terminate")
            # 私有路径 canary 只用于证明 Runtime 将清理异常折叠为固定安全错误码。
            raise OSError(canary)

        def wait(self, timeout: int):
            """两个等待窗口都超时，证明最终固定失败发生在句柄清理之后。"""

            process_calls.append(("wait", timeout))
            raise RUNTIME.subprocess.TimeoutExpired("synthetic", timeout)

        def kill(self) -> None:
            """强杀失败仍不得跳过任一日志句柄。"""

            process_calls.append("kill")
            # 第二个清理阶段继续抛同一私有 canary，确保错误聚合不会回显原文。
            raise OSError(canary)

    class RecordingStream:
        """记录独立 close 调用，避免一个句柄影响另一个句柄的断言。"""

        def __init__(self, name: str, *, fail_close: bool = False) -> None:
            """初始化未关闭句柄，并可让单个 close 携带私有宿主错误。"""

            self.name = name
            self.closed = False
            self.fail_close = fail_close

        def close(self) -> None:
            """记录关闭并切换状态，不创建任何真实宿主文件。"""

            stream_calls.append(self.name)
            self.closed = True
            if self.fail_close:
                # 单个日志句柄关闭失败不能阻止另一个句柄和进程 owner 继续清理。
                raise OSError(canary)

    stdout = RecordingStream("stdout", fail_close=True)
    stderr = RecordingStream("stderr")
    item = RUNTIME.ManagedProcess(
        "synthetic",
        FailingProcess(),  # type: ignore[arg-type]
        stdout,
        stderr,
    )

    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.stop_owned_process(item)
    assert failure.value.stage == "cleanup.owners"
    assert failure.value.code == "failed"
    assert canary not in str(failure.value)
    assert process_calls == ["poll", "terminate", ("wait", 10), "kill", ("wait", 5)]
    assert stream_calls == ["stdout", "stderr"]
    assert stdout.closed is True and stderr.closed is True


def test_owned_processes_close_continues_after_first_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """逆序关闭的第一个 owner 失败后，集合中的其余 owner 仍必须全部尝试。"""

    first = object()
    second = object()
    calls: list[object] = []
    owners = RUNTIME.OwnedProcesses()
    owners.items.extend((first, second))  # type: ignore[arg-type]

    def stop(item: object) -> None:
        """让逆序首项失败，并记录后续 owner 是否仍被调用。"""

        calls.append(item)
        if item is second:
            # 第二个 owner 的私有路径只能被固定清理错误替代，首个 owner 仍必须执行。
            raise OSError("C:\\private-host\\second-owner-secret")

    monkeypatch.setattr(RUNTIME, "stop_owned_process", stop)
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        owners.close()
    assert failure.value.stage == "cleanup.owners"
    assert failure.value.code == "failed"
    assert "private-host" not in str(failure.value)
    assert calls == [second, first]
    assert owners.items == []


def test_run_cleanup_failure_stops_all_owners_and_never_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进程集合清理失败不能阻断三个 Stub，也不能发布看似成功的 final 证据。"""

    work = tmp_path / "cleanup-work"
    work.mkdir()
    artifact = tmp_path / "cleanup-artifact"
    provenance = {name: "0" * 64 for name in RUNTIME.SOURCE_PROVENANCE_NAMES}
    cleanup_calls: list[str] = []
    published: list[Path] = []

    class DummyService:
        """不占端口的 Provider/Webhook 替身，只记录 start/stop 生命周期。"""

        def __init__(self, *args, **kwargs) -> None:
            """用 label 区分合成 A/B Provider；无 label 的实例代表本地 Webhook 替身。"""

            self.label = str(kwargs.get("label", "webhook"))

        def start(self) -> str:
            """返回仅供已替换数据库准备函数接收的合成回环地址。"""

            return "http://127.0.0.1:1"

        def stop(self) -> None:
            """记录每个本地 Stub 都被独立停止，证明失败清理不会提前中断后续 owner。"""

            cleanup_calls.append(
                "webhook" if self.label == "webhook" else f"provider_{self.label}"
            )

    class FailingProcesses:
        """模拟子进程集合完成自身尝试后报告固定边界外宿主错误。"""

        def close(self) -> None:
            """首个 closer 失败，用于证明 helper 仍继续关闭全部 Stub。"""

            cleanup_calls.append("processes")
            # 私有宿主路径只作为泄露 canary，最终 Runtime 错误不得包含该字符串。
            raise OSError("C:\\private-host\\cleanup-secret")

    monkeypatch.setattr(RUNTIME, "capture_source_provenance", lambda *_args: provenance)
    monkeypatch.setattr(RUNTIME, "assert_ports_available", lambda: None)
    monkeypatch.setattr(
        RUNTIME,
        "create_run_directories",
        lambda _run_id: (work, artifact),
    )
    monkeypatch.setattr(RUNTIME, "create_migration_snapshot", lambda *_args: object())
    monkeypatch.setattr(RUNTIME, "ProviderStub", DummyService)
    monkeypatch.setattr(RUNTIME, "WebhookStub", DummyService)
    monkeypatch.setattr(RUNTIME, "OwnedProcesses", FailingProcesses)
    monkeypatch.setattr(RUNTIME, "prepare_database", lambda *_args: object())
    monkeypatch.setattr(RUNTIME, "service_environment", lambda *_args: {})
    monkeypatch.setattr(RUNTIME, "exercise_runtime", lambda *_args: ({}, ()))

    def publish(target: Path, *_args) -> Path:
        """记录任何错误发布；正确实现不应调用该替身。"""

        published.append(target)
        return target

    monkeypatch.setattr(RUNTIME, "write_evidence", publish)

    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.run("prd075-cleanup-failure")
    assert failure.value.stage == "cleanup.owners"
    assert failure.value.code == "failed"
    assert "private-host" not in str(failure.value)
    assert cleanup_calls == ["processes", "webhook", "provider_b", "provider_a"]
    assert published == []
    assert not artifact.exists()


def test_main_rejects_unknown_arguments_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """未知参数中的路径或 canary 不得被 argparse usage/error 原样输出。"""

    canary = "C:\\private-host\\prd075-argument-secret"

    def unexpected_run(_run_id: str | None = None) -> Path:
        """参数错误必须在调用 run 前失败。"""

        # 若进入业务验收器说明 argparse 未在固定边界拒绝未知参数，必须立即失败。
        raise AssertionError("run must not execute for invalid arguments")

    monkeypatch.setattr(RUNTIME, "run", unexpected_run)
    assert RUNTIME.main(["--unexpected", canary]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "PRD-075 runtime acceptance failed: config.arguments:invalid\n"
    )
    assert canary not in captured.err
    assert "private-host" not in captured.err
    assert "usage:" not in captured.err
    assert "Traceback" not in captured.err


def test_main_never_prints_raw_filesystem_path_or_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """最终 CLI 兜底必须把遗漏 OSError 收敛为固定、无路径的一行。"""

    def fail_run(_run_id: str | None = None) -> Path:
        """模拟深层依赖遗漏映射的 Windows 文件系统异常。"""

        # 合成绝对路径只能存在于原始异常，最终 stderr 必须零命中。
        raise OSError("C:\\private-host\\runtime.sqlite3")

    monkeypatch.setattr(RUNTIME, "run", fail_run)
    assert RUNTIME.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "PRD-075 runtime acceptance failed: runtime:dependency_failure\n"
    )
    assert "private-host" not in captured.err
    assert "Traceback" not in captured.err


def test_evidence_publish_failure_keeps_final_directory_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """manifest 写入失败时 staging 可诊断，但 final 证据目录不能半发布。"""

    original_write_text = Path.write_text

    def fail_manifest(path: Path, *args, **kwargs):
        """只让 manifest 写入失败，JSON staging 写入仍按真实路径执行。"""

        if path.name == "SHA256SUMS.txt":
            # manifest 失败发生在 staging 内，用于证明 final 目录尚未发布。
            raise OSError("synthetic manifest failure")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_manifest)
    artifact = tmp_path / "atomic-artifact"
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.write_evidence(
            artifact,
            {},
            {
                "files_scanned": 0,
                "plaintext_matches": 0,
                "sqlite_sidecars_remaining": 0,
            },
            RUNTIME.capture_source_provenance(),
            (),
        )
    assert failure.value.stage == "evidence.publish"
    assert failure.value.code == "filesystem"
    assert not artifact.exists()
    assert (tmp_path / ".atomic-artifact.staging").is_dir()
