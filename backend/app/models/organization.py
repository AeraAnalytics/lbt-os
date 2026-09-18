from datetime import datetime

from pydantic import BaseModel


class OrgCreate(BaseModel):
    name: str
    industry: str | None = None
    city: str = "Denver"
    state: str = "CO"


class OrgUpdate(BaseModel):
    name: str | None = None
    industry: str | None = None
    city: str | None = None
    state: str | None = None
    onboarding_complete: bool | None = None


class OrgOut(BaseModel):
    id: str
    clerk_org_id: str
    name: str
    industry: str | None
    plan: str
    subscription_status: str
    city: str
    state: str
    onboarding_complete: bool
    created_at: datetime


class DemoBootstrapRequest(BaseModel):
    industry: str
    name: str | None = None
    city: str = "Denver"
    state: str = "CO"
    seed: int = 42
    replace_existing: bool = False


class DemoReseedRequest(BaseModel):
    industry: str | None = None
    seed: int = 42
