"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/src/metahub_gateway/dlp/models.py
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DlpRule:
    """数据面运行时 DLP 规则。

    pattern 只在内存中用于匹配用户消息文本，不写入结构化日志、request_logs 或 dlp_matches。
    """

    dlp_rule_id: str
    tenant_id: str
    name: str
    rule_type: str
    pattern: str
    action: str
    priority: int


@dataclass(frozen=True)
class DlpMatch:
    """单条 DLP 命中摘要。

    match_text 仅在函数调用栈内用于计算哈希和长度，不会被写入数据库或日志。
    """

    rule: DlpRule
    message_index: int
    match_text: str


@dataclass(frozen=True)
class DlpDecision:
    """一次请求的 DLP 决策摘要。

    该对象只保存可审计元数据，供 request_logs 写入和异常分支复用，不包含用户输入明文。
    """

    status: str
    rule_id: str | None
    action: str | None
    match_count: int
    reason_code: str | None
    # PRD-070 只允许当前最后一条 user turn 进入累计；历史命中仍可阻断但不得重复加分。
    current_turn_match_count: int = 0
    # 聚合 action 可能来自历史 block；会话累计必须使用当前 turn 自身最强动作，避免历史重计。
    current_turn_action: str = "allow"
