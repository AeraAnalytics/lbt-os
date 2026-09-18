from datetime import datetime

from pydantic import BaseModel


class CustomerCreate(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    tags: list[str] = []
    notes: str | None = None
    lead_id: str | None = None


class CustomerUpdate(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    tags: list[str] | None = None
    notes: str | None = None


class CustomerOut(BaseModel):
    id: str
    org_id: str
    lead_id: str | None
    name: str
    email: str | None
    phone: str | None
    address: str | None
    tags: list[str]
    lifetime_value: float
    total_orders: int
    last_purchase_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
