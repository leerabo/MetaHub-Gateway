"""PRD-077 Runtime 验收器的边界、隐私、原子发布与失败清理合同。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def load_runtime_module():
    """按真实脚本路径加载模块，导入本身不得启动服务或占用端口。"""

    path = Path(__file__).resolve().parents[1] / "prd077-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(
        "prd077_runtime_acceptance_contract",
        path,
    )
    if specification is None or specification.loader is None:
        # 验收器无法加载时立即失败，不能用空模块跳过真实 Runtime 合同。
        raise AssertionError("unable to load PRD-077 runtime acceptance")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


# Runtime 模块只按仓库脚本加载一次，所有用例共享同一受控代码身份。
RUNTIME = load_runtime_module()


def safe_privacy(files: int = 1) -> dict[str, int]:
    """构造已完成 work 扫描的最小安全计数。"""

    return {
        "files_scanned": files,
        "plaintext_matches": 0,
        "sqlite_sidecars_remaining": 0,
    }


def safe_provenance() -> dict[str, str]:
    """返回与正式证据同形但不依赖源码内容的 SHA-256 集合。"""

    return {
        name: format(index, "x") * 64
        for index, name in enumerate(sorted(RUNTIME.SOURCE_PROVENANCE_NAMES), start=1)
    }


def complete_migration() -> dict[str, object]:
    """构造发布门禁接受的四阶段 70/0070 迁移事实。"""

    healthy_70 = {
        "migration_count": 70,
        "latest_migration": "0070",
        "integrity_check": "ok",
        "foreign_key_violations": 0,
    }
    return {
        "fresh": dict(healthy_70),
        "repeat": {**healthy_70, "applied_count": 0},
        "down": {
            "migration_count": 69,
            "latest_migration": "0069",
            "integrity_check": "ok",
            "foreign_key_violations": 0,
        },
        "re_up": dict(healthy_70),
    }


def complete_facts() -> dict[str, object]:
    """构造与真实 Runtime exact 同形的最小安全聚合事实。"""

    return {
        "services": {
            "initial_control_http_200": True,
            "recovered_control_http_200": True,
        },
        "queries": {
            "supported_query_types": 4,
            "source_links_checked": 11,
            "private_response_headers": True,
        },
        "source_failure": {
            "http_503": True,
            "fixed_error_code": True,
            "failure_audited": True,
            "schema_restored": True,
        },
        "concurrency": {
            "attempts": 20,
            "http_200": 20,
            "http_503": 0,
            "unique_query_ids": 20,
            "unique_audit_ids": 20,
            "unique_event_ids": 20,
            "unique_audit_log_ids": 20,
        },
        "access_control": {
            "finance_allowed_usage": True,
            "finance_denied_error": True,
            "security_allowed_error": True,
            "security_denied_cost": True,
            "developer_base_denied": True,
            "read_only_action_blocked": True,
        },
        "isolation": {
            "second_tenant_query": True,
            "cross_tenant_request_not_found": True,
        },
        "event_recovery": {
            "source_pending_before_stop": 33,
            "restart_recovered": True,
            "pending_or_processing": 0,
            "failed": 0,
            "valid_webhooks": 31,
            "invalid_webhooks": 0,
            "forbidden_webhooks": 0,
        },
        "database": {
            "query_count": 33,
            "audit_count": 33,
            "audit_log_count": 33,
            "event_count": 33,
            "one_to_one": True,
            "unique_operation_request_count": 33,
            "concurrent_attempt_count": 20,
            "concurrent_answered_count": 20,
            "concurrent_source_unavailable_count": 0,
            "source_failure_audited": True,
            "zero_model_usage_count": 33,
            "source_facts": {
                "request_log_count": 2,
                "usage_count": 1,
                "usage_token_count": 22,
                "usage_cost_cny_micros": 1_000_000,
                "finops_count": 1,
                "finops_cost_cny_micros": 1_000_000,
                "active_policy_count": 1,
                "published_snapshot_head_count": 1,
                "published_snapshot_version": 1,
            },
            "content_columns": 0,
            "integrity_check": "ok",
            "foreign_key_violations": 0,
        },
    }


def test_runtime_boundaries_are_repository_owned_and_loopback_only() -> None:
    """所有可写目录与监听地址必须冻结在仓库和回环边界。"""

    assert RUNTIME.ROOT == Path(__file__).resolve().parents[3]
    assert RUNTIME.WORK_ROOT == RUNTIME.ROOT / ".tmp" / "prd077-runtime"
    assert RUNTIME.ARTIFACT_ROOT == RUNTIME.ROOT / ".artifacts" / "prd077-runtime"
    assert RUNTIME.LOOPBACK == "127.0.0.1"
    assert RUNTIME.CONCURRENT_ATTEMPTS == 20
    assert RUNTIME.CONCURRENT_HTTP_TIMEOUT_SECONDS == 60
    assert RUNTIME.EVENT_RECOVERY_TIMEOUT_SECONDS == 180
    assert RUNTIME.EVENT_RECOVERY_STALL_SECONDS == 30
    assert RUNTIME.EXPECTED_ASSISTANT_ATTEMPTS == 33
    assert len(RUNTIME.FIXED_PORTS) == len(set(RUNTIME.FIXED_PORTS)) == 2
    assert RUNTIME.IDENTITY_BOUND_MUTATIONS_SUPPORTED is (os.name == "nt")


@pytest.mark.parametrize(
    ("field_name", "environment_name"),
    (
        ("file_encryption_secret", "METAHUB_FILE_ENCRYPTION_SECRET"),
        ("batch_encryption_secret", "METAHUB_BATCH_ENCRYPTION_SECRET"),
        (
            "prompt_template_encryption_secret",
            "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET",
        ),
        ("cache_affinity_hmac_secret", "METAHUB_CACHE_AFFINITY_HMAC_SECRET"),
    ),
)
def test_runtime_service_secret_is_typed_scanned_and_leak_fails_closed(
    tmp_path: Path,
    field_name: str,
    environment_name: str,
) -> None:
    """四个服务 Secret 必须有内存 owner，且任一多字节明文落盘立即失败。"""

    secrets_ = RUNTIME.RuntimeSecrets.create()
    private_value = getattr(secrets_, field_name)
    graph = RUNTIME.BusinessGraph(
        database=tmp_path / "unused.sqlite3",
        database_url="sqlite:///unused.sqlite3",
        tenant_id="tenant_test",
        second_tenant_id="tenant_second",
    )
    environment = RUNTIME.service_environment(
        tmp_path,
        graph,
        secrets_,
        event_enabled=False,
    )
    scan_values = secrets_.scan_values()
    if environment.get(environment_name) != private_value:
        pytest.fail("service secret environment mapping missing")
    if private_value not in scan_values or len(scan_values) != 13:
        pytest.fail("service secret privacy owner missing")
    # Runtime Secret 的 repr 不得包含真实配置，防止异常和测试输出形成持久泄漏。
    if private_value in repr(secrets_):
        pytest.fail("service secret repr exposure")

    leak = tmp_path / "service-secret.log"
    leak.write_bytes(private_value.encode("utf-16-le"))
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.verify_private_storage(tmp_path, scan_values)
    assert (failure.value.stage, failure.value.code) == (
        "privacy.scan",
        "plaintext_found",
    )


def test_seed_accepts_privacy_safe_five_digit_cost_and_projects_finops(tmp_path: Path) -> None:
    """Browser 金额针必须真实穿过 Usage 与 FinOps trigger，不能只通过 stub 合同。"""

    graph = RUNTIME.seed_business_database(
        tmp_path,
        RUNTIME.RuntimeSecrets.create(),
        usage_cost_cny="12345.678901",
        include_normalized_cost=False,
    )
    connection = RUNTIME.connect_database(graph.database_url)
    try:
        usage = connection.execute(
            "SELECT CAST(ROUND(cost_cny*1000000) AS INTEGER) FROM usage_ledger"
        ).fetchone()
        finops = connection.execute(
            "SELECT normalized_cny_micros FROM finops_ledger_entries"
        ).fetchone()
        assert tuple(usage or ()) == (12_345_678_901,)
        assert tuple(finops or ()) == (12_345_678_901,)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.parametrize(
    "unsafe",
    ("../escape", "..\\escape", "C:/outside", "two/segments", "x" * 81),
)
def test_run_id_rejects_paths_and_oversized_values(unsafe: str) -> None:
    """run-id 只能是一个短 ASCII 目录段，不能选择输出位置。"""

    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.safe_run_id(unsafe)
    assert (failure.value.stage, failure.value.code) == ("config.run_id", "invalid")


@pytest.mark.parametrize("encoding", ("utf-8", "utf-16-le", "utf-16-be"))
def test_private_storage_rejects_multiencoding_plaintext(
    tmp_path: Path,
    encoding: str,
) -> None:
    """Prompt、Response、Key 或 Token canary 以常见编码落盘均必须失败。"""

    private = "PRD077_PRIVATE_CANARY_6d81c938"
    (tmp_path / "service.log").write_bytes(private.encode(encoding))
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.verify_private_storage(tmp_path, (private,))
    assert (failure.value.stage, failure.value.code) == (
        "privacy.scan",
        "plaintext_found",
    )


def test_runtime_privacy_scopes_answer_leaves_to_outputs_not_authoritative_source(
    tmp_path: Path,
) -> None:
    """来源叶值可存在于权威业务库，但进入日志或完整回答进入数据库仍失败关闭。"""

    work = tmp_path / "runtime-work"
    logs = work / "logs"
    logs.mkdir(parents=True)
    database = work / "business.sqlite3"
    answer_leaf = f"PRD077_SOURCE_LEAF_{uuid4().hex}"
    answer_serialization = json.dumps(
        {"summary": f"PRD077_PRIVATE_ANSWER_{uuid4().hex}"},
        ensure_ascii=True,
        sort_keys=True,
    )
    private_value = f"PRD077_PRIVATE_VALUE_{uuid4().hex}"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE source_facts(value TEXT NOT NULL)")
        connection.execute("INSERT INTO source_facts(value) VALUES(?)", (answer_leaf,))
        connection.commit()
    finally:
        connection.close()

    privacy = RUNTIME.verify_runtime_private_storage(
        work,
        private_values=(private_value,),
        answer_serializations=(answer_serialization,),
        answer_leaves=(answer_leaf,),
        source_databases=(database,),
    )
    assert privacy == {
        "files_scanned": 1,
        "plaintext_matches": 0,
        "sqlite_sidecars_remaining": 0,
    }

    leak = logs / "service.log"
    leak.write_text(answer_leaf, encoding="utf-8")
    with pytest.raises(RUNTIME.AcceptanceFailure) as log_failure:
        RUNTIME.verify_runtime_private_storage(
            work,
            private_values=(private_value,),
            answer_serializations=(answer_serialization,),
            answer_leaves=(answer_leaf,),
            source_databases=(database,),
        )
    assert (log_failure.value.stage, log_failure.value.code) == (
        "privacy.scan",
        "plaintext_found",
    )
    leak.unlink()

    connection = sqlite3.connect(database)
    try:
        connection.execute("INSERT INTO source_facts(value) VALUES(?)", (answer_serialization,))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RUNTIME.AcceptanceFailure) as response_failure:
        RUNTIME.verify_runtime_private_storage(
            work,
            private_values=(private_value,),
            answer_serializations=(answer_serialization,),
            answer_leaves=(answer_leaf,),
            source_databases=(database,),
        )
    assert (response_failure.value.stage, response_failure.value.code) == (
        "privacy.scan",
        "plaintext_found",
    )


def test_runtime_privacy_accepts_public_leaf_in_both_authoritative_databases(
    tmp_path: Path,
) -> None:
    """业务库和迁移库可含同一公开目录叶值，但任何一个库都不能保存完整回答。"""

    work = tmp_path / "runtime-two-databases"
    work.mkdir()
    answer_leaf = f"gateway.upstream_timeout_{uuid4().hex}"
    answer_serialization = json.dumps(
        {"summary": f"PRD077_PRIVATE_ANSWER_{uuid4().hex}"},
        ensure_ascii=True,
        sort_keys=True,
    )
    databases = tuple(work / name for name in ("business.sqlite3", "migration.sqlite3"))
    for database in databases:
        connection = sqlite3.connect(database)
        try:
            connection.execute("CREATE TABLE public_catalog(value TEXT NOT NULL)")
            connection.execute("INSERT INTO public_catalog(value) VALUES(?)", (answer_leaf,))
            connection.commit()
        finally:
            connection.close()

    result = RUNTIME.verify_runtime_private_storage(
        work,
        private_values=(f"PRD077_PRIVATE_{uuid4().hex}",),
        answer_serializations=(answer_serialization,),
        answer_leaves=(answer_leaf,),
        source_databases=databases,
    )
    assert result["plaintext_matches"] == 0
    assert result["files_scanned"] == 2

    connection = sqlite3.connect(databases[1])
    try:
        connection.execute("INSERT INTO public_catalog(value) VALUES(?)", (answer_serialization,))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.verify_runtime_private_storage(
            work,
            private_values=(f"PRD077_PRIVATE_{uuid4().hex}",),
            answer_serializations=(answer_serialization,),
            answer_leaves=(answer_leaf,),
            source_databases=databases,
        )
    assert (failure.value.stage, failure.value.code) == (
        "privacy.scan",
        "plaintext_found",
    )


def test_runtime_privacy_does_not_exempt_source_database_hardlink_alias(
    tmp_path: Path,
) -> None:
    """来源豁免同时绑定词法路径与 identity，同 inode 的其他 leaf 仍按输出扫描。"""

    work = tmp_path / "runtime-hardlink-work"
    logs = work / "logs"
    logs.mkdir(parents=True)
    database = work / "business.sqlite3"
    answer_leaf = f"PRD077_SOURCE_LEAF_{uuid4().hex}"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE source_facts(value TEXT NOT NULL)")
        connection.execute("INSERT INTO source_facts(value) VALUES(?)", (answer_leaf,))
        connection.commit()
    finally:
        connection.close()
    hardlink = logs / "business-hardlink.sqlite3"
    try:
        os.link(database, hardlink)
    # 该用例依赖主机创建硬链接；能力缺失时只能跳过，不能误判产品行为失败。
    except OSError:
        pytest.skip("host cannot create hardlink fixture")

    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.verify_runtime_private_storage(
            work,
            private_values=(f"PRD077_PRIVATE_{uuid4().hex}",),
            answer_serializations=(json.dumps({"safe": True}),),
            answer_leaves=(answer_leaf,),
            source_databases=(database,),
        )
    assert (failure.value.stage, failure.value.code) == (
        "privacy.scan",
        "plaintext_found",
    )


def test_private_scan_rejects_tree_or_content_change_after_bound_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """枚举后新增文件时，首轮良性读取不能产生通过结论。"""

    scanned = tmp_path / "scanned.txt"
    scanned.write_text("public-a", encoding="utf-8")
    original_read = RUNTIME._read_scanned_file
    mutated = False

    def mutate_after_read(file, guard):
        """在首次文件读取后新增节点，验证末次目录快照能够识别持续变化。"""

        nonlocal mutated
        content = original_read(file, guard)
        if not mutated:
            mutated = True
            (tmp_path / "late.txt").write_text("public", encoding="utf-8")
        return content

    monkeypatch.setattr(RUNTIME, "_read_scanned_file", mutate_after_read)
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.verify_private_storage(tmp_path, ("PRD077_PRIVATE_TREE_RACE_845f",))
    assert (failure.value.stage, failure.value.code) == ("privacy.scan", "tree_changed")
    assert mutated


@pytest.mark.skipif(os.name != "nt", reason="Windows recursive change notifications")
def test_private_scan_rejects_created_then_removed_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """扫描期间创建后删除的同名 entry 不能用相同终态快照掩盖。"""

    scanned = tmp_path / "scanned.txt"
    scanned.write_text("public", encoding="utf-8")
    original_read = RUNTIME._read_scanned_file
    mutated = False

    def create_then_remove(file, guard):
        """在扫描窗口内短暂创建再删除节点，验证目录变化监控不会漏报。"""

        nonlocal mutated
        content = original_read(file, guard)
        if not mutated:
            mutated = True
            transient = tmp_path / "transient.txt"
            transient.write_text("public", encoding="utf-8")
            transient.unlink()
        return content

    monkeypatch.setattr(RUNTIME, "_read_scanned_file", create_then_remove)
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.verify_private_storage(tmp_path, ("PRD077_PRIVATE_RESTORED_TREE_64f1",))
    assert (failure.value.stage, failure.value.code) == ("privacy.scan", "tree_changed")
    assert mutated
    assert not (tmp_path / "transient.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_private_scan_bound_file_handle_blocks_in_place_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首轮枚举持有不共享 WRITE 的文件句柄，扫描结束前不能原地改写后恢复。"""

    scanned = tmp_path / "scanned.txt"
    scanned.write_text("public-a", encoding="utf-8")
    original_read = RUNTIME._read_scanned_file
    attempted = False

    def rewrite_after_read(file, guard):
        """读取绑定文件后尝试原路径改写，验证活跃句柄会拒绝内容替换。"""

        nonlocal attempted
        content = original_read(file, guard)
        if not attempted:
            attempted = True
            with pytest.raises(OSError):
                scanned.write_text("public-b", encoding="utf-8")
        return content

    monkeypatch.setattr(RUNTIME, "_read_scanned_file", rewrite_after_read)
    result = RUNTIME.verify_private_storage(tmp_path, ("PRD077_PRIVATE_FILE_RACE_47b1",))
    assert result["files_scanned"] == 1
    assert attempted


@pytest.mark.parametrize("suffix", ("-wal", "-shm", "-journal"))
def test_private_storage_rejects_sqlite_sidecars(tmp_path: Path, suffix: str) -> None:
    """验证静止终态拒绝残留 SQLite WAL、SHM 或 journal 私有 sidecar。"""

    (tmp_path / "business.sqlite3").write_bytes(b"closed")
    (tmp_path / f"business.sqlite3{suffix}").write_bytes(b"sidecar")
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.verify_private_storage(tmp_path, ("PRD077_PRIVATE_SIDE_CAR",))
    assert (failure.value.stage, failure.value.code) == (
        "cleanup.sqlite_sidecars",
        "remaining",
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound database semantics")
def test_sqlite_quiesce_checkpoints_wal_switches_delete_and_removes_sidecars(
    tmp_path: Path,
) -> None:
    """服务退出后的协调连接必须正规收束 WAL，而不是直接删除 sidecar。"""

    database = tmp_path / "quiesce.sqlite3"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE facts(value TEXT NOT NULL)")
        connection.execute("INSERT INTO facts(value) VALUES('public')")
        connection.commit()
    finally:
        connection.close()

    result = RUNTIME.quiesce_sqlite_database(database, timeout_seconds=1)
    assert result["journal_mode"] == "delete"
    assert result["sqlite_sidecars_remaining"] == 0
    assert all(not Path(f"{database}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))
    check = sqlite3.connect(database)
    try:
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert check.execute("SELECT value FROM facts").fetchone()[0] == "public"
    finally:
        check.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound database semantics")
def test_sqlite_quiesce_fails_closed_while_reader_pins_wal(tmp_path: Path) -> None:
    """仍有活跃连接固定 WAL 快照时必须报告 busy，不能伪造静止终态。"""

    database = tmp_path / "busy.sqlite3"
    reader = sqlite3.connect(database)
    writer: sqlite3.Connection | None = None
    try:
        assert reader.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        reader.execute("CREATE TABLE facts(value INTEGER NOT NULL)")
        reader.execute("INSERT INTO facts(value) VALUES(1)")
        reader.commit()
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
        writer = sqlite3.connect(database)
        writer.execute("INSERT INTO facts(value) VALUES(2)")
        writer.commit()
        writer.close()
        writer = None

        with pytest.raises(RUNTIME.AcceptanceFailure) as captured:
            RUNTIME.quiesce_sqlite_database(database, timeout_seconds=0.05)
        assert captured.value.stage == "cleanup.sqlite"
        assert captured.value.code in {"checkpoint_busy", "failed"}
    finally:
        if writer is not None:
            writer.close()
        reader.close()

    assert (
        RUNTIME.quiesce_sqlite_database(database, timeout_seconds=1)["sqlite_sidecars_remaining"]
        == 0
    )


def test_owned_cleanup_rejects_replacement_even_when_claim_was_copied(tmp_path: Path) -> None:
    """同名新目录复制有效 claim 后仍不能冒充最初创建的文件 identity。"""

    parent = tmp_path / "owner-root"
    directory = parent / "run"
    parent.mkdir()
    directory.mkdir()
    claim = "a" * 64
    RUNTIME._write_owner_claim(directory, claim)
    identity = RUNTIME.owned_directory_identity(directory, parent, stage="test.owner")
    shutil.rmtree(directory)
    directory.mkdir()
    RUNTIME._write_owner_claim(directory, claim)
    marker = directory / "replacement.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(RUNTIME.AcceptanceFailure) as captured:
        RUNTIME.remove_owned_directory(
            directory,
            parent,
            stage="test.cleanup",
            expected_identity=identity,
            owner_claim=claim,
        )
    assert (captured.value.stage, captured.value.code) == (
        "test.cleanup",
        "owner_identity",
    )
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_identity_mutations_fail_closed_without_handle_bound_platform_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无句柄 rename/delete 原语时，发布与清理都必须在首次变更前保留原 owner。"""

    parent = tmp_path / "unsupported-owner-root"
    source = parent / "source"
    target = parent / "target"
    parent.mkdir()
    source.mkdir()
    marker = source / "preserved.txt"
    marker.write_text("preserve", encoding="utf-8")
    identity = RUNTIME.owned_directory_identity(source, parent, stage="test.owner")
    monkeypatch.setattr(RUNTIME, "IDENTITY_BOUND_MUTATIONS_SUPPORTED", False)

    with pytest.raises(RUNTIME.AcceptanceFailure) as rename_failure:
        RUNTIME.rename_owned_directory(
            source,
            target,
            parent,
            stage="test.publish",
            expected_identity=identity,
        )
    assert (rename_failure.value.stage, rename_failure.value.code) == (
        "test.publish",
        "rename_unsupported",
    )
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not target.exists()

    with pytest.raises(RUNTIME.AcceptanceFailure) as delete_failure:
        RUNTIME.remove_owned_directory(
            source,
            parent,
            stage="test.cleanup",
            expected_identity=identity,
        )
    assert (delete_failure.value.stage, delete_failure.value.code) == (
        "test.cleanup",
        "delete_unsupported",
    )
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not any(
        path.name.startswith(RUNTIME.OWNER_QUARANTINE_PREFIX) for path in parent.iterdir()
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound mutation semantics")
def test_owned_cleanup_renames_verified_owner_before_recursive_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """递归删除首次调用前，公开 owner 路径已不存在且随机 quarantine 保持原 identity。"""

    parent = tmp_path / "owner-root"
    directory = parent / "run"
    parent.mkdir()
    directory.mkdir()
    claim = "b" * 64
    RUNTIME._write_owner_claim(directory, claim)
    identity = RUNTIME.owned_directory_identity(directory, parent, stage="test.owner")
    marker = directory / "owned.txt"
    marker.write_text("owned", encoding="utf-8")
    original_rmtree = RUNTIME.shutil.rmtree
    observed: list[Path] = []

    def inspect_then_remove(path: Path) -> None:
        """删除隔离目录前复核身份、位置与内容，确保只回收当前 owner 节点。"""

        observed.append(path)
        assert not os.path.lexists(directory)
        assert path.parent == parent
        assert path.name.startswith(RUNTIME.OWNER_QUARANTINE_PREFIX)
        assert RUNTIME.owned_directory_identity(path, parent, stage="test.quarantine") == identity
        assert (path / "owned.txt").read_text("utf-8") == "owned"
        original_rmtree(path)

    monkeypatch.setattr(RUNTIME.shutil, "rmtree", inspect_then_remove)
    RUNTIME.remove_owned_directory(
        directory,
        parent,
        stage="test.cleanup",
        expected_identity=identity,
        owner_claim=claim,
    )

    assert len(observed) == 1
    assert not directory.exists()
    assert not observed[0].exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound mutation semantics")
def test_owned_cleanup_does_not_touch_replacement_created_after_quarantine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """攻击者在隔离后复制 claim 到原路径，新目录不得被递归删除。"""

    parent = tmp_path / "owner-root"
    directory = parent / "run"
    parent.mkdir()
    directory.mkdir()
    claim = "c" * 64
    RUNTIME._write_owner_claim(directory, claim)
    identity = RUNTIME.owned_directory_identity(directory, parent, stage="test.owner")
    original_rmtree = RUNTIME.shutil.rmtree

    def replace_before_first_delete(quarantine: Path) -> None:
        """首次删除隔离目录时创建同名替代节点，验证清理不会误删新 generation。"""

        directory.mkdir()
        RUNTIME._write_owner_claim(directory, claim)
        (directory / "replacement.txt").write_text("preserve", encoding="utf-8")
        original_rmtree(quarantine)

    monkeypatch.setattr(RUNTIME.shutil, "rmtree", replace_before_first_delete)
    RUNTIME.remove_owned_directory(
        directory,
        parent,
        stage="test.cleanup",
        expected_identity=identity,
        owner_claim=claim,
    )

    assert (directory / "replacement.txt").read_text("utf-8") == "preserve"


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_owned_cleanup_blocks_quarantine_replacement_after_claim_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """claim 复核后到首次 rmtree 之间，quarantine 根仍由不共享 DELETE 的句柄固定。"""

    parent = tmp_path / "owner-root"
    directory = parent / "run"
    parent.mkdir()
    directory.mkdir()
    claim = "d" * 64
    RUNTIME._write_owner_claim(directory, claim)
    identity = RUNTIME.owned_directory_identity(directory, parent, stage="test.owner")
    (directory / "owned.txt").write_text("owned", encoding="utf-8")
    original_claim = RUNTIME._require_owner_claim
    replacement_attempted = False

    def try_replacement(path: Path, digest: str, **kwargs) -> None:
        """在隔离目录守卫生效期间尝试替换路径，验证 Windows 删除身份仍被锁定。"""

        nonlocal replacement_attempted
        original_claim(path, digest, **kwargs)
        if (
            path.name.startswith(RUNTIME.OWNER_QUARANTINE_PREFIX)
            and not replacement_attempted
            and _guard_active
        ):
            replacement_attempted = True
            saved = parent / "saved-owner"
            with pytest.raises(OSError):
                path.replace(saved)

    _guard_active = False
    original_enter = RUNTIME.WindowsPathIdentityGuard.__enter__
    original_exit = RUNTIME.WindowsPathIdentityGuard.__exit__

    def mark_guard_enter(guard):
        """记录路径身份守卫进入时刻，限定替换攻击只发生在保护窗口内。"""

        nonlocal _guard_active
        entered = original_enter(guard)
        _guard_active = True
        return entered

    def mark_guard_exit(guard, exception_type, exception, traceback):
        """记录路径身份守卫退出时刻，并确保异常路径也复位测试观测状态。"""

        nonlocal _guard_active
        try:
            return original_exit(guard, exception_type, exception, traceback)
        finally:
            _guard_active = False

    monkeypatch.setattr(RUNTIME.WindowsPathIdentityGuard, "__enter__", mark_guard_enter)
    monkeypatch.setattr(RUNTIME.WindowsPathIdentityGuard, "__exit__", mark_guard_exit)
    monkeypatch.setattr(RUNTIME, "_require_owner_claim", try_replacement)
    RUNTIME.remove_owned_directory(
        directory,
        parent,
        stage="test.cleanup",
        expected_identity=identity,
        owner_claim=claim,
    )

    assert replacement_attempted
    assert not (parent / "saved-owner").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound mutation semantics")
def test_publish_rejects_staging_replacement_before_bound_rename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """发布 rename 必须绑定创建时 staging identity，扫描后同名 replacement 不得被移动。"""

    provenance = safe_provenance()
    monkeypatch.setattr(RUNTIME, "capture_source_provenance", lambda: provenance)
    original_rename = RUNTIME.rename_owned_directory
    replacement: Path | None = None

    def replace_then_rename(
        source: Path,
        target: Path,
        parent: Path,
        *,
        stage: str,
        expected_identity: tuple[int, int],
    ) -> None:
        """发布改名前替换 staging 路径，验证旧 generation 不会覆盖正式证据。"""

        nonlocal replacement
        saved = parent / "saved-staging"
        source.replace(saved)
        source.mkdir()
        replacement = source / "unknown.txt"
        replacement.write_text("preserve", encoding="utf-8")
        original_rename(
            source,
            target,
            parent,
            stage=stage,
            expected_identity=expected_identity,
        )

    monkeypatch.setattr(RUNTIME, "rename_owned_directory", replace_then_rename)
    artifact = tmp_path / "artifact-bound-publish"
    with pytest.raises(RUNTIME.AcceptanceFailure) as captured:
        RUNTIME.write_evidence(
            artifact,
            tmp_path / "removed-work",
            complete_facts(),
            complete_migration(),
            safe_privacy(),
            provenance,
            ("PRD077_PRIVATE_BOUND_PUBLISH_83fa",),
        )

    assert captured.value.code == "owner_identity"
    assert replacement is not None and replacement.read_text("utf-8") == "preserve"
    assert not artifact.exists()
    shutil.rmtree(tmp_path / "saved-staging")


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_publish_blocks_staging_replacement_while_bound_rename_is_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """句柄验证后到 rename 之间，staging 仍不可被改名成同名 replacement。"""

    provenance = safe_provenance()
    monkeypatch.setattr(RUNTIME, "capture_source_provenance", lambda: provenance)
    original_rename = RUNTIME.WindowsPathIdentityGuard.rename
    attempted = False

    def try_replacement(guard, target: Path, *, stage: str) -> None:
        """句柄绑定 staging 后尝试搬走原节点，验证发布仍使用受保护的身份。"""

        nonlocal attempted
        if guard.path.name.endswith(".staging") and not attempted:
            attempted = True
            with pytest.raises(OSError):
                guard.path.replace(tmp_path / "saved-bound-staging")
        original_rename(guard, target, stage=stage)

    monkeypatch.setattr(RUNTIME.WindowsPathIdentityGuard, "rename", try_replacement)
    artifact = tmp_path / "artifact-bound-window"
    evidence = RUNTIME.write_evidence(
        artifact,
        tmp_path / "removed-work",
        complete_facts(),
        complete_migration(),
        safe_privacy(),
        provenance,
        ("PRD077_PRIVATE_BOUND_WINDOW_8d31",),
    )

    assert attempted
    assert evidence == artifact / "runtime-acceptance.json"
    assert not (tmp_path / "saved-bound-staging").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound mutation semantics")
def test_publish_post_rename_failure_cleans_final_by_owner_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rename 已发生但 helper 后验失败时，cleanup 仍必须找到并删除同 identity final。"""

    provenance = safe_provenance()
    monkeypatch.setattr(RUNTIME, "capture_source_provenance", lambda: provenance)
    original_rename = RUNTIME.rename_owned_directory

    def rename_then_fail(
        source: Path,
        target: Path,
        parent: Path,
        *,
        stage: str,
        expected_identity: tuple[int, int],
    ) -> None:
        """完成正式目录改名后注入失败，验证异常清理仍能回收已发布 generation。"""

        original_rename(
            source,
            target,
            parent,
            stage=stage,
            expected_identity=expected_identity,
        )
        if stage == "evidence.publish":
            raise RUNTIME.AcceptanceFailure(stage, "synthetic_post_rename")

    monkeypatch.setattr(RUNTIME, "rename_owned_directory", rename_then_fail)
    artifact = tmp_path / "artifact-post-rename-failure"
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.write_evidence(
            artifact,
            tmp_path / "removed-work",
            complete_facts(),
            complete_migration(),
            safe_privacy(),
            provenance,
            ("PRD077_PRIVATE_POST_RENAME_61f3",),
        )
    assert (failure.value.stage, failure.value.code) == (
        "evidence.publish",
        "synthetic_post_rename",
    )
    assert not artifact.exists()
    assert not (tmp_path / ".artifact-post-rename-failure.staging").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound database semantics")
def test_sqlite_quiesce_rejects_lstat_to_connect_replacement_before_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """初始 lstat 后替换 DB 时 fail closed，替换库 journal_mode 不得被改写。"""

    database = tmp_path / "original.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    for path, value in ((database, "original"), (replacement, "replacement")):
        connection = sqlite3.connect(path)
        try:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
            connection.execute("INSERT INTO marker(value) VALUES(?)", (value,))
            connection.commit()
        finally:
            connection.close()
    expected_identity = RUNTIME.regular_file_identity(
        database,
        stage="test.database_identity",
    )
    original_guard = RUNTIME.WindowsDatabaseIdentityGuard

    def replace_then_guard(path: Path, identity: tuple[int, int], *, stage: str):
        """在数据库身份采集后替换路径，验证句柄守卫拒绝连接到替代文件。"""

        database.unlink()
        replacement.replace(database)
        return original_guard(path, identity, stage=stage)

    monkeypatch.setattr(RUNTIME, "WindowsDatabaseIdentityGuard", replace_then_guard)
    with pytest.raises(RUNTIME.AcceptanceFailure) as captured:
        RUNTIME.quiesce_sqlite_database(
            database,
            timeout_seconds=1,
            expected_identity=expected_identity,
        )

    assert captured.value.code in {"database_identity", "database_handle"}
    probe = sqlite3.connect(database)
    try:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert probe.execute("SELECT value FROM marker").fetchone()[0] == "replacement"
    finally:
        probe.close()


def test_sqlite_quiesce_without_bound_identity_support_does_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不能绑定 SQLite 文件 identity 的平台在 checkpoint/journal 写入前失败关闭。"""

    database = tmp_path / "unsupported.sqlite3"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker(value) VALUES('preserve')")
        connection.commit()
    finally:
        connection.close()
    before = database.read_bytes()
    monkeypatch.setattr(RUNTIME, "IDENTITY_BOUND_MUTATIONS_SUPPORTED", False)

    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.quiesce_sqlite_database(database, timeout_seconds=1)
    assert (failure.value.stage, failure.value.code) == (
        "cleanup.sqlite",
        "database_identity_unsupported",
    )
    assert database.read_bytes() == before
    probe = sqlite3.connect(database)
    try:
        assert probe.execute("SELECT value FROM marker").fetchone()[0] == "preserve"
    finally:
        probe.close()


def test_private_storage_rejects_file_symlink_without_following(
    tmp_path: Path,
) -> None:
    """文件 symlink 即使指向公开内容也不得被扫描器跳过。"""

    target = tmp_path / "target.txt"
    target.write_text("public", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    # 该用例依赖主机创建文件符号链接；能力缺失时跳过而不降低扫描策略。
    except OSError:
        pytest.skip("host cannot create file symlink fixture")

    with pytest.raises(RUNTIME.AcceptanceFailure) as captured:
        RUNTIME.verify_private_storage(tmp_path, ("PRD077_PRIVATE_LINK",))

    assert (captured.value.stage, captured.value.code) == ("privacy.scan", "reparse")


def test_private_storage_rejects_directory_junction_and_broken_reparse(
    tmp_path: Path,
) -> None:
    """目录 junction 存在或目标被删除后都必须固定失败，不能静默跳过子树。"""

    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "public.txt").write_text("public", encoding="utf-8")
    junction = tmp_path / "junction"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if created.returncode != 0:
        pytest.skip("host cannot create junction fixture")
    try:
        with pytest.raises(RUNTIME.AcceptanceFailure) as live:
            RUNTIME.verify_private_storage(tmp_path, ("PRD077_PRIVATE_JUNCTION",))
        assert (live.value.stage, live.value.code) == ("privacy.scan", "reparse")
        (target / "public.txt").unlink()
        target.rmdir()
        with pytest.raises(RUNTIME.AcceptanceFailure) as broken:
            RUNTIME.verify_private_storage(tmp_path, ("PRD077_PRIVATE_JUNCTION",))
        assert (broken.value.stage, broken.value.code) == ("privacy.scan", "reparse")
    finally:
        os.rmdir(junction)


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_private_scan_blocks_pending_directory_replacement_at_scandir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """pending 目录在真正 scandir 时仍由不共享 DELETE 的句柄固定。"""

    child = tmp_path / "pending"
    child.mkdir()
    (child / "public.txt").write_text("public", encoding="utf-8")
    original_scandir = RUNTIME.os.scandir
    attempted = False

    def replace_before_scandir(path: Path):
        """进入子目录扫描前尝试搬走节点，验证目录句柄持续约束公开路径身份。"""

        nonlocal attempted
        if Path(path) == child and not attempted:
            attempted = True
            with pytest.raises(OSError):
                child.replace(tmp_path / "saved-pending")
        return original_scandir(path)

    monkeypatch.setattr(RUNTIME.os, "scandir", replace_before_scandir)
    result = RUNTIME.verify_private_storage(tmp_path, ("PRD077_PRIVATE_PENDING_7f4c",))

    assert attempted
    assert result["files_scanned"] == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_private_scan_blocks_file_replacement_during_bound_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """文件内容从枚举 identity 的句柄读取，读取期间无法换成同名 symlink。"""

    scanned = tmp_path / "scanned.txt"
    scanned.write_text("public", encoding="utf-8")
    original_duplicate = RUNTIME._duplicate_windows_handle
    attempted = False

    def replace_before_duplicate(handle, *, stage: str) -> int:
        """复制文件句柄前尝试搬走节点，验证读取全过程仍绑定原始文件身份。"""

        nonlocal attempted
        attempted = True
        with pytest.raises(OSError):
            scanned.replace(tmp_path / "saved-file")
        return original_duplicate(handle, stage=stage)

    monkeypatch.setattr(RUNTIME, "_duplicate_windows_handle", replace_before_duplicate)
    result = RUNTIME.verify_private_storage(tmp_path, ("PRD077_PRIVATE_FILE_SWAP_5c29",))

    assert attempted
    assert result["files_scanned"] == 1


def test_source_provenance_binds_runtime_helper_source_trees_and_0070_pair() -> None:
    """正式摘要必须绑定 Runtime/helper、Shared/Control 源码树和迁移。"""

    provenance = RUNTIME.capture_source_provenance()
    assert set(provenance) == set(safe_provenance())
    assert (
        provenance["runtime_acceptance"]
        == hashlib.sha256(Path(RUNTIME.__file__).read_bytes()).hexdigest()
    )
    assert (
        provenance["runtime_helper"]
        == hashlib.sha256(
            (RUNTIME.ROOT / "scripts" / "acceptance" / "prd075-runtime-acceptance.py").read_bytes()
        ).hexdigest()
    )
    assert provenance["shared_source_tree"] == RUNTIME._source_tree_sha256(
        RUNTIME.ROOT / "shared-sdk" / "python" / "metahub_shared"
    )
    assert provenance["control_source_tree"] == RUNTIME._source_tree_sha256(
        RUNTIME.ROOT / "control-plane-api" / "src" / "metahub_control"
    )
    assert (
        provenance["migration_0070_up"]
        == hashlib.sha256(
            (RUNTIME.MIGRATIONS_DIR / "0070_read_only_admin_ai_assistant.sql").read_bytes()
        ).hexdigest()
    )
    assert (
        provenance["migration_0070_down"]
        == hashlib.sha256(
            (RUNTIME.MIGRATIONS_DIR / "0070_read_only_admin_ai_assistant.down.sql").read_bytes()
        ).hexdigest()
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound mutation semantics")
def test_evidence_is_atomically_published_scanned_and_sha_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """final 只含经 staging 与发布后扫描的 JSON、manifest，且总体目标保持 false。"""

    artifact = tmp_path / "artifact"
    work = tmp_path / "removed-work"
    private = ("PRD077_PRIVATE_EVIDENCE_9a3d",)
    provenance = safe_provenance()
    monkeypatch.setattr(RUNTIME, "capture_source_provenance", lambda: provenance)
    evidence_path = RUNTIME.write_evidence(
        artifact,
        work,
        complete_facts(),
        complete_migration(),
        safe_privacy(3),
        provenance,
        private,
    )

    assert not (tmp_path / ".artifact.staging").exists()
    assert {path.name for path in artifact.iterdir()} == {
        "runtime-acceptance.json",
        "SHA256SUMS.txt",
    }
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["all_108_prds_complete"] is False
    assert payload["privacy"] == {
        "files_scanned": 5,
        "work_files_scanned": 3,
        "artifact_files_scanned": 2,
        "plaintext_matches": 0,
        "sqlite_sidecars_remaining": 0,
    }
    assert private[0].encode() not in evidence_path.read_bytes()
    assert (artifact / "SHA256SUMS.txt").read_text(encoding="ascii") == (
        f"{hashlib.sha256(evidence_path.read_bytes()).hexdigest()}  runtime-acceptance.json\n"
    )


def test_evidence_publish_without_bound_mutations_writes_no_staging_or_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无句柄 identity 变更支持时，证据构造在创建 staging 前固定失败。"""

    monkeypatch.setattr(RUNTIME, "IDENTITY_BOUND_MUTATIONS_SUPPORTED", False)
    artifact = tmp_path / "unsupported-artifact"
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.write_evidence(
            artifact,
            tmp_path / "removed-work",
            complete_facts(),
            complete_migration(),
            safe_privacy(),
            safe_provenance(),
            ("PRD077_PRIVATE_UNSUPPORTED_PUBLISH_6e31",),
        )
    assert (failure.value.stage, failure.value.code) == (
        "evidence.publish",
        "identity_mutations_unsupported",
    )
    assert not artifact.exists()
    assert not (tmp_path / ".unsupported-artifact.staging").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound mutation semantics")
def test_publish_failure_removes_staging_and_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """manifest 写入失败不能留下半发布 staging 或 final 目录。"""

    original_write_text = Path.write_text

    def fail_manifest(path: Path, *args, **kwargs):
        """只在本次 staging manifest 写入处制造固定文件系统失败。"""

        if path.name == "SHA256SUMS.txt":
            # 固定故障不携带临时路径或证据正文，只验证原子发布清理行为。
            raise OSError("synthetic manifest failure")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_manifest)
    artifact = tmp_path / "failed-artifact"
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.write_evidence(
            artifact,
            tmp_path / "removed-work",
            complete_facts(),
            complete_migration(),
            safe_privacy(),
            safe_provenance(),
            ("PRD077_PRIVATE_FAILURE_837a",),
        )
    assert (failure.value.stage, failure.value.code) == (
        "evidence.publish",
        "filesystem",
    )
    assert not artifact.exists()
    assert not (tmp_path / ".failed-artifact.staging").exists()


def test_unknown_evidence_key_fails_before_filesystem_write(tmp_path: Path) -> None:
    """未知或正文形态事实不能绕过 exact coverage 白名单。"""

    artifact = tmp_path / "forbidden-artifact"
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.write_evidence(
            artifact,
            tmp_path / "removed-work",
            {"answer": "masked"},
            complete_migration(),
            safe_privacy(),
            safe_provenance(),
            ("PRD077_PRIVATE_FORBIDDEN_9b0e",),
        )
    assert (failure.value.stage, failure.value.code) == (
        "evidence.coverage",
        "facts_shape",
    )
    assert not artifact.exists()


@pytest.mark.parametrize(
    "case",
    (
        "missing_concurrency",
        "nineteen_concurrent",
        "missing_source_failure",
        "missing_reup",
        "thirty_two_queries",
    ),
)
def test_incomplete_runtime_coverage_never_publishes(
    case: str,
    tmp_path: Path,
) -> None:
    """任一关键 Runtime 或迁移事实缺失都不能生成 passed 目录。"""

    facts = deepcopy(complete_facts())
    migration = deepcopy(complete_migration())
    # 每个分支只破坏一个必需事实，证明 summary validator 对缺项和计数漂移失败关闭。
    if case == "missing_concurrency":
        facts.pop("concurrency")
    elif case == "nineteen_concurrent":
        facts["concurrency"]["attempts"] = 19  # type: ignore[index]
    elif case == "missing_source_failure":
        facts.pop("source_failure")
    elif case == "missing_reup":
        migration.pop("re_up")
    else:
        facts["database"]["query_count"] = 32  # type: ignore[index]
    artifact = tmp_path / f"artifact-{case}"
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.write_evidence(
            artifact,
            tmp_path / "removed-work",
            facts,
            migration,
            safe_privacy(),
            safe_provenance(),
            ("PRD077_PRIVATE_COVERAGE_71cf",),
        )
    assert failure.value.stage == "evidence.coverage"
    assert not artifact.exists()
    assert not (tmp_path / f".{artifact.name}.staging").exists()


def test_runtime_coverage_accepts_audited_concurrency_source_deadlines() -> None:
    """过载 503 可以存在，但必须与数据库中二十组一对一失败事实精确对应。"""

    facts = complete_facts()
    facts["concurrency"]["http_200"] = 3  # type: ignore[index]
    facts["concurrency"]["http_503"] = 17  # type: ignore[index]
    facts["database"]["concurrent_answered_count"] = 3  # type: ignore[index]
    facts["database"]["concurrent_source_unavailable_count"] = 17  # type: ignore[index]
    RUNTIME.validate_runtime_coverage(facts, complete_migration())


@pytest.mark.parametrize("case", ("missing", "uppercase", "short"))
def test_malformed_provenance_never_publishes(case: str, tmp_path: Path) -> None:
    """Provenance 缺项或非 canonical SHA-256 时必须在写盘前失败。"""

    provenance = safe_provenance()
    first = sorted(provenance)[0]
    if case == "missing":
        provenance.pop(first)
    elif case == "uppercase":
        provenance[first] = "A" * 64
    else:
        provenance[first] = "a" * 63
    artifact = tmp_path / f"artifact-provenance-{case}"
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.write_evidence(
            artifact,
            tmp_path / "removed-work",
            complete_facts(),
            complete_migration(),
            safe_privacy(),
            provenance,
            ("PRD077_PRIVATE_PROVENANCE_8d2f",),
        )
    assert failure.value.stage == "evidence.provenance"
    assert not artifact.exists()
    assert not (tmp_path / f".{artifact.name}.staging").exists()


def test_existing_work_tree_refuses_passed_evidence(tmp_path: Path) -> None:
    """work 未真实删除时，cleanup 字段不能被构造器硬编码为成功。"""

    work = tmp_path / "owned-work"
    work.mkdir()
    artifact = tmp_path / "artifact-work-remains"
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.write_evidence(
            artifact,
            work,
            complete_facts(),
            complete_migration(),
            safe_privacy(),
            safe_provenance(),
            ("PRD077_PRIVATE_WORK_REMAINS_11a4",),
        )
    assert (failure.value.stage, failure.value.code) == (
        "cleanup.work",
        "remaining",
    )
    assert not artifact.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound mutation semantics")
def test_source_drift_after_staging_removes_staging_and_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """staging 扫描后源码漂移时，rename 前必须删除本批次全部目录。"""

    provenance = safe_provenance()
    drifted = {**provenance, "control_source_tree": "f" * 64}
    monkeypatch.setattr(RUNTIME, "capture_source_provenance", lambda: drifted)
    artifact = tmp_path / "artifact-source-drift"
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.write_evidence(
            artifact,
            tmp_path / "removed-work",
            complete_facts(),
            complete_migration(),
            safe_privacy(),
            provenance,
            ("PRD077_PRIVATE_SOURCE_DRIFT_663b",),
        )
    assert (failure.value.stage, failure.value.code) == (
        "evidence.provenance",
        "changed",
    )
    assert not artifact.exists()
    assert not (tmp_path / f".{artifact.name}.staging").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound mutation semantics")
def test_published_privacy_failure_removes_staging_and_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目录 rename 后的最终隐私扫描失败也不能留下 passed artifact。"""

    provenance = safe_provenance()
    monkeypatch.setattr(RUNTIME, "capture_source_provenance", lambda: provenance)
    original_scan = RUNTIME.verify_private_storage
    scans = 0

    def fail_published(path: Path, private_values: tuple[str, ...]):
        """staging 正常扫描，final 扫描时制造固定隐私失败。"""

        nonlocal scans
        scans += 1
        if scans == 2:
            raise RUNTIME.AcceptanceFailure("privacy.scan", "plaintext_found")
        return original_scan(path, private_values)

    monkeypatch.setattr(RUNTIME, "verify_private_storage", fail_published)
    artifact = tmp_path / "artifact-published-scan"
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.write_evidence(
            artifact,
            tmp_path / "removed-work",
            complete_facts(),
            complete_migration(),
            safe_privacy(),
            provenance,
            ("PRD077_PRIVATE_PUBLISHED_SCAN_46df",),
        )
    assert (failure.value.stage, failure.value.code) == (
        "privacy.scan",
        "plaintext_found",
    )
    assert scans == 2
    assert not artifact.exists()
    assert not (tmp_path / f".{artifact.name}.staging").exists()


def test_role_answer_canaries_and_artifact_plaintext_fail_closed(
    tmp_path: Path,
) -> None:
    """Finance/Security/第二租户回答进入 work 或证据时都必须被隐私门禁发现。"""

    payloads = tuple(
        {
            "answer": {
                "summary": f"PRD077 role answer private summary {role}",
                "claims": [{"claim_id": role, "value": index}],
            }
        }
        for index, role in enumerate(("finance", "security", "second"), start=1)
    )
    canaries = tuple(
        value
        for payload in payloads
        for value in RUNTIME.private_answer_canaries(payload, stage="privacy.roles")
    )
    assert all(payload["answer"]["summary"] in canaries for payload in payloads)
    work = tmp_path / "role-answer-work"
    work.mkdir()
    (work / "service.log").write_text(canaries[-1], encoding="utf-8")
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.verify_private_storage(work, canaries)
    assert (failure.value.stage, failure.value.code) == (
        "privacy.scan",
        "plaintext_found",
    )

    artifact = tmp_path / "artifact-private-value"
    with pytest.raises(RUNTIME.AcceptanceFailure) as artifact_failure:
        RUNTIME.write_evidence(
            artifact,
            tmp_path / "removed-work",
            complete_facts(),
            complete_migration(),
            safe_privacy(),
            safe_provenance(),
            ("initial_control_http_200",),
        )
    assert (artifact_failure.value.stage, artifact_failure.value.code) == (
        "evidence.privacy",
        "plaintext",
    )
    assert not artifact.exists()


@pytest.mark.parametrize(
    "query_type",
    (
        "usage_statistics",
        "error_explanation",
        "cost_advice",
        "policy_explanation",
    ),
)
@pytest.mark.parametrize("encoding", ("utf-8", "utf-16-le", "utf-16-be"))
def test_each_query_type_partial_answer_leaf_leak_fails_closed(
    tmp_path: Path,
    query_type: str,
    encoding: str,
) -> None:
    """四类回答的随机长叶值即使脱离 JSON 单独落盘，也必须被多编码扫描发现。"""

    private_leaf = f"PRD077_{query_type}_{uuid4().hex}"
    payload = {
        "query_type": query_type,
        "answer": {
            "summary": "PRD077 deterministic public answer summary",
            "claims": [
                {
                    "claim_id": "private_leaf_contract",
                    "claim_type": "text",
                    "label": private_leaf,
                    "value": "public",
                    "unit": "text",
                    "source_ids": ["src_overview"],
                }
            ],
            "recommendations": [],
        },
    }
    canaries = RUNTIME.private_answer_canaries(payload, stage="privacy.answer_leaf")
    if private_leaf not in canaries:
        pytest.fail("long answer leaf missing from privacy scan")
    leak = tmp_path / "partial-answer.log"
    leak.write_bytes(private_leaf.encode(encoding))
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.verify_private_storage(tmp_path, canaries)
    assert (failure.value.stage, failure.value.code) == (
        "privacy.scan",
        "plaintext_found",
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound mutation semantics")
def test_run_rechecks_live_source_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime 源码或迁移在执行中漂移时不得发布 final。"""

    work = tmp_path / "work"
    work.mkdir()
    artifact = tmp_path / "artifact"
    baseline = safe_provenance()
    drifted = {**baseline, "migration_set": "f" * 64}
    reads = 0
    published: list[Path] = []

    class DummyReceiver:
        """提供不启动线程或端口的 Receiver 合同。"""

        def start(self) -> str:
            """返回固定回环地址而不创建真实网络监听。"""

            return "http://127.0.0.1:18877/events"

        def stop(self) -> None:
            """模拟幂等停止，证明失败清理不会依赖外部线程。"""

            return None

    def capture():
        """首次冻结 baseline，发布前返回漂移摘要。"""

        nonlocal reads
        reads += 1
        return baseline if reads == 1 else drifted

    def publish(target: Path, *_args):
        """记录任何不应发生的发布调用。"""

        published.append(target)
        return target

    monkeypatch.setattr(RUNTIME, "assert_ports_available", lambda: None)
    monkeypatch.setattr(RUNTIME, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(RUNTIME, "ARTIFACT_ROOT", tmp_path)

    def create_owned_run(_run_id: str, *, owner_claim: str):
        """让夹具保留生产代码要求的排他 claim 与真实目录 identity。"""

        RUNTIME._write_owner_claim(work, owner_claim)
        return work, artifact

    monkeypatch.setattr(RUNTIME, "create_run_directories", create_owned_run)
    monkeypatch.setattr(RUNTIME, "capture_source_provenance", capture)
    monkeypatch.setattr(RUNTIME, "WebhookReceiver", DummyReceiver)
    monkeypatch.setattr(RUNTIME, "migration_roundtrip", lambda _work: {})
    monkeypatch.setattr(
        RUNTIME,
        "seed_business_database",
        lambda *_args: SimpleNamespace(database=work / "business.sqlite3"),
    )
    (work / "business.sqlite3").write_bytes(b"public-test-database")
    monkeypatch.setattr(RUNTIME, "exercise_runtime", lambda *_args: ({}, (), (), ()))
    monkeypatch.setattr(RUNTIME, "quiesce_sqlite_database", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        RUNTIME,
        "verify_runtime_private_storage",
        lambda *_args, **_kwargs: safe_privacy(),
    )
    monkeypatch.setattr(RUNTIME, "verify_private_storage", lambda *_args: safe_privacy())
    monkeypatch.setattr(RUNTIME, "write_evidence", publish)

    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.run("prd077-source-drift")
    assert (failure.value.stage, failure.value.code) == (
        "evidence.provenance",
        "changed",
    )
    assert published == []
    assert not work.exists()
    assert not artifact.exists()


def test_run_without_bound_mutations_fails_before_creating_run_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不支持句柄 identity 变更的平台不能创建 work、启动服务或写 evidence。"""

    called = False

    def unexpected_create(*_args, **_kwargs):
        """记录不支持身份变更平台上的意外写入，并立即终止测试执行。"""

        nonlocal called
        called = True
        # 平台能力预检必须早于任何目录创建，触发即证明验收器违反失败关闭顺序。
        raise AssertionError("run directories must not be created")

    monkeypatch.setattr(RUNTIME, "IDENTITY_BOUND_MUTATIONS_SUPPORTED", False)
    monkeypatch.setattr(RUNTIME, "WORK_ROOT", tmp_path / "work-root")
    monkeypatch.setattr(RUNTIME, "ARTIFACT_ROOT", tmp_path / "artifact-root")
    monkeypatch.setattr(RUNTIME, "create_run_directories", unexpected_create)
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.run("unsupported-platform")
    assert (failure.value.stage, failure.value.code) == (
        "config.platform",
        "identity_mutations_unsupported",
    )
    assert not called
    assert not (tmp_path / "work-root").exists()
    assert not (tmp_path / "artifact-root").exists()


def test_main_hides_unexpected_path_and_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """深层异常不能把宿主路径、参数或 traceback 输出到终端。"""

    def fail_run(_run_id: str | None = None) -> Path:
        """注入包含宿主路径的异常，验证 CLI 只输出固定安全坐标。"""

        # 私有路径仅存在异常对象内，最终 stderr 必须由 main 完全折叠。
        raise OSError("C:\\private-host\\runtime.sqlite3")

    monkeypatch.setattr(RUNTIME, "run", fail_run)
    assert RUNTIME.main(["--run-id", "safe-run"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("PRD-077 runtime acceptance failed: runtime:unexpected_failure\n")
    assert "private-host" not in captured.err
    assert "Traceback" not in captured.err
