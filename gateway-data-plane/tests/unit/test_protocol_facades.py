"""协议门面回归：同一员工 KEY 可复用到 Responses 与 Anthropic Messages。"""

from metahub_gateway.api.openai_compatible import (
    _anthropic_to_chat_payload,
    _responses_to_chat_payload,
    _to_anthropic_payload,
    _to_responses_payload,
    unversioned_router,
)


def test_unversioned_gateway_aliases_keep_one_employee_service_address() -> None:
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in unversioned_router.routes}

    assert ("/chat/completions", ("POST",)) in paths
    assert ("/responses", ("POST",)) in paths
    assert ("/messages", ("POST",)) in paths
    assert ("/models", ("GET",)) in paths
    assert ("/embeddings", ("POST",)) in paths


def test_responses_function_call_round_trip_shape() -> None:
    translated = _responses_to_chat_payload(
        {
            "model": "company-default",
            "instructions": "Be concise",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "查一下"}],
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "结果"},
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "查找",
                    "parameters": {"type": "object"},
                }
            ],
        }
    )
    assert translated["messages"][0] == {"role": "system", "content": "Be concise"}
    assert translated["messages"][1]["content"][0]["text"] == "查一下"
    assert translated["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "结果",
    }
    assert translated["tools"][0]["function"]["name"] == "lookup"

    response = _to_responses_payload(
        {
            "id": "chatcmpl_1",
            "model": "company-default",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
        {"model": "company-default"},
    )
    assert response["output"][0]["type"] == "function_call"
    assert response["output"][0]["call_id"] == "call_1"


def test_responses_without_model_preserves_default_route_resolution() -> None:
    translated = _responses_to_chat_payload({"input": "用默认模型回答"})

    assert "model" not in translated
    assert translated["messages"] == [{"role": "user", "content": "用默认模型回答"}]


def test_anthropic_messages_maps_system_tools_and_tool_use() -> None:
    translated = _anthropic_to_chat_payload(
        {
            "model": "company-default",
            "system": [{"type": "text", "text": "You are helpful"}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "查一下"}]},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_1",
                            "name": "lookup",
                            "input": {"q": "x"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_1",
                            "content": "结果",
                        }
                    ],
                },
            ],
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "tool", "name": "lookup"},
        }
    )
    assert translated["messages"][0] == {"role": "system", "content": "You are helpful"}
    assert translated["messages"][2]["tool_calls"][0]["function"]["name"] == "lookup"
    assert translated["messages"][3]["role"] == "tool"
    assert translated["tool_choice"] == {"type": "function", "function": {"name": "lookup"}}

    response = _to_anthropic_payload(
        {
            "id": "chatcmpl_2",
            "model": "company-default",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "完成",
                        "tool_calls": [
                            {
                                "id": "tool_1",
                                "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        },
        {"model": "company-default"},
    )
    assert response["content"][0] == {"type": "text", "text": "完成"}
    assert response["content"][1]["type"] == "tool_use"
    assert response["stop_reason"] == "tool_use"


def test_anthropic_messages_without_model_preserves_default_route_resolution() -> None:
    translated = _anthropic_to_chat_payload(
        {"messages": [{"role": "user", "content": "用默认模型回答"}]}
    )

    assert "model" not in translated
    assert translated["messages"] == [{"role": "user", "content": "用默认模型回答"}]
