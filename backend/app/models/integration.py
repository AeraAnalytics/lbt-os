from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

SUPPORTED_PROVIDERS = {"quickbooks", "hubspot", "stripe"}
SYNC_STATUSES = {"connected", "disconnected", "error"}
RUN_STATUSES = {"pending", "running", "success", "partial", "failed"}


class IntegrationConnectionCreate(BaseModel):
    provider: str
    label: str | None = None
    credentials: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    external_account_id: str | None = None
    external_account_name: str | None = None

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        if value not in SUPPORTED_PROVIDERS:
            raise ValueError(f"provider must be one of {sorted(SUPPORTED_PROVIDERS)}")
        return value


class IntegrationConnectionUpdate(BaseModel):
    label: str | None = None
    credentials: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    status: str | None = None
    external_account_id: str | None = None
    external_account_name: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is not None and value not in SYNC_STATUSES:
            raise ValueError(f"status must be one of {SYNC_STATUSES}")
        return value


class IntegrationConnectionOut(BaseModel):
    id: str
    org_id: str
    provider: str
    label: str | None
    status: str
    config: dict[str, Any]
    external_account_id: str | None
    external_account_name: str | None
    last_synced_at: datetime | None
    last_sync_status: str | None
    last_sync_error: str | None
    created_at: datetime
    updated_at: datetime


class IntegrationSyncRunOut(BaseModel):
    id: str
    org_id: str
    connection_id: str
    provider: str
    trigger_source: str
    status: str
    stats: dict[str, Any]
    error: str | None
    started_at: datetime
    finished_at: datetime | None
