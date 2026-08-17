"""验证 PAGE-117 浏览器验收的目录归属、迁移、权限与证据契约。"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import threading
import uuid
from pathlib import Path

import pytest
from metahub_shared.db import connect_database

# 仓库根目录与被测入口共同限定测试只能加载仓库内的浏览器验收实现。
ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "scripts" / "acceptance" / "prd085-browser-harness.py"


def _load_harness():
    """按固定模块名加载浏览器验收脚本，避免测试期间重复执行模块初始化。"""
    name = "_metahub_prd085_browser_harness_tests"
    current = sys.modules.get(name)
    if current is not None:
        return current
    specification = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


# 被测模块只初始化一次，供全部合同用例共享同一组静态契约定义。
HARNESS = _load_harness()


def _run_id(label: str) -> str:
    """生成仅供单个测试使用的唯一运行标识，防止临时目录相互冲突。"""
    return f"unit-{label}-{uuid.uuid4().hex}"


def _remove_claimed(path: Path, root: Path, owner: str) -> None:
    """为测试目录补齐归属声明后，调用生产清理逻辑删除精确子目录。"""
    if not path.exists():
        return
    claim = path / HARNESS.OWNER_FILENAME
    if not claim.exists():
        claim.write_text(f"{owner}\n", encoding="ascii")
    HARNESS._remove_owned_directory(path, root, owner, stage="cleanup.unit")


def _complete_facts() -> dict[str, object]:
    """构造满足浏览器验收精确模式的完整事实集合，供正反例复用。"""
    desktop = {name: True for name in HARNESS.DESKTOP_FACT_KEYS}
    desktop.update({"width": 1280, "height": 720, "overlap_count": 0})
    mobile = {name: True for name in HARNESS.MOBILE_FACT_KEYS}
    mobile.update({"width": 390, "height": 844, "overlap_count": 0})
    return {
        "status": "passed",
        "desktop": desktop,
        "mobile": mobile,
        "lifecycle": {name: True for name in HARNESS.LIFECYCLE_FACT_KEYS},
        "network": {
            "external_requests": 0,
            "forbidden_queries": 0,
            "console_errors": 0,
            "page_errors": 0,
            "login_statuses": [200, 200, 200, 200, 200, 200],
            "logout_statuses": [200, 200, 401, 200, 200],
            "funnel_get_count": 9,
            "funnel_statuses": [200, 200, 200, 403, 200, 401, 200, 401, 200],
            "no_store_responses": True,
            "request_contract_exact": True,
            "no_response_body_capture": True,
        },
        "screenshots": list(HARNESS.SCREENSHOTS),
        "all_108_prds_complete": False,
    }


def test_repository_boundaries_and_artifact_whitelist_are_frozen() -> None:
    """确认运行目录、迁移版本、产物白名单和来源文件均固定在仓库边界内。"""
    assert HARNESS.ROOT == ROOT
    assert HARNESS.WORK_ROOT == ROOT / ".tmp" / "prd085-browser-harness"
    assert HARNESS.ARTIFACT_ROOT == ROOT / ".artifacts" / "prd085-browser"
    assert HARNESS.HARNESS_PORT == 18887
    assert HARNESS.EXPECTED_MIGRATION_COUNT == 78
    assert HARNESS.EXPECTED_MIGRATION_VERSION == "0078"
    assert HARNESS.PREVIOUS_MIGRATION_VERSION == "0077"
    assert HARNESS.ALLOWED_ARTIFACTS == frozenset(
        (*HARNESS.SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json")
    )
    assert all(path.resolve().is_relative_to(ROOT) for path in HARNESS.SOURCE_FILES.values())
    assert {"migration_up", "migration_down", "browser_base_prd083"} <= set(
        HARNESS.SOURCE_FILES
    )


def test_owned_directories_reject_wrong_owner_and_clean_exact_children() -> None:
    """确认错误所有者不能清理目录，而正确所有者仅清理本次运行目录。"""
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, final = HARNESS.create_run_directories(_run_id("owner"), owner)
    try:
        assert not final.exists()
        with pytest.raises(HARNESS.AcceptanceFailure) as denied:
            HARNESS._remove_owned_directory(
                work, HARNESS.WORK_ROOT, "0" * 64, stage="cleanup.unit"
            )
        assert (denied.value.stage, denied.value.code) == ("cleanup.unit", "owner")
        assert work.exists()
    finally:
        _remove_claimed(staging, HARNESS.ARTIFACT_ROOT, owner)
        _remove_claimed(work, HARNESS.WORK_ROOT, owner)


def test_empty_database_round_trip_seeds_page_117_tenants_and_revision_20() -> None:
    """确认空库迁移往返后保留双租户权限种子、事件与数据库完整性。"""
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, final = HARNESS.create_run_directories(_run_id("migration"), owner)
    graph = None
    try:
        graph = HARNESS.prepare_database(work, HARNESS.RUNTIME.RuntimeSecrets.create())
        assert graph.migration == {
            "fresh": {
                "count": 78,
                "version": "0078",
                "integrity": "ok",
                "foreign_key_violations": 0,
            },
            "repeat": {
                "count": 78,
                "version": "0078",
                "integrity": "ok",
                "foreign_key_violations": 0,
            },
            "down": {
                "count": 77,
                "version": "0077",
                "integrity": "ok",
                "foreign_key_violations": 0,
            },
            "reup": {
                "count": 78,
                "version": "0078",
                "integrity": "ok",
                "foreign_key_violations": 0,
            },
        }
        connection = connect_database(graph.database_url)
        try:
            grants = dict(
                connection.execute(
                    """
                    SELECT role.code,COUNT(permission.permission_id) FROM roles role
                    LEFT JOIN role_permissions relation ON relation.tenant_id=role.tenant_id
                      AND relation.role_id=role.role_id
                    LEFT JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                      AND permission.permission_id=relation.permission_id
                      AND permission.code='product_analytics.read'
                    WHERE role.tenant_id=? AND role.code IN('ops_admin','developer')
                    GROUP BY role.code
                    """,
                    (graph.tenant_id,),
                ).fetchall()
            )
            assert grants == {"developer": 0, "ops_admin": 1}
            secondary = connection.execute(
                """
                SELECT COUNT(*) AS permission_count,MIN(permission.catalog_revision) AS revision
                FROM role_permissions relation
                JOIN roles role ON role.tenant_id=relation.tenant_id
                  AND role.role_id=relation.role_id
                JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                  AND permission.permission_id=relation.permission_id
                WHERE role.tenant_id=? AND role.code='admin'
                  AND permission.code='product_analytics.read'
                """,
                (HARNESS.SECONDARY_TENANT_ID,),
            ).fetchone()
            assert (secondary["permission_count"], secondary["revision"]) == (1, 20)
            assert HARNESS._event_counts(connection, graph.tenant_id) == (2, 2)
            assert HARNESS._event_counts(connection, HARNESS.SECONDARY_TENANT_ID) == (1, 1)
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            connection.close()
        assert not final.exists()
        assert not (staging / "browser-acceptance.json").exists()
        assert not (staging / "sha256-manifest.json").exists()
    finally:
        if graph is not None:
            HARNESS.RUNTIME.quiesce_database(graph)
        _remove_claimed(staging, HARNESS.ARTIFACT_ROOT, owner)
        _remove_claimed(work, HARNESS.WORK_ROOT, owner)


def test_permission_drift_cycles_only_ops_product_analytics_read() -> None:
    """确认三次撤权与两次恢复只改变目标分析权限，并保持其他授权。"""
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, _final = HARNESS.create_run_directories(_run_id("revoke"), owner)
    graph = None
    try:
        graph = HARNESS.prepare_database(work, HARNESS.RUNTIME.RuntimeSecrets.create())
        candidate = object.__new__(HARNESS.BrowserHarness)
        candidate.graph = graph
        candidate.lock = threading.Lock()
        candidate.permissions_revoked = False
        candidate.permission_revocation_count = 0
        candidate.permission_restore_count = 0
        candidate.revoke_product_analytics_read()
        candidate.restore_product_analytics_read()
        candidate.revoke_product_analytics_read()
        candidate.restore_product_analytics_read()
        candidate.revoke_product_analytics_read()
        connection = connect_database(graph.database_url)
        try:
            product_grants = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM role_permissions relation
                    JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                      AND permission.permission_id=relation.permission_id
                    WHERE relation.tenant_id=? AND relation.role_id=?
                      AND permission.code='product_analytics.read'
                    """,
                    (graph.tenant_id, graph.authorized_role_id),
                ).fetchone()[0]
            )
            retained = int(
                connection.execute(
                    "SELECT COUNT(*) FROM role_permissions WHERE tenant_id=? AND role_id=?",
                    (graph.tenant_id, graph.authorized_role_id),
                ).fetchone()[0]
            )
            assert product_grants == 0
            assert retained > 0
            assert candidate.permissions_revoked is True
            assert candidate.permission_revocation_count == 3
            assert candidate.permission_restore_count == 2
        finally:
            connection.close()
    finally:
        if graph is not None:
            HARNESS.RUNTIME.quiesce_database(graph)
        _remove_claimed(staging, HARNESS.ARTIFACT_ROOT, owner)
        _remove_claimed(work, HARNESS.WORK_ROOT, owner)


def test_browser_fact_validator_is_exact_and_keeps_global_incomplete() -> None:
    """确认浏览器事实采用精确契约，且总体 PRD 完成标志必须保持未完成。"""
    facts = _complete_facts()
    assert HARNESS.validate_browser_facts(facts) is facts
    facts["all_108_prds_complete"] = True
    with pytest.raises(HARNESS.AcceptanceFailure):
        HARNESS.validate_browser_facts(facts)


def test_source_snapshot_and_secrets_are_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """确认来源快照漂移会失败，运行凭据也不会通过对象表示泄露。"""
    snapshot = HARNESS._source_provenance()
    changed = {**snapshot, "migration_up": "0" * 64}
    monkeypatch.setattr(HARNESS, "_source_provenance", lambda: changed)
    with pytest.raises(HARNESS.AcceptanceFailure):
        HARNESS._assert_source_snapshot(snapshot)

    monkeypatch.setenv(HARNESS.ADMIN_PASSWORD_ENV, "Admin-Aa1-" + "a" * 40)
    monkeypatch.setenv(HARNESS.ROLE_PASSWORD_ENV, "Role-Aa1-" + "r" * 40)
    monkeypatch.setenv(HARNESS.STOP_TOKEN_ENV, "Stop-" + "s" * 64)
    secrets_ = HARNESS.BrowserSecrets.from_environment()
    rendered = repr(secrets_)
    assert secrets_.runtime.admin_password not in rendered
    assert secrets_.runtime.role_password not in rendered
    assert secrets_.stop_token not in rendered
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert '"all_108_prds_complete": False' in source
    assert "sandbox_permissions" not in source


def test_privacy_diagnostic_rejects_private_values_in_every_scope() -> None:
    """确认工作区与暂存区中的浏览器私密值都会触发隐私诊断失败。"""
    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    work, staging, _final = HARNESS.create_run_directories(_run_id("privacy"), owner)
    candidate = object.__new__(HARNESS.BrowserHarness)
    candidate.secrets = HARNESS.BrowserSecrets(
        runtime=HARNESS.RUNTIME.RuntimeSecrets.create(),
        stop_token="Stop-" + "s" * 64,
    )
    private_session = "Session-" + "b" * 48
    candidate.private_extra_labels = ("browser_session",)
    candidate.private_extra = (private_session,)
    assert private_session in candidate._private_values_for_scope("work")
    assert private_session in candidate._private_values_for_scope("staging")

    try:
        evidence = staging / "evidence.json"
        evidence.write_text(private_session, encoding="utf-8")
        with pytest.raises(HARNESS.AcceptanceFailure) as exposed:
            candidate._diagnose_private_storage(staging, "staging")
        assert (exposed.value.stage, exposed.value.code) == (
            "privacy.diagnostic",
            "staging.json.browser_session",
        )
    finally:
        _remove_claimed(staging, HARNESS.ARTIFACT_ROOT, owner)
        _remove_claimed(work, HARNESS.WORK_ROOT, owner)


def test_abort_attempts_every_cleanup_and_never_overrides_primary_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """确认单项清理失败不会跳过其他目录、数据库静置或 HTTP socket 关闭。"""

    class FailingProcesses:
        """模拟进程树关闭失败，验证 abort 仍会继续后续资源收口。"""

        @staticmethod
        def close() -> None:
            """抛出固定异常，且异常不得逃出 abort 覆盖主失败。"""
            # 固定注入进程关闭失败，验证其余资源仍继续收口。
            raise RuntimeError("injected")

    class FailingServer:
        """记录 socket 关闭尝试并模拟 Windows 终态错误。"""

        def __init__(self) -> None:
            """初始化仅含调用次数的无私密测试替身。"""
            self.close_calls = 0

        def server_close(self) -> None:
            """登记关闭后抛错，验证 Harness 仍清空 server owner。"""
            self.close_calls += 1
            # socket 关闭异常只用于证明 server owner 最终仍会清空。
            raise RuntimeError("injected")

    owner = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    paths = tuple(tmp_path / name for name in ("final", "staging", "work"))
    for path in paths:
        path.mkdir()
        (path / HARNESS.OWNER_FILENAME).write_text(f"{owner}\n", encoding="ascii")
    cleanup_attempts: list[Path] = []
    quiesce_attempts: list[object] = []

    def failing_remove(path: Path, *_args, **_kwargs) -> None:
        """每个目录均登记后失败，证明循环不会在首项提前退出。"""
        cleanup_attempts.append(path)
        # 每项都失败才能证明三个目录之间没有异常短路。
        raise RuntimeError("injected")

    def failing_quiesce(graph: object) -> None:
        """登记数据库静置调用后失败，后续目录和 socket 仍必须执行。"""
        quiesce_attempts.append(graph)
        # 数据库静置失败不能阻止目录与 HTTP server 清理。
        raise RuntimeError("injected")

    monkeypatch.setattr(HARNESS, "_remove_owned_directory", failing_remove)
    monkeypatch.setattr(HARNESS.RUNTIME, "quiesce_database", failing_quiesce)
    candidate = object.__new__(HARNESS.BrowserHarness)
    candidate.processes = FailingProcesses()
    candidate.graph = object()
    candidate.final, candidate.staging, candidate.work = paths
    candidate.owner = owner
    server = FailingServer()
    candidate.server = server

    candidate.abort()

    assert quiesce_attempts == [candidate.graph]
    assert cleanup_attempts == list(paths)
    assert server.close_calls == 1
    assert candidate.server is None
