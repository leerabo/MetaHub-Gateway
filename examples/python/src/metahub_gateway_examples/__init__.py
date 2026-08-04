"""MetaHub Gateway 的官方 OpenAI Python 客户端示例包。

该包只封装示例所需的配置读取、公开客户端构建和安全终端输出，实际 HTTP
协议、序列化和重试均交给官方 ``openai`` 包，避免形成另一套私有 SDK。
"""

from .config import ExampleConfig, ExampleConfigError, load_config

__all__ = ["ExampleConfig", "ExampleConfigError", "load_config"]

