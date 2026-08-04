"""验证环境变量配置解析、边界约束与密钥安全。"""

from __future__ import annotations

import pytest
from metahub_gateway_examples.config import ExampleConfigError, load_config


def valid_environment(**overrides: str) -> dict[str, str]:
    """构造包含 Virtual Key 的最小合法测试环境并应用覆盖项。

    测试密钥只存在于内存映射，不写入项目文件；各测试可覆盖单一变量以精确验证
    配置分支。
    """

    environment = {"METAHUB_VIRTUAL_KEY": "vk_test_secret"}
    environment.update(overrides)
    return environment


def test_load_config_reads_all_supported_environment_variables() -> None:
    """完整环境变量应准确映射到客户端配置并规范化 base URL。"""

    config = load_config(
        valid_environment(
            METAHUB_BASE_URL="https://gateway.example.com/v1/",
            METAHUB_CHAT_MODEL="chat-alias",
            METAHUB_EMBEDDING_MODEL="embedding-alias",
            METAHUB_TIMEOUT_SECONDS="12.5",
            METAHUB_MAX_RETRIES="4",
        )
    )

    assert config.base_url == "https://gateway.example.com/v1"
    assert config.virtual_key == "vk_test_secret"
    assert config.chat_model == "chat-alias"
    assert config.embedding_model == "embedding-alias"
    assert config.timeout_seconds == 12.5
    assert config.max_retries == 4


def test_load_config_uses_documented_defaults() -> None:
    """仅提供 Virtual Key 时应使用 README 和模板声明的默认配置。"""

    config = load_config(valid_environment())

    assert config.base_url == "http://localhost:8000/v1"
    assert config.chat_model == "default-chat"
    assert config.embedding_model == "default-embedding"
    assert config.timeout_seconds == 30.0
    assert config.max_retries == 2


def test_missing_virtual_key_reports_only_fixed_variable_code() -> None:
    """缺少 Virtual Key 时异常应指出固定变量名且不包含其他环境值。"""

    with pytest.raises(ExampleConfigError) as captured:
        load_config({"UNRELATED_SECRET": "do-not-leak"})

    assert captured.value.code == "config.METAHUB_VIRTUAL_KEY_missing"
    assert "do-not-leak" not in str(captured.value)


@pytest.mark.parametrize("value", ["text", "0", "-1", "300.1", "nan", "inf"])
def test_timeout_rejects_non_numeric_or_out_of_range_values(value: str) -> None:
    """超时非数字、非有限范围或越界时应返回固定安全配置错误。"""

    with pytest.raises(ExampleConfigError) as captured:
        load_config(valid_environment(METAHUB_TIMEOUT_SECONDS=value))

    assert captured.value.code.startswith("config.METAHUB_TIMEOUT_SECONDS_")
    assert value not in str(captured.value)


@pytest.mark.parametrize("value", ["1.5", "-1", "11", "many"])
def test_retries_rejects_non_integer_or_out_of_range_values(value: str) -> None:
    """重试次数不是 0 至 10 的整数时应拒绝且不回显输入。"""

    with pytest.raises(ExampleConfigError) as captured:
        load_config(valid_environment(METAHUB_MAX_RETRIES=value))

    assert captured.value.code.startswith("config.METAHUB_MAX_RETRIES_")
    assert value not in str(captured.value)


@pytest.mark.parametrize(
    "value",
    ["gateway.local/v1", "ftp://gateway.local/v1", "https://user:pass@gateway.local/v1"],
)
def test_base_url_rejects_invalid_scheme_host_or_embedded_credentials(value: str) -> None:
    """base URL 缺少协议、协议非法或内嵌凭据时必须在建连前拒绝。"""

    with pytest.raises(ExampleConfigError) as captured:
        load_config(valid_environment(METAHUB_BASE_URL=value))

    assert captured.value.code.startswith("config.METAHUB_BASE_URL_")
    assert "user:pass" not in str(captured.value)

