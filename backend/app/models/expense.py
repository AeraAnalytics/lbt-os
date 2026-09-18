from datetime import date, datetime

from pydantic import BaseModel, field_validator

EXPENSE_CATEGORIES = {
    "payroll", "materials", "marketing", "rent", "utilities",
    "equipment", "insurance", "software", "misc",
}


class ExpenseCreate(BaseModel):
    category: str
    description: str
    amount: float
    vendor: str | None = None
    receipt_url: str | None = None
    is_recurring: bool = False
    recurrence_period: str | None = None
    expense_date: date = date.today()

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        if v not in EXPENSE_CATEGORIES:
            raise ValueError(f"category must be one of {EXPENSE_CATEGORIES}")
        return v

    @field_validator("amount")
    @classmethod
    def positive_amount(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("amount must be positive")
        return v


class ExpenseUpdate(BaseModel):
    category: str | None = None
    description: str | None = None
    amount: float | None = None
    vendor: str | None = None
    receipt_url: str | None = None
    is_recurring: bool | None = None
    recurrence_period: str | None = None
    expense_date: date | None = None


class ExpenseOut(BaseModel):
    id: str
    org_id: str
    category: str
    description: str
    amount: float
    vendor: str | None
    receipt_url: str | None
    is_recurring: bool
    recurrence_period: str | None
    expense_date: date
    created_at: datetime
    updated_at: datetime
