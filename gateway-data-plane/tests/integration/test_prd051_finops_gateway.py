"""PRD-051 Gateway FinOps 正式账本、持久 spool 与事件线程边界专项测试。

测试只使用随机测试 ID、固定测试价格和安全元数据；不会写入 Prompt、Response、
Provider Key、Token、真实 Endpoint 或调整原因。所有数据库查询都显式携带 tenant_id。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from metahub_gateway.api.openai_compatible import _enqueue_finops_ledger_event
from metahub_gateway.usage import ledger as ledger_module
from metahub_gateway.usage import spool as spool_module
from metahub_gateway.usage.ledger import (
    RequestLogContext,
    UsageLedgerIntent,
    _serialize_usage_spool_record,
    claim_gateway_request,
    record_request_log,
    safe_record_usage_ledger,
)
from metahub_gateway.usage.spool import (
    UsageSpool,
    UsageSpoolError,
    UsageSpoolReplayBatch,
    UsageSpoolReplaySummary,
    install_usage_spool_recovery,
    replay_usage_spool_batch,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event


@pytest.fixture(scope="module")
def gateway_config(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """创建只属于本模块的 0041 数据库和本地 spool 配置。

    参数：pytest 模块级临时目录工厂。返回：Gateway 账本与 spool 所需最小配置。
    关键约束：数据库和文件队列都位于 pytest basetemp，绝不回落到共享开发库。
    """

    root = tmp_path_factory.mktemp("prd051-finops-gateway")
    database_path = root / "gateway.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    apply_migrations(database_url)
    seed_result = apply_seed_data(database_url)
    return SimpleNamespace(
        database_url=database_url,
        tenant_id=seed_result.tenant_id,
        finops_usage_spool_root=str(root / "spool"),
        auth_token_secret="prd051-gateway-test-hmac-secret-32-characters",
        finops_usage_spool_claim_lease_seconds=60,
        finops_usage_spool_poll_interval_seconds=1,
    )


def _metadata(config: SimpleNamespace) -> dict[str, str]:
    """读取 default 租户的模型、Provider、Virtual Key 和环境安全 ID。"""

    connection = connect_database(config.database_url)
    try:
        row = connection.execute(
            """
            SELECT model.model_id, model.model_alias,
                   provider.provider_id, provider.provider_code,
                   key.virtual_key_id, environment.environment_id,
                   environment.code AS environment_code
            FROM models model
            JOIN providers provider
              ON provider.tenant_id=model.tenant_id
             AND provider.provider_id=model.provider_id
            JOIN virtual_keys key ON key.tenant_id=model.tenant_id
            JOIN environments environment ON environment.tenant_id=model.tenant_id
            WHERE model.tenant_id=? AND environment.code='dev'
            ORDER BY model.model_id, key.virtual_key_id
            LIMIT 1
            """,
            (config.tenant_id,),
        ).fetchone()
        assert row is not None
        return {name: str(row[name]) for name in row.keys()}
    finally:
        connection.close()


def _priced_context(config: SimpleNamespace, suffix: str) -> RequestLogContext:
    """创建已 claim、已记录且具备固定版本价格的同租户请求上下文。"""

    metadata = _metadata(config)
    connection = connect_database(config.database_url)
    try:
        # 固定测试价格让 spool 用例能证明恢复的是原证据，而不是退化为重放时现价。
        connection.execute(
            """
            UPDATE models
            SET input_price_per_1k='0.010000', output_price_per_1k='0.020000',
                currency='CNY', price_schema_version='prd051-test-v1'
            WHERE tenant_id=? AND model_id=?
            """,
            (config.tenant_id, metadata["model_id"]),
        )
        connection.commit()
    finally:
        connection.close()
    context = RequestLogContext(
        tenant_id=config.tenant_id,
        request_id=f"req-prd051-{suffix}",
        trace_id=f"trace-prd051-{suffix}",
        virtual_key_id=metadata["virtual_key_id"],
        environment_id=metadata["environment_id"],
        environment_code=metadata["environment_code"],
        provider_id=metadata["provider_id"],
        model_id=metadata["model_id"],
        endpoint="chat/completions",
        request_model=metadata["model_alias"],
        resolved_model=metadata["model_alias"],
        provider_code=metadata["provider_code"],
    )
    claim_gateway_request(config, context)
    record_request_log(
        config,
        context,
        status="success",
        error_code=None,
        latency_ms=12,
    )
    return context


def _usage() -> dict[str, object]:
    """返回金额可精确反算的安全归一 Usage。"""

    return {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cache_tokens": 0,
        "estimated": False,
        "usage_source": "upstream",
        "raw_usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _entry_count(config: SimpleNamespace, request_id: str) -> int:
    """按 tenant+request 统计正式 Usage 分录，禁止全局请求 ID 查询。"""

    connection = connect_database(config.database_url)
    try:
        row = connection.execute(
            """
            SELECT COUNT(*) AS total
            FROM finops_ledger_entries
            WHERE tenant_id=? AND source_request_id=? AND entry_type='usage_charge'
            """,
            (config.tenant_id, request_id),
        ).fetchone()
        return int(row["total"])
    finally:
        connection.close()


def test_direct_commit_enqueues_only_created_event(gateway_config: SimpleNamespace) -> None:
    """主库 commit 返回正式分录，created 只入队一次而 existing 不重复发送。"""

    context = _priced_context(gateway_config, "direct")
    committed = safe_record_usage_ledger(
        gateway_config,
        context,
        _usage(),
        require_durable=True,
    )
    assert committed is not None and committed.created is True
    assert committed.price_evidence_status == "priced"
    assert committed.price_snapshot_source == "model_catalog"
    assert committed.amount == "0.002000"
    state = SimpleNamespace(runtime_event_queue=asyncio.Queue())
    assert _enqueue_finops_ledger_event(state, committed, context) is True
    assert _enqueue_finops_ledger_event(
        state, replace(committed, created=False), context
    ) is False
    event = state.runtime_event_queue.get_nowait()
    assert event.event_type == "ledger.entry_created"
    assert event.event_id == committed.event_id
    assert state.runtime_event_queue.empty()
    assert _entry_count(gateway_config, context.request_id) == 1


class _FailUsageInsertConnection:
    """允许选价和快照 INSERT，但在 Usage INSERT 注入一次 SQLite 故障。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """保存真实连接，事务/关闭操作仍完整委托。"""

        self.connection = connection

    def execute(self, statement: str, parameters: object = ()) -> sqlite3.Cursor:
        """在 PriceSnapshot 已选定后失败，验证 spool 捕获原价格证据。"""

        # 只拦截 Usage INSERT，确保前置选价和快照写入真实执行后再模拟故障。
        if "INSERT INTO usage_ledger" in statement:
            raise sqlite3.OperationalError("injected_usage_insert_failure")
        return self.connection.execute(statement, parameters)

    def commit(self) -> None:
        """委托提交；故障用例正常不会抵达这里。"""

        self.connection.commit()

    def rollback(self) -> None:
        """委托回滚，确保故障前插入的快照不会形成半事实。"""

        self.connection.rollback()

    def close(self) -> None:
        """确定性释放真实 SQLite 连接，避免故障注入用例遗留文件句柄。"""

        self.connection.close()


class _FailPriceSnapshotInsertConnection(_FailUsageInsertConnection):
    """在选价查询完成后、PriceSnapshot INSERT 时注入一次 SQLite 故障。"""

    def execute(self, statement: str, parameters: object = ()) -> sqlite3.Cursor:
        """拒绝快照 INSERT，其他查询直接委托，验证写入前证据捕获边界。"""

        # 选价 SELECT 正常执行，仅在快照持久化点失败以覆盖最窄故障窗口。
        if "INSERT INTO price_snapshots" in statement:
            raise sqlite3.OperationalError("injected_price_snapshot_insert_failure")
        return self.connection.execute(statement, parameters)


def test_database_failure_spools_and_replays_exactly_once(
    gateway_config: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快照后数据库故障先 fsync spool，恢复后保留原价且正式分录/事件各一次。"""

    context = replace(
        _priced_context(gateway_config, "replay"),
        intent_risk_result_id="irr_" + "5" * 32,
        intent_risk_schedule_status="queued",
        intent_risk_mode="async_light",
        intent_risk_reason_code="queued",
        intent_risk_detector_version="intent-rule-engine-v1",
    )
    original_connect = ledger_module._connect
    monkeypatch.setattr(
        ledger_module,
        "_connect",
        lambda config: _FailUsageInsertConnection(connect_database(config.database_url)),
    )
    assert (
        safe_record_usage_ledger(
            gateway_config,
            context,
            _usage(),
            require_durable=True,
        )
        is None
    )
    monkeypatch.setattr(ledger_module, "_connect", original_connect)
    spool = UsageSpool(gateway_config)
    pending = list(spool.pending.glob("*.json"))
    assert len(pending) == 1
    # 文件名是 HMAC，不得直接暴露 tenant/request/Usage ID。
    assert gateway_config.tenant_id not in pending[0].name
    assert context.request_id not in pending[0].name

    async def replay_twice() -> tuple[UsageSpoolReplaySummary, UsageSpoolReplaySummary, Any]:
        """在同一事件循环创建队列并执行两轮恢复，第二轮必须为空。"""

        state = SimpleNamespace(runtime_event_queue=asyncio.Queue())
        first = await replay_usage_spool_batch(gateway_config, state)
        second = await replay_usage_spool_batch(gateway_config, state)
        return first, second, state.runtime_event_queue

    first, second, queue = asyncio.run(replay_twice())
    assert first == UsageSpoolReplaySummary(
        claimed=1,
        committed=1,
        existing=0,
        deferred=0,
        events_enqueued=1,
        available=True,
    )
    assert second.claimed == second.committed == second.existing == 0
    assert second.events_enqueued == 0
    event = queue.get_nowait()
    assert event.event_type == "ledger.entry_created"
    assert queue.empty()
    assert _entry_count(gateway_config, context.request_id) == 1
    connection = connect_database(gateway_config.database_url)
    try:
        row = connection.execute(
            """
            SELECT entry.price_schema_version, entry.price_snapshot_source,
                   entry.price_evidence_status, entry.posting_source,
                   request.intent_risk_result_id, request.intent_risk_schedule_status
            FROM finops_ledger_entries entry
            JOIN request_logs request
              ON request.tenant_id=entry.tenant_id
             AND request.request_id=entry.source_request_id
            WHERE entry.tenant_id=? AND entry.source_request_id=?
            """,
            (gateway_config.tenant_id, context.request_id),
        ).fetchone()
        assert dict(row) == {
            "price_schema_version": "prd051-test-v1",
            "price_snapshot_source": "model_catalog",
            "price_evidence_status": "priced",
            "posting_source": "spool_replay",
            "intent_risk_result_id": "irr_" + "5" * 32,
            "intent_risk_schedule_status": "queued",
        }
    finally:
        connection.close()
    assert not list(spool.pending.glob("*.json"))
    assert not list(spool.processing.glob("*.claim"))


def test_price_snapshot_insert_failure_preserves_original_evidence(
    gateway_config: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快照 INSERT 自身失败时，spool 仍保存已选择单价并按原证据恢复。"""

    context = _priced_context(gateway_config, "snapshot-insert-failure")
    original_connect = ledger_module._connect
    monkeypatch.setattr(
        ledger_module,
        "_connect",
        lambda config: _FailPriceSnapshotInsertConnection(
            connect_database(config.database_url)
        ),
    )
    assert (
        safe_record_usage_ledger(
            gateway_config,
            context,
            _usage(),
            require_durable=True,
        )
        is None
    )
    monkeypatch.setattr(ledger_module, "_connect", original_connect)

    spool = UsageSpool(gateway_config)
    claims = spool.claim_batch()
    assert len(claims) == 1
    captured = claims[0].payload["estimate"]
    assert captured["price_schema_version"] == "prd051-test-v1"
    assert captured["amount"] == "0.002000"
    assert captured["evidence"]["source"] == "model_catalog"
    spool.release(claims[0])

    async def replay_once() -> tuple[UsageSpoolReplaySummary, asyncio.Queue[Any]]:
        """恢复单条故障记录并返回摘要和事件队列。"""

        state = SimpleNamespace(runtime_event_queue=asyncio.Queue())
        summary = await replay_usage_spool_batch(gateway_config, state)
        return summary, state.runtime_event_queue

    summary, queue = asyncio.run(replay_once())
    assert summary.committed == summary.events_enqueued == 1
    assert queue.qsize() == 1
    connection = connect_database(gateway_config.database_url)
    try:
        row = connection.execute(
            """
            SELECT price_schema_version, price_snapshot_source, price_evidence_status
            FROM finops_ledger_entries
            WHERE tenant_id=? AND source_request_id=?
            """,
            (gateway_config.tenant_id, context.request_id),
        ).fetchone()
        assert dict(row) == {
            "price_schema_version": "prd051-test-v1",
            "price_snapshot_source": "model_catalog",
            "price_evidence_status": "priced",
        }
    finally:
        connection.close()


def _store_tampered_spool_record(tmp_path: Path, *, root_name: str) -> UsageSpool:
    """创建一条 HMAC 已失配的 pending 记录，供普通与 Windows 回退路径复用。"""

    config = SimpleNamespace(
        finops_usage_spool_root=str(tmp_path / root_name),
        auth_token_secret="prd051-tamper-test-secret-at-least-32-characters",
        finops_usage_spool_claim_lease_seconds=60,
    )
    context = RequestLogContext(
        tenant_id="default",
        request_id="req-prd051-tamper",
        trace_id="trace-prd051-tamper",
    )
    intent = UsageLedgerIntent(
        usage_ledger_id="usg_" + "a" * 32,
        pricing_observed_at="2026-07-27T00:00:00Z",
        raw_usage_value={},
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        cache_tokens=0,
        estimated=False,
        usage_source="upstream",
        estimate_reason=None,
        provider_cost_amount=None,
        provider_cost_currency=None,
        provider_cost_cny=None,
        normalization_warnings=[],
    )
    spool = UsageSpool(config)
    assert spool.store(_serialize_usage_spool_record(context, intent)) is True
    record_path = next(spool.pending.glob("*.json"))
    envelope = json.loads(record_path.read_text(encoding="utf-8"))
    envelope["payload"]["context"]["request_id"] = "req-prd051-tampered"
    record_path.write_text(
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return spool


def test_tampered_spool_record_moves_to_quarantine(tmp_path: Path) -> None:
    """内容被改而 HMAC 未更新时绝不重放，并原子移动到 quarantine。"""

    spool = _store_tampered_spool_record(tmp_path, root_name="tamper-spool")
    assert spool.claim_batch() == []
    assert not list(spool.pending.glob("*.json"))
    assert len(list(spool.quarantine.glob("*.bad"))) == 1


def test_windows_empty_glob_falls_back_and_quarantines_tampered_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows 通配枚举持续为空时改用一次安全目录枚举，且仍执行完整 HMAC 校验。"""

    writer = _store_tampered_spool_record(tmp_path, root_name="windows-empty-spool")
    # 显式丢弃进程内提示，模拟 Gateway 重启后只凭持久目录恢复。
    published_path = next(writer.pending.glob("*.json"))
    writer._forget_pending(published_path)
    spool = UsageSpool(
        SimpleNamespace(
            finops_usage_spool_root=str(writer.root),
            auth_token_secret="prd051-tamper-test-secret-at-least-32-characters",
            finops_usage_spool_claim_lease_seconds=60,
        )
    )
    original_glob = Path.glob
    original_iterdir = Path.iterdir
    pending_scans = 0
    direct_scans = 0

    def empty_pending_glob(path: Path, pattern: str):
        """模拟 Windows 过滤驱动让全部有界 glob 重试都暂时不可见。"""

        nonlocal pending_scans
        if path == spool.pending and pattern == "*.json":
            pending_scans += 1
            return iter(())
        return original_glob(path, pattern)

    def counted_iterdir(path: Path):
        """记录直接目录枚举只发生一次，并返回真实目录内容。"""

        nonlocal direct_scans
        if path == spool.pending:
            direct_scans += 1
        return original_iterdir(path)

    monkeypatch.setattr(spool_module, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(Path, "glob", empty_pending_glob)
    monkeypatch.setattr(Path, "iterdir", counted_iterdir)
    assert spool.claim_batch() == []
    assert pending_scans == 3
    assert direct_scans == 1
    assert not list(original_glob(spool.pending, "*.json"))
    assert len(list(original_glob(spool.quarantine, "*.bad"))) == 1


def test_cross_instance_pending_hint_survives_empty_directory_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 writer/replay 的不同实例共享刚发布提示，并在目录暂不可见时安全领取。"""

    writer = _store_tampered_spool_record(tmp_path, root_name="pending-hint-spool")
    spool = UsageSpool(
        SimpleNamespace(
            finops_usage_spool_root=str(writer.root),
            auth_token_secret="prd051-tamper-test-secret-at-least-32-characters",
            finops_usage_spool_claim_lease_seconds=60,
        )
    )
    original_glob = Path.glob
    original_rename = spool_module.os.rename
    pending_scans = 0
    rename_attempts = 0

    def empty_pending_glob(path: Path, pattern: str):
        """模拟同进程 reader 暂时无法从通配枚举看见 writer 已发布的记录。"""

        nonlocal pending_scans
        if path == spool.pending and pattern == "*.json":
            pending_scans += 1
            return iter(())
        return original_glob(path, pattern)

    def delayed_pending_rename(source: object, destination: object) -> None:
        """模拟 Windows 精确路径在前三次原子领取时仍报告短暂不可见。"""

        nonlocal rename_attempts
        if Path(source).parent == spool.pending:
            rename_attempts += 1
            if rename_attempts <= 3:
                # 故障只覆盖有界重试窗口，第四次必须调用真实 rename 完成领取。
                raise FileNotFoundError
        original_rename(source, destination)

    monkeypatch.setattr(Path, "glob", empty_pending_glob)
    monkeypatch.setattr(spool_module, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(spool_module.os, "rename", delayed_pending_rename)
    monkeypatch.setattr(spool_module.time, "sleep", lambda _seconds: None)
    assert spool.claim_batch() == []
    assert pending_scans == 1
    assert rename_attempts == 4
    assert not list(original_glob(spool.pending, "*.json"))
    assert len(list(original_glob(spool.quarantine, "*.bad"))) == 1


def test_pending_hint_is_isolated_by_spool_root(tmp_path: Path) -> None:
    """进程内提示只能被同一 spool 根目录的 replay 实例看见。"""

    writer = _store_tampered_spool_record(tmp_path, root_name="isolated-writer-spool")
    published_path = next(writer.pending.glob("*.json"))
    other = UsageSpool(
        SimpleNamespace(
            finops_usage_spool_root=str(tmp_path / "isolated-reader-spool"),
            auth_token_secret="prd051-tamper-test-secret-at-least-32-characters",
            finops_usage_spool_claim_lease_seconds=60,
        )
    )
    try:
        assert published_path in writer._remembered_pending()
        assert other._remembered_pending() == set()
        assert other.claim_batch() == []
        assert published_path.is_file()
    finally:
        writer._forget_pending(published_path)


def test_spool_accepts_legacy_v1_v2_v3_v4_and_current_v5_payloads(tmp_path: Path) -> None:
    """升级兼容：精确接受历史 v1-v4 与输出安全 v5，未知版本仍拒绝。"""

    config = SimpleNamespace(
        finops_usage_spool_root=str(tmp_path / "versioned-spool"),
        auth_token_secret="prd068-version-test-secret-at-least-32-characters",
        finops_usage_spool_claim_lease_seconds=60,
    )
    context = RequestLogContext(
        tenant_id="default",
        request_id="req-prd068-spool-version",
        trace_id="trace-prd068-spool-version",
        policy_mask_applied=True,
        policy_mask_policy_id="pol_prd068_mask",
        output_safety_scan_id="oss_" + "f" * 32,
        output_safety_scan_status="completed",
        output_safety_decision="clean",
        billing_disposition="not_required",
    )
    intent = UsageLedgerIntent(
        usage_ledger_id="usg_" + "b" * 32,
        pricing_observed_at="2026-08-03T00:00:00Z",
        raw_usage_value={},
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        cache_tokens=0,
        estimated=False,
        usage_source="upstream",
        estimate_reason=None,
        provider_cost_amount=None,
        provider_cost_currency=None,
        provider_cost_cny=None,
        normalization_warnings=[],
    )
    current = _serialize_usage_spool_record(context, intent)
    spool = UsageSpool(config)
    assert current["schema_version"] == 5
    assert spool.store(current) is True

    # v4 含 PRD-070 会话风险字段但不含 PRD-071 输出安全四元组。
    v4 = {
        **current,
        "schema_version": 4,
        "context": {
            key: value
            for key, value in current["context"].items()
            if not key.startswith("output_safety_") and key != "billing_disposition"
        },
        "usage": {**current["usage"], "usage_ledger_id": "usg_" + "f" * 32},
    }
    assert spool.store(v4) is True

    # v3 含 PRD-069 intent 字段但不含 PRD-070 会话风险字段；升级期间必须继续可落盘。
    v3 = {
        **v4,
        "schema_version": 3,
        "context": {
            key: value
            for key, value in v4["context"].items()
            if not key.startswith("session_risk_")
        },
        "usage": {**current["usage"], "usage_ledger_id": "usg_" + "e" * 32},
    }
    assert spool.store(v3) is True

    # v2 含 PRD-068 mask 字段但不含 PRD-069 intent 字段，恢复时必须映射为 disabled。
    v2 = {
        **v3,
        "schema_version": 2,
        "context": {
            key: value
            for key, value in v3["context"].items()
            if not key.startswith("intent_risk_")
        },
        "usage": {**current["usage"], "usage_ledger_id": "usg_" + "d" * 32},
    }
    assert spool.store(v2) is True

    # v1 不含 PRD-068 mask 字段；保留读取兼容才能恢复升级前已经 fsync 的待入账事实。
    legacy = {
        **v2,
        "schema_version": 1,
        "context": {
            key: value
            for key, value in v2["context"].items()
            if key not in {"policy_mask_applied", "policy_mask_policy_id"}
        },
        "usage": {**current["usage"], "usage_ledger_id": "usg_" + "c" * 32},
    }
    assert spool.store(legacy) is True

    # 未来版本可能新增尚未审查的字段，旧进程不能把它宽松解释为当前合同。
    unsupported = {**current, "schema_version": 6}
    with pytest.raises(UsageSpoolError, match="finops_spool_payload_version_invalid"):
        spool.store(unsupported)


def test_non_stream_dual_failure_raises_uniform_error(
    gateway_config: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主库与 spool 同时失败时，非流式强持久合同抛固定业务码且不泄露路径。"""

    context = _priced_context(gateway_config, "dual-failure")

    def fail_database(_config: object) -> sqlite3.Connection:
        """注入不含真实路径的固定数据库故障。"""

        # 固定错误正文避免真实数据库路径进入测试失败输出或统一错误响应。
        raise DbError("db.connection_failed", "unavailable", "retry")

    def fail_spool(_config: object, _payload: dict[str, Any]) -> bool:
        """注入固定 spool 故障，验证强持久化分支返回统一业务错误。"""

        # 第二存储边界也失败时必须走固定 503，不能泄露文件系统目录或原异常。
        raise UsageSpoolError("finops_spool_publish_failed")

    monkeypatch.setattr(ledger_module, "_connect", fail_database)
    monkeypatch.setattr(spool_module, "store_usage_spool_record", fail_spool)
    with pytest.raises(ApiError) as raised:
        safe_record_usage_ledger(
            gateway_config,
            context,
            _usage(),
            require_durable=True,
        )
    assert raised.value.code == "finops.usage_record_unavailable"


def test_replay_enqueues_on_event_loop_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLite/file 收集在线程执行，asyncio Queue 入队严格回到调用事件循环线程。"""

    worker_threads: list[int] = []
    enqueue_threads: list[int] = []
    event = build_webhook_event(
        tenant_id="default",
        event_type="route.fallback_triggered",
        payload={"reason": "test"},
        trace_id="trace-prd051-thread",
    )

    def collect(_config: object, *, limit: int) -> UsageSpoolReplayBatch:
        """记录 to_thread 身份并返回一个不可变安全事件。"""

        assert limit == spool_module.REPLAY_BATCH_SIZE
        worker_threads.append(threading.get_ident())
        return UsageSpoolReplayBatch(
            summary=UsageSpoolReplaySummary(claimed=1, committed=1),
            events=(event,),
        )

    def enqueue(state: object, queued_event: object) -> bool:
        """记录入队线程并模拟成功，避免测试依赖真实 dispatcher worker。"""

        assert queued_event is event
        assert state is app_state
        enqueue_threads.append(threading.get_ident())
        return True

    app_state = SimpleNamespace()
    monkeypatch.setattr(spool_module, "_collect_usage_spool_batch", collect)
    monkeypatch.setattr(spool_module, "enqueue_runtime_event", enqueue)

    async def run() -> tuple[int, UsageSpoolReplaySummary]:
        """返回事件循环线程和恢复摘要。"""

        loop_thread = threading.get_ident()
        summary = await replay_usage_spool_batch(SimpleNamespace(), app_state)
        return loop_thread, summary

    loop_thread, summary = asyncio.run(run())
    assert worker_threads and worker_threads[0] != loop_thread
    assert enqueue_threads == [loop_thread]
    assert summary.events_enqueued == 1


def test_spool_lifespan_replays_periodically_and_stops_before_outer_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生命周期先恢复、再周期轮询，退出时先停 worker 后交还外层 lifespan。"""

    lifecycle_events: list[str] = []

    @asynccontextmanager
    async def outer_lifespan(_app: FastAPI):
        """记录被组合的外层 lifespan 进入和退出顺序。"""

        lifecycle_events.append("outer_enter")
        try:
            yield
        finally:
            lifecycle_events.append("outer_exit")

    async def fake_replay(_config: object, _state: object) -> UsageSpoolReplaySummary:
        """记录启动恢复和周期恢复调用，不执行真实文件或数据库操作。"""

        lifecycle_events.append("replay")
        return UsageSpoolReplaySummary(claimed=1, committed=1)

    app = FastAPI(lifespan=outer_lifespan)
    app.state.config = SimpleNamespace(finops_usage_spool_poll_interval_seconds=0.01)
    monkeypatch.setattr(spool_module, "replay_usage_spool_batch", fake_replay)
    install_usage_spool_recovery(app)

    async def run_lifespan() -> None:
        """运行完整组合生命周期并等待至少一次周期轮询。"""

        async with app.router.lifespan_context(app):
            assert lifecycle_events[:2] == ["outer_enter", "replay"]
            for _ in range(20):
                if lifecycle_events.count("replay") >= 2:
                    break
                await asyncio.sleep(0.005)
            assert lifecycle_events.count("replay") >= 2
            assert app.state.finops_usage_spool_summary.committed == 1

    asyncio.run(run_lifespan())
    assert lifecycle_events[-1] == "outer_exit"
    assert app.state.finops_usage_spool_summary is None
