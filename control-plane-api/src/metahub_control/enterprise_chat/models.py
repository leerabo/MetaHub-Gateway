"""PRD-066 企业 Chat 的严格 HTTP 数据模型。

请求模型禁止额外字段，避免浏览器注入 tenant、Virtual Key、Provider、Endpoint 或任意
OpenAI 扩展参数。消息正文只存在于当前模型实例和 HTTP 调用栈，任何持久化 DTO 都不能
复用这些正文结构。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from metahub_control.prompt_templates.models import ChatPromptTemplateOption


class ChatLimits(BaseModel):
    """返回 PAGE-093 必须遵守的有界输入和会话限制。"""

    max_history_messages: int
    max_message_chars: int
    max_history_bytes: int
    session_ttl_seconds: int


class ChatProjectOption(BaseModel):
    """一个可供企业 Chat 选择的项目/环境安全投影。

    ``availability`` 只说明后端能否唯一绑定运行 Key，不披露 Key ID、前缀或数量。
    """

    project_id: str
    name: str
    environment_id: str
    environment_name: str
    availability: Literal["ready", "key_unavailable", "ambiguous_key"]


class ChatModelAliasOption(BaseModel):
    """当前租户 active 模型别名的最小展示字段。"""

    model_alias_id: str
    alias: str
    display_name: str
    environment_id: str
    # 目录只投影 active 别名；固定字段让浏览器无需猜测省略值，也不允许调用方构造其他状态。
    status: Literal["active"] = "active"


class ChatOptionsResponse(BaseModel):
    """企业 Chat 选项目录响应，统一携带链路标识。"""

    options_revision: str
    projects: list[ChatProjectOption]
    model_aliases: list[ChatModelAliasOption]
    # 该最小投影只在实时具有 prompt_template.use 时填充，不授予定义读取或管理权限。
    prompt_templates: list[ChatPromptTemplateOption] = Field(default_factory=list)
    limits: ChatLimits
    request_id: str
    trace_id: str


class CreateChatSessionRequest(BaseModel):
    """创建短生命周期会话所需的三个稳定资源 ID。

    租户、用户、登录 Session、模型别名文本和 Virtual Key 均由后端恢复，客户端不能提交。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    project_id: StrictStr = Field(min_length=1, max_length=120)
    environment_id: StrictStr = Field(min_length=1, max_length=120)
    model_alias_id: StrictStr = Field(min_length=1, max_length=120)


class ChatSessionInfo(BaseModel):
    """页面可见的会话安全摘要，不包含运行 Key 或正文。"""

    chat_session_id: str
    project_id: str
    project_name: str
    environment_id: str
    environment_name: str
    model_alias_id: str
    model_alias: str
    state_revision: int
    expires_at: datetime


class ChatSessionResponse(BaseModel):
    """创建企业 Chat 会话的统一响应，只投影安全会话摘要和当前链路标识。"""

    session: ChatSessionInfo
    limits: ChatLimits
    request_id: str
    trace_id: str


class ChatHistoryMessage(BaseModel):
    """只在当前发送调用栈存活的一条纯文本消息。

    ``repr=False`` 降低框架或调试器意外打印正文的风险；服务层仍会再次执行角色交替、
    字符数和 UTF-8 总字节数校验，不能只依赖前端控件。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    role: Literal["user", "assistant"]
    content: StrictStr = Field(min_length=1, max_length=8000, repr=False)


class SendChatMessageRequest(BaseModel):
    """发送一次普通或模板非流式企业 Chat 请求。

    ``client_message_id`` 是页面生成的规范 UUID，用于阻止重复点击或网络重试造成二次计费。
    模板身份只允许 version ID 与变量 map 成对出现；template ID、正文、system message 和 tenant
    均不属于公开 wire。普通 history 为奇数并以 user 结束，模板 history 为空或完整消息对。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    client_message_id: StrictStr = Field(min_length=36, max_length=36)
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=20, repr=False)
    prompt_template_version_id: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    template_variables: dict[StrictStr, StrictStr] | None = Field(
        default=None,
        max_length=32,
        repr=False,
    )

    @model_validator(mode="after")
    def validate_template_pair_and_history(self) -> SendChatMessageRequest:
        """冻结两个互斥 wire，并拒绝靠 null、空普通 history 或半对历史绕过服务校验。"""

        version_present = "prompt_template_version_id" in self.model_fields_set
        variables_present = "template_variables" in self.model_fields_set
        if (
            version_present != variables_present
            or (version_present and self.prompt_template_version_id is None)
            or (variables_present and self.template_variables is None)
        ):
            # 版本与变量必须成对出现；半对 wire 统一失败，不能猜测用户是否意图降级为普通 Chat。
            raise ValueError("chat.request_invalid")
        if version_present:
            if len(self.history) % 2 != 0:
                # 模板最终 user 由 Gateway 渲染；客户端只能提交此前完整 user/assistant 对。
                raise ValueError("chat.request_invalid")
        elif not self.history or len(self.history) % 2 != 1:
            # 普通 Chat 继续要求以本轮 user 结束，PRD-067 不放宽既有历史合同。
            raise ValueError("chat.request_invalid")
        return self


class ChatAssistantMessage(BaseModel):
    """本次成功响应中的助手纯文本；该对象不得被持久化或记录。"""

    role: Literal["assistant"] = "assistant"
    content: str = Field(repr=False)


class ChatPolicySummary(BaseModel):
    """RequestLog 中策略执行结果的固定安全摘要。"""

    decision: str
    reason_code: str | None = None


class ChatExecutionSummary(BaseModel):
    """页面展示的 Token、成本和治理事实白名单。"""

    request_id: str
    trace_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    cost_cny: str
    policy: ChatPolicySummary
    fallback_triggered: bool
    cache_status: Literal["hit", "miss", "not_configured", "unknown"]


class ChatMessageResponse(BaseModel):
    """一次成功发送的页面响应。

    assistant 正文只返回给当前浏览器；数据库只保存 message ID、状态和 execution 安全元数据。
    """

    chat_session_id: str
    chat_message_id: str
    client_message_id: str
    assistant: ChatAssistantMessage
    execution: ChatExecutionSummary
    session_revision: int
    request_id: str
    trace_id: str


class ChatTemplateMessageResponse(ChatMessageResponse):
    """模板调用成功响应，额外返回仅供当前浏览器内存 history 使用的渲染 user 正文。"""

    rendered_user: str = Field(repr=False)
