"""PRD-103 对外严格 DTO，只投影支付流程所需的非敏感商业事实。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Currency = Literal["CNY", "USD"]
PaymentProvider = Literal["mockpay", "cloudpay"]
OrderStatus = Literal[
    "pending_payment",
    "paid",
    "fulfilled",
    "past_due",
    "canceled",
    "refunded",
    "partially_refunded",
]
PaymentIntentStatus = Literal[
    "requires_action", "processing", "succeeded", "failed", "canceled"
]
RefundStatus = Literal["requested", "processing", "succeeded", "failed", "rejected"]


class PaymentResponseModel(BaseModel):
    """拒绝未声明字段，防止渠道引用、风控细节或回调正文意外进入公共响应。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class PaymentIntentCreateRequest(PaymentResponseModel):
    """客户只选择当前 Workspace 的待支付订单和受支持渠道。"""

    order_id: str
    provider: PaymentProvider


class PaymentIntentInfo(PaymentResponseModel):
    """支付意图摘要不包含渠道引用、签名密钥或支付方法详情。"""

    payment_intent_id: str
    order_id: str
    provider: PaymentProvider
    status: PaymentIntentStatus
    amount_minor: int = Field(ge=0, le=1_000_000_000_000)
    currency: Currency
    state_revision: int = Field(ge=1)
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class PaymentIntentResponse(PaymentResponseModel):
    """短期 client token 只在创建成功时返回一次，重放只返回稳定摘要。"""

    payment_intent: PaymentIntentInfo
    client_token: str | None = None
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class RefundCreateRequest(PaymentResponseModel):
    """退款申请不接受 captured 金额、渠道引用或状态等服务端权威字段。"""

    order_id: str
    amount_minor: int = Field(gt=0, le=1_000_000_000_000)
    reason_code: Literal["customer_request", "service_failure", "duplicate_charge"]
    confirmed: Literal[True]


class RefundInfo(PaymentResponseModel):
    """退款摘要只暴露客户可核对的金额、原因与审核状态。"""

    refund_id: str
    order_id: str
    payment_intent_id: str
    amount_minor: int = Field(gt=0, le=1_000_000_000_000)
    currency: Currency
    reason_code: Literal["customer_request", "service_failure", "duplicate_charge"]
    status: RefundStatus
    state_revision: int = Field(ge=1)
    requested_at: datetime
    updated_at: datetime


class RefundResponse(PaymentResponseModel):
    """退款命令响应保持幂等标记与链路标识，不返回内部审核备注。"""

    refund: RefundInfo
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class PlatformRefundQueueResponse(PaymentResponseModel):
    """平台退款队列只返回审核所需摘要，不暴露 Workspace owner 或渠道引用。"""

    refunds: tuple[RefundInfo, ...]
    request_id: str
    trace_id: str


class PlatformRefundDecisionRequest(PaymentResponseModel):
    """平台审核只接受固定决定和原因码，禁止自由文本进入持久事实。"""

    decision: Literal["approve", "reject"]
    reason_code: Literal["policy_approved", "amount_invalid", "evidence_missing"]
    confirmed: Literal[True]


class SubscriptionOrderInfo(PaymentResponseModel):
    """订单摘要以原币小单位整数呈现，并隐藏规范 intent hash。"""

    order_id: str
    subscription_id: str
    plan_version_id: str
    order_kind: Literal["create", "upgrade", "downgrade", "renewal"]
    status: OrderStatus
    amount_minor: int = Field(ge=0, le=1_000_000_000_000)
    captured_amount_minor: int = Field(ge=0, le=1_000_000_000_000)
    refunded_amount_minor: int = Field(ge=0, le=1_000_000_000_000)
    currency: Currency
    state_revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_amounts(self) -> SubscriptionOrderInfo:
        """公共 DTO 也拒绝退款大于实收或实收大于应付的损坏事实。"""

        if self.captured_amount_minor > self.amount_minor:
            raise ValueError("captured amount exceeds order amount")
        if self.refunded_amount_minor > self.captured_amount_minor:
            raise ValueError("refunded amount exceeds captured amount")
        return self


class DunningCaseInfo(PaymentResponseModel):
    """催收只返回固定策略引用和下一动作时间，不暴露渠道失败原文。"""

    dunning_case_id: str
    order_id: str
    status: Literal["open", "recovered", "closed"]
    cycle_number: int = Field(ge=1, le=12)
    template_reference: str
    next_action_at: datetime | None = None


class BillingOrdersResponse(PaymentResponseModel):
    """聚合当前 Workspace 的订单、支付、退款和催收安全摘要。"""

    orders: tuple[SubscriptionOrderInfo, ...]
    payment_intents: tuple[PaymentIntentInfo, ...]
    refunds: tuple[RefundInfo, ...]
    dunning_cases: tuple[DunningCaseInfo, ...]
    request_id: str
    trace_id: str


class PaymentWebhookReceiptResponse(PaymentResponseModel):
    """渠道快速应答只确认 receipt 身份和持久化状态。"""

    receipt_id: str
    status: Literal["pending", "processing", "completed", "conflict", "failed"]
    idempotent_replay: bool
    request_id: str
    trace_id: str
