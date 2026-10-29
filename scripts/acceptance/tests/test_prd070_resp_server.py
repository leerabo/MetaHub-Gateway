"""PRD-070 回环 RESP 验收服务与生产 Redis Store 的端到端合同测试。

测试只监听 ``127.0.0.1`` 动态端口，覆盖三轮累计、pending ACK、冻结、重复 turn、重置和
数据库快照恢复；不会访问外部 Redis，也不会把原始 Session/Turn Header 写入状态或日志。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest
from metahub_gateway.session_risk import (
    RedisSessionRiskStore,
    SessionRiskAssessment,
    SessionRiskScope,
)


def _load_server_module() -> ModuleType:
    """从带连字符的正式脚本路径加载验收服务模块，并保持唯一模块身份。"""

    path = Path(__file__).resolve().parents[1] / "prd070-resp-server.py"
    specification = importlib.util.spec_from_file_location("prd070_resp_server", path)
    if specification is None or specification.loader is None:
        # 无法加载正式脚本就不能用测试替身冒充验收服务，必须明确失败。
        raise AssertionError("unable to load PRD-070 RESP server")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


# 正式脚本只加载一次，避免多份内存状态令同一回环合同产生不同类身份。
SERVER = _load_server_module()


def _assessment(turn: int) -> SessionRiskAssessment:
    """构造一个可追踪但不含业务正文的高危当前轮摘要。"""

    digit = format(turn, "x")
    # 摘要只使用规范测试 HMAC，原始 Session/Turn Header 从未进入模型或服务状态。
    return SessionRiskAssessment(
        turn_key_digest="srh_" + digit * 64,
        turn_intent_digest="srh_" + format(turn + 3, "x") * 64,
        jailbreak_count_delta=1,
        sensitive_count_delta=0,
        tool_inducement_count_delta=0,
        turn_delta=3_600,
        input_truncated=False,
        detector_version="intent-rule-engine-v1",
        ruleset_version="session-risk-ruleset-v1",
        threshold_version="session-risk-threshold-v1",
        source_request_id=f"req-prd070-resp-{turn}",
        source_trace_id=f"trace-prd070-resp-{turn}",
        intent_risk_type="jailbreak",
        intent_risk_score=9_600,
        dlp_action="allow",
        dlp_current_turn_match_count=0,
        tool_inducement_detected=False,
    )


@pytest.mark.anyio
async def test_resp_server_runs_three_turn_freeze_ack_reset_and_restore() -> None:
    """生产 Store 通过真实 TCP 三轮冻结，并完成 ACK、重复、重置和只增恢复。"""

    state = SERVER._RespState()
    server = await asyncio.start_server(
        lambda reader, writer: SERVER._handle_client(state, reader, writer),
        "127.0.0.1",
        0,
    )
    socket = server.sockets[0] if server.sockets else None
    if socket is None:
        server.close()
        await server.wait_closed()
        # 动态端口缺失意味着没有覆盖生产 TCP/RESP 路径，不能继续运行内存断言。
        raise AssertionError("loopback server did not bind")
    port = int(socket.getsockname()[1])
    store = RedisSessionRiskStore(
        f"redis://127.0.0.1:{port}/0",
        connect_timeout_seconds=1.0,
        io_timeout_seconds=1.0,
    )
    scope = SessionRiskScope(
        tenant_id="tenant-prd070-resp",
        virtual_key_id="vk_prd070_resp",
        scope_type="api_session",
        scope_key="srh_" + "a" * 64,
        owner_slot="srh_" + "b" * 64,
        hmac_key_version=1,
    )

    try:
        assert await store.ping() is True
        states: list[str] = []
        frozen_snapshot = None
        for turn in (1, 2, 3):
            result = await store.accumulate(scope, _assessment(turn))
            assert result.pending is not None
            assert result.snapshot is not None
            states.append(result.snapshot.state)
            if turn == 1:
                # 未 ACK 时 verifier 必须同时识别 score、pending 和版本化 turn claim。
                references = await store.inspect_hmac_rotation_references(
                    current_version=2,
                    removing_version=1,
                )
                assert references.score_references == 1
                assert references.pending_references == 1
                assert references.turn_claim_references == 1
                assert references.ready is False
            assert await store.ack_pending(
                scope,
                result.snapshot.score_id,
                result.snapshot.revision,
                result.pending.assessment.turn_key_digest,
            )
            frozen_snapshot = result.snapshot
        assert states == ["monitoring", "high_risk", "frozen"]
        assert frozen_snapshot is not None
        assert frozen_snapshot.score == 10_000
        references = await store.inspect_hmac_rotation_references(
            current_version=2,
            removing_version=1,
        )
        assert references.score_references == 1
        assert references.pending_references == 0
        assert references.turn_claim_references == 3
        assert references.unknown_version_references == 0
        assert references.legacy_turn_claims == 0

        duplicate = await store.accumulate(scope, _assessment(3))
        assert duplicate.duplicate is True
        assert duplicate.snapshot == frozen_snapshot
        assert await store.reset(
            scope,
            frozen_snapshot.score_id,
            frozen_snapshot.revision,
        )
        assert await store.load(scope) is None
        assert await store.restore(scope, frozen_snapshot)
        assert await store.load(scope) == frozen_snapshot

        # 清空已完成主流程的状态后，单独验证 current 不阻断、未知版本和旧裸 claim 失败关闭。
        state.scores.clear()
        state.score_deadlines.clear()
        state.claims.clear()
        deadline = time.time() + 1_800
        for index, claim_value in enumerate(
            (
                "v1:2:" + "srh_" + "4" * 64,
                "v1:7:" + "srh_" + "5" * 64,
                "srh_" + "6" * 64,
            ),
            start=1,
        ):
            owner = "srh_" + format(index + 6, "x") * 64
            turn = "srh_" + format(index + 9, "x") * 64
            state.claims[f"metahub:sr:v1:{{{owner}}}:turn:{turn}"] = (claim_value, deadline)
        references = await store.inspect_hmac_rotation_references(
            current_version=2,
            removing_version=1,
        )
        assert references.score_references == 0
        assert references.turn_claim_references == 0
        assert references.unknown_version_references == 1
        assert references.legacy_turn_claims == 1
        assert references.ready is False
    finally:
        server.close()
        await server.wait_closed()

    # 状态键和 claim 只允许 HMAC 摘要；测试 tenant、Virtual Key 明文不能进入服务字典键。
    serialized_keys = repr((tuple(state.scores), tuple(state.claims)))
    assert "tenant-prd070-resp" not in serialized_keys
    assert "vk_prd070_resp" not in serialized_keys
