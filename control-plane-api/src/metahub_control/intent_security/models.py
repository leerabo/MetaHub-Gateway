"""PRD-069 轻量敏感意图分类的控制面公开 DTO。

本模块只描述允许返回给浏览器的安全摘要。输入正文、规范化文本、命中片段、租户和
内部 actor 都不属于响应合同，避免 Pydantic ``repr``、异常或 OpenAPI 示例扩大泄露面。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RiskType = Literal[
    "none",
    "credential_exfiltration",
    "system_prompt_extraction",
    "policy_bypass",
    "jailbreak",
]


class IntentRiskResultInfo(BaseModel):
    """一次手工分类已经提交的不可变安全结果。

    分数使用 0..10000 整数基点，前端不得把历史结果交给当前规则重新计算；版本、原因码
    和完成时间共同解释当时事实。该 DTO 故意不含 tenant、actor 和任何输入派生正文。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    intent_risk_result_id: str = Field(pattern=r"^irr_[0-9a-f]{32}$")
    status: Literal["clean", "detected"]
    classification_mode: Literal["manual_sync"] = "manual_sync"
    risk_type: RiskType
    risk_score: int = Field(ge=0, le=10_000)
    risk_score_scale: Literal[10_000] = 10_000
    action: Literal["allow", "review"]
    reason_code: str = Field(min_length=1, max_length=128)
    detector_version: str = Field(min_length=1, max_length=128)
    ruleset_version: str = Field(min_length=1, max_length=128)
    input_code_points: int = Field(ge=0, le=20_000)
    input_truncated: Literal[False] = False
    duration_ms: int = Field(ge=0, le=86_400_000)
    completed_at: datetime


class IntentClassificationResponse(BaseModel):
    """手工分类响应信封，统一携带内部生成的 request_id 与 trace_id。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    intent_risk_result: IntentRiskResultInfo
    request_id: str = Field(min_length=1, max_length=160)
    trace_id: str = Field(min_length=1, max_length=160)


__all__ = ["IntentClassificationResponse", "IntentRiskResultInfo", "RiskType"]
