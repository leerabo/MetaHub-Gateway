"""验证官方客户端惰性导入和公开构造参数。"""

from __future__ import annotations

import importlib

import pytest
from fakes import FakeClient, FakeCompletions, FakeEmbeddings, FakeOpenAIModule
from metahub_gateway_examples.client import (
    OpenAIClientUnavailable,
    build_openai_client,
    load_openai_module,
)
from metahub_gateway_examples.config import ExampleConfig


def example_config() -> ExampleConfig:
    """返回便于核对官方构造器参数的固定配置对象。"""

    return ExampleConfig(
        base_url="https://gateway.example.com/v1",
        virtual_key="vk_constructor_secret",
        chat_model="chat-model",
        embedding_model="embedding-model",
        timeout_seconds=21.5,
        max_retries=3,
    )


def test_build_openai_client_passes_only_public_constructor_options() -> None:
    """客户端构建必须准确传入 base_url、Virtual Key、timeout 和 max_retries。"""

    client = FakeClient(FakeCompletions(), FakeEmbeddings())
    module = FakeOpenAIModule(client)

    result = build_openai_client(example_config(), module)

    assert result is client
    assert module.constructor_calls == [
        {
            "base_url": "https://gateway.example.com/v1",
            "api_key": "vk_constructor_secret",
            "timeout": 21.5,
            "max_retries": 3,
        }
    ]


def test_load_openai_module_converts_missing_dependency_to_fixed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """官方依赖缺失时应惰性失败并隐藏本机导入异常细节。"""

    def missing_import(name: str) -> None:
        """模拟包含本机路径的导入失败，验证其不会成为公开异常文本。"""

        # 故意把敏感本机路径放入底层异常，公开错误必须将其完全隐藏。
        raise ModuleNotFoundError(f"{name} missing at C:/secret/python")

    monkeypatch.setattr(importlib, "import_module", missing_import)

    with pytest.raises(OpenAIClientUnavailable) as captured:
        load_openai_module()

    assert captured.value.code == "client.dependency_missing"
    assert "C:/secret/python" not in str(captured.value)


def test_build_openai_client_hides_initialization_exception() -> None:
    """客户端初始化失败时不得在公开异常中回显密钥或代理配置。"""

    module = FakeOpenAIModule(
        None,
        initialization_error=RuntimeError(
            "Authorization: Bearer vk_constructor_secret via http://proxy-secret"
        ),
    )

    with pytest.raises(OpenAIClientUnavailable) as captured:
        build_openai_client(example_config(), module)

    assert captured.value.code == "client.initialization_failed"
    assert "vk_constructor_secret" not in str(captured.value)
    assert "proxy-secret" not in str(captured.value)
