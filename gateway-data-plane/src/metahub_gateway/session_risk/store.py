"""PRD-070 会话风险 Redis/InMemory 原子热状态 Store。

生产实现使用独立 RESP 客户端和单次 Lua/EVAL 完成 turn 幂等、组合分数、TTL、revision、
阈值跃迁和唯一 pending 摘要。它不复用早期限流 Store，因为后者缺少连接/读取 deadline、
数组响应和多 key 原子语义。Redis URL、密码、原始 Session/Turn Header 与正文不会进入
异常、日志、对象 repr 或 Redis key。
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import unquote, urlparse
from uuid import uuid4

from .models import (
    SessionRiskAccumulateResult,
    SessionRiskAssessment,
    SessionRiskContractError,
    SessionRiskPending,
    SessionRiskPendingEvidence,
    SessionRiskScope,
    SessionRiskSnapshot,
    SessionRiskStoreUnavailable,
    SessionRiskTurnConflict,
)

# 活跃窗口与 turn claim 都固定为 30 分钟。安全 turn 只建立 claim，不刷新风险窗口。
SESSION_RISK_TTL_SECONDS = 1_800
_SCHEMA_VERSION = 1
_SCORE_SCALE = 10_000
_HIGH_RISK_THRESHOLD = 7_000
_FREEZE_THRESHOLD = 9_000
_HIGH_RISK_MIN_TURNS = 2
_FREEZE_MIN_TURNS = 3


class SessionRiskStore(Protocol):
    """定义生产 Redis 与测试内存实现必须共同满足的原子合同。"""

    async def load(self, scope: SessionRiskScope) -> SessionRiskSnapshot | None:
        """读取当前活动 score；过期且无 pending 的 cycle 返回空。"""

    async def accumulate(
        self,
        scope: SessionRiskScope,
        assessment: SessionRiskAssessment,
    ) -> SessionRiskAccumulateResult:
        """一次性提交不可变 turn assessment，并返回重复或待持久结果。"""

    async def load_pending(self, scope: SessionRiskScope) -> SessionRiskPending | None:
        """读取唯一待持久摘要；未知字段、损坏类型或 owner 漂移必须失败关闭。"""

    async def ack_pending(
        self,
        scope: SessionRiskScope,
        score_id: str,
        revision: int,
        turn_key_digest: str,
    ) -> bool:
        """数据库提交后使用风险分数、修订号和轮次摘要三元条件确认待提交证据。

        参数必须来自同一持久事务；返回是否精确清除，旧 ACK 不能删除并发产生的新证据。
        """

    async def restore(
        self,
        scope: SessionRiskScope,
        snapshot: SessionRiskSnapshot,
    ) -> bool:
        """Redis key 缺失或 revision 较旧时，以持久快照执行只增恢复。"""

    async def reset(self, scope: SessionRiskScope, score_id: str, revision: int) -> bool:
        """人工 resolve 后仅删除匹配 cycle/revision，不能误删并发新 cycle。"""

    async def ping(self) -> bool:
        """验证 Store 当前可用，且不泄露连接配置。"""


@dataclass(slots=True)
class _MemoryEntry:
    """保存内存 Store 单一所有者的活动快照与唯一待提交证据。

    该结构只供确定性测试使用，用来模拟 Redis 同槽位原子状态，不得作为多副本生产共享存储。
    """

    snapshot: SessionRiskSnapshot
    pending: SessionRiskPending | None


class InMemorySessionRiskStore:
    """严格模拟 Redis 合同的测试 Store，不用于生产多副本运行。

    参数：可注入 epoch 秒时间源和故障状态。所有读写都经过同一 ``asyncio.Lock``，确保
    并发测试可真实验证单 turn 去重、跨阈值唯一 revision 和 pending 清债顺序。
    """

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        available: bool = True,
    ) -> None:
        """初始化空热状态；测试可通过 ``set_available`` 注入 Redis 故障。"""

        self._now = now or time.time
        self._available = available
        self._lock = asyncio.Lock()
        self._entries: dict[str, _MemoryEntry] = {}
        # 内存替身也保存版本化 claim，避免测试通过裸 digest 掩盖生产轮换 verifier 的依赖。
        self._claims: dict[tuple[str, str], tuple[int, str, int]] = {}

    def set_available(self, available: bool) -> None:
        """切换测试故障状态；生产代码不得依赖该注入接口。"""

        self._available = available

    async def ping(self) -> bool:
        """可用时返回真，不可用时抛固定 Store 异常。"""

        self._ensure_available()
        return True

    async def load(self, scope: SessionRiskScope) -> SessionRiskSnapshot | None:
        """读取 owner 活跃状态，并按风险窗口清理未冻结过期 cycle。"""

        self._ensure_available()
        async with self._lock:
            self._expire_locked()
            entry = self._entries.get(scope.owner_slot)
            _ensure_entry_owner(scope, entry)
            return entry.snapshot if entry is not None else None

    async def accumulate(
        self,
        scope: SessionRiskScope,
        assessment: SessionRiskAssessment,
    ) -> SessionRiskAccumulateResult:
        """按生产 Lua 顺序原子执行 pending、claim、累计和跃迁。"""

        self._ensure_available()
        async with self._lock:
            self._expire_locked()
            entry = self._entries.get(scope.owner_slot)
            _ensure_entry_owner(scope, entry)
            if entry is not None and entry.pending is not None:
                # 先清旧债再声明新 turn，防止失败尝试留下“已处理”的伪 claim。
                raise SessionRiskPendingEvidence("session risk pending evidence")

            claim_key = (scope.owner_slot, assessment.turn_key_digest)
            existing_claim = self._claims.get(claim_key)
            if existing_claim is not None:
                existing_version, existing_intent, _expires_at = existing_claim
                # 同一 turn 摘要只能绑定首次意图；内容变化必须报冲突，完全相同才按幂等成功返回。
                if (
                    existing_version != scope.hmac_key_version
                    or existing_intent != assessment.turn_intent_digest
                ):
                    # 同 claim key 出现版本或意图差异属于冲突，不能覆盖首次声明。
                    raise SessionRiskTurnConflict("session risk turn conflict")
                # 只有版本和意图都逐字一致才返回幂等成功，且不刷新 TTL。
                return SessionRiskAccumulateResult(
                    snapshot=entry.snapshot if entry is not None else None,
                    pending=None,
                    duplicate=True,
                )
            claim_expires_at = int(self._now()) + SESSION_RISK_TTL_SECONDS
            self._claims[claim_key] = (
                scope.hmac_key_version,
                assessment.turn_intent_digest,
                claim_expires_at,
            )

            # 零分 turn 只保留幂等 claim；不能创建 score、revision、pending 或刷新风险 TTL。
            if assessment.turn_delta == 0:
                return SessionRiskAccumulateResult(
                    snapshot=entry.snapshot if entry is not None else None,
                    pending=None,
                )

            before = entry.snapshot if entry is not None else None
            occurred_at_epoch = int(self._now())
            snapshot = _next_snapshot(scope, assessment, before, occurred_at_epoch)
            pending = _pending_from_transition(scope, assessment, before, snapshot)
            self._entries[scope.owner_slot] = _MemoryEntry(snapshot=snapshot, pending=pending)
            # 返回值必须与刚写入的同一原子状态一致，避免测试替身掩盖 Redis Lua 合同漂移。
            return SessionRiskAccumulateResult(snapshot=snapshot, pending=pending)

    async def load_pending(self, scope: SessionRiskScope) -> SessionRiskPending | None:
        """读取指定所有者唯一的待提交证据并暂停风险窗口自动过期。

        返回待提交对象或空值；存在对象时必须先完成数据库幂等持久化和 ACK，才能接受下一轮累计。
        """

        self._ensure_available()
        async with self._lock:
            self._expire_locked()
            entry = self._entries.get(scope.owner_slot)
            _ensure_entry_owner(scope, entry)
            return entry.pending if entry is not None else None

    async def ack_pending(
        self,
        scope: SessionRiskScope,
        score_id: str,
        revision: int,
        turn_key_digest: str,
    ) -> bool:
        """仅在三元组逐字匹配时清 pending，防止 ACK 删除较新证据。"""

        self._ensure_available()
        async with self._lock:
            entry = self._entries.get(scope.owner_slot)
            _ensure_entry_owner(scope, entry)
            if entry is None or entry.pending is None:
                return False
            pending = entry.pending
            if (
                pending.snapshot.score_id != score_id
                or pending.snapshot.revision != revision
                or pending.assessment.turn_key_digest != turn_key_digest
            ):
                return False
            entry.pending = None
            return True

    async def restore(
        self,
        scope: SessionRiskScope,
        snapshot: SessionRiskSnapshot,
    ) -> bool:
        """只恢复同 HMAC 版本且 revision 更高的持久快照。"""

        self._ensure_available()
        _ensure_snapshot_scope(scope, snapshot)
        async with self._lock:
            self._expire_locked()
            entry = self._entries.get(scope.owner_slot)
            _ensure_entry_owner(scope, entry)
            # 未提交证据代表数据库尚未接纳旧 revision，此时恢复会覆盖安全事实，必须失败关闭。
            if entry is not None and entry.pending is not None:
                raise SessionRiskPendingEvidence("session risk pending evidence")
            if entry is not None and entry.snapshot.revision >= snapshot.revision:
                return False
            self._entries[scope.owner_slot] = _MemoryEntry(snapshot=snapshot, pending=None)
            return True

    async def reset(self, scope: SessionRiskScope, score_id: str, revision: int) -> bool:
        """只删除精确匹配且无 pending 的 cycle，claim 继续自然到期防止重复加分。"""

        self._ensure_available()
        async with self._lock:
            entry = self._entries.get(scope.owner_slot)
            _ensure_entry_owner(scope, entry)
            if entry is None or entry.pending is not None:
                return False
            if entry.snapshot.score_id != score_id or entry.snapshot.revision != revision:
                return False
            self._entries.pop(scope.owner_slot, None)
            return True

    def _ensure_available(self) -> None:
        """把测试故障折叠为与生产 Store 相同的固定异常。"""

        # 故障注入不能泄露底层原因，并应触发与 Redis 失联相同的安全回退路径。
        if not self._available:
            raise SessionRiskStoreUnavailable("session risk store unavailable")

    def _expire_locked(self) -> None:
        """清理 claim 和无 pending 的非冻结过期 cycle；调用方必须持锁。"""

        now = int(self._now())
        expired_claims = [
            key
            for key, (_version, _intent, expires_at) in self._claims.items()
            if expires_at <= now
        ]
        for key in expired_claims:
            self._claims.pop(key, None)
        expired_entries = [
            owner_slot
            for owner_slot, entry in self._entries.items()
            if entry.pending is None
            and entry.snapshot.expires_at_epoch is not None
            and entry.snapshot.expires_at_epoch <= now
        ]
        for owner_slot in expired_entries:
            self._entries.pop(owner_slot, None)


@dataclass(frozen=True, slots=True)
class _RedisConnectionConfig:
    """保存仅在进程内使用的 Redis 连接参数，密码不参与 repr。"""

    host: str
    port: int
    db: int
    username: str | None
    password: str | None
    use_tls: bool


@dataclass(frozen=True, slots=True)
class SessionRiskRedisHmacReferenceReport:
    """保存 previous HMAC 移除前 Redis 全库只读核对的安全计数。

    所有字段都只表示引用数量，不携带 Redis key、owner slot、turn digest、连接地址或密码。
    ``unknown_version_references`` 表示状态使用了 current/previous 之外的版本；
    ``legacy_turn_claims`` 表示升级前没有版本字段的短期 claim。两者都必须阻断删 key，
    不能把“无法证明”解释为“没有引用”。
    """

    score_references: int
    pending_references: int
    turn_claim_references: int
    unknown_version_references: int
    legacy_turn_claims: int

    @property
    def ready(self) -> bool:
        """仅在 Redis 中没有目标版本或不可证明引用时允许继续维护流程。"""

        return not any(
            (
                self.score_references,
                self.pending_references,
                self.turn_claim_references,
                self.unknown_version_references,
                self.legacy_turn_claims,
            )
        )


# SCAN 只匹配 PRD-070 命名空间，随后仍以 exact regex 复核 key，防止相似业务 key 被误读。
_SCORE_SCAN_PATTERN = "metahub:sr:v1:*:score"
_TURN_SCAN_PATTERN = "metahub:sr:v1:*:turn:*"
_SCORE_KEY_PATTERN = re.compile(r"^metahub:sr:v1:\{(srh_[0-9a-f]{64})\}:score$")
_TURN_KEY_PATTERN = re.compile(
    r"^metahub:sr:v1:\{srh_[0-9a-f]{64}\}:turn:srh_[0-9a-f]{64}$"
)
# 新 claim 固定包含 schema、正整数 key version 与 intent HMAC；旧裸 digest 仅用于在 TTL 内兼容重放。
_TURN_CLAIM_VALUE_PATTERN = re.compile(r"^v1:([1-9][0-9]{0,9}):(srh_[0-9a-f]{64})$")
_LEGACY_TURN_CLAIM_VALUE_PATTERN = re.compile(r"^srh_[0-9a-f]{64}$")
_MAX_HMAC_KEY_VERSION = 2_147_483_647
_MAX_SCAN_PAGES = 100_000


# Lua 输入通常由 Python 固定合同生成，但仍须在任何 SET/HSET 前独立 exact-validate；Redis
# 脚本运行时错误不会回滚较早写入，若先建 claim 再发现坏参数会把合法重试永久误判为重复。
_ACCUMULATE_SCRIPT = r"""
local score_key = KEYS[1]
local claim_key = KEYS[2]

local function integer_between(value, minimum, maximum)
  local number = tonumber(value)
  return number and number == math.floor(number) and number >= minimum and number <= maximum
end

local function fixed_hex_id(value, prefix, hex_length)
  if type(value) ~= 'string' or string.len(value) ~= string.len(prefix) + hex_length then
    return false
  end
  if string.sub(value, 1, string.len(prefix)) ~= prefix then return false end
  return not string.find(string.sub(value, string.len(prefix) + 1), '[^0-9a-f]')
end

local function bounded_ascii(value, maximum)
  if type(value) ~= 'string' or string.len(value) < 1 or string.len(value) > maximum then
    return false
  end
  for index = 1, string.len(value) do
    local byte = string.byte(value, index)
    if byte < 32 or byte > 126 then return false end
  end
  return true
end

local function bounded_request_id(value)
  if type(value) ~= 'string' or string.len(value) < 1 or string.len(value) > 128 then
    return false
  end
  for index = 1, string.len(value) do
    local byte = string.byte(value, index)
    local allowed = (byte >= 48 and byte <= 57) or (byte >= 65 and byte <= 90) or
                    (byte >= 97 and byte <= 122) or byte == 46 or byte == 95 or
                    byte == 58 or byte == 45
    if not allowed or (index == 1 and not ((byte >= 48 and byte <= 57) or
                                           (byte >= 65 and byte <= 90) or
                                           (byte >= 97 and byte <= 122))) then
      return false
    end
  end
  return true
end

if #KEYS ~= 2 or #ARGV ~= 23 then return redis.error_reply('BAD_ARITY') end
if not fixed_hex_id(ARGV[1], 'srh_', 64) or not fixed_hex_id(ARGV[2], 'srh_', 64) then
  return redis.error_reply('BAD_DIGEST')
end
if string.sub(claim_key, -74) ~= ':turn:' .. ARGV[1] then
  return redis.error_reply('BAD_CLAIM_KEY')
end
if not integer_between(ARGV[3], 0, 4000) or
   not integer_between(ARGV[4], 0, 1) or
   not integer_between(ARGV[5], 0, 1) or
   not integer_between(ARGV[6], 0, 1) then
  return redis.error_reply('BAD_DELTA')
end
if ARGV[7] ~= 'session-risk-threshold-v1' or not integer_between(ARGV[8], 1, 2147483647) or
   not integer_between(ARGV[9], 1, 9007199254740991) then
  return redis.error_reply('BAD_VERSION')
end
if not bounded_request_id(ARGV[10]) or not bounded_request_id(ARGV[11]) or
   (ARGV[12] ~= '0' and ARGV[12] ~= '1') or not bounded_ascii(ARGV[13], 128) or
   not bounded_ascii(ARGV[14], 128) then
  return redis.error_reply('BAD_SOURCE')
end
if ARGV[15] ~= '' and not fixed_hex_id(ARGV[15], 'irr_', 32) then
  return redis.error_reply('BAD_INTENT_ID')
end
if ARGV[16] ~= '' and not fixed_hex_id(ARGV[16], 'chm_', 32) then
  return redis.error_reply('BAD_MESSAGE_ID')
end
if not fixed_hex_id(ARGV[17], 'srs_', 32) or ARGV[18] ~= '1800' then
  return redis.error_reply('BAD_SCORE_ID')
end
local intent_type = ARGV[19]
if intent_type ~= 'none' and intent_type ~= 'credential_exfiltration' and
   intent_type ~= 'system_prompt_extraction' and intent_type ~= 'policy_bypass' and
   intent_type ~= 'jailbreak' then
  return redis.error_reply('BAD_INTENT')
end
if not integer_between(ARGV[20], 0, 10000) then return redis.error_reply('BAD_INTENT') end
local dlp_action = ARGV[21]
if dlp_action ~= 'allow' and dlp_action ~= 'log_only' and dlp_action ~= 'block' then
  return redis.error_reply('BAD_DLP')
end
if not integer_between(ARGV[22], 0, 100) or (ARGV[23] ~= '0' and ARGV[23] ~= '1') then
  return redis.error_reply('BAD_SIGNAL')
end

local intent_score = tonumber(ARGV[20])
local intent_delta = 0
if intent_type == 'none' then
  if intent_score ~= 0 then return redis.error_reply('BAD_INTENT') end
elseif intent_score >= 9500 then intent_delta = 3600
elseif intent_score >= 8000 then intent_delta = 2600
elseif intent_score >= 6500 then intent_delta = 1800
end
local dlp_matches = tonumber(ARGV[22])
local dlp_delta = 0
if dlp_action == 'allow' then
  if dlp_matches ~= 0 then return redis.error_reply('BAD_DLP') end
elseif dlp_matches < 1 then return redis.error_reply('BAD_DLP')
elseif dlp_action == 'block' then dlp_delta = 3000
else dlp_delta = 1200 end
local tool_delta = ARGV[23] == '1' and 2400 or 0
local expected_delta = math.min(4000, intent_delta + dlp_delta + tool_delta)
local expected_jailbreak = intent_delta > 0 and 1 or 0
local expected_sensitive = (
  dlp_delta > 0 or
  (intent_delta > 0 and intent_type == 'credential_exfiltration')
) and 1 or 0
local expected_tool = ARGV[23] == '1' and 1 or 0
if tonumber(ARGV[3]) ~= expected_delta or tonumber(ARGV[4]) ~= expected_jailbreak or
   tonumber(ARGV[5]) ~= expected_sensitive or tonumber(ARGV[6]) ~= expected_tool then
  return redis.error_reply('BAD_COMBINATION')
end

if redis.call('HEXISTS', score_key, 'pending_revision') == 1 then
  return {'PENDING'}
end
local existing_intent = redis.call('GET', claim_key)
local claim_value = 'v1:' .. ARGV[8] .. ':' .. ARGV[2]
if existing_intent then
  -- 升级前的裸 digest 最多保留一个 30 分钟 TTL；仍按同 key 幂等，
  -- 但 verifier 会在其过期前阻断删 key。
  if existing_intent == claim_value or existing_intent == ARGV[2] then return {'DUPLICATE'} end
  return {'CONFLICT'}
end
local delta = tonumber(ARGV[3])
if delta == 0 then
  local clean_claim = redis.call('SET', claim_key, claim_value, 'EX', ARGV[18], 'NX')
  if not clean_claim then return {'RETRY'} end
  return {'CLEAN'}
end

local old_score = tonumber(redis.call('HGET', score_key, 'score') or '0')
local old_turns = tonumber(redis.call('HGET', score_key, 'risky_turn_count') or '0')
local old_revision = tonumber(redis.call('HGET', score_key, 'revision') or '0')
local old_jailbreak = tonumber(redis.call('HGET', score_key, 'jailbreak_count') or '0')
local old_sensitive = tonumber(redis.call('HGET', score_key, 'sensitive_count') or '0')
local old_tool = tonumber(redis.call('HGET', score_key, 'tool_inducement_count') or '0')
local old_state = redis.call('HGET', score_key, 'state') or 'none'
local score_id = redis.call('HGET', score_key, 'score_id') or ARGV[17]
local window_started = tonumber(redis.call('HGET', score_key, 'window_started_at_epoch') or ARGV[9])
local stored_hmac = tonumber(redis.call('HGET', score_key, 'hmac_key_version') or ARGV[8])
local stored_threshold = redis.call('HGET', score_key, 'threshold_version') or ARGV[7]
if not old_score or old_score < 0 or old_score > 10000 or not old_turns or old_turns < 0 or
   not old_revision or old_revision < 0 or not old_jailbreak or old_jailbreak < 0 or
   not old_sensitive or old_sensitive < 0 or not old_tool or old_tool < 0 or
   not window_started or not stored_hmac or not fixed_hex_id(score_id, 'srs_', 32) then
  return redis.error_reply('BAD_STATE')
end
if old_revision > 0 then
  if stored_hmac ~= tonumber(ARGV[8]) or stored_threshold ~= ARGV[7] then
    return redis.error_reply('OWNER_MISMATCH')
  end
  if old_state ~= 'monitoring' and old_state ~= 'high_risk' and old_state ~= 'frozen' then
    return redis.error_reply('BAD_STATE')
  end
end

local new_score = math.min(10000, old_score + delta)
local new_turns = old_turns + 1
local new_revision = old_revision + 1
local new_jailbreak = old_jailbreak + tonumber(ARGV[4])
local new_sensitive = old_sensitive + tonumber(ARGV[5])
local new_tool = old_tool + tonumber(ARGV[6])
local new_state = 'monitoring'
if new_score >= 9000 and new_turns >= 3 then
  new_state = 'frozen'
elseif new_score >= 7000 and new_turns >= 2 then
  new_state = 'high_risk'
end
local high_transition = (old_state ~= 'high_risk' and old_state ~= 'frozen') and
                        (new_state == 'high_risk' or new_state == 'frozen')
local freeze_transition = old_state ~= 'frozen' and new_state == 'frozen'
local expires_at = 0
if new_state ~= 'frozen' then expires_at = tonumber(ARGV[9]) + tonumber(ARGV[18]) end

local pending = {
  schema_version=1, score_id=score_id, score_before=old_score, score_after=new_score,
  state_before=old_state, state_after=new_state, revision=new_revision,
  risky_turn_count=new_turns, jailbreak_count=new_jailbreak,
  sensitive_count=new_sensitive, tool_inducement_count=new_tool,
  window_started_at_epoch=window_started, last_risk_at_epoch=tonumber(ARGV[9]),
  expires_at_epoch=expires_at, hmac_key_version=tonumber(ARGV[8]),
  threshold_version=ARGV[7], high_risk_transition=high_transition,
  freeze_transition=freeze_transition, turn_key_digest=ARGV[1], turn_intent_digest=ARGV[2],
  turn_delta=delta, jailbreak_count_delta=tonumber(ARGV[4]),
  sensitive_count_delta=tonumber(ARGV[5]), tool_inducement_count_delta=tonumber(ARGV[6]),
  source_request_id=ARGV[10], source_trace_id=ARGV[11],
  input_truncated=ARGV[12] == '1', detector_version=ARGV[13], ruleset_version=ARGV[14],
  intent_risk_type=ARGV[19], intent_risk_score=tonumber(ARGV[20]),
  dlp_current_turn_match_count=tonumber(ARGV[22]), tool_inducement_detected=ARGV[23] == '1',
  occurred_at_epoch=tonumber(ARGV[9])
}
if ARGV[15] ~= '' then
  pending.intent_risk_result_id=ARGV[15]
else
  pending.intent_risk_result_id=cjson.null
end
if ARGV[16] ~= '' then
  pending.chat_message_id=ARGV[16]
else
  pending.chat_message_id=cjson.null
end
pending.dlp_action=ARGV[21]

-- cjson 编码也可能报错；必须在 claim/score 任一写入前完成，确保参数故障零残留。
local pending_payload = cjson.encode(pending)
local claim_ok = redis.call('SET', claim_key, claim_value, 'EX', ARGV[18], 'NX')
if not claim_ok then return {'RETRY'} end

redis.call('HSET', score_key,
  'schema_version', '1', 'score_id', score_id, 'score', tostring(new_score),
  'risky_turn_count', tostring(new_turns), 'jailbreak_count', tostring(new_jailbreak),
  'sensitive_count', tostring(new_sensitive), 'tool_inducement_count', tostring(new_tool),
  'state', new_state, 'revision', tostring(new_revision),
  'window_started_at_epoch', tostring(window_started),
  'last_risk_at_epoch', ARGV[9], 'expires_at_epoch', tostring(expires_at),
  'hmac_key_version', ARGV[8], 'threshold_version', ARGV[7],
  'pending_revision', tostring(new_revision), 'pending_turn_digest', ARGV[1],
  'pending_payload', pending_payload)
redis.call('PERSIST', score_key)
return {'ACCUMULATED'}
"""

_ACK_SCRIPT = r"""
local score_id = redis.call('HGET', KEYS[1], 'score_id')
local revision = redis.call('HGET', KEYS[1], 'pending_revision')
local turn_digest = redis.call('HGET', KEYS[1], 'pending_turn_digest')
if not score_id or not revision or not turn_digest then return 0 end
if score_id ~= ARGV[1] or revision ~= ARGV[2] or turn_digest ~= ARGV[3] then return 0 end
redis.call('HDEL', KEYS[1], 'pending_revision', 'pending_turn_digest', 'pending_payload')
local state = redis.call('HGET', KEYS[1], 'state')
if state == 'frozen' then redis.call('PERSIST', KEYS[1])
else
  local expires_at = tonumber(redis.call('HGET', KEYS[1], 'expires_at_epoch') or '0')
  local ttl = math.max(1, expires_at - tonumber(ARGV[4]))
  redis.call('EXPIRE', KEYS[1], ttl)
end
return 1
"""

_RESTORE_SCRIPT = r"""
if redis.call('HEXISTS', KEYS[1], 'pending_revision') == 1 then return -1 end
local current_revision = tonumber(redis.call('HGET', KEYS[1], 'revision') or '0')
if current_revision >= tonumber(ARGV[8]) then return 0 end
redis.call('HSET', KEYS[1],
  'schema_version', '1', 'score_id', ARGV[1], 'score', ARGV[2],
  'risky_turn_count', ARGV[3], 'jailbreak_count', ARGV[4],
  'sensitive_count', ARGV[5], 'tool_inducement_count', ARGV[6],
  'state', ARGV[7], 'revision', ARGV[8], 'window_started_at_epoch', ARGV[9],
  'last_risk_at_epoch', ARGV[10], 'expires_at_epoch', ARGV[11],
  'hmac_key_version', ARGV[12], 'threshold_version', ARGV[13])
if ARGV[7] == 'frozen' then redis.call('PERSIST', KEYS[1])
else redis.call('EXPIREAT', KEYS[1], ARGV[11]) end
return 1
"""

_RESET_SCRIPT = r"""
if redis.call('HEXISTS', KEYS[1], 'pending_revision') == 1 then return 0 end
if redis.call('HGET', KEYS[1], 'score_id') ~= ARGV[1] then return 0 end
if redis.call('HGET', KEYS[1], 'revision') ~= ARGV[2] then return 0 end
return redis.call('DEL', KEYS[1])
"""


class RedisSessionRiskStore:
    """使用短连接、硬 deadline、RESP 数组和 Lua 的生产会话风险 Store。"""

    def __init__(
        self,
        redis_url: str,
        *,
        connect_timeout_seconds: float = 0.5,
        io_timeout_seconds: float = 0.5,
    ) -> None:
        """解析连接配置；URL 和密码只保存在不可序列化私有字段。"""

        self._config = _parse_redis_url(redis_url)
        self._connect_timeout_seconds = _bounded_timeout(connect_timeout_seconds)
        self._io_timeout_seconds = _bounded_timeout(io_timeout_seconds)

    async def ping(self) -> bool:
        """执行 PING；任何网络、认证或协议错误都折叠为固定 Store 异常。"""

        response = await self._command("PING")
        # 只接受协议规定的 PONG；代理或错误服务返回其他文本时不能误判热状态可用。
        if response != "PONG":
            raise SessionRiskStoreUnavailable("session risk store unavailable")
        return True

    async def inspect_hmac_rotation_references(
        self,
        *,
        current_version: int,
        removing_version: int,
    ) -> SessionRiskRedisHmacReferenceReport:
        """只读扫描 Redis，统计阻止 previous HMAC 移除的引用。

        参数：仍配置的 current version 与准备移除的 previous version。返回：仅含计数的安全
        报告。关键约束：运维必须先停止 Gateway 新写入；本方法不删除、续期或 ACK 任何 key，
        也不把扫描到的 owner/turn 摘要带出 Store。未知版本、旧裸 claim 或协议污染一律阻断。
        """

        _validate_rotation_versions(current_version, removing_version)
        known_versions = {current_version, removing_version}
        score_references = 0
        pending_references = 0
        turn_claim_references = 0
        unknown_version_references = 0
        legacy_turn_claims = 0

        # 先确认连接可用，再扫描两个互斥 key 形状；SCAN 重复结果由集合去重。
        await self.ping()
        score_keys = await self._scan_rotation_keys(_SCORE_SCAN_PATTERN)
        turn_keys = await self._scan_rotation_keys(_TURN_SCAN_PATTERN)
        for score_key in sorted(score_keys):
            match = _SCORE_KEY_PATTERN.fullmatch(score_key)
            if match is None:
                # MATCH 不是结构验证；相似命名空间 key 不能被宽松当作会话风险状态。
                raise SessionRiskStoreUnavailable("session risk store unavailable")
            values = _exact_hash_response(await self._command("HGETALL", score_key))
            if not values:
                # SCAN 与读取之间自然过期属于已无引用，不把消失 key 误报为损坏。
                continue
            snapshot = _snapshot_from_hash(values)
            if snapshot.hmac_key_version == removing_version:
                score_references += 1
            elif snapshot.hmac_key_version not in known_versions:
                unknown_version_references += 1

            pending_payload = values.get("pending_payload")
            if pending_payload is not None:
                # 使用真实 exact pending parser；合成 scope 只承载扫描 key 中已有的 HMAC owner，
                # 不引入 tenant、Virtual Key 或原始 Session 明文。
                inspection_scope = SessionRiskScope(
                    tenant_id="rotation-verifier",
                    virtual_key_id="rotation-verifier",
                    scope_type="api_session",
                    scope_key=match.group(1),
                    owner_slot=match.group(1),
                    hmac_key_version=snapshot.hmac_key_version,
                )
                pending = _pending_from_json(inspection_scope, pending_payload)
                if pending.snapshot.hmac_key_version == removing_version:
                    pending_references += 1
                elif pending.snapshot.hmac_key_version not in known_versions:
                    unknown_version_references += 1

        for turn_key in sorted(turn_keys):
            if _TURN_KEY_PATTERN.fullmatch(turn_key) is None:
                # SCAN MATCH 结果仍需 exact key 复核，防止相似命名空间污染验收结论。
                raise SessionRiskStoreUnavailable("session risk store unavailable")
            raw_claim = await self._command("GET", turn_key)
            if raw_claim is None:
                # turn claim 可能在 SCAN 后自然到期；不存在即不再引用任何密钥版本。
                continue
            if type(raw_claim) is not str:
                # claim 只允许 Bulk String；数组或整数响应意味着协议状态不可解释。
                raise SessionRiskStoreUnavailable("session risk store unavailable")
            if _LEGACY_TURN_CLAIM_VALUE_PATTERN.fullmatch(raw_claim) is not None:
                # 旧值没有版本，必须等待其固定 TTL 自然归零，不能猜测它属于 current。
                legacy_turn_claims += 1
                continue
            claim_version, _intent_digest = _parse_turn_claim_value(raw_claim)
            if claim_version == removing_version:
                turn_claim_references += 1
            elif claim_version not in known_versions:
                unknown_version_references += 1

        # 报告只返回聚合计数，扫描到的 owner、turn 和 intent 摘要均停留在函数局部变量。
        return SessionRiskRedisHmacReferenceReport(
            score_references=score_references,
            pending_references=pending_references,
            turn_claim_references=turn_claim_references,
            unknown_version_references=unknown_version_references,
            legacy_turn_claims=legacy_turn_claims,
        )

    async def _scan_rotation_keys(self, pattern: str) -> set[str]:
        """穷尽单个 Redis DB 的 SCAN 游标，并严格解析二维数组响应。

        返回去重后的 key 集合。页数、游标、元素类型或 key 长度异常都表示无法证明引用归零，
        因而统一失败关闭；错误中不包含 pattern、cursor 或实际 key。
        """

        cursor = "0"
        seen_cursors: set[str] = set()
        keys: set[str] = set()
        for _page in range(_MAX_SCAN_PAGES):
            response = await self._command("SCAN", cursor, "MATCH", pattern, "COUNT", "256")
            next_cursor, page_keys = _parse_scan_response(response)
            for key in page_keys:
                # 会话风险 key 有固定短形状；超长 key 可能是协议污染，不能带入内存集合。
                if len(key.encode("utf-8")) > 256:
                    # 超长 key 不进入集合，避免恶意 Redis 状态放大 verifier 内存。
                    raise SessionRiskStoreUnavailable("session risk store unavailable")
                keys.add(key)
            if next_cursor == "0":
                return keys
            if next_cursor in seen_cursors:
                # 非零游标循环会让扫描永远不完整，必须拒绝“部分扫描成功”。
                raise SessionRiskStoreUnavailable("session risk store unavailable")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        # 达到硬页数仍未回到零游标表示扫描不完整，不能输出 ready。
        raise SessionRiskStoreUnavailable("session risk store unavailable")

    async def load(self, scope: SessionRiskScope) -> SessionRiskSnapshot | None:
        """读取 exact HGETALL 快照，不容忍未知字段或 owner 版本漂移。"""

        response = await self._command("HGETALL", _score_key(scope))
        values = _exact_hash_response(response)
        if not values:
            return None
        snapshot = _snapshot_from_hash(values)
        _ensure_snapshot_scope(scope, snapshot)
        return snapshot

    async def accumulate(
        self,
        scope: SessionRiskScope,
        assessment: SessionRiskAssessment,
    ) -> SessionRiskAccumulateResult:
        """通过单次 EVAL 原子提交 turn；状态响应之外的任何形状都失败关闭。"""

        score_id = f"srs_{uuid4().hex}"
        occurred_at_epoch = int(time.time())
        response = await self._command(
            "EVAL",
            _ACCUMULATE_SCRIPT,
            "2",
            _score_key(scope),
            _claim_key(scope, assessment.turn_key_digest),
            assessment.turn_key_digest,
            assessment.turn_intent_digest,
            str(assessment.turn_delta),
            str(assessment.jailbreak_count_delta),
            str(assessment.sensitive_count_delta),
            str(assessment.tool_inducement_count_delta),
            assessment.threshold_version,
            str(scope.hmac_key_version),
            str(occurred_at_epoch),
            assessment.source_request_id,
            assessment.source_trace_id,
            "1" if assessment.input_truncated else "0",
            assessment.detector_version,
            assessment.ruleset_version,
            assessment.intent_risk_result_id or "",
            assessment.chat_message_id or "",
            score_id,
            str(SESSION_RISK_TTL_SECONDS),
            assessment.intent_risk_type,
            str(assessment.intent_risk_score),
            assessment.dlp_action or "",
            str(assessment.dlp_current_turn_match_count),
            "1" if assessment.tool_inducement_detected else "0",
        )
        status = _status_response(response)
        # pending 必须先进入数据库并精确 ACK，禁止新 turn 越过未提交的安全证据。
        if status == "PENDING":
            raise SessionRiskPendingEvidence("session risk pending evidence")
        # 相同 turn 摘要绑定了不同意图，按显式冲突拒绝，不能当作重复请求吞掉。
        if status == "CONFLICT":
            raise SessionRiskTurnConflict("session risk turn conflict")
        if status == "RETRY":
            # NX 极小竞态必须由调用方按一次有界重试处理，不能在 Store 内忙等。
            raise SessionRiskStoreUnavailable("session risk store unavailable")
        if status == "DUPLICATE":
            return SessionRiskAccumulateResult(
                snapshot=await self.load(scope),
                pending=None,
                duplicate=True,
            )
        # 零风险 turn 只写幂等 claim，读取当前快照用于保持调用方响应合同一致。
        if status == "CLEAN":
            return SessionRiskAccumulateResult(snapshot=await self.load(scope), pending=None)
        # Lua 状态枚举是内部安全协议；未知值说明部署版本不一致，必须失败关闭。
        if status != "ACCUMULATED":
            raise SessionRiskStoreUnavailable("session risk store unavailable")
        pending = await self.load_pending(scope)
        # 有分累计必须同时生成 pending，缺失意味着 Redis 原子证据不完整，不能继续持久化。
        if pending is None:
            raise SessionRiskStoreUnavailable("session risk store unavailable")
        # 以 pending 内同一 revision 的快照返回，避免第二次读取被并发状态替换。
        return SessionRiskAccumulateResult(snapshot=pending.snapshot, pending=pending)

    async def load_pending(self, scope: SessionRiskScope) -> SessionRiskPending | None:
        """解析 exact-schema pending JSON，并逐字段重建不可变模型。"""

        response = await self._command("HGET", _score_key(scope), "pending_payload")
        if response is None:
            return None
        # pending 仅允许有限 UTF-8 JSON；异常类型或超大载荷按协议污染处理。
        if type(response) is not str or len(response.encode("utf-8")) > 16_384:
            raise SessionRiskStoreUnavailable("session risk store unavailable")
        return _pending_from_json(scope, response)

    async def ack_pending(
        self,
        scope: SessionRiskScope,
        score_id: str,
        revision: int,
        turn_key_digest: str,
    ) -> bool:
        """运行三元 CAS ACK，并根据 frozen/窗口终点恢复 PERSIST 或 TTL。"""

        response = await self._command(
            "EVAL",
            _ACK_SCRIPT,
            "1",
            _score_key(scope),
            score_id,
            str(revision),
            turn_key_digest,
            str(int(time.time())),
        )
        # ACK 只接受 Lua CAS 的零一整数，拒绝 bool、字符串及代理改写后的宽松响应。
        if type(response) is not int or response not in {0, 1}:
            raise SessionRiskStoreUnavailable("session risk store unavailable")
        return bool(response)

    async def restore(
        self,
        scope: SessionRiskScope,
        snapshot: SessionRiskSnapshot,
    ) -> bool:
        """使用 Lua 只增恢复持久快照；存在 pending 时明确拒绝。"""

        _ensure_snapshot_scope(scope, snapshot)
        expires_at = snapshot.expires_at_epoch or 0
        response = await self._command(
            "EVAL",
            _RESTORE_SCRIPT,
            "1",
            _score_key(scope),
            snapshot.score_id,
            str(snapshot.score),
            str(snapshot.risky_turn_count),
            str(snapshot.jailbreak_count),
            str(snapshot.sensitive_count),
            str(snapshot.tool_inducement_count),
            snapshot.state,
            str(snapshot.revision),
            str(snapshot.window_started_at_epoch),
            str(snapshot.last_risk_at_epoch),
            str(expires_at),
            str(snapshot.hmac_key_version),
            snapshot.threshold_version,
        )
        # -1 是 Lua 对 pending 覆盖风险的专用拒绝码，需保留为可区分业务冲突。
        if response == -1:
            raise SessionRiskPendingEvidence("session risk pending evidence")
        # 其余恢复结果只能是未更新或已更新，任何其他形状都表明脚本合同漂移。
        if type(response) is not int or response not in {0, 1}:
            raise SessionRiskStoreUnavailable("session risk store unavailable")
        return bool(response)

    async def reset(self, scope: SessionRiskScope, score_id: str, revision: int) -> bool:
        """仅删除分数标识与修订号精确匹配且没有待提交证据的 Redis 周期。

        返回是否完成删除；CAS 不命中表示出现并发新事实，调用方不得扩大条件重试或误删新周期。
        """

        response = await self._command(
            "EVAL",
            _RESET_SCRIPT,
            "1",
            _score_key(scope),
            score_id,
            str(revision),
        )
        # reset 是精确 CAS 删除，只接受零一整数，避免类型强转把异常响应当成成功。
        if type(response) is not int or response not in {0, 1}:
            raise SessionRiskStoreUnavailable("session risk store unavailable")
        return bool(response)

    async def _command(self, *parts: str) -> object:
        """在连接、认证、写入和响应读取各阶段应用硬 deadline。

        异常只保留固定 Store 文本；Redis URL、密码、Lua 服务端消息和 socket 目标都不会
        进入 ``__cause__``，避免通用 ASGI 日志意外展开敏感连接信息。
        """

        writer: asyncio.StreamWriter | None = None
        try:
            ssl_context = ssl.create_default_context() if self._config.use_tls else None
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self._config.host,
                    self._config.port,
                    ssl=ssl_context,
                    server_hostname=self._config.host if ssl_context is not None else None,
                ),
                timeout=self._connect_timeout_seconds,
            )
            await self._authenticate(reader, writer)
            writer.write(_encode_command(parts))
            await asyncio.wait_for(writer.drain(), timeout=self._io_timeout_seconds)
            return await asyncio.wait_for(
                _read_response(reader),
                timeout=self._io_timeout_seconds,
            )
        # 调用取消属于上层生命周期信号，必须原样传播，不能被归类为 Redis 故障。
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        # 网络、认证和协议细节可能含连接配置，统一去因果链后转换为固定安全异常。
        except Exception:
            raise SessionRiskStoreUnavailable("session risk store unavailable") from None
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    # 关闭故障不能覆盖已经获得的业务结果或固定 Store 异常。
                    pass

    async def _authenticate(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """按需 AUTH/SELECT；密码只进入已加密或回环连接的 RESP 写缓冲。"""

        if self._config.password is not None:
            auth_parts = (
                ("AUTH", self._config.username, self._config.password)
                if self._config.username is not None
                else ("AUTH", self._config.password)
            )
            writer.write(_encode_command(auth_parts))
            await asyncio.wait_for(writer.drain(), timeout=self._io_timeout_seconds)
            auth_response = await asyncio.wait_for(
                _read_response(reader),
                timeout=self._io_timeout_seconds,
            )
            # RESP simple-string 不是天然成功证明；只接受 AUTH 的精确 OK，防止无关响应绕过认证。
            if auth_response != "OK":
                raise SessionRiskStoreUnavailable("session risk store unavailable")
        if self._config.db:
            writer.write(_encode_command(("SELECT", str(self._config.db))))
            await asyncio.wait_for(writer.drain(), timeout=self._io_timeout_seconds)
            select_response = await asyncio.wait_for(
                _read_response(reader),
                timeout=self._io_timeout_seconds,
            )
            if select_response != "OK":
                # 数据库选择失败时不能继续在默认 DB 读写，否则不同环境会共享错误的热状态。
                raise SessionRiskStoreUnavailable("session risk store unavailable")


def _next_snapshot(
    scope: SessionRiskScope,
    assessment: SessionRiskAssessment,
    before: SessionRiskSnapshot | None,
    occurred_at_epoch: int,
) -> SessionRiskSnapshot:
    """按冻结阈值合同和 Store 时钟构造下一 revision，供内存实现复核 Lua。"""

    score_before = before.score if before is not None else 0
    turns_before = before.risky_turn_count if before is not None else 0
    score = min(_SCORE_SCALE, score_before + assessment.turn_delta)
    risky_turn_count = turns_before + 1
    state = _state_for(score, risky_turn_count)
    expires_at = None if state == "frozen" else occurred_at_epoch + SESSION_RISK_TTL_SECONDS
    # 快照只携带摘要计数与版本；原始会话、turn 和正文不会进入可持久化热状态。
    return SessionRiskSnapshot(
        score_id=before.score_id if before is not None else f"srs_{uuid4().hex}",
        score=score,
        risky_turn_count=risky_turn_count,
        jailbreak_count=(before.jailbreak_count if before else 0)
        + assessment.jailbreak_count_delta,
        sensitive_count=(before.sensitive_count if before else 0)
        + assessment.sensitive_count_delta,
        tool_inducement_count=(before.tool_inducement_count if before else 0)
        + assessment.tool_inducement_count_delta,
        state=state,
        revision=(before.revision if before else 0) + 1,
        window_started_at_epoch=(
            before.window_started_at_epoch if before else occurred_at_epoch
        ),
        last_risk_at_epoch=occurred_at_epoch,
        expires_at_epoch=expires_at,
        hmac_key_version=scope.hmac_key_version,
        threshold_version=assessment.threshold_version,
    )


def _pending_from_transition(
    scope: SessionRiskScope,
    assessment: SessionRiskAssessment,
    before: SessionRiskSnapshot | None,
    snapshot: SessionRiskSnapshot,
) -> SessionRiskPending:
    """从 before/after 构造唯一安全 pending，不复制正文或原始 Header。"""

    state_before = before.state if before is not None else "none"
    # pending 固化同一次跃迁的 before/after，供数据库事务幂等落地并精确 ACK。
    return SessionRiskPending(
        scope=scope,
        assessment=assessment,
        snapshot=snapshot,
        score_before=before.score if before is not None else 0,
        state_before=state_before,
        high_risk_transition=(
            state_before not in {"high_risk", "frozen"}
            and snapshot.state in {"high_risk", "frozen"}
        ),
        freeze_transition=state_before != "frozen" and snapshot.state == "frozen",
    )


def _state_for(score: int, risky_turn_count: int) -> str:
    """按 ``>=`` 与最少风险 turn 双门限返回固定状态。"""

    if score >= _FREEZE_THRESHOLD and risky_turn_count >= _FREEZE_MIN_TURNS:
        return "frozen"
    if score >= _HIGH_RISK_THRESHOLD and risky_turn_count >= _HIGH_RISK_MIN_TURNS:
        return "high_risk"
    return "monitoring"


def _ensure_entry_owner(scope: SessionRiskScope, entry: _MemoryEntry | None) -> None:
    """复核内存状态的 HMAC owner 版本，模拟 Redis/DB owner 漂移失败关闭。"""

    if entry is not None:
        _ensure_snapshot_scope(scope, entry.snapshot)


def _ensure_snapshot_scope(scope: SessionRiskScope, snapshot: SessionRiskSnapshot) -> None:
    """禁止 current/previous HMAC 版本在同一 owner 状态上被静默混用。"""

    # HMAC 版本漂移可能让不同租户所有者碰撞到错误状态，必须在读取边界失败关闭。
    if snapshot.hmac_key_version != scope.hmac_key_version:
        raise SessionRiskStoreUnavailable("session risk hmac version unavailable")


def _score_key(scope: SessionRiskScope) -> str:
    """构造只包含 HMAC 所有者槽位的 Redis 风险分数键。

    返回值不含租户、会话或 Virtual Key 原文，降低运维扫描 Redis 时泄露业务身份的风险。
    """

    return f"metahub:sr:v1:{{{scope.owner_slot}}}:score"


def _claim_key(scope: SessionRiskScope, turn_key_digest: str) -> str:
    """构造与风险分数位于同一集群槽位的轮次声明键。

    同槽位是 Lua 原子累计的前提；键中只放带域 HMAC 摘要，不保存公开 Header 或消息标识原文。
    """

    return f"metahub:sr:v1:{{{scope.owner_slot}}}:turn:{turn_key_digest}"


def _turn_claim_value(hmac_key_version: int, turn_intent_digest: str) -> str:
    """构造含 schema/version 的固定 claim 值，供验收替身与测试复用生产合同。

    参数必须是 Lua 同边界的正整数版本和规范 HMAC；返回值不会包含 Session/Turn Header 或
    Prompt。该函数不负责 Redis 写入，生产原子写仍由 Lua 在单次 EVAL 内完成。
    """

    if (
        type(hmac_key_version) is not int
        or not 1 <= hmac_key_version <= _MAX_HMAC_KEY_VERSION
        or type(turn_intent_digest) is not str
        or _LEGACY_TURN_CLAIM_VALUE_PATTERN.fullmatch(turn_intent_digest) is None
    ):
        # Lua/Python 合同任一输入不规范都会让 claim 无法被 verifier 精确解释。
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    return f"v1:{hmac_key_version}:{turn_intent_digest}"


def _parse_turn_claim_value(raw: str) -> tuple[int, str]:
    """解析 exact claim schema；未知 schema、前导零或超大版本均失败关闭。"""

    match = _TURN_CLAIM_VALUE_PATTERN.fullmatch(raw) if type(raw) is str else None
    if match is None:
        # 未知 schema 或非规范 digest 不允许回退为升级前裸值。
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    version = int(match.group(1))
    if version > _MAX_HMAC_KEY_VERSION:
        # 版本上限与 Lua 一致，防止不同运行时对超大整数解释不一致。
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    return version, match.group(2)


def _validate_rotation_versions(current_version: int, removing_version: int) -> None:
    """验证 verifier 只能删除严格小于 current 的正整数 previous 版本。"""

    if (
        type(current_version) is not int
        or type(removing_version) is not int
        or not 1 <= removing_version < current_version <= _MAX_HMAC_KEY_VERSION
    ):
        # verifier 只允许 current/previous 的严格递增组合，禁止删除当前版本。
        raise SessionRiskStoreUnavailable("session risk store unavailable")


# SCAN 是 verifier 完整性的核心边界，独立 helper 统一拒绝分页协议漂移。
def _parse_scan_response(response: object) -> tuple[str, list[str]]:
    """严格解析 Redis ``SCAN`` 的游标与 key 数组，拒绝代理改写或分页协议漂移。"""

    if (
        type(response) is not list
        or len(response) != 2
        or type(response[0]) is not str
        or type(response[1]) is not list
    ):
        # 第一层必须精确是 cursor 与 key 数组，不能把代理响应宽松展开。
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    cursor = response[0]
    page = response[1]
    if (
        not cursor.isascii()
        or not cursor.isdecimal()
        or (len(cursor) > 1 and cursor.startswith("0"))
        or int(cursor) > 18_446_744_073_709_551_615
        or any(type(key) is not str for key in page)
    ):
        # 非规范游标或非字符串 key 会破坏分页去重和 exact key 校验。
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    return cursor, page


# Redis Hash 字段白名单冻结完整快照与 pending 元数据，未知字段一律视为协议污染。
_HASH_FIELDS = frozenset(
    {
        "schema_version",
        "score_id",
        "score",
        "risky_turn_count",
        "jailbreak_count",
        "sensitive_count",
        "tool_inducement_count",
        "state",
        "revision",
        "window_started_at_epoch",
        "last_risk_at_epoch",
        "expires_at_epoch",
        "hmac_key_version",
        "threshold_version",
        "pending_revision",
        "pending_turn_digest",
        "pending_payload",
    }
)
# 快照读取必须具备除 pending 三字段外的全部键，避免用缺省值掩盖部分写入。
_SNAPSHOT_REQUIRED_FIELDS = _HASH_FIELDS - {
    "pending_revision",
    "pending_turn_digest",
    "pending_payload",
}


def _exact_hash_response(response: object) -> dict[str, str]:
    """把 RESP HGETALL 数组转换为 exact-key 字典，拒绝重复、奇数或未知字段。"""

    # HGETALL 必须是偶数长度数组；宽松接受其他形状会错配字段和值并污染租户状态。
    if type(response) is not list or len(response) % 2:
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    result: dict[str, str] = {}
    for index in range(0, len(response), 2):
        key = response[index]
        value = response[index + 1]
        # 字段和值只接受字符串且字段须唯一命中白名单，防止重复键覆盖安全元数据。
        if (
            type(key) is not str
            or type(value) is not str
            or key in result
            or key not in _HASH_FIELDS
        ):
            # 任一元素违约都会破坏字段配对或白名单边界，不能保留部分解析结果。
            raise SessionRiskStoreUnavailable("session risk store unavailable")
        result[key] = value
    # 非空 Hash 必须包含完整快照，不能用 Python 缺省值掩盖 Redis 部分写入。
    if result and not _SNAPSHOT_REQUIRED_FIELDS.issubset(result):
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    pending_fields = {"pending_revision", "pending_turn_digest", "pending_payload"}
    # pending 三字段必须全有或全无，部分证据无法安全地提交或精确 ACK。
    if result and bool(pending_fields & result.keys()) != pending_fields.issubset(result):
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    return result


def _snapshot_from_hash(values: Mapping[str, str]) -> SessionRiskSnapshot:
    """严格解析 Redis Hash；损坏数字或状态统一折叠为 Store unavailable。"""

    try:
        # schema_version 是 Redis 快照的解释边界；仅“字段存在”不足以证明当前代码理解其语义。
        if values["schema_version"] != "1":
            raise ValueError
        expires_raw = _strict_int(values["expires_at_epoch"])
        # 模型构造器继续校验范围和状态，使 Redis 数据与内存实现共享同一不可变合同。
        return SessionRiskSnapshot(
            score_id=values["score_id"],
            score=_strict_int(values["score"]),
            risky_turn_count=_strict_int(values["risky_turn_count"]),
            jailbreak_count=_strict_int(values["jailbreak_count"]),
            sensitive_count=_strict_int(values["sensitive_count"]),
            tool_inducement_count=_strict_int(values["tool_inducement_count"]),
            state=cast(object, values["state"]),
            revision=_strict_int(values["revision"]),
            window_started_at_epoch=_strict_int(values["window_started_at_epoch"]),
            last_risk_at_epoch=_strict_int(values["last_risk_at_epoch"]),
            expires_at_epoch=None if expires_raw == 0 else expires_raw,
            hmac_key_version=_strict_int(values["hmac_key_version"]),
            threshold_version=values["threshold_version"],
        )
    # 任一字段损坏都折叠为固定异常，既失败关闭也避免在日志中展开 Redis 原始内容。
    except (KeyError, TypeError, ValueError, SessionRiskContractError):
        raise SessionRiskStoreUnavailable("session risk store unavailable") from None


# pending JSON 使用独立 exact-key 白名单，确保 Lua 与 Python 对同一证据只有一种解释。
_PENDING_KEYS = frozenset(
    {
        "schema_version",
        "score_id",
        "score_before",
        "score_after",
        "state_before",
        "state_after",
        "revision",
        "risky_turn_count",
        "jailbreak_count",
        "sensitive_count",
        "tool_inducement_count",
        "window_started_at_epoch",
        "last_risk_at_epoch",
        "expires_at_epoch",
        "hmac_key_version",
        "threshold_version",
        "high_risk_transition",
        "freeze_transition",
        "turn_key_digest",
        "turn_intent_digest",
        "turn_delta",
        "jailbreak_count_delta",
        "sensitive_count_delta",
        "tool_inducement_count_delta",
        "source_request_id",
        "source_trace_id",
        "input_truncated",
        "detector_version",
        "ruleset_version",
        "intent_risk_result_id",
        "intent_risk_type",
        "intent_risk_score",
        "dlp_action",
        "dlp_current_turn_match_count",
        "tool_inducement_detected",
        "occurred_at_epoch",
        "chat_message_id",
    }
)


def _pending_from_json(scope: SessionRiskScope, raw: str) -> SessionRiskPending:
    """用 JSON API 解析 pending exact schema，禁止字符串拆词或宽松字段忽略。"""

    try:
        value = json.loads(raw)
        if (
            type(value) is not dict
            or frozenset(value) != _PENDING_KEYS
            or value["schema_version"] != 1
        ):
            # exact schema 不匹配代表 Lua/Python 版本漂移，不能忽略未知字段继续恢复证据。
            raise ValueError
        snapshot = SessionRiskSnapshot(
            score_id=_json_str(value, "score_id"),
            score=_json_int(value, "score_after"),
            risky_turn_count=_json_int(value, "risky_turn_count"),
            jailbreak_count=_json_int(value, "jailbreak_count"),
            sensitive_count=_json_int(value, "sensitive_count"),
            tool_inducement_count=_json_int(value, "tool_inducement_count"),
            state=cast(object, _json_str(value, "state_after")),
            revision=_json_int(value, "revision"),
            window_started_at_epoch=_json_int(value, "window_started_at_epoch"),
            last_risk_at_epoch=_json_int(value, "last_risk_at_epoch"),
            expires_at_epoch=(
                None
                if _json_int(value, "expires_at_epoch") == 0
                else _json_int(value, "expires_at_epoch")
            ),
            hmac_key_version=_json_int(value, "hmac_key_version"),
            threshold_version=_json_str(value, "threshold_version"),
        )
        assessment = SessionRiskAssessment(
            turn_key_digest=_json_str(value, "turn_key_digest"),
            turn_intent_digest=_json_str(value, "turn_intent_digest"),
            jailbreak_count_delta=_json_int(value, "jailbreak_count_delta"),
            sensitive_count_delta=_json_int(value, "sensitive_count_delta"),
            tool_inducement_count_delta=_json_int(value, "tool_inducement_count_delta"),
            turn_delta=_json_int(value, "turn_delta"),
            input_truncated=_json_bool(value, "input_truncated"),
            detector_version=_json_str(value, "detector_version"),
            ruleset_version=_json_str(value, "ruleset_version"),
            threshold_version=_json_str(value, "threshold_version"),
            source_request_id=_json_str(value, "source_request_id"),
            source_trace_id=_json_str(value, "source_trace_id"),
            intent_risk_result_id=_json_optional_str(value, "intent_risk_result_id"),
            intent_risk_type=_json_str(value, "intent_risk_type"),
            intent_risk_score=_json_int(value, "intent_risk_score"),
            dlp_action=_json_optional_str(value, "dlp_action"),
            dlp_current_turn_match_count=_json_int(value, "dlp_current_turn_match_count"),
            tool_inducement_detected=_json_bool(value, "tool_inducement_detected"),
            chat_message_id=_json_optional_str(value, "chat_message_id"),
        )
        pending = SessionRiskPending(
            scope=scope,
            assessment=assessment,
            snapshot=snapshot,
            score_before=_json_int(value, "score_before"),
            state_before=_json_str(value, "state_before"),
            high_risk_transition=_json_bool(value, "high_risk_transition"),
            freeze_transition=_json_bool(value, "freeze_transition"),
        )
        _ensure_snapshot_scope(scope, snapshot)
        return pending
    # JSON、字段、类型或模型约束任一失败都不回显原始 pending，以免泄露内部摘要和请求标识。
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, SessionRiskContractError):
        raise SessionRiskStoreUnavailable("session risk store unavailable") from None


def _status_response(response: object) -> str:
    """只接受单元素字符串数组，拒绝 Redis 脚本协议漂移。"""

    # EVAL 的状态包必须精确为单字符串数组，避免把错误代理响应误当作业务状态。
    if type(response) is not list or len(response) != 1 or type(response[0]) is not str:
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    return response[0]


def _strict_int(value: str) -> int:
    """解析 canonical 非负十进制，拒绝符号、空白和前导零歧义。"""

    # Redis 数字字段不做隐式 trim 或符号转换，保证 Lua 与 Python 对签名状态解释一致。
    if type(value) is not str or not value.isascii() or not value.isdecimal():
        raise ValueError
    return int(value)


def _json_int(value: Mapping[str, object], key: str) -> int:
    """读取 JSON 真整数，显式拒绝 bool 这一 int 子类。"""

    item = value[key]
    # bool 在 Python 中是 int 子类，但 pending JSON 合同必须区分计数和真假字段。
    if type(item) is not int:
        raise ValueError
    return item


def _json_bool(value: Mapping[str, object], key: str) -> bool:
    """读取严格 JSON 布尔值并拒绝整数零一等宽松替代。

    返回真实布尔类型；类型漂移表示 pending 证据损坏，调用方必须失败关闭而非隐式转换。
    """

    item = value[key]
    # 只接受 JSON 原生布尔，禁止字符串或零一值绕过证据类型约束。
    if type(item) is not bool:
        raise ValueError
    return item


def _json_str(value: Mapping[str, object], key: str) -> str:
    """读取 JSON 字符串，不执行隐式类型转换。"""

    item = value[key]
    # 摘要与版本字段不可由数字等类型自动转写，否则 ACK 比较可能产生歧义。
    if type(item) is not str:
        raise ValueError
    return item


def _json_optional_str(value: Mapping[str, object], key: str) -> str | None:
    """读取可空 JSON 字符串并严格处理 Lua 空值映射。

    返回字符串或 Python 空值；任何其他类型都会拒绝，以免损坏的 pending 字段参与证据恢复。
    """

    item = value[key]
    if item is None:
        return None
    # 可空仅表示允许 null，不代表允许把其他 JSON 类型序列化成字符串。
    if type(item) is not str:
        raise ValueError
    return item


def _parse_redis_url(redis_url: str) -> _RedisConnectionConfig:
    """解析 redis/rediss URL，拒绝 query/fragment、非法端口和数据库编号。"""

    try:
        parsed = urlparse(redis_url)
        port = parsed.port or (6380 if parsed.scheme == "rediss" else 6379)
    # URL 类型或端口语法错误不回显原值，避免凭据通过异常 cause 进入 Gateway 日志。
    except (TypeError, ValueError):
        raise SessionRiskStoreUnavailable("session risk store unavailable") from None
    if (
        parsed.scheme not in {"redis", "rediss"}
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or not 1 <= port <= 65_535
        or (parsed.username is not None and not parsed.password)
    ):
        # ACL 用户名没有非空密码时 AUTH 根本不会执行；启动阶段直接拒绝，不能静默降级为匿名连接。
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    db_text = parsed.path[1:] if parsed.path.startswith("/") else parsed.path
    # 仅允许单段 ASCII 十进制 DB，拒绝路径穿透式多段文本和宽松 Unicode 数字。
    if "/" in db_text or (db_text and (not db_text.isascii() or not db_text.isdecimal())):
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    db = int(db_text or "0")
    # Redis DB 编号使用受控上限，避免错误配置选择非预期逻辑库或触发实现差异。
    if not 0 <= db <= 255:
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    return _RedisConnectionConfig(
        host=parsed.hostname,
        port=port,
        db=db,
        username=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
        use_tls=parsed.scheme == "rediss",
    )


def _bounded_timeout(value: float) -> float:
    """限制 Redis deadline，防止错误配置把 Gateway 热路径永久挂起。"""

    # bool 和过小/过大值都拒绝，确保所有连接与读取阶段存在可预测的失败关闭窗口。
    if type(value) not in {int, float} or not 0.05 <= float(value) <= 10.0:
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    return float(value)


def _encode_command(parts: tuple[str, ...] | list[str]) -> bytes:
    """将命令编码为 RESP2 数组；仅调用栈持有 AUTH 密码字节。"""

    # 空命令或非字符串参数属于内部合同错误，禁止依赖隐式 str 转换把对象 repr 发往 Redis。
    if not parts or any(type(part) is not str for part in parts):
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    encoded = [part.encode("utf-8") for part in parts]
    payload = [f"*{len(encoded)}\r\n".encode("ascii")]
    for part in encoded:
        payload.extend((f"${len(part)}\r\n".encode("ascii"), part, b"\r\n"))
    return b"".join(payload)


async def _read_response(reader: asyncio.StreamReader, *, depth: int = 0) -> object:
    """递归解析本合同需要的 RESP2 类型，并限制数组深度与 Bulk 大小。"""

    # 限制嵌套深度，防止恶意或错误端点通过递归数组耗尽 Gateway 调用栈。
    if depth > 4:
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    prefix = await reader.readexactly(1)
    line = await reader.readline()
    # 每个 RESP 头必须在大小上限内并以 CRLF 终止，半包或超长头均失败关闭。
    if len(line) > 1_048_576 or not line.endswith(b"\r\n"):
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    body = line[:-2]
    if prefix == b"+":
        return body.decode("utf-8")
    if prefix == b":" :
        return int(body.decode("ascii"))
    if prefix == b"$":
        length = int(body.decode("ascii"))
        if length == -1:
            return None
        # Bulk 长度设硬上限且不接受其他负数，避免不受控内存分配和协议歧义。
        if not 0 <= length <= 1_048_576:
            raise SessionRiskStoreUnavailable("session risk store unavailable")
        data = await reader.readexactly(length)
        # Bulk 正文后的 CRLF 是帧边界，缺失时不可继续解析后续租户请求响应。
        if await reader.readexactly(2) != b"\r\n":
            raise SessionRiskStoreUnavailable("session risk store unavailable")
        return data.decode("utf-8")
    if prefix == b"*":
        length = int(body.decode("ascii"))
        if length == -1:
            return None
        # 数组元素数量受限，避免错误服务用巨大数组占用热路径 CPU 和内存。
        if not 0 <= length <= 128:
            raise SessionRiskStoreUnavailable("session risk store unavailable")
        return [await _read_response(reader, depth=depth + 1) for _ in range(length)]
    if prefix == b"-":
        # Redis 错误正文可能含脚本、地址或部署细节，统一丢弃且不设置异常 cause。
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    # 未知 RESP 前缀说明连接端点或协议版本错误，统一失败关闭且不回显原始字节。
    raise SessionRiskStoreUnavailable("session risk store unavailable")


__all__ = [
    "InMemorySessionRiskStore",
    "RedisSessionRiskStore",
    "SESSION_RISK_TTL_SECONDS",
    "SessionRiskStore",
]
