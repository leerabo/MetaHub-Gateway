"""严格、有界且保留原始字节的 SSE event framer。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .models import OutputFragment, OutputSafetyError, SseFrame
from .scanner import extract_full_output_fragments

# 单个未完成 SSE event 的原始字节硬上限，防止 Provider 用缺失终止空行制造无界缓冲。
MAX_SSE_EVENT_BYTES = 1_048_576


class BoundedSseEventFramer:
    """把任意网络 chunk 组装成完整 SSE event。

    启用输出规则时，调用方必须只转发本类返回的完整 frame，不能透传内部残缺 buffer。
    零规则模式不使用本类做安全门禁，继续沿用既有宽容 Usage 解析和 raw passthrough。
    """

    def __init__(self) -> None:
        """初始化当前行、当前 event 的有界字节缓冲和协议终止状态。"""

        self._line_buffer = bytearray()
        self._raw_event = bytearray()
        self._data_lines: list[str] = []
        self._event_name: str | None = None
        # `[DONE]` 是不可逆的协议终点；终点后的 Provider 垃圾数据既不能发送，
        # 也不能反向污染已经完成的帧。
        self._terminated = False

    def feed(self, chunk: bytes) -> tuple[SseFrame, ...]:
        """消费一个 raw chunk 并返回其中完成的零到多个 event。

        参数：Provider 返回的原始 bytes。
        返回：严格解析且保留原始 bytes 的完整 frame。
        关键约束：跨 chunk 的半行/半 event 不返回；事件超限、UTF-8 或 JSON 歧义固定失败关闭；
        `[DONE]` 之后的同批及后续数据不再解析。
        """

        # 安全 framer 只接受网络原始 bytes，禁止调用方绕过严格 UTF-8 解码边界。
        if not isinstance(chunk, bytes):
            raise TypeError("output_safety_sse_chunk_invalid")
        if self._terminated:
            return ()
        self._line_buffer.extend(chunk)
        frames: list[SseFrame] = []
        while True:
            newline = self._line_buffer.find(b"\n")
            if newline < 0:
                self._ensure_capacity(len(self._raw_event) + len(self._line_buffer))
                break
            raw_line = bytes(self._line_buffer[: newline + 1])
            del self._line_buffer[: newline + 1]
            self._raw_event.extend(raw_line)
            self._ensure_capacity(len(self._raw_event))
            logical_line = raw_line[:-1]
            if logical_line.endswith(b"\r"):
                logical_line = logical_line[:-1]
            if logical_line == b"":
                frame = self._finish_event()
                frames.append(frame)
                if frame.is_done:
                    # 必须在这里停止顺序解析，否则同一 raw chunk 中终点后的畸形事件
                    # 会让整个已经完成的批次失败。
                    self._terminated = True
                    self._line_buffer.clear()
                    break
                continue
            self._consume_line(logical_line)
        return tuple(frames)

    def finish(self) -> None:
        """在 Provider EOF 时确认不存在残缺 event；尾窗扫描由上层随后执行。"""

        # EOF 仍有任一协议缓冲时正文完整性无法证明，清零引用后固定失败关闭。
        if self._line_buffer or self._raw_event or self._data_lines or self._event_name is not None:
            self._clear()
            raise OutputSafetyError("output_safety.sse_event_incomplete")

    def _consume_line(self, line: bytes) -> None:
        """解析单条 SSE 行，注释和未知字段只影响原样透传，不进入正文投影。"""

        try:
            text = line.decode("utf-8", errors="strict")
        # Provider 非法字节不能用替换字符继续扫描，否则可能改写关键词或 JSON 结构。
        except UnicodeDecodeError:
            self._clear()
            raise OutputSafetyError("output_safety.sse_utf8_invalid") from None
        if text.startswith(":"):
            return
        field, separator, value = text.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data_lines.append(value)
        elif field == "event":
            self._event_name = value or None

    def _finish_event(self) -> SseFrame:
        """完成当前 event，strict-parse data 并立即清空内部正文引用。"""

        raw = bytes(self._raw_event)
        data_text = "\n".join(self._data_lines) if self._data_lines else None
        event_name = self._event_name
        self._clear_event()
        if data_text is None or data_text == "":
            return SseFrame(
                raw_bytes=raw,
                event_name=event_name,
                data_text=data_text,
                is_done=False,
                fragments=(),
            )
        if data_text.strip() == "[DONE]":
            return SseFrame(
                raw_bytes=raw,
                event_name=event_name,
                data_text=data_text,
                is_done=True,
                fragments=(),
            )
        payload = _strict_json_object(data_text)
        fragments = _stream_fragments(payload)
        return SseFrame(
            raw_bytes=raw,
            event_name=event_name,
            data_text=data_text,
            is_done=False,
            fragments=fragments,
        )

    def _ensure_capacity(self, size: int) -> None:
        """限制最多一个未完成 event 的内存占用。"""

        # 超限时必须同时清除正文引用并固定报错，不能把截断 event 继续交给下游。
        if size > MAX_SSE_EVENT_BYTES:
            self._clear()
            raise OutputSafetyError("output_safety.sse_event_too_large")

    def _clear_event(self) -> None:
        """清除已完成 event 的所有可能正文引用。"""

        self._raw_event.clear()
        self._data_lines.clear()
        self._event_name = None

    def _clear(self) -> None:
        """故障时清除行与 event 缓冲，避免正文延长驻留时间。"""

        self._line_buffer.clear()
        self._clear_event()


def _strict_json_object(data_text: str) -> Mapping[str, Any]:
    """解析 SSE JSON object，并拒绝重复键和非有限数字。"""

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """把 JSON object pairs 转成字典，重复 key 固定失败关闭。"""

        result: dict[str, Any] = {}
        for key, value in pairs:
            # 重复键在不同 JSON 实现中可能选择不同值，安全投影必须拒绝这种歧义。
            if key in result:
                raise OutputSafetyError("output_safety.sse_json_duplicate_key")
            result[key] = value
        return result

    def reject_non_finite_constant(_: str) -> object:
        """拒绝 JSON 标准之外的 NaN 和正负 Infinity。

        Python 默认解码器会接受这些跨语言语义不一致的常量；输出安全门禁必须在正文投影前失败关闭，
        并且只抛出固定原因码，避免把 Provider 原始 token 或解析位置带入异常日志。
        """

        # 回调参数本身就是不可信 Provider token；固定折叠可避免异常链或日志意外回显原始值。
        raise OutputSafetyError("output_safety.sse_json_invalid")

    try:
        payload = json.loads(
            data_text,
            object_pairs_hook=unique_object,
            parse_constant=reject_non_finite_constant,
        )
    # 已归一的安全错误直接传播固定原因码，禁止被二次包装并附带 Provider 正文。
    except OutputSafetyError:
        raise
    # 其他 JSON/Unicode 解析失败统一折叠，不把原始 token、位置或底层异常链返回。
    except (json.JSONDecodeError, UnicodeError, ValueError):
        raise OutputSafetyError("output_safety.sse_json_invalid") from None
    # SSE data 必须是 JSON object；数组和标量没有可验证的 Chat 正文字段合同。
    if not isinstance(payload, Mapping):
        raise OutputSafetyError("output_safety.sse_json_invalid")
    return payload


def _stream_fragments(payload: Mapping[str, Any]) -> tuple[OutputFragment, ...]:
    """复用全文投影并把流式 delta/choice 字段保持为独立 lane。"""

    # 全文投影本身已同时识别 message、delta、choice.text 和 tool arguments；SSE 通常只携带 delta。
    return extract_full_output_fragments(payload)
