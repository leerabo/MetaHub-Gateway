"""开发者门户公开 DTO。

模型显式区分快速开始代码片段与可下载 Python/JavaScript 示例元数据。所有响应都只承载
公开配置和认证上下文租户，不得加入 Virtual Key、Provider Key、登录 Token 或服务器绝对路径。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class SDKExample(BaseModel):
    """SDK 示例 DTO。

    示例由后端按当前租户和运行配置生成，代码中只能出现环境变量或占位符，不能包含明文 Virtual Key。
    """

    id: str
    tenant_id: str
    language: Literal["python", "javascript", "curl"]
    title: str
    description: str
    install_command: str | None
    code: str
    copy_label: str
    base_url: str
    model_alias: str
    uses_env_var: bool


class SDKEnvironmentVariable(BaseModel):
    """描述受控 SDK 示例支持的单个环境变量。

    ``default_value`` 只允许保存可公开的运行默认值；Virtual Key 必须返回 ``None``，
    防止下载页把任何认证凭据误当成配置示例下发给浏览器。
    """

    name: str
    required: bool
    default_value: str | None
    description: str


class SDKCommand(BaseModel):
    """描述受控 SDK 示例项目的一条固定运行命令。

    命令由服务端常量生成，不接受用户输入或文件路径拼接，前端只能展示和复制。
    """

    # Python 继续只使用 install/chat/embeddings；configure/stream 仅供 Node 四步流程使用。
    id: Literal["install", "configure", "chat", "embeddings", "stream"]
    label: str
    command: str


class PythonSDKExampleResponse(BaseModel):
    """承载当前租户可下载的 Python OpenAI-compatible 示例元数据。

    响应只包含公开配置、固定文件清单和认证上下文中的 ``tenant_id``；Virtual Key、
    Provider Key、Token 与本机绝对路径均不属于该接口的数据契约。
    """

    id: str
    tenant_id: str
    language: Literal["python"]
    title: str
    description: str
    package_name: str
    version: str
    repository_path: str
    download_url: str
    gateway_base_url: str
    chat_model_alias: str
    chat_model_configured: bool = False
    embedding_model_alias: str
    environment_variables: list[SDKEnvironmentVariable]
    commands: list[SDKCommand]
    files: list[str]
    capabilities: list[str]
    request_id: str
    trace_id: str


class JavaScriptSDKExampleResponse(BaseModel):
    """承载当前租户可下载的 JavaScript/Node OpenAI-compatible 示例元数据。

    DTO 只公开固定运行时、受控文件清单和当前租户推荐的 Chat 别名。浏览器直连能力固定
    为关闭，安全提示不得被调用方覆盖，避免前端误把长期 Virtual Key 当成浏览器配置。
    """

    id: str
    tenant_id: str
    language: Literal["javascript"]
    title: str
    description: str
    package_name: str
    version: str
    runtime: str
    repository_path: str
    download_url: str
    gateway_base_url: str
    chat_model_alias: str
    chat_model_configured: bool = False
    environment_variables: list[SDKEnvironmentVariable]
    commands: list[SDKCommand]
    files: list[str]
    capabilities: list[str]
    browser_direct_supported: bool
    security_notice: str
    request_id: str
    trace_id: str


class QuickstartStep(BaseModel):
    """快速开始步骤 DTO。

    步骤状态让前端能区分已经满足的配置和需要跳转处理的配置缺口。
    """

    id: str
    tenant_id: str
    order: int
    title: str
    description: str
    status: Literal["ready", "action_required", "missing"]
    action_label: str
    navigation_item_id: str
    target_hash: str


class QuickstartCommonError(BaseModel):
    """快速开始常见错误 DTO。

    错误说明只包含公开错误码、原因和修复建议，不包含服务端堆栈、Provider Key 或请求正文。
    """

    code: str
    title: str
    cause: str
    fix: str
    link_target: str


class QuickstartDataResponse(BaseModel):
    """快速开始页响应。

    响应面向当前租户生成，携带 request_id/trace_id，便于把页面加载问题和后端日志关联。
    """

    tenant_id: str
    gateway_base_url: str
    model_alias: str
    virtual_key_placeholder: str
    has_active_virtual_key: bool
    virtual_key_prefix: str | None
    steps: list[QuickstartStep]
    examples: list[SDKExample]
    common_errors: list[QuickstartCommonError]
    request_id: str
    trace_id: str
