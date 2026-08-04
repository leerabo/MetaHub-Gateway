"""PRD-067 企业 Prompt 模板的跨服务安全合同。

Control 与 Gateway 必须复用这里的定义校验、内存渲染和加密实现，避免两端对占位符、
AAD 或大小边界产生不同解释。模块只处理当前调用栈中的正文，不记录、缓存或持久化明文。
"""

from .contracts import (
    MAX_PROMPT_CONTENT_BYTES,
    MAX_RENDERED_PROMPT_BYTES,
    MAX_TEMPLATE_VARIABLES,
    PROMPT_TEMPLATE_SCHEMA_VERSION,
    PromptTemplateCipherInvalid,
    PromptTemplateCipherUnavailable,
    PromptTemplateContractError,
    PromptTemplateDefinition,
    PromptTemplateDefinitionUnavailable,
    PromptTemplateOwner,
    PromptTemplateSecretUnavailable,
    PromptVariableDefinition,
    canonical_definition_bytes,
    change_summary_hmac,
    decrypt_definition,
    decrypt_prompt_template_definition,
    encrypt_definition,
    encrypt_prompt_template_definition,
    parse_definition_bytes,
    prompt_template_intent_hmac,
    render_prompt,
    validate_definition,
    verify_prompt_template_intent_hmac,
)

# 兼容别名让 Control/Gateway 与共享专项测试表达同一业务语义，避免调用方再包装出第三套
# 占位符合同。真正的实现仍只有 contracts.py 中的一份严格解析与渲染逻辑。
PromptTemplateValidationError = PromptTemplateContractError
validate_prompt_template_definition = validate_definition
definition_from_canonical_bytes = parse_definition_bytes
render_prompt_template = render_prompt

__all__ = [
    "MAX_PROMPT_CONTENT_BYTES",
    "MAX_RENDERED_PROMPT_BYTES",
    "MAX_TEMPLATE_VARIABLES",
    "PROMPT_TEMPLATE_SCHEMA_VERSION",
    "PromptTemplateCipherInvalid",
    "PromptTemplateCipherUnavailable",
    "PromptTemplateContractError",
    "PromptTemplateDefinition",
    "PromptTemplateDefinitionUnavailable",
    "PromptTemplateOwner",
    "PromptTemplateSecretUnavailable",
    "PromptTemplateValidationError",
    "PromptVariableDefinition",
    "canonical_definition_bytes",
    "change_summary_hmac",
    "decrypt_definition",
    "decrypt_prompt_template_definition",
    "definition_from_canonical_bytes",
    "encrypt_definition",
    "encrypt_prompt_template_definition",
    "parse_definition_bytes",
    "prompt_template_intent_hmac",
    "render_prompt",
    "render_prompt_template",
    "validate_definition",
    "validate_prompt_template_definition",
    "verify_prompt_template_intent_hmac",
]
