"""PRD-070 HMAC 轮换 Redis 全库引用扫描的纯内存合同测试。

本模块只替换 ``RedisSessionRiskStore._command``，不会创建 socket、连接外部 Redis、
访问开发数据库或调用运行中的 Gateway。测试数据仅含规范合成摘要，用于证明 verifier
穷尽 SCAN 分页、聚合安全计数，并在游标循环或协议污染时失败关闭。
"""

from __future__ import annotations

import json

import pytest
from metahub_gateway.session_risk import (
    RedisSessionRiskStore,
    SessionRiskRedisHmacReferenceReport,
    SessionRiskStoreUnavailable,
)

# 测试固定使用生产命名空间，避免 fake 命令因自造简化 key 掩盖 exact regex 边界。
_SCORE_SCAN_PATTERN = "metahub:sr:v1:*:score"
_TURN_SCAN_PATTERN = "metahub:sr:v1:*:turn:*"


def _digest(marker: str) -> str:
    """生成不含租户、Session、Turn 或正文原文的规范测试 HMAC 摘要。"""

    assert len(marker) == 1 and marker in "0123456789abcdef"
    return f"srh_{marker * 64}"


def _score_key(owner_marker: str) -> str:
    """生成与生产 Lua 同槽位、仅含合成 owner 摘要的 score key。"""

    return f"metahub:sr:v1:{{{_digest(owner_marker)}}}:score"


def _turn_key(owner_marker: str, turn_marker: str) -> str:
    """生成与生产 Lua 相同形状的短期 turn claim key。"""

    return f"metahub:sr:v1:{{{_digest(owner_marker)}}}:turn:{_digest(turn_marker)}"


def _pending_payload(version: int, marker: str) -> str:
    """构造 exact-schema pending JSON，供扫描器复用真实解析和版本校验逻辑。"""

    occurred_at = 1_700_700_000
    payload = {
        "schema_version": 1,
        "score_id": f"srs_{marker * 32}",
        "score_before": 0,
        "score_after": 3_600,
        "state_before": "none",
        "state_after": "monitoring",
        "revision": 1,
        "risky_turn_count": 1,
        "jailbreak_count": 1,
        "sensitive_count": 0,
        "tool_inducement_count": 0,
        "window_started_at_epoch": occurred_at,
        "last_risk_at_epoch": occurred_at,
        "expires_at_epoch": occurred_at + 1_800,
        "hmac_key_version": version,
        "threshold_version": "session-risk-threshold-v1",
        "high_risk_transition": False,
        "freeze_transition": False,
        "turn_key_digest": _digest(marker),
        "turn_intent_digest": _digest("f"),
        "turn_delta": 3_600,
        "jailbreak_count_delta": 1,
        "sensitive_count_delta": 0,
        "tool_inducement_count_delta": 0,
        "source_request_id": f"req_prd070_scan_{marker}",
        "source_trace_id": f"trc_prd070_scan_{marker}",
        "input_truncated": False,
        "detector_version": "intent-v1",
        "ruleset_version": "session-risk-ruleset-v1",
        "intent_risk_result_id": None,
        "intent_risk_type": "jailbreak",
        "intent_risk_score": 9_000,
        "dlp_action": "allow",
        "dlp_current_turn_match_count": 0,
        "tool_inducement_detected": False,
        "occurred_at_epoch": occurred_at,
        "chat_message_id": None,
    }
    # 紧凑编码保持 fake 与 Lua cjson 负载形状接近；字段仍由生产 exact parser 逐项验证。
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _score_hash(version: int, marker: str, *, pending: bool = False) -> list[str]:
    """构造生产 HGETALL 返回的扁平字符串数组，可选择携带完整 pending 三元组。"""

    occurred_at = 1_700_700_000
    values = {
        "schema_version": "1",
        "score_id": f"srs_{marker * 32}",
        "score": "3600",
        "risky_turn_count": "1",
        "jailbreak_count": "1",
        "sensitive_count": "0",
        "tool_inducement_count": "0",
        "state": "monitoring",
        "revision": "1",
        "window_started_at_epoch": str(occurred_at),
        "last_risk_at_epoch": str(occurred_at),
        "expires_at_epoch": str(occurred_at + 1_800),
        "hmac_key_version": str(version),
        "threshold_version": "session-risk-threshold-v1",
    }
    if pending:
        # Redis pending 元数据必须三字段同在，否则无法进行数据库提交与三元 CAS ACK。
        values.update(
            pending_revision="1",
            pending_turn_digest=_digest(marker),
            pending_payload=_pending_payload(version, marker),
        )
    response: list[str] = []
    for key, value in values.items():
        response.extend((key, value))
    return response


@pytest.mark.anyio
async def test_inspector_counts_all_reference_types_across_scan_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多页主流程：分别统计 previous score/pending/turn、未知版本和旧裸 claim。"""

    previous_score = _score_key("1")
    current_score = _score_key("2")
    unknown_score = _score_key("3")
    previous_turn = _turn_key("1", "4")
    current_turn = _turn_key("2", "5")
    unknown_turn = _turn_key("3", "6")
    legacy_turn = _turn_key("4", "7")
    scan_pages = {
        (_SCORE_SCAN_PATTERN, "0"): ["17", [previous_score, current_score]],
        # Redis SCAN 允许重复 key；集合去重必须避免 previous score 被重复计数。
        (_SCORE_SCAN_PATTERN, "17"): ["0", [unknown_score, previous_score]],
        (_TURN_SCAN_PATTERN, "0"): ["29", [previous_turn, current_turn]],
        (_TURN_SCAN_PATTERN, "29"): [
            "0",
            [unknown_turn, legacy_turn, previous_turn],
        ],
    }
    score_values = {
        previous_score: _score_hash(1, "1", pending=True),
        current_score: _score_hash(2, "2"),
        unknown_score: _score_hash(7, "3"),
    }
    claim_values = {
        previous_turn: f"v1:1:{_digest('8')}",
        current_turn: f"v1:2:{_digest('9')}",
        unknown_turn: f"v1:7:{_digest('a')}",
        legacy_turn: _digest("b"),
    }
    calls: list[tuple[object, ...]] = []
    store = RedisSessionRiskStore("redis://127.0.0.1:6379/0")

    async def command(*parts: object) -> object:
        """按命令合同返回本地固定响应，并记录分页游标以证明扫描被穷尽。"""

        calls.append(parts)
        if parts == ("PING",):
            return "PONG"
        if parts[0] == "SCAN":
            assert parts[2:] in {
                ("MATCH", _SCORE_SCAN_PATTERN, "COUNT", "256"),
                ("MATCH", _TURN_SCAN_PATTERN, "COUNT", "256"),
            }
            return scan_pages[(parts[3], parts[1])]
        if parts[0] == "HGETALL":
            return score_values[parts[1]]
        if parts[0] == "GET":
            return claim_values[parts[1]]
        # 任何未列入合同的命令都表示生产扫描行为发生漂移，测试必须立即暴露。
        raise AssertionError("unexpected fake Redis command")

    monkeypatch.setattr(store, "_command", command)
    report = await store.inspect_hmac_rotation_references(
        current_version=2,
        removing_version=1,
    )

    assert report == SessionRiskRedisHmacReferenceReport(
        score_references=1,
        pending_references=1,
        turn_claim_references=1,
        unknown_version_references=2,
        legacy_turn_claims=1,
    )
    assert report.ready is False
    assert calls[:5] == [
        ("PING",),
        ("SCAN", "0", "MATCH", _SCORE_SCAN_PATTERN, "COUNT", "256"),
        ("SCAN", "17", "MATCH", _SCORE_SCAN_PATTERN, "COUNT", "256"),
        ("SCAN", "0", "MATCH", _TURN_SCAN_PATTERN, "COUNT", "256"),
        ("SCAN", "29", "MATCH", _TURN_SCAN_PATTERN, "COUNT", "256"),
    ]
    assert calls.count(("HGETALL", previous_score)) == 1
    assert calls.count(("GET", previous_turn)) == 1


@pytest.mark.anyio
async def test_inspector_allows_current_only_and_scan_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """零引用流程：current 状态不阻断，SCAN 后自然过期的 key 也不制造假引用。"""

    current_score = _score_key("1")
    expired_score = _score_key("2")
    current_turn = _turn_key("1", "3")
    expired_turn = _turn_key("2", "4")
    store = RedisSessionRiskStore("redis://127.0.0.1:6379/0")

    async def command(*parts: object) -> object:
        """模拟 current 引用和读取前自然过期，不连接真实 Redis。"""

        if parts == ("PING",):
            return "PONG"
        if parts[:4] == ("SCAN", "0", "MATCH", _SCORE_SCAN_PATTERN):
            return ["0", [current_score, expired_score]]
        if parts[:4] == ("SCAN", "0", "MATCH", _TURN_SCAN_PATTERN):
            return ["0", [current_turn, expired_turn]]
        if parts == ("HGETALL", current_score):
            return _score_hash(2, "1")
        if parts == ("HGETALL", expired_score):
            return []
        if parts == ("GET", current_turn):
            return f"v1:2:{_digest('5')}"
        if parts == ("GET", expired_turn):
            return None
        # current-only 替身仅允许上述只读命令，额外调用不能被默认空响应掩盖。
        raise AssertionError("unexpected fake Redis command")

    monkeypatch.setattr(store, "_command", command)
    report = await store.inspect_hmac_rotation_references(
        current_version=2,
        removing_version=1,
    )

    assert report == SessionRiskRedisHmacReferenceReport(0, 0, 0, 0, 0)
    assert report.ready is True


@pytest.mark.anyio
async def test_inspector_rejects_nonzero_scan_cursor_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """游标循环意味着全库扫描无法完成，必须返回固定异常且不能读取部分 key。"""

    calls: list[tuple[object, ...]] = []
    store = RedisSessionRiskStore("redis://127.0.0.1:6379/0")

    async def command(*parts: object) -> object:
        """模拟非终止游标循环，并禁止扫描完成前读取任何业务状态。"""

        calls.append(parts)
        if parts == ("PING",):
            return "PONG"
        if parts[0] == "SCAN":
            next_cursor = {"0": "7", "7": "11", "11": "7"}[parts[1]]
            return [next_cursor, []]
        # 游标没有回到零就读取业务 key，会把部分扫描误当作全库扫描结果。
        raise AssertionError("游标未完整归零前不应读取任何业务 key")

    monkeypatch.setattr(store, "_command", command)
    with pytest.raises(SessionRiskStoreUnavailable) as caught:
        await store.inspect_hmac_rotation_references(current_version=2, removing_version=1)

    assert str(caught.value) == "session risk store unavailable"
    assert caught.value.__cause__ is None
    assert [call[1] for call in calls if call[0] == "SCAN"] == ["0", "7", "11"]
    assert not any(call[0] in {"HGETALL", "GET"} for call in calls)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad_scan_response",
    (
        None,
        ("0", []),
        ["01", []],
        ["18446744073709551616", []],
        ["0", [7]],
        ["0", ["x" * 257]],
    ),
)
async def test_inspector_rejects_scan_protocol_pollution(
    monkeypatch: pytest.MonkeyPatch,
    bad_scan_response: object,
) -> None:
    """SCAN 外层、游标、元素类型或 key 长度漂移均须脱敏失败关闭。"""

    store = RedisSessionRiskStore("redis://127.0.0.1:6379/0")

    async def command(*parts: object) -> object:
        """仅返回一个被参数化污染的首屏 SCAN 响应。"""

        if parts == ("PING",):
            return "PONG"
        if parts[0] == "SCAN":
            return bad_scan_response
        # 污染的首屏必须直接失败关闭，不能继续使用其后任何伪造状态。
        raise AssertionError("污染响应之后不应继续读取")

    monkeypatch.setattr(store, "_command", command)
    with pytest.raises(SessionRiskStoreUnavailable) as caught:
        await store.inspect_hmac_rotation_references(current_version=2, removing_version=1)

    assert str(caught.value) == "session risk store unavailable"
    assert caught.value.__cause__ is None
    assert repr(bad_scan_response) not in str(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("score_response", "claim_response"),
    (
        (_score_hash(1, "1") + ["unknown_field", "secret-marker"], None),
        (["schema_version", "2"] + _score_hash(1, "1")[2:], None),
        (None, ["v1:1:protocol-array"]),
        (None, "v2:1:protocol-string"),
    ),
)
async def test_inspector_rejects_hash_and_claim_protocol_pollution(
    monkeypatch: pytest.MonkeyPatch,
    score_response: object,
    claim_response: object,
) -> None:
    """未知 Hash 字段/schema 及非规范 claim 类型/schema 不能被误报为安全计数。"""

    score_key = _score_key("1")
    turn_key = _turn_key("1", "2")
    store = RedisSessionRiskStore("redis://127.0.0.1:6379/0")

    async def command(*parts: object) -> object:
        """根据参数化分支只暴露一个污染的 score 或 turn claim。"""

        if parts == ("PING",):
            return "PONG"
        if parts[:4] == ("SCAN", "0", "MATCH", _SCORE_SCAN_PATTERN):
            return ["0", [score_key] if score_response is not None else []]
        if parts[:4] == ("SCAN", "0", "MATCH", _TURN_SCAN_PATTERN):
            return ["0", [turn_key] if claim_response is not None else []]
        if parts == ("HGETALL", score_key):
            return score_response
        if parts == ("GET", turn_key):
            return claim_response
        # 测试替身只允许目标污染分支所需命令，防止新增读取被宽松响应掩盖。
        raise AssertionError("unexpected fake Redis command")

    monkeypatch.setattr(store, "_command", command)
    with pytest.raises(SessionRiskStoreUnavailable) as caught:
        await store.inspect_hmac_rotation_references(current_version=2, removing_version=1)

    assert str(caught.value) == "session risk store unavailable"
    assert caught.value.__cause__ is None
