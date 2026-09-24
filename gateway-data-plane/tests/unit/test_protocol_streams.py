"""Consume actual compatibility streams, including client tool-call round trips."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from metahub_gateway.api import openai_compatible as api
from metahub_gateway.api.openai_compatible import (
    _adapt_protocol_response,
    _anthropic_to_chat_payload,
    _responses_to_chat_payload,
)
from openai import OpenAI
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse


def wire(payload):
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"


def chat(delta, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


async def consume(protocol, chunks, closed=None):
    async def source():
        try:
            for chunk in chunks:
                yield chunk
        finally:
            if closed is not None:
                closed.append(True)

    original = StreamingResponse(source(), headers={"x-request-id": "req_synthetic"})
    adapted = _adapt_protocol_response(
        original,
        protocol=protocol,
        request_payload={"model": "company-default"},
    )
    assert adapted is original
    assert adapted.headers["x-request-id"] == "req_synthetic"
    return [chunk async for chunk in adapted.body_iterator]


def events(chunks):
    return [
        json.loads(line[6:])
        for line in b"".join(chunks).decode().splitlines()
        if line.startswith("data: ")
    ]


def tool_chunks():
    return [
        wire(
            chat(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_original",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": ""},
                        }
                    ]
                }
            )
        ),
        wire(chat({"tool_calls": [{"index": 0, "function": {"arguments": '{"q":'}}]})),
        wire(
            chat({"tool_calls": [{"index": 0, "function": {"arguments": '"上海"}'}}]}, "tool_calls")
        ),
        wire(
            {
                "choices": [],
                "usage": {"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26},
            }
        ),
        b"data: [DONE]\n\n",
    ]


@pytest.mark.parametrize("protocol", ["responses", "anthropic"])
def test_text_stream_survives_utf8_byte_splits_and_preserves_usage(protocol):
    raw = (
        wire(chat({"content": "你好世界"}, "stop"))
        + wire(
            {
                "choices": [],
                "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
            }
        )
        + b"data: [DONE]\n\n"
    )
    closed = []
    output = events(
        asyncio.run(consume(protocol, [raw[i : i + 1] for i in range(len(raw))], closed))
    )
    assert closed == [True]
    if protocol == "responses":
        final = output[-1]["response"]
        assert final["output"][0]["content"][0]["text"] == "你好世界"
        assert final["usage"] == {"input_tokens": 7, "output_tokens": 4, "total_tokens": 11}
        assert [item["sequence_number"] for item in output] == list(range(len(output)))
    else:
        assert output[0]["message"]["id"].startswith("msg_")
        assert (
            "".join(
                item["delta"]["text"] for item in output if item["type"] == "content_block_delta"
            )
            == "你好世界"
        )
        assert output[-2]["delta"]["stop_reason"] == "end_turn"
        assert output[-2]["usage"]["output_tokens"] == 4


def test_openai_sdk_consumes_responses_tool_stream_and_replays_tool_result():
    encoded = b"".join(asyncio.run(consume("responses", tool_chunks())))
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=encoded,
            headers={"Content-Type": "text/event-stream"},
        )
    )
    with OpenAI(api_key="synthetic-test", http_client=httpx.Client(transport=transport)) as client:
        with client.responses.stream(model="company-default", input="lookup") as stream:
            list(stream)
            final = stream.get_final_response()
    call = final.output[0].model_dump()
    assert call["call_id"] == "call_original"
    assert call["name"] == "lookup"
    assert json.loads(call["arguments"]) == {"q": "上海"}
    assert final.usage.total_tokens == 26
    replay = _responses_to_chat_payload(
        {
            "model": "company-default",
            "input": [
                call,
                {"type": "function_call_output", "call_id": call["call_id"], "output": "结果"},
            ],
        }
    )
    assert replay["messages"][0]["tool_calls"][0]["id"] == "call_original"
    assert replay["messages"][1]["tool_call_id"] == "call_original"


def test_anthropic_stream_preserves_tool_identity_and_requests_tool_execution():
    output = events(asyncio.run(consume("anthropic", tool_chunks())))
    block = next(item["content_block"] for item in output if item["type"] == "content_block_start")
    arguments = "".join(
        item["delta"]["partial_json"] for item in output if item["type"] == "content_block_delta"
    )
    assert block["id"] == "call_original"
    assert block["name"] == "lookup"
    block["input"] = json.loads(arguments)
    assert output[-2]["delta"]["stop_reason"] == "tool_use"
    assert output[-2]["usage"]["output_tokens"] == 9
    replay = _anthropic_to_chat_payload(
        {
            "model": "company-default",
            "messages": [
                {"role": "assistant", "content": [block]},
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": block["id"], "content": "结果"}
                    ],
                },
            ],
        }
    )
    assert replay["messages"][1]["tool_call_id"] == "call_original"


@pytest.mark.parametrize("protocol", ["responses", "anthropic"])
@pytest.mark.parametrize(
    "tail",
    [
        wire({"error": {"code": "gateway.upstream_timeout", "message": "Request failed"}}),
        b'data: {"choices":',
        b"",
    ],
)
def test_errors_and_truncated_streams_do_not_report_success(protocol, tail):
    output = events(asyncio.run(consume(protocol, [wire(chat({"content": "partial"})), tail])))
    assert any(item["type"] in {"error", "response.failed"} for item in output)
    assert not any(item["type"] in {"response.completed", "message_stop"} for item in output)


@pytest.mark.parametrize("protocol", ["responses", "anthropic"])
def test_output_limit_is_not_reported_as_completed_work(protocol):
    output = events(
        asyncio.run(
            consume(
                protocol,
                [
                    wire(chat({"content": "partial"}, "length")),
                    b"data: [DONE]\n\n",
                ],
            )
        )
    )
    if protocol == "responses":
        assert output[-1]["type"] == "response.incomplete"
        assert output[-1]["response"]["incomplete_details"]["reason"] == "max_output_tokens"
    else:
        assert output[-2]["delta"]["stop_reason"] == "max_tokens"


@pytest.mark.parametrize("stop_event", [b"response.created", b"response.output_text.delta"])
def test_closing_adapted_stream_closes_canonical_iterator_immediately(stop_event):
    closed = []

    async def run():
        async def source():
            try:
                yield wire(chat({"content": "hello"}))
                await asyncio.Event().wait()
            finally:
                closed.append(True)

        response = _adapt_protocol_response(
            StreamingResponse(source()), protocol="responses", request_payload={"model": "default"}
        )
        async for chunk in response.body_iterator:
            if stop_event in chunk:
                break
        await response.body_iterator.aclose()
        assert closed == [True]

    asyncio.run(run())


@pytest.mark.parametrize(
    "endpoint,header",
    [
        ("responses", "authorization"),
        ("messages", "authorization"),
        ("messages", "x-api-key"),
    ],
)
def test_facades_share_canonical_identity_and_forward_disconnect(monkeypatch, endpoint, header):
    async def run():
        payload = (
            {"model": "company-default", "input": "hello"}
            if endpoint == "responses"
            else {
                "model": "company-default",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
        messages = [
            {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False},
            {"type": "http.disconnect"},
        ]

        async def receive():
            return messages.pop(0)

        credential = (
            "Bearer synthetic-employee" if header == "authorization" else "synthetic-employee"
        )
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": f"/v1/{endpoint}",
                "headers": [
                    (header.encode(), credential.encode()),
                    (b"x-request-id", b"req_protocol"),
                ],
            },
            receive,
        )

        async def canonical(translated, **kwargs):
            assert kwargs == {
                "endpoint": "chat/completions",
                "request_kind": "chat",
                "audit_endpoint": endpoint,
            }
            assert translated.headers["authorization"] == "Bearer synthetic-employee"
            assert translated.headers["x-request-id"] == "req_protocol"
            assert (await translated.json())["messages"] == [{"role": "user", "content": "hello"}]
            assert await translated.receive() == {"type": "http.disconnect"}
            return JSONResponse(
                {
                    "id": "synthetic",
                    "model": "company-default",
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "hello"},
                            "finish_reason": "stop",
                        },
                    ],
                }
            )

        monkeypatch.setattr(api, "_handle_gateway_request", canonical)
        response = await getattr(api, endpoint)(request)
        assert response.status_code == 200
        assert messages == []

    asyncio.run(run())
