"""PRD-076 Runtime 验收器的迁移隔离、隐私、发布与清理合同测试。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


def load_runtime_module():
    """按真实脚本路径加载验收器，导入本身不得启动服务或占用端口。"""

    path = Path(__file__).resolve().parents[1] / "prd076-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(
        "prd076_runtime_acceptance_contract",
        path,
    )
    if specification is None or specification.loader is None:
        # 该测试冻结验收器的租户、恢复、证据和隐私边界，防止后续修改绕过门禁。
        raise AssertionError("unable to load PRD-076 runtime acceptance")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


# Runtime 模块是整组合同测试的唯一受控入口，导入时不得启动服务或泄露验收私有值。
RUNTIME = load_runtime_module()


def test_fixed_runtime_boundaries_are_repository_owned_and_loopback_only() -> None:
    """运行树、制品树与全部监听端口必须冻结在仓库和回环边界。"""

    assert RUNTIME.ROOT == Path(__file__).resolve().parents[3]
    assert RUNTIME.WORK_ROOT == RUNTIME.ROOT / ".tmp" / "prd076-runtime"
    assert RUNTIME.ARTIFACT_ROOT == RUNTIME.ROOT / ".artifacts" / "prd076-runtime"
    assert RUNTIME.LOOPBACK == "127.0.0.1"
    assert len(RUNTIME.FIXED_PORTS) == len(set(RUNTIME.FIXED_PORTS)) == 5


@pytest.mark.parametrize("sidecar_suffix", ("-wal", "-shm", "-journal"))
def test_private_storage_requires_zero_sqlite_sidecars(
    tmp_path: Path,
    sidecar_suffix: str,
) -> None:
    """静止终态必须拒绝任何 SQLite sidecar，即使其中不含 canary。"""

    private_value = "prd076-private-canary"
    (tmp_path / "business.sqlite3").write_bytes(b"closed database placeholder")
    assert RUNTIME.verify_private_storage(tmp_path, (private_value,)) == {
        "files_scanned": 1,
        "plaintext_matches": 0,
        "sqlite_sidecars_remaining": 0,
    }
    (tmp_path / f"business.sqlite3{sidecar_suffix}").write_bytes(b"sidecar")
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.verify_private_storage(tmp_path, (private_value,))
    assert (failure.value.stage, failure.value.code) == (
        "cleanup.sqlite_sidecars",
        "remaining",
    )


@pytest.mark.parametrize("encoding", ("utf-8", "utf-16-le", "utf-16-be"))
def test_private_storage_rejects_multiencoding_canary(
    tmp_path: Path,
    encoding: str,
) -> None:
    """Prompt、Response、Key 等随机 canary 的常见编码落盘均必须失败关闭。"""

    value = "PRD076_PRIVATE_CANARY_7b10b47d"
    (tmp_path / "service.log").write_bytes(value.encode(encoding))
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.verify_private_storage(tmp_path, (value,))
    assert (failure.value.stage, failure.value.code) == (
        "privacy.scan",
        "plaintext_found",
    )


def test_source_provenance_binds_runtime_helper_and_complete_0069_set() -> None:
    """最终摘要必须绑定本脚本、复用 helper、迁移全集及 0069 up/down。"""

    provenance = RUNTIME.capture_source_provenance()
    assert set(provenance) == RUNTIME.SOURCE_PROVENANCE_NAMES
    files = RUNTIME.source_provenance_files(RUNTIME.MIGRATIONS_DIR)
    for name, path in files.items():
        assert provenance[name] == hashlib.sha256(path.read_bytes()).hexdigest()
    migration_set = hashlib.sha256()
    for path in sorted(RUNTIME.MIGRATIONS_DIR.glob("*.sql"), key=lambda item: item.name):
        name = path.name.encode("utf-8")
        content = path.read_bytes()
        migration_set.update(len(name).to_bytes(4, "big"))
        migration_set.update(name)
        migration_set.update(len(content).to_bytes(8, "big"))
        migration_set.update(content)
    assert provenance["migration_set"] == migration_set.hexdigest()


def test_migration_snapshot_freezes_0069_up_and_down_in_memory(tmp_path: Path) -> None:
    """同一快照必须精确冻结 69 个 up 和唯一 0069 down，不能运行时重读 live SQL。"""

    work = tmp_path / "work"
    work.mkdir()
    provenance = RUNTIME.capture_source_provenance()
    snapshot = RUNTIME.create_migration_snapshot(work, provenance)
    assert tuple(item.version for item in snapshot.up) == RUNTIME.MIGRATION_UP_VERSIONS
    assert snapshot.up[-1].source_sha256 == provenance["migration_0069_up"]
    assert snapshot.down_0069.source_sha256 == provenance["migration_0069_down"]
    assert snapshot.down_0069.execution_sha256 == hashlib.sha256(
        snapshot.down_0069_sql.encode("utf-8")
    ).hexdigest()

    changed = snapshot.directory / "0069_response_cache_optional_capability.sql"
    changed.write_bytes(changed.read_bytes() + b"\n-- synthetic drift\n")
    assert RUNTIME.capture_source_provenance(snapshot.directory) != provenance


def test_migration_database_executes_fresh_repeat_down_and_reup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """独立空库必须真实完成 fresh/repeat/0069 down/re-up 及完整性与外键检查。"""

    work = tmp_path / "migration-roundtrip"
    work.mkdir()
    snapshot = RUNTIME.create_migration_snapshot(
        work,
        RUNTIME.capture_source_provenance(),
    )
    calls: list[tuple[str, str, object]] = []
    original_apply = RUNTIME.apply_runtime_migrations
    original_rollback = RUNTIME.rollback_runtime_migration

    def tracked_apply(database_url: str, frozen, *, stage: str):
        """该测试冻结验收器的租户、恢复、证据和隐私边界，防止后续修改绕过门禁。 当前定义负责 tracked_apply 对应步骤。"""
        calls.append(("up", stage, frozen))
        return original_apply(database_url, frozen, stage=stage)

    def tracked_rollback(database_url: str, frozen, *, stage: str):
        """该测试冻结验收器的租户、恢复、证据和隐私边界，防止后续修改绕过门禁。 当前定义负责 tracked_rollback 对应步骤。"""
        calls.append(("down", stage, frozen))
        return original_rollback(database_url, frozen, stage=stage)

    monkeypatch.setattr(RUNTIME, "apply_runtime_migrations", tracked_apply)
    monkeypatch.setattr(RUNTIME, "rollback_runtime_migration", tracked_rollback)
    result = RUNTIME.prepare_migration_database(work, snapshot)

    assert result["fresh_up"]["migration_count"] == 69
    assert result["repeat_noop"]["applied_count"] == 0
    assert result["down"]["latest_migration"] == "0068"
    assert result["re_up"]["latest_migration"] == "0069"
    assert [(kind, stage) for kind, stage, _snapshot in calls] == [
        ("up", "migration_database.fresh"),
        ("up", "migration_database.repeat"),
        ("down", "migration_database.down"),
        ("up", "migration_database.reup"),
    ]
    assert all(frozen is snapshot for _kind, _stage, frozen in calls)


def test_business_database_path_is_distinct_from_roundtrip_database(tmp_path: Path) -> None:
    """产生事实的业务库不得复用已经执行 down/re-up 的迁移验收库。"""

    migration_database = RUNTIME.migration_database_path(tmp_path)
    business_database = RUNTIME.business_database_path(tmp_path)
    assert migration_database != business_database
    assert migration_database.parent == business_database.parent == tmp_path.resolve()


def test_down_guard_requires_fixed_failure_and_unchanged_database(
    tmp_path: Path,
) -> None:
    """业务事实阻断必须在 DDL 前发生，并以固定 code 证明 schema 与文件摘要不变。"""

    work = tmp_path / "guard"
    work.mkdir()
    snapshot = RUNTIME.create_migration_snapshot(
        work,
        RUNTIME.capture_source_provenance(),
    )
    database = RUNTIME.business_database_path(work)
    url = RUNTIME.database_url(database)
    result = RUNTIME.apply_runtime_migrations(url, snapshot, stage="test.guard.migrate")
    assert result.applied[-1] == "0069"
    connection = RUNTIME.connect_database(url)
    try:
        connection.execute(
            """
            INSERT INTO tenants(
              tenant_id,slug,name,service_mode,status,created_at,updated_at
            ) VALUES(
              'tenant_default','prd076-guard','PRD076 guard','internal_only','active',
              '2026-08-10T00:00:00Z','2026-08-10T00:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO response_cache_tag_generations(
              tenant_id,hmac_key_version,tag_kind,tag_digest,generation,updated_at
            ) VALUES(
              'tenant_default',1,'tag',
              '0000000000000000000000000000000000000000000000000000000000000000',
              1,'2026-08-10T00:00:00Z'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    guard = RUNTIME.verify_business_down_guard(database, snapshot, work)
    assert guard == {
        "blocked": True,
        "error_code": "response_cache.maintenance_down_blocked",
        "schema_unchanged": True,
        "database_unchanged": True,
        "latest_migration": "0069",
    }


def test_evidence_is_exact_whitelist_and_sha_only(tmp_path: Path) -> None:
    """最终目录只允许安全 JSON 与单行 SHA，不得容纳路径或隐私形态字段。"""

    artifact = tmp_path / "artifact"
    provenance = RUNTIME.capture_source_provenance()
    facts = RUNTIME.empty_safe_facts()
    evidence_path = RUNTIME.write_evidence(
        artifact,
        facts,
        {"files_scanned": 0, "plaintext_matches": 0, "sqlite_sidecars_remaining": 0},
        provenance,
        (),
    )
    assert {item.name for item in artifact.iterdir()} == {
        "runtime-acceptance.json",
        "SHA256SUMS.txt",
    }
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert set(evidence) == RUNTIME.EVIDENCE_TOP_LEVEL_KEYS
    assert evidence["source_provenance"] == {"algorithm": "sha256", **provenance}
    manifest = (artifact / "SHA256SUMS.txt").read_text(encoding="ascii")
    assert manifest == (
        f"{hashlib.sha256(evidence_path.read_bytes()).hexdigest()}  "
        "runtime-acceptance.json\n"
    )


@pytest.mark.parametrize(
    "forbidden",
    (
        {"prompt": "masked"},
        {"response": "masked"},
        {"provider_key": "masked"},
        {"token": "masked"},
        {"secret": "masked"},
        {"database_path": "masked"},
        {"endpoint": "masked"},
    ),
)
def test_evidence_rejects_forbidden_field_shapes(forbidden: dict[str, str]) -> None:
    """即使值已掩码，Prompt/Response/Key/Token/Secret/路径形态的键也不得出现。"""

    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.assert_safe_evidence(forbidden, ())
    assert failure.value.stage == "evidence.privacy"
    assert failure.value.code == "forbidden_key"


def test_run_rechecks_live_source_and_snapshot_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源文件或冻结快照漂移后不得发布 final 证据。"""

    work = tmp_path / "work"
    work.mkdir()
    artifact = tmp_path / "artifact"
    snapshot_dir = work / "migration-snapshot"
    snapshot_dir.mkdir()
    base = {name: "0" * 64 for name in RUNTIME.SOURCE_PROVENANCE_NAMES}
    drifted = {**base, "migration_set": "1" * 64}
    snapshot = RUNTIME.synthetic_snapshot(snapshot_dir)
    reads = 0
    published: list[Path] = []

    def fake_capture(directory: Path | None = None):
        """该测试冻结验收器的租户、恢复、证据和隐私边界，防止后续修改绕过门禁。 当前定义负责 fake_capture 对应步骤。"""
        nonlocal reads
        if directory is None:
            reads += 1
            return drifted if reads > 1 else base
        return base

    monkeypatch.setattr(RUNTIME, "capture_source_provenance", fake_capture)
    monkeypatch.setattr(RUNTIME, "assert_ports_available", lambda: None)
    monkeypatch.setattr(RUNTIME, "create_run_directories", lambda _run_id: (work, artifact))
    monkeypatch.setattr(RUNTIME, "create_migration_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(RUNTIME, "prepare_migration_database", lambda *_args: {})
    monkeypatch.setattr(
        RUNTIME,
        "execute_business_runtime",
        lambda *_args: (RUNTIME.empty_safe_facts(), ()),
    )
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
        """该测试冻结验收器的租户、恢复、证据和隐私边界，防止后续修改绕过门禁。 当前定义负责 record_publish 对应步骤。"""
        published.append(target)
        return target

    monkeypatch.setattr(RUNTIME, "write_evidence", record_publish)
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.run("prd076-source-drift")
    assert (failure.value.stage, failure.value.code) == (
        "evidence.provenance",
        "source_changed",
    )
    assert published == []
    assert not artifact.exists()


def test_recovered_hit_waits_through_short_settle_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claimed source may remain leased across multiple short polling windows.

    该测试冻结验收器的租户、恢复、证据和隐私边界，防止后续修改绕过门禁。 当前定义负责 test_recovered_hit_waits_through_short_settle_timeouts 对应步骤。"""

    calls = 0
    settled = {"pending": 0, "leased": 0, "dispatched": 1}

    def wait_for_settlement(_graph: object, *, timeout: float) -> dict[str, int]:
        """该测试冻结验收器的租户、恢复、证据和隐私边界，防止后续修改绕过门禁。 当前定义负责 wait_for_settlement 对应步骤。"""
        nonlocal calls
        calls += 1
        assert timeout == 2.5
        if calls <= 2:
            raise RUNTIME.AcceptanceFailure("recovery.outbox", "settle_timeout")
        return settled

    class WebhookSnapshot:
        """该测试冻结验收器的租户、恢复、证据和隐私边界，防止后续修改绕过门禁。 当前定义负责 WebhookSnapshot 对应步骤。"""

        @staticmethod
        def snapshot() -> dict[str, int]:
            """该测试冻结验收器的租户、恢复、证据和隐私边界，防止后续修改绕过门禁。 当前定义负责 snapshot 对应步骤。"""
            # 该测试冻结验收器的租户、恢复、证据和隐私边界，防止后续修改绕过门禁。
            return {"hits": 2}

    monkeypatch.setattr(RUNTIME, "wait_outbox_settled", wait_for_settlement)
    result = RUNTIME.wait_for_recovered_hit(
        object(),
        WebhookSnapshot(),
        baseline_hits=1,
    )

    assert calls == 3
    assert result is settled


@pytest.mark.parametrize(
    ("stage", "code"),
    (
        ("recovery.outbox", "database_unavailable"),
        ("recovery.other", "settle_timeout"),
    ),
)
def test_recovered_hit_does_not_swallow_non_target_failures(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    code: str,
) -> None:
    """Only the exact bounded-settlement timeout is retryable.

    该测试冻结验收器的租户、恢复、证据和隐私边界，防止后续修改绕过门禁。 当前定义负责 test_recovered_hit_does_not_swallow_non_target_failures 对应步骤。"""

    expected = RUNTIME.AcceptanceFailure(stage, code)

    def fail_wait(_graph: object, *, timeout: float) -> dict[str, int]:
        """该测试冻结验收器的租户、恢复、证据和隐私边界，防止后续修改绕过门禁。 当前定义负责 fail_wait 对应步骤。"""
        assert timeout == 2.5
        raise expected

    monkeypatch.setattr(RUNTIME, "wait_outbox_settled", fail_wait)
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.wait_for_recovered_hit(object(), object(), baseline_hits=0)

    assert failure.value is expected


def test_main_uses_fixed_errors_without_echoing_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """未知参数即使携带私有值也只能输出固定错误坐标。"""

    private_value = "C:/private/secret-token"
    assert RUNTIME.main(["--unknown", private_value]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "PRD-076 runtime acceptance failed: config.arguments:invalid\n"
    )
    assert private_value not in captured.err


def _complete_runtime_coverage() -> dict[str, object]:
    """构造只含发布门禁所需布尔和计数的安全 Runtime 覆盖事实。"""

    return {
        "isolation": {
            "tenant_mismatch_rejected": True,
            "virtual_key_miss": True,
            "same_model_data_class_miss": True,
            "resolved_model_miss": True,
            "prompt_miss": True,
            "second_tenant_legal_miss": True,
        },
        "cache_flow": {
            "miss_fill_hit": True,
            "hit_provider_delta": 0,
            "ttl_boundary_miss": True,
            "concurrent_single_fill_owner": True,
            "concurrent_fill_owner_count": 1,
            "concurrent_non_owner_count": 1,
        },
        "billing": {
            "charge_off_zero": True,
            "charge_on_positive": True,
            "budget_exhaustion_blocked_before_provider": True,
            "budget_block_provider_delta": 0,
        },
        "recovery": {
            "control_outage_source_pending": True,
            "control_restart_dispatched": True,
            "failure_spool_record_created": True,
            "commit_unknown_recovered": True,
            "failure_spool_pending": 0,
            "failure_spool_processing": 0,
            "failure_spool_quarantine": 0,
        },
    }


def test_runtime_coverage_requires_exact_five_dimension_misses() -> None:
    """五维不能用泛化结论替代，尤其要有同 model 的 data class 和第二租户合法 miss。"""

    facts = _complete_runtime_coverage()
    RUNTIME.validate_runtime_coverage(facts)
    for name in (
        "tenant_mismatch_rejected",
        "virtual_key_miss",
        "same_model_data_class_miss",
        "resolved_model_miss",
        "prompt_miss",
        "second_tenant_legal_miss",
    ):
        changed = json.loads(json.dumps(facts))
        changed["isolation"][name] = False
        with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
            RUNTIME.validate_runtime_coverage(changed)
        assert (failure.value.stage, failure.value.code) == (
            "evidence.runtime_coverage",
            "incomplete",
        )


@pytest.mark.parametrize(
    ("section", "name"),
    (
        ("cache_flow", "ttl_boundary_miss"),
        ("cache_flow", "concurrent_single_fill_owner"),
        ("billing", "budget_exhaustion_blocked_before_provider"),
    ),
)
def test_runtime_coverage_requires_ttl_concurrency_and_budget_preprovider_block(
    section: str,
    name: str,
) -> None:
    """TTL、并发与预算必须是实际可判定事实，不能只依赖已有分层单测。"""

    facts = _complete_runtime_coverage()
    facts[section][name] = False
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.validate_runtime_coverage(facts)
    assert (failure.value.stage, failure.value.code) == (
        "evidence.runtime_coverage",
        "incomplete",
    )


@pytest.mark.parametrize(
    ("name", "invalid_value"),
    (
        ("failure_spool_record_created", False),
        ("commit_unknown_recovered", False),
        ("failure_spool_pending", 1),
        ("failure_spool_processing", 1),
        ("failure_spool_quarantine", 1),
    ),
)
def test_runtime_coverage_rejects_empty_directory_only_failure_spool_claims(
    name: str,
    invalid_value: bool | int,
) -> None:
    """failure spool 必须真实创建并恢复；仅看到最终目录为空不足以发布。"""

    facts = _complete_runtime_coverage()
    facts["recovery"][name] = invalid_value
    with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
        RUNTIME.validate_runtime_coverage(facts)
    assert (failure.value.stage, failure.value.code) == (
        "evidence.runtime_coverage",
        "incomplete",
    )


def test_runtime_coverage_requires_zero_provider_delta_for_hit_and_budget_block() -> None:
    """命中和预算阻断都必须以 Provider 调用计数不增长作独立回证。"""

    for section, name in (
        ("cache_flow", "hit_provider_delta"),
        ("billing", "budget_block_provider_delta"),
    ):
        facts = _complete_runtime_coverage()
        facts[section][name] = 1
        with pytest.raises(RUNTIME.AcceptanceFailure) as failure:
            RUNTIME.validate_runtime_coverage(facts)
        assert (failure.value.stage, failure.value.code) == (
            "evidence.runtime_coverage",
            "incomplete",
        )
