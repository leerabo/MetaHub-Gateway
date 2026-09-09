"""Consumer directory DTOs deliberately have no email, password or role fields."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CreateConsumerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str = Field(min_length=1, max_length=160)
    department: str = Field(default="", max_length=160)

    @field_validator("display_name", "department")
    @classmethod
    def reject_controls(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("control characters are not allowed")
        return value


class UpdateConsumerRequest(CreateConsumerRequest):
    revision: int = Field(ge=1, strict=True)
    status: Literal["active", "disabled"]


class ConsumerInfo(BaseModel):
    consumer_id: str
    display_name: str
    department: str
    status: Literal["active", "disabled"]
    revision: int
    key_count: int
    created_at: datetime
    updated_at: datetime


class ConsumerResponse(BaseModel):
    consumer: ConsumerInfo
    request_id: str
    trace_id: str


class ConsumerListResponse(BaseModel):
    consumers: list[ConsumerInfo]
    total: int
    limit: int
    offset: int
    request_id: str
    trace_id: str
