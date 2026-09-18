from datetime import datetime

from pydantic import BaseModel, field_validator

LEAD_STATUSES = {"new", "contacted", "qualified", "proposal", "won", "lost"}
LEAD_SOURCES  = {"google", "referral", "social", "yelp", "cold_call", "walk_in", "website", "other"}


class LeadCreate(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    source: str | None = None
    status: str = "new"
    service_interest: str | None = None
    estimated_value: float | None = None
    notes: str | None = None
    assigned_to: str | None = None
    follow_up_at: datetime | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in LEAD_STATUSES:
            raise ValueError(f"status must be one of {LEAD_STATUSES}")
        return v


class LeadUpdate(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    source: str | None = None
    status: str | None = None
    service_interest: str | None = None
    estimated_value: float | None = None
    notes: str | None = None
    assigned_to: str | None = None
    follow_up_at: datetime | None = None
    contacted_at: datetime | None = None
    converted_at: datetime | None = None
    lost_reason: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is not None and v not in LEAD_STATUSES:
            raise ValueError(f"status must be one of {LEAD_STATUSES}")
        return v


class LeadOut(BaseModel):
    id: str
    org_id: str
    name: str
    email: str | None
    phone: str | None
    source: str | None
    status: str
    service_interest: str | None
    estimated_value: float | None
    notes: str | None
    assigned_to: str | None
    follow_up_at: datetime | None
    contacted_at: datetime | None
    converted_at: datetime | None
    lost_reason: str | None
    created_at: datetime
    updated_at: datetime
