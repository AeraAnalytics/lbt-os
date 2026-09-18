from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

VisitorEventType = Literal[
    "page_view",
    "cta_click",
    "info_submitted",
    "test_ping",
]


class VisitorEventCreate(BaseModel):
    event_type: VisitorEventType
    visitor_id: str = Field(min_length=8, max_length=96)
    session_id: str = Field(min_length=8, max_length=96)
    path: str = Field(default="/", max_length=512)
    source: str | None = Field(default=None, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None

