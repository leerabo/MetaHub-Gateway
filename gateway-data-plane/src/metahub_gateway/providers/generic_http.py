"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/src/metahub_gateway/providers/generic_http.py
"""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleAdapter


class GenericHttpConfigAdapter(OpenAICompatibleAdapter):
    """通用 HTTP 配置 Adapter。

    M0 的通用 Adapter 复用 OpenAI-compatible JSON/SSE 发送能力，但保留独立名称和
    工厂分支，后续可以在不改数据面主流程的情况下扩展路径映射、认证头和字段翻译。
    """

    name = "generic_http"
    version = "1.0.0"
