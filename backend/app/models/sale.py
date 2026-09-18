from datetime import datetime

from pydantic import BaseModel, field_validator

PAYMENT_STATUSES = {"pending", "paid", "refunded"}


class SaleCreate(BaseModel):
    customer_id: str | None = None
    lead_id: str | None = None
    service: str
    amount: float
    cost: float = 0.0
    payment_method: str | None = None
    payment_status: str = "pending"
    source: str | None = None
    invoice_number: str | None = None
    notes: str | None = None
    sold_at: datetime | None = None

    @field_validator("amount", "cost")
    @classmethod
    def non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("amount and cost must be >= 0")
        return v

    @field_validator("payment_status")
    @classmethod
    def validate_payment_status(cls, v: str) -> str:
        if v not in PAYMENT_STATUSES:
            raise ValueError(f"payment_status must be one of {PAYMENT_STATUSES}")
        return v


class SaleUpdate(BaseModel):
    service: str | None = None
    amount: float | None = None
    cost: float | None = None
    payment_method: str | None = None
    payment_status: str | None = None
    source: str | None = None
    invoice_number: str | None = None
    notes: str | None = None
    sold_at: datetime | None = None


class SaleOut(BaseModel):
    id: str
    org_id: str
    customer_id: str | None
    lead_id: str | None
    service: str
    amount: float
    cost: float
    profit: float
    payment_method: str | None
    payment_status: str
    source: str | None
    invoice_number: str | None
    notes: str | None
    sold_at: datetime
    created_at: datetime
    updated_at: datetime
