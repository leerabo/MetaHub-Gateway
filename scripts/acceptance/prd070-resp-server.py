"""PRD-070 本地运行验收专用的确定性 RESP2 会话风险服务。

该服务只绑定 ``127.0.0.1``，让正式 ``RedisSessionRiskStore`` 在没有外部 Redis binary 的
沙箱内走真实 TCP、RESP 数组和 EVAL 合同。它只保存 HMAC 键、固定计数及安全摘要，不保存
tenant、Session/Turn Header、Prompt、Response、Provider Key 或 Token；不得用于生产部署。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from contextlib import suppress
from fnmatch import fnmatchcase

from metahub_gateway.session_risk import SessionRiskAssessment
from metahub_gateway.session_risk.store import (
    SESSION_RISK_TTL_SECONDS,
    _ACCUMULATE_SCRIPT,
    _ACK_SCRIPT,
    _RESET_SCRIPT,
    _RESTORE_SCRIPT,
    _turn_claim_value,
)

# 验收端点固定回环绑定；开放到局域网会让无认证测试协议越过开发机边界。
_HOST = "127.0.0.1"
_DEFAULT_PORT = 6389
_MAX_COMMAND_PARTS = 128
_MAX_BULK_BYTES = 1_048_576
_DIGEST_RE = re.compile(r"^srh_[0-9a-f]{64}$")
_SCORE_ID_RE = re.compile(r"^srs_[0-9a-f]{32}$")


class _ProtocolError(ValueError):
    """表示本地探针收到不属于 PRD-070 冻结合同的命令或参数。"""


class _RespState:
    """保存单进程验收所需的 claim、score Hash 与到期时间。

    所有操作都由连接处理器在同一 ``asyncio.Lock`` 内调用，从而模拟 Redis Lua 的原子提交；
    数据仅驻留内存，进程退出即清空，不能被误用为生产级持久存储。
    """

    def __init__(self) -> None:
        """初始化不含业务明文的内存状态。"""

        self.claims: dict[str, tuple[str, float]] = {}
        self.scores: dict[str, dict[str, str]] = {}
        self.score_deadlines: dict[str, float] = {}
        self.lock = asyncio.Lock()

    def execute(self, command: tuple[str, ...]) -> bytes:
        """执行一条 exact RESP 命令并返回完整 RESP2 响应帧。"""

        self._purge_expired()
        if command == ("PING",):
            return _simple("PONG")
        if len(command) == 2 and command[0] == "HGETALL":
            score = self.scores.get(command[1])
            if score is None:
                return _array(())
            flattened = tuple(part for item in score.items() for part in item)
            return _array(flattened)
        if len(command) == 3 and command[0] == "HGET":
            score = self.scores.get(command[1])
            return _bulk(score.get(command[2]) if score is not None else None)
        if len(command) == 2 and command[0] == "GET":
            claim = self.claims.get(command[1])
            return _bulk(claim[0] if claim is not None else None)
        if len(command) == 6 and command[0] == "SCAN":
            return self._scan(command)
        if command and command[0] == "EVAL":
            return self._execute_eval(command)
        # 探针只实现生产 Store 当前使用的最小命令集，未知命令不得静默返回成功。
        raise _ProtocolError("unsupported command")

    def _scan(self, command: tuple[str, ...]) -> bytes:
        """实现 verifier 所需的单页只读 SCAN，不返回命名空间外的状态。"""

        if command[1] != "0" or command[2] != "MATCH" or command[4] != "COUNT":
            # 本地状态只支持零游标单页合同，其他分页形状不能伪装成生产扫描。
            raise _ProtocolError("invalid scan")
        count = _strict_positive_int(command[5])
        if count > 10_000:
            # 过大 COUNT 不符合 Store 固定请求，也可能放大验收服务内存。
            raise _ProtocolError("invalid scan")
        # 本地验收状态有严格小上限，单页返回即可；生产 Redis 仍由 Store 穷尽真实游标。
        all_keys = (*self.scores.keys(), *self.claims.keys())
        matched = tuple(sorted(key for key in all_keys if fnmatchcase(key, command[3])))
        return _scan_array("0", matched)

    def _execute_eval(self, command: tuple[str, ...]) -> bytes:
        """按脚本文本精确分派累计、ACK、恢复和重置四种原子操作。"""

        # EVAL 至少包含脚本、key 数量和一个 key；不完整帧不能进入任何状态写入。
        if len(command) < 4:
            raise _ProtocolError("invalid eval")
        script = command[1]
        try:
            key_count = int(command[2])
        # key 数量解析失败说明 RESP 内容与生产 Store 合同不一致，必须零写入拒绝。
        except ValueError as exc:
            raise _ProtocolError("invalid eval") from exc
        # key 切片必须完全位于命令数组内，避免把缺失 key 的 ARGV 错位解释为 owner。
        if key_count < 1 or len(command) < 3 + key_count:
            raise _ProtocolError("invalid eval")
        keys = command[3 : 3 + key_count]
        arguments = command[3 + key_count :]
        if script == _ACCUMULATE_SCRIPT:
            return _array((self._accumulate(keys, arguments),))
        if script == _ACK_SCRIPT:
            return _integer(self._ack(keys, arguments))
        if script == _RESTORE_SCRIPT:
            return _integer(self._restore(keys, arguments))
        if script == _RESET_SCRIPT:
            return _integer(self._reset(keys, arguments))
        # 必须逐字匹配仓库脚本，避免旧 Gateway 与新验收服务组合时伪造通过。
        raise _ProtocolError("unknown script")

    def _accumulate(self, keys: tuple[str, ...], arguments: tuple[str, ...]) -> str:
        """实现生产 Lua 的 23 参数校验、幂等 claim、累计和唯一 pending。"""

        # 生产脚本固定两 key、二十三参数；任何版本漂移都不能以缺省值继续累计。
        if len(keys) != 2 or len(arguments) != 23:
            raise _ProtocolError("invalid accumulate arity")
        score_key, claim_key = keys
        assessment, occurred_at, hmac_version = _assessment_from_arguments(arguments)
        score_id = arguments[16]
        if (
            _SCORE_ID_RE.fullmatch(score_id) is None
            or arguments[17] != str(SESSION_RISK_TTL_SECONDS)
            or not claim_key.endswith(f":turn:{assessment.turn_key_digest}")
        ):
            # score/claim 标识共同决定幂等和租户 owner，任一不匹配都必须在 claim 前拒绝。
            raise _ProtocolError("invalid accumulate identity")
        _validate_signal_combination(assessment)

        score = self.scores.get(score_key)
        # 未 ACK 的安全事实拥有最高优先级，不能先占用下一轮 claim 再返回失败。
        if score is not None and "pending_revision" in score:
            return "PENDING"
        existing_claim = self.claims.get(claim_key)
        claim_value = _turn_claim_value(hmac_version, assessment.turn_intent_digest)
        if existing_claim is not None:
            return (
                "DUPLICATE"
                if existing_claim[0] in {claim_value, assessment.turn_intent_digest}
                else "CONFLICT"
            )
        claim_deadline = time.time() + SESSION_RISK_TTL_SECONDS
        self.claims[claim_key] = (claim_value, claim_deadline)
        # 零风险轮次只建立幂等 claim，不得创建或刷新风险窗口。
        if assessment.turn_delta == 0:
            return "CLEAN"

        old = score or {}
        old_score = _state_int(old, "score", 0)
        old_turns = _state_int(old, "risky_turn_count", 0)
        old_revision = _state_int(old, "revision", 0)
        old_jailbreak = _state_int(old, "jailbreak_count", 0)
        old_sensitive = _state_int(old, "sensitive_count", 0)
        old_tool = _state_int(old, "tool_inducement_count", 0)
        old_state = old.get("state", "none")
        if old_revision and (
            old.get("hmac_key_version") != str(hmac_version)
            or old.get("threshold_version") != assessment.threshold_version
            or old_state not in {"monitoring", "high_risk", "frozen"}
        ):
            # owner 版本或阈值漂移意味着当前 key 的解释不再唯一，验收服务同样失败关闭。
            raise _ProtocolError("owner mismatch")

        new_score = min(10_000, old_score + assessment.turn_delta)
        new_turns = old_turns + 1
        new_revision = old_revision + 1
        new_jailbreak = old_jailbreak + assessment.jailbreak_count_delta
        new_sensitive = old_sensitive + assessment.sensitive_count_delta
        new_tool = old_tool + assessment.tool_inducement_count_delta
        new_state = _next_state(new_score, new_turns)
        window_started = _state_int(old, "window_started_at_epoch", occurred_at)
        expires_at = 0 if new_state == "frozen" else occurred_at + SESSION_RISK_TTL_SECONDS
        active_score_id = old.get("score_id", score_id)

        pending = {
            "schema_version": 1,
            "score_id": active_score_id,
            "score_before": old_score,
            "score_after": new_score,
            "state_before": old_state,
            "state_after": new_state,
            "revision": new_revision,
            "risky_turn_count": new_turns,
            "jailbreak_count": new_jailbreak,
            "sensitive_count": new_sensitive,
            "tool_inducement_count": new_tool,
            "window_started_at_epoch": window_started,
            "last_risk_at_epoch": occurred_at,
            "expires_at_epoch": expires_at,
            "hmac_key_version": hmac_version,
            "threshold_version": assessment.threshold_version,
            "high_risk_transition": (
                old_state not in {"high_risk", "frozen"}
                and new_state in {"high_risk", "frozen"}
            ),
            "freeze_transition": old_state != "frozen" and new_state == "frozen",
            "turn_key_digest": assessment.turn_key_digest,
            "turn_intent_digest": assessment.turn_intent_digest,
            "turn_delta": assessment.turn_delta,
            "jailbreak_count_delta": assessment.jailbreak_count_delta,
            "sensitive_count_delta": assessment.sensitive_count_delta,
            "tool_inducement_count_delta": assessment.tool_inducement_count_delta,
            "source_request_id": assessment.source_request_id,
            "source_trace_id": assessment.source_trace_id,
            "input_truncated": assessment.input_truncated,
            "detector_version": assessment.detector_version,
            "ruleset_version": assessment.ruleset_version,
            "intent_risk_result_id": assessment.intent_risk_result_id,
            "intent_risk_type": assessment.intent_risk_type,
            "intent_risk_score": assessment.intent_risk_score,
            "dlp_action": assessment.dlp_action,
            "dlp_current_turn_match_count": assessment.dlp_current_turn_match_count,
            "tool_inducement_detected": assessment.tool_inducement_detected,
            "occurred_at_epoch": occurred_at,
            "chat_message_id": assessment.chat_message_id,
        }
        self.scores[score_key] = {
            "schema_version": "1",
            "score_id": active_score_id,
            "score": str(new_score),
            "risky_turn_count": str(new_turns),
            "jailbreak_count": str(new_jailbreak),
            "sensitive_count": str(new_sensitive),
            "tool_inducement_count": str(new_tool),
            "state": new_state,
            "revision": str(new_revision),
            "window_started_at_epoch": str(window_started),
            "last_risk_at_epoch": str(occurred_at),
            "expires_at_epoch": str(expires_at),
            "hmac_key_version": str(hmac_version),
            "threshold_version": assessment.threshold_version,
            "pending_revision": str(new_revision),
            "pending_turn_digest": assessment.turn_key_digest,
            "pending_payload": json.dumps(
                pending,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
        }
        # pending 相当于 Redis PERSIST；只有数据库 ACK 后才重新启动非冻结 TTL。
        self.score_deadlines.pop(score_key, None)
        return "ACCUMULATED"

    def _ack(self, keys: tuple[str, ...], arguments: tuple[str, ...]) -> int:
        """按 score/revision/turn 三元 CAS 清除待提交证据，并恢复非冻结周期 TTL。"""

        # ACK 固定一个 score key 和四个参数，错位参数可能误删另一轮证据，必须直接拒绝。
        if len(keys) != 1 or len(arguments) != 4:
            raise _ProtocolError("invalid ack arity")
        score_key = keys[0]
        score = self.scores.get(score_key)
        if score is None:
            return 0
        if (
            score.get("score_id") != arguments[0]
            or score.get("pending_revision") != arguments[1]
            or score.get("pending_turn_digest") != arguments[2]
        ):
            # CAS 不命中表示并发证据已经变化，不能扩大条件删除新的 pending。
            return 0
        score.pop("pending_revision", None)
        score.pop("pending_turn_digest", None)
        score.pop("pending_payload", None)
        if score.get("state") == "frozen":
            self.score_deadlines.pop(score_key, None)
        else:
            acknowledged_at = _strict_non_negative_int(arguments[3])
            expires_at = _state_int(score, "expires_at_epoch", 0)
            self.score_deadlines[score_key] = float(max(acknowledged_at + 1, expires_at))
        return 1

    def _restore(self, keys: tuple[str, ...], arguments: tuple[str, ...]) -> int:
        """只增恢复数据库快照，不覆盖 pending 或较新 revision。"""

        # 恢复脚本固定一个 key 和十三字段，缺失字段不得用空值拼成看似有效的快照。
        if len(keys) != 1 or len(arguments) != 13:
            raise _ProtocolError("invalid restore arity")
        score_key = keys[0]
        current = self.scores.get(score_key)
        if current is not None and "pending_revision" in current:
            return -1
        revision = _strict_non_negative_int(arguments[7])
        if current is not None and _state_int(current, "revision", 0) >= revision:
            return 0
        restored = {
            "schema_version": "1",
            "score_id": arguments[0],
            "score": arguments[1],
            "risky_turn_count": arguments[2],
            "jailbreak_count": arguments[3],
            "sensitive_count": arguments[4],
            "tool_inducement_count": arguments[5],
            "state": arguments[6],
            "revision": arguments[7],
            "window_started_at_epoch": arguments[8],
            "last_risk_at_epoch": arguments[9],
            "expires_at_epoch": arguments[10],
            "hmac_key_version": arguments[11],
            "threshold_version": arguments[12],
        }
        # 构造公开模型可复核所有数字和状态，这里还需拒绝验收服务不支持的异常标识。
        if _SCORE_ID_RE.fullmatch(restored["score_id"]) is None:
            raise _ProtocolError("invalid restore score")
        self.scores[score_key] = restored
        if restored["state"] == "frozen":
            self.score_deadlines.pop(score_key, None)
        else:
            self.score_deadlines[score_key] = float(
                _strict_non_negative_int(restored["expires_at_epoch"])
            )
        return 1

    def _reset(self, keys: tuple[str, ...], arguments: tuple[str, ...]) -> int:
        """仅删除 score ID/revision 精确匹配且没有 pending 的周期。"""

        # reset 固定一个 key 和 CAS 二元组，额外或缺失参数都可能扩大删除边界。
        if len(keys) != 1 or len(arguments) != 2:
            raise _ProtocolError("invalid reset arity")
        score_key = keys[0]
        score = self.scores.get(score_key)
        if score is None or "pending_revision" in score:
            return 0
        if score.get("score_id") != arguments[0] or score.get("revision") != arguments[1]:
            return 0
        self.scores.pop(score_key, None)
        self.score_deadlines.pop(score_key, None)
        return 1

    def _purge_expired(self) -> None:
        """按本机时钟清理 claim 和已 ACK 的非冻结 score。"""

        now = time.time()
        expired_claims = [
            key for key, (_intent, deadline) in self.claims.items() if deadline <= now
        ]
        for key in expired_claims:
            self.claims.pop(key, None)
        expired_scores = [key for key, deadline in self.score_deadlines.items() if deadline <= now]
        for key in expired_scores:
            self.score_deadlines.pop(key, None)
            self.scores.pop(key, None)


def _assessment_from_arguments(
    arguments: tuple[str, ...],
) -> tuple[SessionRiskAssessment, int, int]:
    """把 Lua ARGV 重建为 Shared 不可变摘要，并返回事件时间与 HMAC 版本。"""

    try:
        occurred_at = _strict_positive_int(arguments[8])
        hmac_version = _strict_positive_int(arguments[7])
        assessment = SessionRiskAssessment(
            turn_key_digest=arguments[0],
            turn_intent_digest=arguments[1],
            turn_delta=_strict_non_negative_int(arguments[2]),
            jailbreak_count_delta=_strict_non_negative_int(arguments[3]),
            sensitive_count_delta=_strict_non_negative_int(arguments[4]),
            tool_inducement_count_delta=_strict_non_negative_int(arguments[5]),
            threshold_version=arguments[6],
            source_request_id=arguments[9],
            source_trace_id=arguments[10],
            input_truncated=_strict_flag(arguments[11]),
            detector_version=arguments[12],
            ruleset_version=arguments[13],
            intent_risk_result_id=arguments[14] or None,
            chat_message_id=arguments[15] or None,
            intent_risk_type=arguments[18],
            intent_risk_score=_strict_non_negative_int(arguments[19]),
            dlp_action=arguments[20],
            dlp_current_turn_match_count=_strict_non_negative_int(arguments[21]),
            tool_inducement_detected=_strict_flag(arguments[22]),
        )
    # 固定协议错误不会拼接摘要或 request_id，避免探针 stderr 变成敏感信息旁路。
    except (IndexError, TypeError, ValueError) as exc:
        raise _ProtocolError("invalid assessment") from exc
    return assessment, occurred_at, hmac_version


def _validate_signal_combination(assessment: SessionRiskAssessment) -> None:
    """复算固定信号权重，防止伪造 delta 绕过生产 Lua 组合校验。"""

    intent_delta = 0
    if assessment.intent_risk_type == "none":
        # none 必须严格配零分，防止伪造无类别高分绕过审计分类枚举。
        if assessment.intent_risk_score != 0:
            raise _ProtocolError("invalid intent score")
    elif assessment.intent_risk_score >= 9_500:
        intent_delta = 3_600
    elif assessment.intent_risk_score >= 8_000:
        intent_delta = 2_600
    elif assessment.intent_risk_score >= 6_500:
        intent_delta = 1_800

    dlp_delta = 0
    if assessment.dlp_action == "allow":
        # allow 与零命中必须同时成立，避免敏感命中在摘要中被伪装为放行。
        if assessment.dlp_current_turn_match_count != 0:
            raise _ProtocolError("invalid dlp count")
    # 非 allow 动作至少要有一个当前轮命中，否则风险分数缺少可追踪事实。
    elif assessment.dlp_current_turn_match_count < 1:
        raise _ProtocolError("invalid dlp count")
    elif assessment.dlp_action == "block":
        dlp_delta = 3_000
    else:
        dlp_delta = 1_200
    tool_delta = 2_400 if assessment.tool_inducement_detected else 0
    expected_delta = min(4_000, intent_delta + dlp_delta + tool_delta)
    expected_categories = (
        1 if intent_delta > 0 else 0,
        1
        if dlp_delta > 0
        or (intent_delta > 0 and assessment.intent_risk_type == "credential_exfiltration")
        else 0,
        1 if assessment.tool_inducement_detected else 0,
    )
    actual_categories = (
        assessment.jailbreak_count_delta,
        assessment.sensitive_count_delta,
        assessment.tool_inducement_count_delta,
    )
    if assessment.turn_delta != expected_delta or actual_categories != expected_categories:
        # 分类摘要和固定权重不一致时必须零写入，避免验收服务掩盖 Gateway 合同错误。
        raise _ProtocolError("invalid signal combination")


def _next_state(score: int, turns: int) -> str:
    """按固定分数与风险轮次双门限计算状态，避免单次误判直接冻结企业会话。"""

    if score >= 9_000 and turns >= 3:
        return "frozen"
    if score >= 7_000 and turns >= 2:
        return "high_risk"
    return "monitoring"


def _state_int(values: dict[str, str], key: str, default: int) -> int:
    """读取 canonical 非负整数状态，损坏字段立即拒绝。"""

    if key not in values:
        return default
    return _strict_non_negative_int(values[key])


def _strict_positive_int(value: str) -> int:
    """解析不带空白、符号或 Unicode 歧义的正整数。"""

    parsed = _strict_non_negative_int(value)
    # HMAC 版本和事件时间必须为正，零值无法表示可追踪的有效安全事实。
    if parsed < 1:
        raise _ProtocolError("invalid positive integer")
    return parsed


def _strict_non_negative_int(value: str) -> int:
    """解析 canonical ASCII 非负整数，拒绝 bool 和宽松转换。"""

    # 不执行 trim、符号或 Unicode 数字转换，确保 Python 与 Lua 的证据解释一致。
    if type(value) is not str or not value.isascii() or not value.isdecimal():
        raise _ProtocolError("invalid integer")
    return int(value)


def _strict_flag(value: str) -> bool:
    """把 Lua 零一标志转换为布尔，拒绝其他字符串。"""

    # 只接受 canonical 零一，避免 truthy 字符串把截断或工具诱导事实解释反转。
    if value not in {"0", "1"}:
        raise _ProtocolError("invalid flag")
    return value == "1"


async def _read_command(reader: asyncio.StreamReader) -> tuple[str, ...]:
    """严格读取一个只含 Bulk String 的 RESP2 命令数组。"""

    header = await reader.readline()
    # 命令顶层必须是 CRLF 终止数组；Simple String 等形状可能造成命令注入歧义。
    if not header.startswith(b"*") or not header.endswith(b"\r\n"):
        raise _ProtocolError("invalid array")
    try:
        item_count = int(header[1:-2].decode("ascii"))
    # 数组长度必须是 ASCII 整数，解析异常不回显原始网络字节。
    except (UnicodeDecodeError, ValueError) as exc:
        raise _ProtocolError("invalid array") from exc
    # 元素数量设硬上限，防止错误端点令验收进程无界读取或分配内存。
    if not 1 <= item_count <= _MAX_COMMAND_PARTS:
        raise _ProtocolError("invalid array size")
    parts: list[str] = []
    for _index in range(item_count):
        bulk_header = await reader.readline()
        # 每个命令参数必须为 Bulk String，禁止混用 RESP 类型改变长度边界。
        if not bulk_header.startswith(b"$") or not bulk_header.endswith(b"\r\n"):
            raise _ProtocolError("invalid bulk")
        try:
            byte_count = int(bulk_header[1:-2].decode("ascii"))
        # Bulk 长度解析失败时必须停止当前连接，不能猜测正文边界继续读取。
        except (UnicodeDecodeError, ValueError) as exc:
            raise _ProtocolError("invalid bulk") from exc
        # 单参数上限覆盖 Lua 脚本但阻止无界正文进入只应承载摘要的验收服务。
        if not 0 <= byte_count <= _MAX_BULK_BYTES:
            raise _ProtocolError("invalid bulk size")
        raw = await reader.readexactly(byte_count)
        # 正文后的 CRLF 是下一个参数的唯一帧边界，缺失时整条命令失败。
        if await reader.readexactly(2) != b"\r\n":
            raise _ProtocolError("invalid bulk terminator")
        parts.append(raw.decode("utf-8"))
    return tuple(parts)


async def _handle_client(
    state: _RespState,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """处理一个短连接；不记录命令内容、地址、脚本正文或异常细节。"""

    try:
        while True:
            command = await _read_command(reader)
            async with state.lock:
                response = state.execute(command)
            writer.write(response)
            await writer.drain()
    # 生产 Store 每条业务命令使用短连接，完整响应后主动断开属于正常生命周期。
    except asyncio.IncompleteReadError:
        pass
    except (ConnectionError, UnicodeDecodeError, _ProtocolError, ValueError):
        # 错误帧保持固定文本，绝不回显 HMAC、request_id、Lua 或客户端端点。
        with suppress(ConnectionError, OSError):
            writer.write(b"-ERR invalid request\r\n")
            await writer.drain()
    finally:
        writer.close()
        with suppress(ConnectionError, OSError):
            await writer.wait_closed()


def _simple(value: str) -> bytes:
    """把固定服务状态编码为 RESP2 Simple String，调用方不得传入动态异常正文。"""

    return f"+{value}\r\n".encode("ascii")


def _bulk(value: str | None) -> bytes:
    """把安全状态字段编码为 UTF-8 Bulk String，空值使用 Redis nil 表示。"""

    if value is None:
        return b"$-1\r\n"
    raw = value.encode("utf-8")
    return f"${len(raw)}\r\n".encode("ascii") + raw + b"\r\n"


def _array(values: tuple[str, ...]) -> bytes:
    """编码只含字符串元素的有界 RESP2 数组。"""

    return f"*{len(values)}\r\n".encode("ascii") + b"".join(_bulk(value) for value in values)


# verifier 需要二维数组；独立编码器避免普通字符串数组错误展平 keys。
def _scan_array(cursor: str, values: tuple[str, ...]) -> bytes:
    """编码 Redis SCAN 的游标与 key 二维数组，保持生产客户端分页响应边界。"""

    return b"*2\r\n" + _bulk(cursor) + _array(values)


def _integer(value: int) -> bytes:
    """把 Lua CAS 的零、一或专用负一结果编码为 canonical RESP2 整数帧。"""

    return f":{value}\r\n".encode("ascii")


async def _serve(port: int) -> None:
    """启动回环监听并持续服务，直到验收进程被显式终止。"""

    state = _RespState()
    server = await asyncio.start_server(
        lambda reader, writer: _handle_client(state, reader, writer),
        _HOST,
        port,
    )
    sockets = server.sockets or ()
    # 没有监听 socket 就不能宣称完成真实 TCP 验收，必须终止而非降级为内存调用。
    if not sockets:
        server.close()
        await server.wait_closed()
        # 关闭空服务后返回固定启动错误，不包含端口占用进程或底层 socket 细节。
        raise RuntimeError("PRD-070 RESP server failed to bind")
    actual_port = int(sockets[0].getsockname()[1])
    print(
        json.dumps(
            {"status": "ready", "host": _HOST, "port": actual_port},
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    async with server:
        await server.serve_forever()


def main() -> int:
    """解析本地端口并运行验收服务；不接受外部绑定地址。"""

    parser = argparse.ArgumentParser(description="PRD-070 loopback RESP acceptance server")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65_535:
        parser.error("port must be between 1 and 65535")
    try:
        asyncio.run(_serve(arguments.port))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
