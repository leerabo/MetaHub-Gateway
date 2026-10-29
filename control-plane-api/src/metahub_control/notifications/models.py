"""Strict public DTOs for PRD-105 notifications and template versions."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NotificationCategory = Literal[
    "security", "billing", "verification", "payments", "invoices", "operations"
]
NotificationChannel = Literal["in_app", "email", "webhook", "sms", "web_push"]
TemplateChannel = Literal["in_app", "email", "webhook"]
NotificationEvent = Literal[
    "security.sign_in_risk",
    "subscription.status_changed",
    "verification.status_changed",
    "payment.status_changed",
    "invoice.issued",
    "invoice.reversed",
    "infrastructure.backup_completed",
    "gateway.health_changed",
]


class NotificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NotificationInfo(NotificationModel):
    notification_id: str
    event_type: NotificationEvent
    category: NotificationCategory
    subject: str
    body: str
    read: bool
    created_at: datetime


class NotificationListResponse(NotificationModel):
    notifications: tuple[NotificationInfo, ...]
    unread_count: int = Field(ge=0)
    next_cursor: str | None = None
    request_id: str
    trace_id: str


class NotificationReadResponse(NotificationModel):
    notification: NotificationInfo
    request_id: str
    trace_id: str


class NotificationPreferenceInfo(NotificationModel):
    category: NotificationCategory
    channel: NotificationChannel
    enabled: bool
    mandatory: bool
    available: bool


class NotificationPreferencesResponse(NotificationModel):
    preferences: tuple[NotificationPreferenceInfo, ...]
    revision: int = Field(ge=1)
    request_id: str
    trace_id: str


class NotificationPreferenceChange(NotificationModel):
    category: NotificationCategory
    channel: NotificationChannel
    enabled: bool


class NotificationPreferencesPatchRequest(NotificationModel):
    changes: list[NotificationPreferenceChange] = Field(min_length=1, max_length=30)


class NotificationTemplateInfo(NotificationModel):
    notification_template_id: str
    template_code: str
    event_type: NotificationEvent
    category: NotificationCategory
    channel: TemplateChannel
    locale: Literal["en", "zh-CN"]
    version: int = Field(ge=1)
    parameter_allowlist: tuple[str, ...]
    subject_template: str
    body_template: str
    content_sha256: str
    status: Literal["draft", "published"]
    revision: int = Field(ge=1)
    created_at: datetime
    published_at: datetime | None = None


class NotificationTemplateListResponse(NotificationModel):
    templates: tuple[NotificationTemplateInfo, ...]
    catalog_revision: int = Field(ge=1)
    request_id: str
    trace_id: str


class NotificationTemplateCreateRequest(NotificationModel):
    template_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]+$")
    event_type: NotificationEvent
    channel: TemplateChannel
    locale: Literal["en", "zh-CN"]
    parameter_allowlist: list[str] = Field(min_length=1, max_length=12)
    subject_template: str = Field(min_length=1, max_length=200)
    body_template: str = Field(min_length=1, max_length=4000)
    status: Literal["draft", "published"]


class NotificationTemplateResponse(NotificationModel):
    template: NotificationTemplateInfo
    catalog_revision: int = Field(ge=1)
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


__all__ = [
    "NotificationInfo",
    "NotificationListResponse",
    "NotificationPreferenceChange",
    "NotificationPreferencesPatchRequest",
    "NotificationPreferencesResponse",
    "NotificationReadResponse",
    "NotificationTemplateCreateRequest",
    "NotificationTemplateListResponse",
    "NotificationTemplateResponse",
]
