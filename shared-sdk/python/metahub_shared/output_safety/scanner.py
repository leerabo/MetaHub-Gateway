"""输出侧全文与流式滑窗规则扫描。

扫描器复用 DLP 的“任一 block 优先，否则按冻结顺序取首条命中”语义。正文只存在于有界内存，决策对象
仅包含稳定 ID、枚举和计数。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import (
    OutputFragment,
    OutputRule,
    OutputRuleSnapshot,
    OutputSafetyError,
    OutputScanDecision,
)

# 以下边界共同限制单响应、单 lane 与多 lane 的正文驻留量，并为最长关键词保留跨窗口重叠。
MAX_FULL_OUTPUT_CODEPOINTS = 262_144
MAX_OUTPUT_FRAGMENTS = 1_024
MAX_LANE_CODEPOINTS = 8_192
MAX_TOTAL_LANE_CODEPOINTS = 65_536
MIN_LANE_OVERLAP_CODEPOINTS = 512


def extract_full_output_fragments(
    payload: Mapping[str, Any],
    *,
    max_codepoints: int = MAX_FULL_OUTPUT_CODEPOINTS,
) -> tuple[OutputFragment, ...]:
    """从非流式 OpenAI-compatible Chat 响应提取 exact 文本投影。

    参数：Provider Adapter 已确认成功的响应 Mapping。
    返回：按 choice/字段/tool call 隔离的短期片段。
    关键约束：未知扩展字段被忽略，已知字段结构损坏则失败关闭，避免把未扫描正文误判为 clean。
    """

    if (
        type(max_codepoints) is not int
        or not 1 <= max_codepoints <= MAX_FULL_OUTPUT_CODEPOINTS
    ):
        # 调用方只能收紧冻结硬上限，不能通过部署配置扩展为无界全文扫描。
        raise ValueError("output_safety_full_text_limit_invalid")
    choices = payload.get("choices")
    if choices is None:
        return ()
    # choices 是 Provider 正文容器；类型不确定时不能按空输出降级为 clean。
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)):
        raise OutputSafetyError("output_safety.response_shape_invalid")
    fragments: list[OutputFragment] = []
    total_codepoints = 0
    for position, choice in enumerate(choices):
        # 单个 choice 结构损坏会让正文投影不完整，必须统一失败关闭。
        if not isinstance(choice, Mapping):
            raise OutputSafetyError("output_safety.response_shape_invalid")
        choice_key = _choice_key(choice.get("index"), position)
        message = choice.get("message")
        if message is not None:
            # 已声明 message 却不是对象时无法可靠定位 content/refusal/tool 字段。
            if not isinstance(message, Mapping):
                raise OutputSafetyError("output_safety.response_shape_invalid")
            total_codepoints = _append_content_fragments(
                fragments,
                (choice_key, "message.content"),
                message.get("content"),
                total_codepoints,
                max_codepoints,
            )
            total_codepoints = _append_optional_text(
                fragments,
                (choice_key, "message.refusal"),
                message.get("refusal"),
                total_codepoints,
                max_codepoints,
            )
            total_codepoints = _append_tool_call_fragments(
                fragments,
                choice_key,
                "message.tool_calls",
                message.get("tool_calls"),
                total_codepoints,
                max_codepoints,
            )
        delta = choice.get("delta")
        if delta is not None:
            # 流式 delta 与非流式 message 采用相同失败关闭原则，禁止跳过损坏正文。
            if not isinstance(delta, Mapping):
                raise OutputSafetyError("output_safety.response_shape_invalid")
            total_codepoints = _append_content_fragments(
                fragments,
                (choice_key, "delta.content"),
                delta.get("content"),
                total_codepoints,
                max_codepoints,
            )
            total_codepoints = _append_optional_text(
                fragments,
                (choice_key, "delta.refusal"),
                delta.get("refusal"),
                total_codepoints,
                max_codepoints,
            )
            total_codepoints = _append_tool_call_fragments(
                fragments,
                choice_key,
                "delta.tool_calls",
                delta.get("tool_calls"),
                total_codepoints,
                max_codepoints,
            )
        total_codepoints = _append_optional_text(
            fragments,
            (choice_key, "choice.text"),
            choice.get("text"),
            total_codepoints,
            max_codepoints,
        )
    return tuple(fragments)


def scan_full_output(
    snapshot: OutputRuleSnapshot,
    payload: Mapping[str, Any],
    *,
    max_codepoints: int = MAX_FULL_OUTPUT_CODEPOINTS,
) -> OutputScanDecision:
    """对完整非流式响应执行全文检测并返回无正文判定。

    参数：已按 tenant 冻结的规则快照和成功响应。
    返回：clean、matched 或 blocked 判定。
    关键约束：所有已知文本字段完整扫描，超过 262,144 code points 时失败关闭而不抽样。
    """

    fragments = extract_full_output_fragments(payload, max_codepoints=max_codepoints)
    lane_texts: OrderedDict[tuple[str, ...], str] = OrderedDict()
    for fragment in fragments:
        lane_texts[fragment.lane] = lane_texts.get(fragment.lane, "") + fragment.text
    matched = _matched_rules(snapshot.rules, tuple(lane_texts.values()))
    return _decision(
        snapshot.rules,
        matched,
        checkpoint_count=1,
        scanned_token_count=sum(_estimate_tokens(text) for text in lane_texts.values()),
    )


@dataclass
class _LaneState:
    """单个逻辑输出 lane 的有界内存状态。"""

    text: str
    touched_sequence: int


class SlidingOutputScanner:
    """按估算 Token 检查点扫描流式输出，并保留有限 lane 窗口。"""

    def __init__(
        self,
        snapshot: OutputRuleSnapshot,
        *,
        interval_tokens: int,
        window_tokens: int,
    ) -> None:
        """初始化滑窗扫描器并校验固定范围。

        参数：冻结快照、检查间隔和窗口 Token 数。
        返回值：无。
        关键约束：正文属性均为私有且 repr 不可达，窗口同时受 Token 与 code-point 硬边界控制。
        """

        # 部署值超出冻结范围会破坏扫描频率或制造 CPU 放大，构造阶段直接拒绝。
        if not 8 <= interval_tokens <= 256:
            raise ValueError("output_safety_interval_invalid")
        # 窗口必须覆盖至少一个完整检查间隔，否则相邻检查点之间会留下不可复核缺口。
        if not 32 <= window_tokens <= 1_024 or window_tokens < interval_tokens:
            raise ValueError("output_safety_window_invalid")
        self._snapshot = snapshot
        self._interval_tokens = interval_tokens
        self._window_tokens = window_tokens
        self._lanes: OrderedDict[tuple[str, ...], _LaneState] = OrderedDict()
        self._matched_rule_ids: set[str] = set()
        self._checkpoint_count = 0
        self._cjk_count = 0
        self._other_nonspace_count = 0
        self._last_checkpoint_tokens = 0
        self._sequence = 0
        self._dirty_since_checkpoint = False

    def feed_sse_frame(
        self,
        fragments: Sequence[OutputFragment],
    ) -> OutputScanDecision | None:
        """消费一个完整 SSE frame 的文本片段，并在跨检查点时先扫描再允许调用方发送。

        参数：同一 frame 的 exact fragments。
        返回：命中 block 时立即返回 blocked；否则返回 None 并由 finish 给出最终 clean/matched。
        关键约束：当前 frame 必须在本函数返回非 blocked 后才可 note_chunk/yield。
        """

        for fragment in fragments:
            pending_chars: list[str] = []
            for char in fragment.text:
                pending_chars.append(char)
                self._observe_char(char)
                # 未达到冻结 Token 间隔时继续收集；code-point 淘汰仍由追加门禁独立保护。
                if self.scanned_token_count < self._last_checkpoint_tokens + self._interval_tokens:
                    continue
                decision = self._append_lane_text(fragment.lane, "".join(pending_chars))
                pending_chars.clear()
                if decision is not None:
                    return decision
                self._last_checkpoint_tokens += self._interval_tokens
                # code-point 淘汰门禁可能已扫描刚追加的文本；仅在仍有脏数据时补 Token
                # 检查点，避免重复匹配，同时保持 last_checkpoint 的节拍单调推进。
                decision = (
                    self._scan_checkpoint()
                    if self._dirty_since_checkpoint
                    else self.current_decision()
                )
                if decision.decision == "blocked":
                    return decision
            if pending_chars:
                decision = self._append_lane_text(fragment.lane, "".join(pending_chars))
                if decision is not None:
                    return decision
        return None

    def finish(self) -> OutputScanDecision:
        """在 `[DONE]` 或 EOF 前强制扫描尾窗并返回最终判定。"""

        if self._dirty_since_checkpoint or self._checkpoint_count == 0:
            decision = self._scan_checkpoint()
            if decision.decision == "blocked":
                return decision
        return _decision(
            self._snapshot.rules,
            _rules_from_ids(self._snapshot.rules, self._matched_rule_ids),
            checkpoint_count=self._checkpoint_count,
            scanned_token_count=self.scanned_token_count,
        )

    @property
    def scanned_token_count(self) -> int:
        """返回已观察模型文本的确定性估算 Token 数。"""

        return self._cjk_count + (self._other_nonspace_count + 3) // 4

    @property
    def checkpoint_count(self) -> int:
        """返回已执行的实际规则检查次数。"""

        return self._checkpoint_count

    def current_decision(self) -> OutputScanDecision:
        """返回截至最近完整 event 已确认的安全判定，不执行尾窗检查。

        参数：无。返回：clean 或已累计的 matched 判定及当前安全计数。
        关键约束：调用方只在流不完整/解析失败时把 matched 证据带入 unavailable；本方法不能
        被当作完整流 clean 证明，也不能替代 ``finish()`` 的强制尾窗扫描。
        """

        return _decision(
            self._snapshot.rules,
            _rules_from_ids(self._snapshot.rules, self._matched_rule_ids),
            checkpoint_count=self._checkpoint_count,
            scanned_token_count=self.scanned_token_count,
        )

    def _append_lane_text(
        self,
        lane: tuple[str, ...],
        fragment_text: str,
    ) -> OutputScanDecision | None:
        """分段追加已计量正文，并保证任何 code-point 淘汰都发生在扫描之后。

        参数：逻辑 lane 与当前完整 SSE event 的文本片段。
        返回：淘汰前命中 block 时返回判定，否则返回 None。
        关键约束：空白不增加 Token，不能依赖 Token 检查点保护它前面的正文；单次追加必须分段，
        避免 Provider 用超长低 Token event 制造无界临时候选字符串。
        """

        if not fragment_text:
            return None
        for start in range(0, len(fragment_text), MAX_LANE_CODEPOINTS):
            decision = self._append_lane_piece(
                lane,
                fragment_text[start : start + MAX_LANE_CODEPOINTS],
            )
            if decision is not None:
                return decision
        return None

    def _append_lane_piece(
        self,
        lane: tuple[str, ...],
        piece: str,
    ) -> OutputScanDecision | None:
        """追加一个有界片段，在每 lane 或总量回收前完成最后一次规则扫描。"""

        self._sequence += 1
        state = self._lanes.get(lane)
        text = (state.text if state is not None else "") + piece
        # 每个 lane 只保留配置的最近 Token 窗口，并额外保留固定 code-point 边界，确保最长
        # 512 字符 keyword 跨检查点仍可命中；部署 window 由此真正控制扫描范围和内存占用。
        window_start = _token_window_start(text, self._window_tokens)
        if window_start > 0:
            text = text[max(0, window_start - MIN_LANE_OVERLAP_CODEPOINTS) :]
        self._lanes[lane] = _LaneState(text=text, touched_sequence=self._sequence)
        self._lanes.move_to_end(lane)
        self._dirty_since_checkpoint = True

        scanned_before_eviction = False
        if len(text) > MAX_LANE_CODEPOINTS:
            # 低 Token 正文后可跟随任意空白；必须先扫描完整候选，再丢弃超出硬上限的旧前缀。
            decision = self._scan_checkpoint()
            if decision.decision == "blocked":
                return decision
            scanned_before_eviction = True
            self._lanes[lane] = _LaneState(
                text=text[-MAX_LANE_CODEPOINTS:],
                touched_sequence=self._sequence,
            )

        total = sum(len(current.text) for current in self._lanes.values())
        if total > MAX_TOTAL_LANE_CODEPOINTS:
            if not scanned_before_eviction:
                # 总量回收可能淘汰另一个尚未到 Token 检查点的 lane，因此必须扫描全部当前候选。
                decision = self._scan_checkpoint()
                if decision.decision == "blocked":
                    return decision
            self._enforce_total_lane_bound()
        return None

    def _observe_char(self, char: str) -> None:
        """更新全流 Token 近似计数；空白仍由独立 code-point 边界约束。"""

        if char.isspace():
            return
        if "\u4e00" <= char <= "\u9fff":
            self._cjk_count += 1
        else:
            self._other_nonspace_count += 1

    def _enforce_total_lane_bound(self) -> None:
        """在调用方已完成淘汰前扫描后，按最久未触达 lane 回收旧前缀。"""

        total = sum(len(state.text) for state in self._lanes.values())
        if total <= MAX_TOTAL_LANE_CODEPOINTS:
            return
        for lane, state in list(self._lanes.items()):
            if total <= MAX_TOTAL_LANE_CODEPOINTS:
                break
            removable = max(0, len(state.text) - MIN_LANE_OVERLAP_CODEPOINTS)
            if removable == 0:
                continue
            remove = min(removable, total - MAX_TOTAL_LANE_CODEPOINTS)
            self._lanes[lane] = _LaneState(
                text=state.text[remove:],
                touched_sequence=state.touched_sequence,
            )
            total -= remove
        if total > MAX_TOTAL_LANE_CODEPOINTS:
            # 所有可移除前缀已回收后仍超限，说明 lane 数量本身失控，只能失败关闭。
            raise OutputSafetyError("output_safety.window_capacity_exceeded")

    def _scan_checkpoint(self) -> OutputScanDecision:
        """扫描当前所有有限 lane，并累积本请求命中过的不同规则。"""

        self._checkpoint_count += 1
        self._dirty_since_checkpoint = False
        matched = _matched_rules(
            self._snapshot.rules,
            tuple(state.text for state in self._lanes.values()),
        )
        self._matched_rule_ids.update(rule.dlp_rule_id for rule in matched)
        return _decision(
            self._snapshot.rules,
            _rules_from_ids(self._snapshot.rules, self._matched_rule_ids),
            checkpoint_count=self._checkpoint_count,
            scanned_token_count=self.scanned_token_count,
        )


def _choice_key(value: object, position: int) -> str:
    """生成不含正文的稳定 choice lane 键，非法索引回退到响应内固定位置。"""

    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return f"choice:{value}"
    return f"choice:{position}"


def _append_optional_text(
    fragments: list[OutputFragment],
    lane: tuple[str, ...],
    value: object,
    total: int,
    maximum: int,
) -> int:
    """追加可空字符串字段；出现其他结构时失败关闭。"""

    if value is None:
        return total
    # 可选字段一旦存在就必须是文本，其他结构不能被静默忽略为无输出。
    if not isinstance(value, str):
        raise OutputSafetyError("output_safety.response_shape_invalid")
    return _append_fragment(fragments, lane, value, total, maximum)


def _append_content_fragments(
    fragments: list[OutputFragment],
    lane_prefix: tuple[str, ...],
    value: object,
    total: int,
    maximum: int,
) -> int:
    """投影字符串或 text content parts，忽略明确的非文本 part。"""

    if value is None:
        return total
    if isinstance(value, str):
        return _append_fragment(fragments, lane_prefix, value, total, maximum)
    # content parts 只接受序列；bytes 会绕过文本编码边界，必须固定拒绝。
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise OutputSafetyError("output_safety.response_shape_invalid")
    for position, part in enumerate(value):
        if isinstance(part, str):
            total = _append_fragment(
                fragments,
                lane_prefix + (f"part:{position}",),
                part,
                total,
                maximum,
            )
            continue
        # 每个结构化 part 必须是对象，未知标量不能假装为无正文扩展。
        if not isinstance(part, Mapping):
            raise OutputSafetyError("output_safety.response_shape_invalid")
        text = part.get("text")
        if text is None:
            continue
        # 已出现 text 键却不是字符串意味着 Provider 正文协议损坏，统一失败关闭。
        if not isinstance(text, str):
            raise OutputSafetyError("output_safety.response_shape_invalid")
        part_index = part.get("index")
        key = part_index if isinstance(part_index, int) and part_index >= 0 else position
        total = _append_fragment(
            fragments,
            lane_prefix + (f"part:{key}",),
            text,
            total,
            maximum,
        )
    return total


def _append_tool_call_fragments(
    fragments: list[OutputFragment],
    choice_key: str,
    field_name: str,
    value: object,
    total: int,
    maximum: int,
) -> int:
    """投影工具调用参数正文，并按 choice、字段与工具索引隔离独立扫描 lane。"""

    if value is None:
        return total
    # tool_calls 只能是对象序列，字符串或二进制不能按可迭代对象逐项解析。
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise OutputSafetyError("output_safety.response_shape_invalid")
    for position, call in enumerate(value):
        # 每个工具调用必须提供对象边界，否则 arguments 的所属 lane 无法证明。
        if not isinstance(call, Mapping):
            raise OutputSafetyError("output_safety.response_shape_invalid")
        function = call.get("function")
        if function is None:
            continue
        # function 出现但结构损坏时不能跳过可能存在的参数正文。
        if not isinstance(function, Mapping):
            raise OutputSafetyError("output_safety.response_shape_invalid")
        arguments = function.get("arguments")
        if arguments is None:
            continue
        # 工具参数只扫描字符串投影，未知对象序列化会改变 Provider 原始安全语义。
        if not isinstance(arguments, str):
            raise OutputSafetyError("output_safety.response_shape_invalid")
        call_index = call.get("index")
        key = call_index if isinstance(call_index, int) and call_index >= 0 else position
        total = _append_fragment(
            fragments,
            (choice_key, field_name, f"tool:{key}"),
            arguments,
            total,
            maximum,
        )
    return total


def _append_fragment(
    fragments: list[OutputFragment],
    lane: tuple[str, ...],
    text: str,
    total: int,
    maximum: int,
) -> int:
    """追加一个有界片段，空字符串不创建无意义 lane。"""

    if not text:
        return total
    updated = total + len(text)
    # 全文超过部署硬上限时禁止抽样或截断，否则被丢弃部分可能包含阻断内容。
    if updated > maximum:
        raise OutputSafetyError("output_safety.response_too_large")
    # 片段数量也必须有界，防止大量空字段制造对象和 lane 资源放大。
    if len(fragments) >= MAX_OUTPUT_FRAGMENTS:
        raise OutputSafetyError("output_safety.fragment_limit_exceeded")
    fragments.append(OutputFragment(lane=lane, text=text))
    return updated


def _matched_rules(
    rules: Sequence[OutputRule],
    lane_texts: Sequence[str],
) -> tuple[OutputRule, ...]:
    """返回至少命中一个 lane 的不同规则，绝不返回命中文本。"""

    matched: list[OutputRule] = []
    for rule in rules:
        if any(_rule_matches(rule, text) for text in lane_texts):
            matched.append(rule)
    return tuple(matched)


def _rule_matches(rule: OutputRule, text: str) -> bool:
    """执行单规则匹配；regex 必须在冻结快照阶段使用安全编译器生成。"""

    if rule.rule_type == "keyword":
        return rule.pattern in text
    if rule.rule_type == "regex" and rule.compiled_regex is not None:
        return rule.compiled_regex.search(text) is not None
    # 冻结快照中出现未知类型或未编译 regex 表示安全证据损坏，不能按未命中处理。
    raise OutputSafetyError("output_safety.rule_invalid")


def _rules_from_ids(
    rules: Sequence[OutputRule],
    rule_ids: set[str],
) -> tuple[OutputRule, ...]:
    """按冻结顺序恢复已命中规则，保持 block 与 tie-break 语义稳定。"""

    return tuple(rule for rule in rules if rule.dlp_rule_id in rule_ids)


def _decision(
    rules: Sequence[OutputRule],
    matched: Sequence[OutputRule],
    *,
    checkpoint_count: int,
    scanned_token_count: int,
) -> OutputScanDecision:
    """把规则命中集合转换为不含正文的稳定判定。"""

    del rules  # 决定顺序已由 matched 的冻结顺序携带，避免后续误用未匹配规则。
    if not matched:
        return OutputScanDecision(
            decision="clean",
            action=None,
            dlp_rule_id=None,
            rule_type=None,
            match_count=0,
            checkpoint_count=checkpoint_count,
            scanned_token_count=scanned_token_count,
        )
    blocking = next((rule for rule in matched if rule.action == "block"), None)
    effective = blocking or matched[0]
    return OutputScanDecision(
        decision="blocked" if blocking is not None else "matched",
        action=effective.action,
        dlp_rule_id=effective.dlp_rule_id,
        rule_type=effective.rule_type,
        match_count=len(matched),
        checkpoint_count=checkpoint_count,
        scanned_token_count=scanned_token_count,
    )


def _estimate_tokens(text: str) -> int:
    """使用与 Gateway Usage 相同的 CJK/非空字符确定性近似。"""

    cjk = 0
    other = 0
    for char in text:
        if char.isspace():
            continue
        if "\u4e00" <= char <= "\u9fff":
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


def _token_window_start(text: str, maximum_tokens: int) -> int:
    """返回最近 ``maximum_tokens`` 估算 Token 的最早 code-point 下标。

    参数：单 lane 当前有界文本和冻结窗口 Token 数。返回：不含额外规则重叠的 suffix 起点。
    关键约束：倒序累计与全局估算口径一致；连续空白由独立 code-point 上限负责，不能制造负索引。
    """

    cjk = 0
    other = 0
    start = len(text)
    for index in range(len(text) - 1, -1, -1):
        char = text[index]
        if not char.isspace():
            if "\u4e00" <= char <= "\u9fff":
                cjk += 1
            else:
                other += 1
        # 超过窗口的首个字符确定 suffix 边界；空白本身不增加 Token 但仍保留在相邻文本中。
        if cjk + (other + 3) // 4 > maximum_tokens:
            return index + 1
        start = index
    return start
