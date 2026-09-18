from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet
from fastapi import HTTPException

from supabase import Client

from ..config import settings

EXPENSE_CATEGORY_KEYWORDS = {
    "payroll": "payroll",
    "labor": "payroll",
    "materials": "materials",
    "supply": "materials",
    "marketing": "marketing",
    "ads": "marketing",
    "rent": "rent",
    "lease": "rent",
    "utilities": "utilities",
    "electric": "utilities",
    "software": "software",
    "saas": "software",
    "insurance": "insurance",
    "equipment": "equipment",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fernet() -> Fernet:
    secret = settings.integration_secret_key or settings.api_secret
    if not secret:
        raise HTTPException(status_code=500, detail="Integration credential encryption is not configured.")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def _encrypt_credentials(credentials: dict[str, Any]) -> str:
    payload = json.dumps(credentials).encode("utf-8")
    return _fernet().encrypt(payload).decode("utf-8")


def _decrypt_credentials(ciphertext: str | None) -> dict[str, Any]:
    if not ciphertext:
        return {}
    raw = _fernet().decrypt(ciphertext.encode("utf-8"))
    return json.loads(raw.decode("utf-8"))


def _normalize_expense_category(value: str | None) -> str:
    if not value:
        return "misc"
    normalized = value.strip().lower()
    for keyword, category in EXPENSE_CATEGORY_KEYWORDS.items():
        if keyword in normalized:
            return category
    return "misc"


def _is_missing_table_error(exc: Exception) -> bool:
    return "Could not find the table" in str(exc)


def _safe_optional_query(execute_fn, default):
    try:
        result = execute_fn()
        return result.data or default
    except Exception as exc:
        if _is_missing_table_error(exc):
            return default
        raise


def list_provider_definitions() -> list[dict[str, Any]]:
    return [provider.definition() for provider in PROVIDER_REGISTRY.values()]


def _sign_value(raw: str) -> str:
    secret = (settings.integration_secret_key or settings.api_secret).encode("utf-8")
    return hmac.new(secret, raw.encode("utf-8"), hashlib.sha256).hexdigest()


def build_oauth_state(org_id: str, provider: str) -> str:
    payload = {"org_id": org_id, "provider": provider, "ts": int(datetime.now(timezone.utc).timestamp())}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    return f"{raw}.{_sign_value(raw)}"


def parse_oauth_state(state: str) -> dict[str, Any]:
    try:
        raw, signature = state.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.") from exc
    if not hmac.compare_digest(signature, _sign_value(raw)):
        raise HTTPException(status_code=400, detail="OAuth state signature mismatch.")
    payload = json.loads(base64.urlsafe_b64decode(raw.encode("utf-8")).decode("utf-8"))
    if datetime.now(timezone.utc).timestamp() - int(payload["ts"]) > 900:
        raise HTTPException(status_code=400, detail="OAuth state expired.")
    return payload


def get_frontend_connection_callback(provider: str, status: str, message: str | None = None) -> str:
    query = {"provider": provider, "status": status}
    if message:
        query["message"] = message
    return f"{settings.frontend_url}/app/connections?{urlencode(query)}"


def get_oauth_authorization_url(provider: str, org_id: str) -> str:
    provider_impl = PROVIDER_REGISTRY.get(provider)
    if provider_impl is None:
        raise HTTPException(status_code=400, detail=f"Unsupported provider '{provider}'.")
    return provider_impl.build_oauth_authorization_url(org_id)


def list_connections(db: Client, org_id: str) -> list[dict[str, Any]]:
    return _safe_optional_query(
        lambda: (
        db.table("integration_connections")
        .select("id, org_id, provider, label, status, config, external_account_id, external_account_name, last_synced_at, last_sync_status, last_sync_error, created_at, updated_at")
        .eq("org_id", org_id)
        .order("created_at", desc=True)
        .execute()
        ),
        [],
    )


def list_sync_runs(db: Client, org_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return _safe_optional_query(
        lambda: (
        db.table("integration_sync_runs")
        .select("*")
        .eq("org_id", org_id)
        .order("started_at", desc=True)
        .limit(limit)
        .execute()
        ),
        [],
    )


def get_connection(db: Client, org_id: str, connection_id: str) -> dict[str, Any]:
    try:
        result = (
            db.table("integration_connections")
            .select("*")
            .eq("org_id", org_id)
            .eq("id", connection_id)
            .maybe_single()
            .execute()
        )
    except Exception as exc:
        if _is_missing_table_error(exc):
            raise HTTPException(
                status_code=503,
                detail="Integration tables are not installed in Supabase yet. Apply the integrations migration first.",
            ) from exc
        raise
    if result is None or not result.data:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    return result.data


def create_connection(
    db: Client,
    org_id: str,
    provider: str,
    credentials: dict[str, Any],
    config: dict[str, Any] | None = None,
    label: str | None = None,
    external_account_id: str | None = None,
    external_account_name: str | None = None,
) -> dict[str, Any]:
    provider_impl = PROVIDER_REGISTRY.get(provider)
    if provider_impl is None:
        raise HTTPException(status_code=400, detail=f"Unsupported provider '{provider}'.")
    credentials = provider_impl.validate_credentials(credentials)

    payload = {
        "org_id": org_id,
        "provider": provider,
        "label": label or provider_impl.label,
        "status": "connected",
        "credentials_encrypted": _encrypt_credentials(credentials),
        "config": config or {},
        "external_account_id": external_account_id,
        "external_account_name": external_account_name,
    }
    try:
        result = db.table("integration_connections").insert(payload).execute()
    except Exception as exc:
        if _is_missing_table_error(exc):
            raise HTTPException(
                status_code=503,
                detail="Connectors are not enabled in Supabase yet. Apply supabase/migration_add_integrations.sql first.",
            ) from exc
        raise
    connection_id = result.data[0]["id"]
    return sanitize_connection(get_connection(db, org_id, connection_id))


def update_connection(
    db: Client,
    org_id: str,
    connection_id: str,
    *,
    label: str | None = None,
    credentials: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    status: str | None = None,
    external_account_id: str | None = None,
    external_account_name: str | None = None,
) -> dict[str, Any]:
    existing = get_connection(db, org_id, connection_id)
    provider_impl = PROVIDER_REGISTRY.get(existing["provider"])
    update_data: dict[str, Any] = {}
    if label is not None:
        update_data["label"] = label
    if config is not None:
        update_data["config"] = config
    if status is not None:
        update_data["status"] = status
    if external_account_id is not None:
        update_data["external_account_id"] = external_account_id
    if external_account_name is not None:
        update_data["external_account_name"] = external_account_name
    if credentials is not None:
        merged = _decrypt_credentials(existing.get("credentials_encrypted"))
        merged.update(credentials)
        if provider_impl is not None:
            merged = provider_impl.validate_credentials(merged)
        update_data["credentials_encrypted"] = _encrypt_credentials(merged)
    if not update_data:
        return sanitize_connection(existing)
    result = (
        db.table("integration_connections")
        .update(update_data)
        .eq("id", connection_id)
        .eq("org_id", org_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Integration connection not found.")
    return sanitize_connection(get_connection(db, org_id, connection_id))


def sanitize_connection(connection: dict[str, Any]) -> dict[str, Any]:
    safe = dict(connection)
    safe.pop("credentials_encrypted", None)
    return safe


def _find_connection_by_provider(db: Client, org_id: str, provider: str) -> dict[str, Any] | None:
    try:
        result = (
            db.table("integration_connections")
            .select("*")
            .eq("org_id", org_id)
            .eq("provider", provider)
            .maybe_single()
            .execute()
        )
        if result is None:
            return None
        return result.data
    except Exception as exc:
        if _is_missing_table_error(exc):
            return None
        raise


def upsert_oauth_connection(
    db: Client,
    *,
    org_id: str,
    provider: str,
    credentials: dict[str, Any],
    config: dict[str, Any] | None = None,
    external_account_id: str | None = None,
    external_account_name: str | None = None,
) -> dict[str, Any]:
    existing = _find_connection_by_provider(db, org_id, provider)
    provider_impl = PROVIDER_REGISTRY.get(provider)
    if existing:
        return update_connection(
            db,
            org_id,
            existing["id"],
            credentials=credentials,
            config={**(existing.get("config") or {}), **(config or {})},
            status="connected",
            external_account_id=external_account_id or existing.get("external_account_id"),
            external_account_name=external_account_name or existing.get("external_account_name"),
        )
    return create_connection(
        db,
        org_id,
        provider=provider,
        credentials=credentials,
        config=config or {},
        label=provider_impl.label if provider_impl else provider,
        external_account_id=external_account_id,
        external_account_name=external_account_name,
    )


def exchange_oauth_code(db: Client, provider: str, code: str, state: str, extra_query: dict[str, Any]) -> dict[str, Any]:
    payload = parse_oauth_state(state)
    org_id = payload["org_id"]
    if payload["provider"] != provider:
        raise HTTPException(status_code=400, detail="OAuth provider mismatch.")
    provider_impl = PROVIDER_REGISTRY.get(provider)
    if provider_impl is None:
        raise HTTPException(status_code=400, detail=f"Unsupported provider '{provider}'.")
    return provider_impl.exchange_oauth_code(db, org_id=org_id, code=code, extra_query=extra_query)


def _record_sync_run_start(db: Client, connection: dict[str, Any], trigger_source: str) -> dict[str, Any]:
    try:
        result = db.table("integration_sync_runs").insert({
            "org_id": connection["org_id"],
            "connection_id": connection["id"],
            "provider": connection["provider"],
            "trigger_source": trigger_source,
            "status": "running",
            "stats": {},
            "started_at": _utc_now(),
        }).execute()
    except Exception as exc:
        if _is_missing_table_error(exc):
            raise HTTPException(
                status_code=503,
                detail="Connector sync tables are not installed in Supabase yet. Apply the integrations migration first.",
            ) from exc
        raise
    return result.data[0]


def _record_sync_run_finish(
    db: Client,
    connection: dict[str, Any],
    run_id: str,
    *,
    status: str,
    stats: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    finished_at = _utc_now()
    db.table("integration_sync_runs").update({
        "status": status,
        "stats": stats,
        "error": error,
        "finished_at": finished_at,
    }).eq("id", run_id).execute()
    db.table("integration_connections").update({
        "last_synced_at": finished_at,
        "last_sync_status": status,
        "last_sync_error": error,
        "status": "connected" if status in {"success", "partial"} else "error",
    }).eq("id", connection["id"]).execute()
    return (
        db.table("integration_sync_runs")
        .select("*")
        .eq("id", run_id)
        .single()
        .execute()
        .data
    )


def _get_record_link(
    db: Client,
    connection_id: str,
    object_type: str,
    external_id: str,
) -> dict[str, Any] | None:
    try:
        result = (
            db.table("integration_record_links")
            .select("*")
            .eq("connection_id", connection_id)
            .eq("object_type", object_type)
            .eq("external_id", external_id)
            .maybe_single()
            .execute()
        )
        if result is None:
            return None
        return result.data
    except Exception as exc:
        if _is_missing_table_error(exc):
            raise HTTPException(
                status_code=503,
                detail="Connector record-link tables are not installed in Supabase yet. Apply the integrations migration first.",
            ) from exc
        raise


def _upsert_record_link(
    db: Client,
    *,
    org_id: str,
    connection_id: str,
    provider: str,
    object_type: str,
    external_id: str,
    local_table: str,
    local_id: str,
    fingerprint: str | None = None,
) -> None:
    existing = _get_record_link(db, connection_id, object_type, external_id)
    payload = {
        "org_id": org_id,
        "connection_id": connection_id,
        "provider": provider,
        "object_type": object_type,
        "external_id": external_id,
        "local_table": local_table,
        "local_id": local_id,
        "fingerprint": fingerprint,
        "last_seen_at": _utc_now(),
    }
    if existing:
        db.table("integration_record_links").update(payload).eq("id", existing["id"]).execute()
    else:
        db.table("integration_record_links").insert(payload).execute()


def _upsert_local_row(
    db: Client,
    *,
    org_id: str,
    table: str,
    connection: dict[str, Any],
    object_type: str,
    external_id: str,
    payload: dict[str, Any],
    fingerprint: str | None = None,
) -> str:
    link = _get_record_link(db, connection["id"], object_type, external_id)
    if link:
        db.table(table).update(payload).eq("id", link["local_id"]).eq("org_id", org_id).execute()
        local_id = link["local_id"]
    else:
        insert_result = db.table(table).insert({"org_id": org_id, **payload}).execute()
        local_id = insert_result.data[0]["id"]
    _upsert_record_link(
        db,
        org_id=org_id,
        connection_id=connection["id"],
        provider=connection["provider"],
        object_type=object_type,
        external_id=external_id,
        local_table=table,
        local_id=local_id,
        fingerprint=fingerprint,
    )
    return local_id


def _customer_link_from_external(
    db: Client,
    connection_id: str,
    object_type: str,
    external_id: str | None,
) -> str | None:
    if not external_id:
        return None
    link = _get_record_link(db, connection_id, object_type, external_id)
    if not link:
        return None
    return link["local_id"]


@dataclass
class BaseProvider:
    provider_key: str
    label: str
    category: str
    description: str
    required_credentials: tuple[str, ...] = ()
    oauth_supported: bool = False
    connection_mode: str = "manual"
    schema_mapping: list[dict[str, Any]] = field(default_factory=list)
    credential_fields: list[dict[str, Any]] = field(default_factory=list)

    def definition(self) -> dict[str, Any]:
        return {
            "key": self.provider_key,
            "label": self.label,
            "category": self.category,
            "description": self.description,
            "oauth_supported": self.oauth_supported,
            "connection_mode": self.connection_mode,
            "schema_mapping": self.schema_mapping,
            "credential_fields": self.credential_fields,
        }

    def validate_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        missing = [key for key in self.required_credentials if not credentials.get(key)]
        if missing:
            raise HTTPException(status_code=400, detail=f"Missing required credentials: {', '.join(missing)}")
        return credentials

    def build_oauth_authorization_url(self, org_id: str) -> str:
        raise HTTPException(status_code=400, detail=f"{self.label} does not support OAuth connect.")

    def exchange_oauth_code(
        self,
        db: Client,
        *,
        org_id: str,
        code: str,
        extra_query: dict[str, Any],
    ) -> dict[str, Any]:
        raise HTTPException(status_code=400, detail=f"{self.label} does not support OAuth connect.")

    def refresh_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        return credentials

    def sync(self, db: Client, connection: dict[str, Any], credentials: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class QuickBooksProvider(BaseProvider):
    def __init__(self):
        super().__init__(
            provider_key="quickbooks",
            label="QuickBooks Online",
            category="accounting",
            description="Sync customers, invoices, and expenses from QuickBooks Online into LBT OS.",
            required_credentials=("access_token", "realm_id"),
            oauth_supported=True,
            connection_mode="oauth",
            schema_mapping=[
                {"raw_object": "Customer", "mapped_table": "customers", "key_fields": ["DisplayName", "PrimaryEmailAddr.Address", "PrimaryPhone.FreeFormNumber"]},
                {"raw_object": "Invoice", "mapped_table": "sales", "key_fields": ["TotalAmt", "TxnDate", "CustomerRef.value"]},
                {"raw_object": "Purchase", "mapped_table": "expenses", "key_fields": ["TotalAmt", "TxnDate", "AccountRef.name"]},
            ],
        )

    def build_oauth_authorization_url(self, org_id: str) -> str:
        if not settings.quickbooks_client_id or not settings.quickbooks_redirect_uri:
            raise HTTPException(
                status_code=503,
                detail="QuickBooks OAuth is not configured on this server. Add QUICKBOOKS_CLIENT_ID and QUICKBOOKS_REDIRECT_URI to your environment.",
            )
        params = {
            "client_id": settings.quickbooks_client_id,
            "response_type": "code",
            "scope": "com.intuit.quickbooks.accounting",
            "redirect_uri": settings.quickbooks_redirect_uri,
            "state": build_oauth_state(org_id, self.provider_key),
        }
        return f"https://appcenter.intuit.com/connect/oauth2?{urlencode(params)}"

    def exchange_oauth_code(
        self,
        db: Client,
        *,
        org_id: str,
        code: str,
        extra_query: dict[str, Any],
    ) -> dict[str, Any]:
        token_response = httpx.post(
            "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
            auth=(settings.quickbooks_client_id or "", settings.quickbooks_client_secret or ""),
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.quickbooks_redirect_uri or "",
            },
            timeout=20,
        )
        token_response.raise_for_status()
        token_payload = token_response.json()
        credentials = {
            "access_token": token_payload["access_token"],
            "refresh_token": token_payload.get("refresh_token"),
            "realm_id": extra_query.get("realmId"),
            "token_type": token_payload.get("token_type"),
            "expires_at": int(datetime.now(timezone.utc).timestamp()) + int(token_payload.get("expires_in", 3600)),
            "refresh_expires_in": token_payload.get("x_refresh_token_expires_in"),
        }
        return upsert_oauth_connection(
            db,
            org_id=org_id,
            provider=self.provider_key,
            credentials=credentials,
            external_account_id=extra_query.get("realmId"),
            external_account_name=f"QuickBooks Company {extra_query.get('realmId')}" if extra_query.get("realmId") else None,
        )

    def refresh_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        if credentials.get("expires_at") and int(credentials["expires_at"]) - now_ts > 120:
            return credentials
        if not credentials.get("refresh_token"):
            return credentials
        response = httpx.post(
            "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
            auth=(settings.quickbooks_client_id or "", settings.quickbooks_client_secret or ""),
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": credentials["refresh_token"],
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        credentials.update({
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token", credentials["refresh_token"]),
            "expires_at": now_ts + int(payload.get("expires_in", 3600)),
            "refresh_expires_in": payload.get("x_refresh_token_expires_in", credentials.get("refresh_expires_in")),
        })
        return credentials

    def _base_url(self, credentials: dict[str, Any], endpoint: str) -> str:
        realm_id = credentials["realm_id"]
        sandbox = bool(credentials.get("sandbox"))
        host = "https://sandbox-quickbooks.api.intuit.com" if sandbox else "https://quickbooks.api.intuit.com"
        return f"{host}/v3/company/{realm_id}/{endpoint}"

    def _query(self, credentials: dict[str, Any], query: str) -> dict[str, Any]:
        response = httpx.get(
            self._base_url(credentials, "query"),
            headers={
                "Authorization": f"Bearer {credentials['access_token']}",
                "Accept": "application/json",
            },
            params={"query": query, "minorversion": credentials.get("minorversion", 75)},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def _fetch_entities(self, credentials: dict[str, Any], entity: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page_size = 1000
        start = 1
        while True:
            payload = self._query(
                credentials,
                f"select * from {entity} startposition {start} maxresults {page_size}",
            )
            batch = payload.get("QueryResponse", {}).get(entity) or []
            results.extend(batch)
            if len(batch) < page_size:
                break
            start += page_size
        return results

    def sync(self, db: Client, connection: dict[str, Any], credentials: dict[str, Any]) -> dict[str, Any]:
        customers = self._fetch_entities(credentials, "Customer")
        invoices = self._fetch_entities(credentials, "Invoice")
        purchases = self._fetch_entities(credentials, "Purchase")

        stats = {"customers": 0, "sales": 0, "expenses": 0}

        for customer in customers:
            display_name = customer.get("DisplayName") or customer.get("CompanyName") or customer.get("FullyQualifiedName")
            if not display_name:
                continue
            local_id = _upsert_local_row(
                db,
                org_id=connection["org_id"],
                table="customers",
                connection=connection,
                object_type="customer",
                external_id=str(customer["Id"]),
                payload={
                    "name": display_name,
                    "email": (customer.get("PrimaryEmailAddr") or {}).get("Address"),
                    "phone": (customer.get("PrimaryPhone") or {}).get("FreeFormNumber"),
                    "address": ", ".join(filter(None, [
                        (customer.get("BillAddr") or {}).get("Line1"),
                        (customer.get("BillAddr") or {}).get("City"),
                        (customer.get("BillAddr") or {}).get("CountrySubDivisionCode"),
                    ])) or None,
                    "notes": "Imported from QuickBooks Online.",
                },
                fingerprint=customer.get("SyncToken"),
            )
            stats["customers"] += 1 if local_id else 0

        for invoice in invoices:
            total = float(invoice.get("TotalAmt") or 0)
            if total <= 0:
                continue
            customer_id = _customer_link_from_external(
                db,
                connection["id"],
                "customer",
                (invoice.get("CustomerRef") or {}).get("value"),
            )
            first_line = (invoice.get("Line") or [{}])[0] or {}
            sold_at = invoice.get("TxnDate")
            if sold_at:
                sold_at = f"{sold_at}T00:00:00+00:00"
            _upsert_local_row(
                db,
                org_id=connection["org_id"],
                table="sales",
                connection=connection,
                object_type="invoice",
                external_id=str(invoice["Id"]),
                payload={
                    "customer_id": customer_id,
                    "service": first_line.get("Description") or invoice.get("DocNumber") or "QuickBooks Invoice",
                    "amount": total,
                    "cost": 0,
                    "payment_method": "bank_transfer",
                    "payment_status": "paid" if float(invoice.get("Balance") or 0) <= 0 else "pending",
                    "source": "quickbooks",
                    "invoice_number": invoice.get("DocNumber"),
                    "notes": "Imported from QuickBooks Online invoice.",
                    "sold_at": sold_at or _utc_now(),
                },
                fingerprint=invoice.get("SyncToken"),
            )
            stats["sales"] += 1

        for purchase in purchases:
            total = float(purchase.get("TotalAmt") or 0)
            if total <= 0:
                continue
            expense_date = purchase.get("TxnDate") or date.today().isoformat()
            vendor = (purchase.get("EntityRef") or {}).get("name")
            description = (purchase.get("PrivateNote") or purchase.get("DocNumber") or "QuickBooks expense").strip()
            account_name = (purchase.get("AccountRef") or {}).get("name") or description
            _upsert_local_row(
                db,
                org_id=connection["org_id"],
                table="expenses",
                connection=connection,
                object_type="purchase",
                external_id=str(purchase["Id"]),
                payload={
                    "category": _normalize_expense_category(account_name),
                    "description": description[:200],
                    "amount": total,
                    "vendor": vendor,
                    "is_recurring": False,
                    "expense_date": expense_date,
                },
                fingerprint=purchase.get("SyncToken"),
            )
            stats["expenses"] += 1

        return stats


class HubSpotProvider(BaseProvider):
    def __init__(self):
        super().__init__(
            provider_key="hubspot",
            label="HubSpot CRM",
            category="crm",
            description="Sync contacts and deals from HubSpot so audits can read pipeline activity automatically.",
            required_credentials=("access_token",),
            oauth_supported=True,
            connection_mode="oauth",
            schema_mapping=[
                {"raw_object": "contacts", "mapped_table": "leads", "key_fields": ["firstname", "lastname", "email", "phone"]},
                {"raw_object": "deals", "mapped_table": "leads/sales", "key_fields": ["dealname", "amount", "dealstage", "closedate"]},
            ],
        )

    def build_oauth_authorization_url(self, org_id: str) -> str:
        if not settings.hubspot_client_id or not settings.hubspot_redirect_uri:
            raise HTTPException(
                status_code=503,
                detail="HubSpot OAuth is not configured on this server. Add HUBSPOT_CLIENT_ID and HUBSPOT_REDIRECT_URI to your environment.",
            )
        params = {
            "client_id": settings.hubspot_client_id,
            "redirect_uri": settings.hubspot_redirect_uri,
            "scope": "oauth crm.objects.contacts.read crm.objects.deals.read",
            "state": build_oauth_state(org_id, self.provider_key),
        }
        return f"https://app.hubspot.com/oauth/authorize?{urlencode(params)}"

    def exchange_oauth_code(
        self,
        db: Client,
        *,
        org_id: str,
        code: str,
        extra_query: dict[str, Any],
    ) -> dict[str, Any]:
        token_response = httpx.post(
            "https://api.hubapi.com/oauth/v3/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "client_id": settings.hubspot_client_id or "",
                "client_secret": settings.hubspot_client_secret or "",
                "redirect_uri": settings.hubspot_redirect_uri or "",
                "code": code,
            },
            timeout=20,
        )
        token_response.raise_for_status()
        token_payload = token_response.json()
        credentials = {
            "access_token": token_payload["access_token"],
            "refresh_token": token_payload.get("refresh_token"),
            "hub_id": token_payload.get("hub_id"),
            "scopes": token_payload.get("scopes", []),
            "expires_at": int(datetime.now(timezone.utc).timestamp()) + int(token_payload.get("expires_in", 1800)),
        }
        return upsert_oauth_connection(
            db,
            org_id=org_id,
            provider=self.provider_key,
            credentials=credentials,
            external_account_id=str(token_payload.get("hub_id")) if token_payload.get("hub_id") else None,
            external_account_name=f"HubSpot Portal {token_payload.get('hub_id')}" if token_payload.get("hub_id") else None,
        )

    def refresh_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        if credentials.get("expires_at") and int(credentials["expires_at"]) - now_ts > 120:
            return credentials
        if not credentials.get("refresh_token"):
            return credentials
        response = httpx.post(
            "https://api.hubapi.com/oauth/v3/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "client_id": settings.hubspot_client_id or "",
                "client_secret": settings.hubspot_client_secret or "",
                "redirect_uri": settings.hubspot_redirect_uri or "",
                "refresh_token": credentials["refresh_token"],
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        credentials.update({
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token", credentials["refresh_token"]),
            "expires_at": now_ts + int(payload.get("expires_in", 1800)),
        })
        return credentials

    def _get(self, credentials: dict[str, Any], path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = httpx.get(
            f"https://api.hubapi.com{path}",
            headers={"Authorization": f"Bearer {credentials['access_token']}"},
            params=params or {},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def _fetch_objects(self, credentials: dict[str, Any], object_type: str, properties: list[str]) -> list[dict[str, Any]]:
        after = None
        results: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = {"limit": 100, "properties": ",".join(properties)}
            if after:
                params["after"] = after
            payload = self._get(credentials, f"/crm/v3/objects/{object_type}", params=params)
            results.extend(payload.get("results", []))
            after = payload.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        return results

    def _map_hubspot_status(self, dealstage: str | None) -> str:
        if not dealstage:
            return "new"
        stage = dealstage.lower()
        if "won" in stage:
            return "won"
        if "lost" in stage:
            return "lost"
        if "proposal" in stage or "quote" in stage:
            return "proposal"
        if "qualified" in stage:
            return "qualified"
        if "contact" in stage:
            return "contacted"
        return "new"

    def sync(self, db: Client, connection: dict[str, Any], credentials: dict[str, Any]) -> dict[str, Any]:
        contacts = self._fetch_objects(credentials, "contacts", ["firstname", "lastname", "email", "phone", "hs_analytics_source", "lifecyclestage"])
        deals = self._fetch_objects(credentials, "deals", ["dealname", "amount", "dealstage", "closedate", "pipeline", "hs_analytics_source"])
        stats = {"leads": 0, "sales": 0}

        for contact in contacts:
            props = contact.get("properties", {})
            full_name = " ".join(filter(None, [props.get("firstname"), props.get("lastname")])).strip() or props.get("email") or f"HubSpot Contact {contact['id']}"
            _upsert_local_row(
                db,
                org_id=connection["org_id"],
                table="leads",
                connection=connection,
                object_type="contact",
                external_id=str(contact["id"]),
                payload={
                    "name": full_name,
                    "email": props.get("email"),
                    "phone": props.get("phone"),
                    "source": (props.get("hs_analytics_source") or "website").lower()[:20],
                    "status": "new",
                    "notes": "Imported from HubSpot contact.",
                },
                fingerprint=contact.get("updatedAt"),
            )
            stats["leads"] += 1

        for deal in deals:
            props = deal.get("properties", {})
            status = self._map_hubspot_status(props.get("dealstage"))
            amount = float(props.get("amount") or 0)
            close_date = props.get("closedate")
            close_ts = close_date or _utc_now()
            if close_ts and close_ts.endswith("Z") is False and "T" in close_ts:
                close_ts = close_ts + "Z"
            if status == "won" and amount > 0:
                _upsert_local_row(
                    db,
                    org_id=connection["org_id"],
                    table="sales",
                    connection=connection,
                    object_type="deal_sale",
                    external_id=str(deal["id"]),
                    payload={
                        "service": props.get("dealname") or "HubSpot Deal",
                        "amount": amount,
                        "cost": 0,
                        "payment_status": "paid",
                        "source": (props.get("hs_analytics_source") or "hubspot").lower()[:20],
                        "invoice_number": str(deal["id"]),
                        "notes": "Imported from HubSpot closed-won deal.",
                        "sold_at": close_ts,
                    },
                    fingerprint=deal.get("updatedAt"),
                )
                stats["sales"] += 1
            else:
                _upsert_local_row(
                    db,
                    org_id=connection["org_id"],
                    table="leads",
                    connection=connection,
                    object_type="deal_lead",
                    external_id=str(deal["id"]),
                    payload={
                        "name": props.get("dealname") or f"HubSpot Deal {deal['id']}",
                        "source": (props.get("hs_analytics_source") or "hubspot").lower()[:20],
                        "status": status,
                        "estimated_value": amount if amount > 0 else None,
                        "notes": "Imported from HubSpot deal.",
                    },
                    fingerprint=deal.get("updatedAt"),
                )
                stats["leads"] += 1

        return stats


class StripeProvider(BaseProvider):
    def __init__(self):
        super().__init__(
            provider_key="stripe",
            label="Stripe",
            category="payments",
            description="Sync Stripe customers, charges, and processing fees into customers, sales, and expenses.",
            required_credentials=("api_key",),
            oauth_supported=False,
            connection_mode="manual",
            credential_fields=[
                {
                    "key": "api_key",
                    "label": "Secret or restricted API key",
                    "placeholder": "rk_live_... or sk_live_...",
                    "type": "password",
                    "required": True,
                    "help_text": "Use a read-only restricted key when possible.",
                },
                {
                    "key": "account_name",
                    "label": "Account label",
                    "placeholder": "Main Stripe account",
                    "type": "text",
                    "required": False,
                },
            ],
            schema_mapping=[
                {"raw_object": "customers", "mapped_table": "customers", "key_fields": ["name", "email", "phone"]},
                {"raw_object": "charges", "mapped_table": "sales", "key_fields": ["amount", "currency", "description", "created"]},
                {"raw_object": "balance_transaction.fee", "mapped_table": "expenses", "key_fields": ["fee", "type", "description"]},
            ],
        )

    def validate_credentials(self, credentials: dict[str, Any]) -> dict[str, Any]:
        validated = super().validate_credentials(credentials)
        api_key = str(validated.get("api_key") or "")
        if not api_key.startswith(("sk_", "rk_")):
            raise HTTPException(status_code=400, detail="Stripe credentials must use a secret or restricted API key.")
        return validated

    def _get(self, credentials: dict[str, Any], path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = httpx.get(
            f"https://api.stripe.com{path}",
            headers={"Authorization": f"Bearer {credentials['api_key']}"},
            params=params or {},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def _list_objects(self, credentials: dict[str, Any], path: str, *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        request_params = dict(params or {})
        while True:
            payload = self._get(credentials, path, params=request_params)
            data = payload.get("data", []) or []
            results.extend(data)
            if not payload.get("has_more") or not data:
                break
            request_params["starting_after"] = data[-1]["id"]
        return results

    def sync(self, db: Client, connection: dict[str, Any], credentials: dict[str, Any]) -> dict[str, Any]:
        customers = self._list_objects(credentials, "/v1/customers", params={"limit": 100})
        charges = self._list_objects(
            credentials,
            "/v1/charges",
            params={"limit": 100, "expand[]": "data.balance_transaction"},
        )
        stats = {"customers": 0, "sales": 0, "expenses": 0}

        for customer in customers:
            if customer.get("deleted"):
                continue
            name = customer.get("name") or customer.get("email") or f"Stripe Customer {customer['id']}"
            address = customer.get("address") or {}
            _upsert_local_row(
                db,
                org_id=connection["org_id"],
                table="customers",
                connection=connection,
                object_type="customer",
                external_id=str(customer["id"]),
                payload={
                    "name": name,
                    "email": customer.get("email"),
                    "phone": customer.get("phone"),
                    "address": ", ".join(filter(None, [address.get("line1"), address.get("city"), address.get("state")])),
                    "notes": "Imported from Stripe customer.",
                },
                fingerprint=str(customer.get("created")),
            )
            stats["customers"] += 1

        for charge in charges:
            if charge.get("status") != "succeeded":
                continue
            amount = float(charge.get("amount") or 0) / 100
            if amount <= 0:
                continue
            balance_tx = charge.get("balance_transaction") or {}
            created_at = datetime.fromtimestamp(charge.get("created") or int(datetime.now(timezone.utc).timestamp()), timezone.utc).isoformat()
            description = charge.get("description") or charge.get("statement_descriptor") or f"Stripe charge {charge['id']}"
            customer_id = _customer_link_from_external(db, connection["id"], "customer", charge.get("customer"))
            payment_method_details = charge.get("payment_method_details") or {}
            _upsert_local_row(
                db,
                org_id=connection["org_id"],
                table="sales",
                connection=connection,
                object_type="charge_sale",
                external_id=str(charge["id"]),
                payload={
                    "customer_id": customer_id,
                    "service": description[:120],
                    "amount": amount,
                    "cost": 0,
                    "payment_method": payment_method_details.get("type") or "card",
                    "payment_status": "paid",
                    "source": "stripe",
                    "invoice_number": charge.get("receipt_number") or charge["id"],
                    "notes": "Imported from Stripe charge.",
                    "sold_at": created_at,
                },
                fingerprint=str(charge.get("balance_transaction", {}).get("id") or charge.get("created")),
            )
            stats["sales"] += 1

            fee_amount = float(balance_tx.get("fee") or 0) / 100
            if fee_amount > 0:
                _upsert_local_row(
                    db,
                    org_id=connection["org_id"],
                    table="expenses",
                    connection=connection,
                    object_type="charge_fee",
                    external_id=f"{charge['id']}:fee",
                    payload={
                        "category": "processing_fees",
                        "description": f"Stripe processing fee for {description[:120]}",
                        "amount": fee_amount,
                        "vendor": "Stripe",
                        "is_recurring": False,
                        "expense_date": created_at[:10],
                    },
                    fingerprint=str(balance_tx.get("id") or charge.get("created")),
                )
                stats["expenses"] += 1

        return stats


PROVIDER_REGISTRY: dict[str, BaseProvider] = {
    "quickbooks": QuickBooksProvider(),
    "hubspot": HubSpotProvider(),
    "stripe": StripeProvider(),
}
SUPPORTED_PROVIDERS: dict[str, dict[str, Any]] = {
    key: provider.definition() for key, provider in PROVIDER_REGISTRY.items()
}


def delete_connection(db: Client, org_id: str, connection_id: str) -> None:
    """
    Disconnect and remove a connection record.
    Imported data (leads, customers, sales, expenses) is preserved.
    Only the connection record and its record-links are removed.
    """
    get_connection(db, org_id, connection_id)  # confirms ownership — raises 404 if not found
    db.table("integration_record_links").delete().eq("connection_id", connection_id).execute()
    db.table("integration_sync_runs").delete().eq("connection_id", connection_id).execute()
    db.table("integration_connections").delete().eq("id", connection_id).eq("org_id", org_id).execute()


def run_connection_sync(
    db: Client,
    org_id: str,
    connection_id: str,
    *,
    trigger_source: str = "manual",
) -> dict[str, Any]:
    connection = get_connection(db, org_id, connection_id)
    provider = PROVIDER_REGISTRY.get(connection["provider"])
    if provider is None:
        raise HTTPException(status_code=400, detail=f"No sync provider registered for '{connection['provider']}'.")
    credentials = _decrypt_credentials(connection.get("credentials_encrypted"))
    credentials = provider.refresh_credentials(credentials)
    db.table("integration_connections").update({
        "credentials_encrypted": _encrypt_credentials(credentials),
    }).eq("id", connection["id"]).execute()
    sync_run = _record_sync_run_start(db, connection, trigger_source)
    try:
        stats = provider.sync(db, connection, credentials)
        status = "success" if stats else "partial"
        return _record_sync_run_finish(db, connection, sync_run["id"], status=status, stats=stats)
    except httpx.HTTPStatusError as exc:
        detail = f"{exc.response.status_code} {exc.response.text[:500]}"
        return _record_sync_run_finish(db, connection, sync_run["id"], status="failed", stats={}, error=detail)
    except Exception as exc:
        return _record_sync_run_finish(db, connection, sync_run["id"], status="failed", stats={}, error=str(exc))


def sync_all_connections_for_org(db: Client, org_id: str, *, trigger_source: str = "manual") -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for connection in list_connections(db, org_id):
        if connection.get("status") == "disconnected":
            continue
        runs.append(run_connection_sync(db, org_id, connection["id"], trigger_source=trigger_source))
    return runs


def get_integration_overview(db: Client, org_id: str, *, sync_limit: int = 20, import_limit: int = 20) -> dict[str, Any]:
    from .manual_import import list_import_history

    connections = [sanitize_connection(row) for row in list_connections(db, org_id)]
    sync_runs = list_sync_runs(db, org_id, limit=sync_limit)
    import_history = list_import_history(db, org_id, limit=import_limit)

    runs_by_connection: dict[str, list[dict[str, Any]]] = {}
    for run in sync_runs:
        runs_by_connection.setdefault(run["connection_id"], []).append(run)

    enriched_connections: list[dict[str, Any]] = []
    for connection in connections:
        provider = PROVIDER_REGISTRY.get(connection["provider"])
        runs = runs_by_connection.get(connection["id"], [])
        latest_run = runs[0] if runs else None
        last_successful_run = next((run for run in runs if run.get("status") in {"success", "partial"}), None)
        enriched_connections.append({
            **connection,
            "provider_label": provider.label if provider else connection["provider"],
            "connection_mode": provider.connection_mode if provider else "manual",
            "latest_run": latest_run,
            "last_successful_run": last_successful_run,
        })

    return {
        "providers": list_provider_definitions(),
        "connections": enriched_connections,
        "sync_runs": sync_runs,
        "import_history": import_history,
        "summary": {
            "connections_total": len(connections),
            "connections_healthy": sum(1 for c in connections if c.get("last_sync_status") in {"success", "partial"} or c.get("status") == "connected"),
            "connections_failing": sum(1 for c in connections if c.get("last_sync_status") == "failed" or c.get("status") == "error"),
            "recent_failures": sum(1 for run in sync_runs if run.get("status") == "failed"),
            "last_successful_sync_at": next((run.get("finished_at") for run in sync_runs if run.get("status") in {"success", "partial"}), None),
        },
    }
