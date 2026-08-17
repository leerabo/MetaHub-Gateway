"""验证 PRD-085 运行验收的迁移、证据、隐私、进程与清理契约。"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

# 仓库根目录与被测脚本路径限定动态加载及来源校验的可信边界。
ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "acceptance" / "prd085-runtime-acceptance.py"


def _load():
    """按稳定模块名加载运行验收脚本，并复用已完成初始化的模块实例。"""
    name = "_metahub_prd085_runtime_acceptance_tests"
    current = sys.modules.get(name)
    if current is not None:
        return current
    specification = importlib.util.spec_from_file_location(name, SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


module = _load()


def _complete_payload():
    """构造满足全部运行门禁的完整证据载荷，供精确契约测试复用。"""
    payload = module.empty_evidence_payload()
    payload.update(
        {
            "status": "passed",
            "generated_at": "2026-08-17T12:00:00Z",
            "migration": {
                "fresh": module._expected_migration(78, "0078"),
                "repeat": module._expected_migration(78, "0078"),
                "down": module._expected_migration(77, "0077"),
                "reup": module._expected_migration(78, "0078"),
                "checksum_verified": True,
            },
            "services": dict(module.SERVICE_FACTS),
            "workflow": dict(module.WORKFLOW_FACTS),
            "funnel": {
                "stage_count": 7,
                "all_stages_reached": True,
                "activated": True,
            },
            "traceability": {
                "successful_request_rows": 1,
                "failed_request_rows": 1,
                "usage_rows": 1,
                "usage_total_tokens": 12,
                "positive_cost": True,
                "product_event_rows": 7,
                "source_outbox_sent_rows": 6,
                "webhook_success_rows": 6,
            },
            "database": {
                "integrity": "ok",
                "foreign_key_violations": 0,
                "latest_migration": "0078",
                "nonterminal_outbox_rows": 0,
            },
            "privacy": {
                "service_log_canary_hits": 0,
                "database_secret_canary_hits": 0,
                "formal_evidence_canary_hits": 0,
                "forbidden_key_matches": 0,
            },
            "cleanup": dict(module.CLEANUP_FACTS),
            "source_provenance": module.source_provenance(),
        }
    )
    return payload


def test_empty_evidence_is_explicitly_incomplete_and_exact() -> None:
    """确认初始证据字段集合精确且明确标记为尚未完成验收。"""
    payload = module.empty_evidence_payload()
    assert set(payload) == module.EVIDENCE_TOP_LEVEL_KEYS
    assert payload["prd_id"] == "PRD-085"
    assert payload["status"] == "incomplete"
    assert payload["all_108_prds_complete"] is False


def test_complete_evidence_accepts_only_the_0078_reversible_gate() -> None:
    """确认完整证据只接受包含 0078 可逆迁移结果的验收事实。"""
    module._validate_evidence(_complete_payload())


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("migration", "fresh", "version"), "0077"),
        (("migration", "down", "count"), 78),
        (("migration", "checksum_verified"), False),
        (("services", "console_http"), False),
        (("workflow", "config_import_dual_mapping"), False),
        (("workflow", "outbox_retry_recovered"), False),
        (("funnel", "stage_count"), 6),
        (("funnel", "activated"), False),
        (("traceability", "failed_request_rows"), 0),
        (("traceability", "positive_cost"), False),
        (("database", "foreign_key_violations"), 1),
        (("privacy", "service_log_canary_hits"), 1),
        (("cleanup", "ports_released"), False),
    ],
)
def test_evidence_rejects_mutated_required_facts(path: tuple[str, ...], value: object) -> None:
    """确认任一必需事实被篡改时，证据校验都会稳定拒绝该载荷。"""
    payload = _complete_payload()
    cursor = payload
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = value
    with pytest.raises(module.AcceptanceFailure):
        module._validate_evidence(payload)


def test_evidence_rejects_deleted_sections_and_does_not_mutate() -> None:
    """确认校验过程不修改输入，并拒绝缺少必需分区的证据载荷。"""
    payload = _complete_payload()
    before = copy.deepcopy(payload)
    module._validate_evidence(payload)
    assert payload == before
    del payload["workflow"]
    with pytest.raises(module.AcceptanceFailure):
        module._validate_evidence(payload)


def test_safe_evidence_rejects_private_values_and_forbidden_keys() -> None:
    """确认正式证据拒绝私密值、产品标识误报和全部禁止字段名称。"""
    payload = _complete_payload()
    module.assert_safe_evidence(payload, ())
    private = "Prd085-Private-" + "x" * 64
    with pytest.raises(module.AcceptanceFailure):
        module.assert_safe_evidence(payload, ("PRD-085",))
    with pytest.raises(module.AcceptanceFailure):
        module.assert_safe_evidence({"safe": private}, (private,))
    for key in module.FORBIDDEN_EVIDENCE_KEYS:
        with pytest.raises(module.AcceptanceFailure):
            module.assert_safe_evidence({key: "redacted"}, ())


@pytest.mark.parametrize(
    "value",
    ["../escape", "..\\escape", "a/b", "a\\b", "ab", "a" * 81, " space"],
)
def test_run_id_rejects_escape_and_unbounded_values(value: str) -> None:
    """确认运行标识拒绝路径穿越、分隔符、空白及越界长度输入。"""
    with pytest.raises(module.AcceptanceFailure):
        module._safe_run_id(value)


def test_evidence_manifest_hashes_exact_persisted_bytes(tmp_path: Path) -> None:
    """确认清单哈希覆盖实际落盘字节，且同名正式证据不可被覆盖。"""
    serialized = '{"status":"passed"}\n'
    target, digest = module.publish_evidence(
        serialized,
        artifact_root=tmp_path,
        run_id="contract-run",
    )
    assert target.read_bytes() == serialized.encode()
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()
    assert (tmp_path / "contract-run.sha256").read_text(encoding="ascii") == (
        f"{digest}  contract-run.json\n"
    )
    with pytest.raises(module.AcceptanceFailure):
        module.publish_evidence(serialized, artifact_root=tmp_path, run_id="contract-run")


def test_receiver_fails_first_attempt_then_accepts_without_persisting_target() -> None:
    """确认回调接收器先故障再成功，并且只在内存保留签名与载荷。"""
    receiver = module.WebhookReceiver()
    target, _port = receiver.start()
    try:
        first = httpx.post(target, content=b"{}", timeout=3, trust_env=False)
        second = httpx.post(
            target,
            content=b"{}",
            headers={"X-MetaHub-Signature": "fixed-signature"},
            timeout=3,
            trust_env=False,
        )
    finally:
        receiver.stop()
    assert first.status_code == 503
    assert second.status_code == 204
    assert receiver.attempts == 2
    assert receiver.received == [("fixed-signature", b"{}")]


def test_control_retry_is_bounded_to_public_rbac_contention(monkeypatch) -> None:
    """确认控制面重试仅处理公开的权限竞争错误，其他错误立即返回。"""
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    responses = [
        httpx.Response(503, json={"code": "rbac.unavailable"}),
        httpx.Response(200, json={"status": "ok"}),
    ]
    assert module.transient_control_call(lambda: responses.pop(0)).status_code == 200
    unavailable = httpx.Response(503, json={"code": "product_analytics.unavailable"})
    assert module.transient_control_call(lambda: unavailable) is unavailable


def test_checkpoint_cleanup_removes_sqlite_sidecars(tmp_path: Path) -> None:
    """确认数据库检查点完成后等待并清除 SQLite 日志伴随文件。"""
    database = tmp_path / "runtime.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE facts(value INTEGER NOT NULL)")
    connection.execute("INSERT INTO facts(value) VALUES(1)")
    connection.commit()
    connection.close()
    module.checkpoint_and_wait_for_sidecars(database)
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()


def test_service_environment_enables_only_required_product_workers() -> None:
    """确认验收环境只启用产品分析必需工作器，并关闭无关后台任务。"""
    environment = module.service_environment(
        "sqlite:///private",
        control_port=18001,
        gateway_port=18002,
        console_port=18003,
        stub_port=18004,
        admin_password="private-admin",
        auth_secret="private-auth",
        encryption_secret="private-encryption",
    )
    assert environment["METAHUB_REQUEST_LOG_QUERY_EVENT_ENABLED"] == "true"
    assert environment["METAHUB_PRODUCT_ANALYTICS_OUTBOX_ENABLED"] == "true"
    assert environment["METAHUB_PRODUCT_ANALYTICS_OUTBOX_POLL_INTERVAL_SECONDS"] == "1"
    assert environment["METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED"] == "false"
    assert environment["METAHUB_PROVIDER_HEALTH_ENABLED"] == "false"
    assert environment["METAHUB_APPROVAL_OUTBOX_ENABLED"] == "false"


def test_source_provenance_is_sha256_and_repository_owned() -> None:
    """确认来源证明使用完整哈希，且所有被证明文件都属于当前仓库。"""
    provenance = module.source_provenance()
    assert set(provenance) == set(module.SOURCE_FILES)
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in provenance.values())
    assert all(
        path.resolve().is_relative_to(ROOT.resolve()) for path in module.SOURCE_FILES.values()
    )


def test_runtime_binaries_are_repository_owned() -> None:
    """确认 Python 与 Node 运行时均从仓库内固定工具路径启动。"""
    assert module.PYTHON == ROOT / ".venv" / "Scripts" / "python.exe"
    assert module.NODE == ROOT / ".tools" / "node-v22.23.2-win-arm64" / "node.exe"


def test_runtime_import_manifest_matches_current_config_transfer_contract() -> None:
    """确认运行验收导入清单仍符合当前配置迁移规范化契约。"""
    from metahub_control.config_transfer.service import _normalize_import_document

    plan = _normalize_import_document(module.import_manifest("http://127.0.0.1:54321"))
    assert plan.errors == []
    assert plan.rows["providers"][0].values["provider_type"] == "openai_compatible"
    assert plan.rows["aliases"][0].values["routing_mode"] == "fixed"
    assert plan.rows["virtual_keys"][0].values["rate_limit_status"] == "disabled"


def test_runner_freezes_real_process_privacy_and_product_event_contracts() -> None:
    """确认真实进程、业务路由、事件序列及隐私限制均固化在运行脚本中。"""
    source = SCRIPT.read_text(encoding="utf-8")
    assert module.EXPECTED_MIGRATION_COUNT == 78
    assert module.EXPECTED_MIGRATION_VERSION == "0078"
    assert module.PREVIOUS_MIGRATION_VERSION == "0077"
    assert module.EVENT_TYPES == (
        "tenant.registered",
        "provider.created",
        "virtual_key.created",
        "gateway.first_call_succeeded",
        "budget.configured",
        "teammate.invited",
        "request_logs.viewed",
    )
    for process in (
        "metahub_control.app:app",
        "metahub_gateway.app:app",
        'console-web" / "scripts" / "dev-server.mjs',
    ):
        assert process in source
    for route in (
        '"/config/import"',
        '"/providers"',
        '"/v1/chat/completions"',
        '"/budgets"',
        '"/invitations"',
        '"/request-logs?limit=10"',
        '"/product-analytics/funnel"',
    ):
        assert route in source
    assert '"product_event.recorded"' in source
    assert "rollback_last_migration" in source
    assert "verify_webhook_signature" in source
    assert '"all_108_prds_complete": False' in source
    assert "local-endpoints" not in source
    assert "sandbox_permissions" not in source
    assert "require_escalated" not in source


def test_formal_evidence_contains_no_private_network_or_identity_fields() -> None:
    """确认正式证据不包含私有网络地址、身份标识或敏感请求字段。"""
    payload = _complete_payload()
    encoded = json.dumps(payload, sort_keys=True).casefold()
    for name in (
        "endpoint_url",
        "database_url",
        "authorization",
        "plain_value",
        "provider_key",
        "virtual_key",
        "prompt",
        "response",
        "email",
        "request_id",
        "trace_id",
    ):
        assert f'"{name}"' not in encoded
