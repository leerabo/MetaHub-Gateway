"""Translate the canonical, already governed Chat stream for employee clients.

This layer owns protocol envelopes only. Authentication, DLP, budgets, request
logs and settlement remain in the canonical iterator and response wrapper.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import anyio
from metahub_shared.output_safety import BoundedSseEventFramer, OutputSafetyError
from starlette.responses import StreamingResponse

MAX_OUTPUT_BYTES = 16 * 1024 * 1024


def adapt_protocol_stream(
    response: StreamingResponse,
    *,
    protocol: str,
    model: object,
) -> StreamingResponse:
    """Preserve the response's disconnect/finalization owner and wrap its source once."""
    source = response.body_iterator

    async def stream() -> AsyncIterator[bytes]:
        state = _StreamState(protocol, model)
        framer = BoundedSseEventFramer()
        done = False
        started = False
        try:
            async for chunk in source:
                # Enter the canonical generator before exposing a new yield boundary,
                # so disconnect during our first event still runs its finally block.
                if not started:
                    started = True
                    yield state.start()
                raw = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
                for frame in framer.feed(raw):
                    if frame.is_done:
                        done = True
                    elif frame.data_text:
                        for event in state.feed(json.loads(frame.data_text)):
                            yield event
                        if state.failed:
                            return
            framer.finish()
            if not started:
                yield state.start()
            if not done and state.finish_reason is None:
                yield state.fail()
                return
            for event in state.finish():
                yield event
        except (OutputSafetyError, ValueError, TypeError, UnicodeError):
            # Never return malformed upstream text or claim a truncated stream succeeded.
            yield state.fail()
        finally:
            close = getattr(source, "aclose", None)
            if close is not None:
                with anyio.CancelScope(shield=True):
                    await close()

    response.body_iterator = stream()
    return response


class _StreamState:
    """Keep bounded output and stable item identities through delta and done events."""

    def __init__(self, protocol: str, model: object) -> None:
        self.anthropic = protocol == "anthropic"
        self.identifier = f"{'msg' if self.anthropic else 'resp'}_{uuid4().hex}"
        self.model = model
        self.created_at = int(time.time())
        self.sequence = 0
        self.items: list[dict[str, Any]] = []
        self.text_index: int | None = None
        self.tools: dict[int, dict[str, Any]] = {}
        self.usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.finish_reason: str | None = None
        self.output_bytes = 0
        self.failed = False

    def event(self, kind: str, **values: Any) -> bytes:
        payload = {"type": kind, **values}
        if not self.anthropic:
            payload["sequence_number"] = self.sequence
            self.sequence += 1
        return (
            f"event: {kind}\ndata: "
            + json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n"
        ).encode("utf-8")

    def snapshot(self, status: str) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": self.items,
            "usage": self.usage,
            "error": None,
            "incomplete_details": None,
        }

    def start(self) -> bytes:
        if self.anthropic:
            return self.event(
                "message_start",
                message={
                    "id": self.identifier,
                    "type": "message",
                    "role": "assistant",
                    "model": self.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            )
        return self.event("response.created", response=self.snapshot("in_progress"))

    def fail(self, error: dict[str, Any] | None = None) -> bytes:
        self.failed = True
        safe = error or {
            "code": "gateway.request_invalid",
            "message": "模型响应未完整完成，请重试。",
        }
        if self.anthropic:
            return self.event("error", error={"type": "api_error", **safe})
        return self.event("error", **{key: value for key, value in safe.items() if key != "type"})

    def feed(self, payload: dict[str, Any]) -> list[bytes]:
        if isinstance(payload.get("error"), dict):
            return [self.fail(payload["error"])]
        if not self.model and isinstance(payload.get("model"), str):
            self.model = payload["model"]
        usage = payload.get("usage")
        if isinstance(usage, dict):
            for source, target in (
                ("prompt_tokens", "input_tokens"),
                ("completion_tokens", "output_tokens"),
                ("total_tokens", "total_tokens"),
            ):
                if type(usage.get(source)) is int and usage[source] >= 0:
                    self.usage[target] = usage[source]
            if "total_tokens" not in usage:
                self.usage["total_tokens"] = (
                    self.usage["input_tokens"] + self.usage["output_tokens"]
                )
        result: list[bytes] = []
        for choice in payload.get("choices", []):
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            text = delta.get("content")
            if isinstance(text, str) and text:
                result.extend(self.text(text))
            for call in delta.get("tool_calls") or []:
                result.extend(self.tool(call))
        return result

    def account(self, value: str) -> None:
        self.output_bytes += len(value.encode("utf-8"))
        if self.output_bytes > MAX_OUTPUT_BYTES:
            raise ValueError("protocol_output_limit")

    def text(self, value: str) -> list[bytes]:
        self.account(value)
        events: list[bytes] = []
        if self.text_index is None:
            self.text_index = len(self.items)
            self.items.append(
                {
                    "id": f"msg_{uuid4().hex}",
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [{"type": "output_text", "text": "", "annotations": []}],
                }
            )
            item = self.items[self.text_index]
            if self.anthropic:
                events.append(
                    self.event(
                        "content_block_start",
                        index=self.text_index,
                        content_block={"type": "text", "text": ""},
                    )
                )
            else:
                events.append(
                    self.event(
                        "response.output_item.added",
                        output_index=self.text_index,
                        item={**item, "content": []},
                    )
                )
                events.append(
                    self.event(
                        "response.content_part.added",
                        item_id=item["id"],
                        output_index=self.text_index,
                        content_index=0,
                        part=item["content"][0],
                    )
                )
        item = self.items[self.text_index]
        item["content"][0]["text"] += value
        if self.anthropic:
            events.append(
                self.event(
                    "content_block_delta",
                    index=self.text_index,
                    delta={"type": "text_delta", "text": value},
                )
            )
        else:
            events.append(
                self.event(
                    "response.output_text.delta",
                    item_id=item["id"],
                    output_index=self.text_index,
                    content_index=0,
                    delta=value,
                )
            )
        return events

    def tool(self, call: dict[str, Any]) -> list[bytes]:
        if not isinstance(call, dict) or type(call.get("index")) is not int or call["index"] < 0:
            raise ValueError("protocol_tool_index_invalid")
        index = call["index"]
        if index not in self.tools:
            if len(self.tools) >= 1024:
                raise ValueError("protocol_tool_count_limit")
            self.account("tool")
            self.tools[index] = {
                "id": f"fc_{uuid4().hex}",
                "call_id": "",
                "name": "",
                "arguments": "",
                "output_index": None,
            }
        tool = self.tools[index]
        function = call.get("function") or {}
        if not isinstance(function, dict):
            raise ValueError("protocol_tool_function_invalid")
        for target, value in (("call_id", call.get("id")), ("name", function.get("name"))):
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("protocol_tool_identity_invalid")
                if tool[target] and tool[target] != value:
                    raise ValueError("protocol_tool_identity_changed")
                if not tool[target]:
                    self.account(value)
                    tool[target] = value
        arguments = function.get("arguments", "")
        if not isinstance(arguments, str):
            raise ValueError("protocol_tool_arguments_invalid")
        self.account(arguments)
        tool["arguments"] += arguments
        if not tool["call_id"] or not tool["name"]:
            return []
        events: list[bytes] = []
        if tool["output_index"] is None:
            tool["output_index"] = len(self.items)
            self.items.append(
                {key: tool[key] for key in ("id", "call_id", "name", "arguments")}
                | {"type": "function_call", "status": "in_progress"}
            )
            if self.anthropic:
                events.append(
                    self.event(
                        "content_block_start",
                        index=tool["output_index"],
                        content_block={
                            "type": "tool_use",
                            "id": tool["call_id"],
                            "name": tool["name"],
                            "input": {},
                        },
                    )
                )
            else:
                events.append(
                    self.event(
                        "response.output_item.added",
                        output_index=tool["output_index"],
                        item={**self.items[tool["output_index"]], "arguments": ""},
                    )
                )
            arguments = tool["arguments"]
        self.items[tool["output_index"]]["arguments"] = tool["arguments"]
        if arguments:
            if self.anthropic:
                events.append(
                    self.event(
                        "content_block_delta",
                        index=tool["output_index"],
                        delta={"type": "input_json_delta", "partial_json": arguments},
                    )
                )
            else:
                events.append(
                    self.event(
                        "response.function_call_arguments.delta",
                        item_id=tool["id"],
                        output_index=tool["output_index"],
                        delta=arguments,
                    )
                )
        return events

    def finish(self) -> list[bytes]:
        if any(tool["output_index"] is None for tool in self.tools.values()):
            return [self.fail()]
        incomplete = self.finish_reason in {"length", "content_filter"}
        # A completed tool call must have valid arguments before a client executes it.
        if not incomplete:
            for tool in self.tools.values():
                if not isinstance(json.loads(tool["arguments"]), dict):
                    return [self.fail()]
        events: list[bytes] = []
        for index, item in enumerate(self.items):
            item["status"] = "incomplete" if incomplete else "completed"
            if self.anthropic:
                events.append(self.event("content_block_stop", index=index))
                continue
            if item["type"] == "message":
                part = item["content"][0]
                events.append(
                    self.event(
                        "response.output_text.done",
                        item_id=item["id"],
                        output_index=index,
                        content_index=0,
                        text=part["text"],
                    )
                )
                events.append(
                    self.event(
                        "response.content_part.done",
                        item_id=item["id"],
                        output_index=index,
                        content_index=0,
                        part=part,
                    )
                )
            else:
                events.append(
                    self.event(
                        "response.function_call_arguments.done",
                        item_id=item["id"],
                        output_index=index,
                        name=item["name"],
                        arguments=item["arguments"],
                    )
                )
            events.append(self.event("response.output_item.done", output_index=index, item=item))
        if self.anthropic:
            reason = "max_tokens" if incomplete else "tool_use" if self.tools else "end_turn"
            events.append(
                self.event(
                    "message_delta",
                    delta={"stop_reason": reason, "stop_sequence": None},
                    usage={key: self.usage[key] for key in ("input_tokens", "output_tokens")},
                )
            )
            events.append(self.event("message_stop"))
        else:
            status = "incomplete" if incomplete else "completed"
            response = self.snapshot(status)
            if incomplete:
                response["incomplete_details"] = {
                    "reason": (
                        "max_output_tokens" if self.finish_reason == "length" else "content_filter"
                    )
                }
            events.append(self.event(f"response.{status}", response=response))
        return events
