import csv
import io
from datetime import date
from pathlib import PurePath
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import RedirectResponse, StreamingResponse

from ..auth import AuthContext, get_auth, get_clerk_user_email, require_plan
from ..database import get_db
from ..models.integration import (
    IntegrationConnectionCreate,
    IntegrationConnectionOut,
    IntegrationConnectionUpdate,
    IntegrationSyncRunOut,
)
from ..services.data_export import build_workspace_zip
from ..services.email import send_sync_complete
from ..services.integrations import (
    SUPPORTED_PROVIDERS,
    create_connection,
    delete_connection,
    exchange_oauth_code,
    get_connection,
    get_frontend_connection_callback,
    get_integration_overview,
    get_oauth_authorization_url,
    list_connections,
    list_provider_definitions,
    list_sync_runs,
    run_connection_sync,
    sanitize_connection,
    sync_all_connections_for_org,
    update_connection,
)
from ..services.manual_import import import_csv_rows, list_import_history
from ..services.templates import get_template

router = APIRouter(prefix="/integrations", tags=["integrations"])
ALLOWED_CSV_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "text/plain",
}


@router.get("/providers")
def providers():
    return list_provider_definitions()


@router.post("/oauth/{provider}/start")
def oauth_start(provider: str, auth: Annotated[AuthContext, Depends(get_auth)]):
    return {"authorization_url": get_oauth_authorization_url(provider, auth.org_id)}


@router.get("/oauth/{provider}/callback", include_in_schema=False)
def oauth_callback(
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    realmId: str | None = None,
):
    if error:
        return RedirectResponse(get_frontend_connection_callback(provider, "error", error_description or error))
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state.")
    db = get_db()
    try:
        exchange_oauth_code(db, provider, code, state, {"realmId": realmId})
    except Exception as exc:
        return RedirectResponse(get_frontend_connection_callback(provider, "error", str(exc)))
    return RedirectResponse(get_frontend_connection_callback(provider, "connected"))


@router.get("/connections", response_model=list[IntegrationConnectionOut])
def connections(auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    return [sanitize_connection(row) for row in list_connections(db, auth.org_id)]


@router.get("/overview")
def integration_overview(
    auth: Annotated[AuthContext, Depends(get_auth)],
    sync_limit: int = Query(20, ge=1, le=100),
    import_limit: int = Query(20, ge=1, le=100),
):
    db = get_db()
    return get_integration_overview(db, auth.org_id, sync_limit=sync_limit, import_limit=import_limit)


@router.post("/connections", response_model=IntegrationConnectionOut, status_code=201)
def add_connection(
    body: IntegrationConnectionCreate,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    db = get_db()
    return create_connection(
        db,
        auth.org_id,
        provider=body.provider,
        credentials=body.credentials,
        config=body.config,
        label=body.label,
        external_account_id=body.external_account_id,
        external_account_name=body.external_account_name,
    )


@router.patch("/connections/{connection_id}", response_model=IntegrationConnectionOut)
def patch_connection(
    connection_id: str,
    body: IntegrationConnectionUpdate,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    db = get_db()
    return update_connection(
        db,
        auth.org_id,
        connection_id,
        label=body.label,
        credentials=body.credentials,
        config=body.config,
        status=body.status,
        external_account_id=body.external_account_id,
        external_account_name=body.external_account_name,
    )


@router.get("/connections/{connection_id}", response_model=IntegrationConnectionOut)
def connection_detail(connection_id: str, auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    return sanitize_connection(get_connection(db, auth.org_id, connection_id))


@router.post("/connections/{connection_id}/sync", response_model=IntegrationSyncRunOut)
def sync_connection(
    connection_id: str,
    auth: Annotated[AuthContext, Depends(get_auth)],
    background_tasks: BackgroundTasks,
):
    db  = get_db()
    run = run_connection_sync(db, auth.org_id, connection_id, trigger_source="manual")
    background_tasks.add_task(_email_sync_complete, auth.user_id, run)
    return run


@router.post("/sync-all", response_model=list[IntegrationSyncRunOut])
def sync_all(
    auth: Annotated[AuthContext, Depends(get_auth)],
    background_tasks: BackgroundTasks,
):
    db   = get_db()
    runs = sync_all_connections_for_org(db, auth.org_id, trigger_source="manual")
    for run in runs:
        background_tasks.add_task(_email_sync_complete, auth.user_id, run)
    return runs


def _email_sync_complete(user_id: str, run: dict) -> None:
    email = get_clerk_user_email(user_id)
    if not email:
        return
    provider_key   = run.get("provider", "")
    provider_label = SUPPORTED_PROVIDERS.get(provider_key, {}).get("label", provider_key.title())
    # Look up org name via db would require a db call here; use a short label instead
    send_sync_complete(
        to=email,
        org_name="Your workspace",
        provider_label=provider_label,
        stats=run.get("stats") or {},
        status=run.get("status", "unknown"),
    )


@router.get("/sync-runs", response_model=list[IntegrationSyncRunOut])
def sync_runs(
    auth: Annotated[AuthContext, Depends(get_auth)],
    limit: int = Query(20, ge=1, le=100),
):
    db = get_db()
    return list_sync_runs(db, auth.org_id, limit=limit)


@router.post("/manual-import")
async def manual_import_csv(
    auth: Annotated[AuthContext, Depends(get_auth)],
    entity_type: str = Query(..., pattern="^(leads|customers|sales|expenses)$"),
    file: UploadFile = File(...),
):
    filename = (file.filename or "").strip()
    if PurePath(filename).suffix.lower() != ".csv":
        raise HTTPException(status_code=400, detail="Please upload a CSV file.")
    if file.content_type and file.content_type.lower() not in ALLOWED_CSV_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Uploaded file must have a CSV content type.")
    db = get_db()
    return await import_csv_rows(db, auth.org_id, entity_type, file, filename=filename)


@router.get("/import-history")
def import_history(
    auth: Annotated[AuthContext, Depends(get_auth)],
    limit: int = Query(30, ge=1, le=100),
):
    db = get_db()
    return list_import_history(db, auth.org_id, limit=limit)


@router.get("/import-template/{entity_type}")
def download_import_template(
    entity_type: str,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    """Return a pre-filled CSV template file tailored to the org's industry."""
    if entity_type not in ("leads", "customers", "sales", "expenses"):
        raise HTTPException(status_code=400, detail=f"Unsupported entity type '{entity_type}'.")

    db = get_db()
    org_result = db.table("organizations").select("industry").eq("id", auth.org_id).single().execute()
    industry = org_result.data.get("industry") if org_result.data else None
    template = get_template(industry)

    services = (template.get("services") or ["Service A", "Service B"])[:3] if template else ["Service A", "Service B"]
    lead_sources = (template.get("lead_sources") or ["referral", "google"])[:3] if template else ["referral", "google"]
    expense_categories = (template.get("expense_categories") or ["materials", "marketing"])[:3] if template else ["materials", "marketing"]
    customers = (template.get("sample_customers") or ["Acme Corp", "Peak Co"])[:3] if template else ["Acme Corp", "Peak Co"]
    vendors = (template.get("sample_vendors") or ["Supply Co", "Materials Depot"])[:2] if template else ["Supply Co", "Materials Depot"]
    today = date.today().isoformat()

    TEMPLATES_BY_TYPE: dict[str, tuple[list[str], list[list[str]]]] = {
        "leads": (
            ["name", "email", "phone", "source", "status", "service_interest", "estimated_value", "notes"],
            [
                ["Jane Smith", "jane@example.com", "303-555-0101", lead_sources[0], "new", services[0], "4500", "Interested in spring service"],
                ["Carlos Ruiz", "carlos@example.com", "720-555-0202", lead_sources[1 % len(lead_sources)], "contacted", services[1 % len(services)], "8200", "Needs quote by end of month"],
                ["Morgan Lee", "", "303-555-0303", "referral", "qualified", services[2 % len(services)], "12000", "Referred by existing customer"],
            ],
        ),
        "customers": (
            ["name", "email", "phone", "address", "notes"],
            [
                [customers[0], "contact@example.com", "303-555-1001", "100 Main St, Denver, CO 80203", "Long-term customer"],
                [customers[1 % len(customers)], "info@example.com", "720-555-1002", "200 Park Ave, Denver, CO 80205", "Prefers email contact"],
                [customers[2 % len(customers)], "ops@example.com", "303-555-1003", "300 Blake St, Denver, CO 80205", ""],
            ],
        ),
        "sales": (
            ["service", "amount", "cost", "payment_method", "payment_status", "source", "invoice_number", "sold_at", "notes"],
            [
                [services[0], "5800.00", "2100.00", "card", "paid", lead_sources[0], "INV-001", today, ""],
                [services[1 % len(services)], "12500.00", "4800.00", "bank_transfer", "paid", lead_sources[1 % len(lead_sources)], "INV-002", today, "Seasonal job"],
                [services[2 % len(services)], "3200.00", "1100.00", "check", "pending", "referral", "INV-003", today, "Net 30"],
            ],
        ),
        "expenses": (
            ["category", "description", "amount", "vendor", "expense_date", "is_recurring", "recurrence_period"],
            [
                [expense_categories[0], f"{expense_categories[0].title()} purchase", "1200.00", vendors[0], today, "false", ""],
                [expense_categories[1 % len(expense_categories)], "Monthly marketing spend", "450.00", vendors[1 % len(vendors)], today, "true", "monthly"],
                ["insurance", "General liability premium", "325.00", "State Farm", today, "true", "monthly"],
            ],
        ),
    }

    headers, rows = TEMPLATES_BY_TYPE[entity_type]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    csv_bytes = output.getvalue().encode("utf-8")

    filename = f"lbt-{entity_type}-template.csv"
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/connections/{connection_id}", status_code=204)
def disconnect_connection(
    connection_id: str,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    """
    Remove a connection and its sync history.
    Imported data (leads/customers/sales/expenses) is preserved.
    """
    db = get_db()
    delete_connection(db, auth.org_id, connection_id)


@router.get("/export/workspace.zip")
def export_workspace(auth: Annotated[AuthContext, Depends(get_auth)]):
    """Download all org data as a ZIP of CSVs for data portability."""
    db  = get_db()
    raw = build_workspace_zip(db, auth.org_id)
    return StreamingResponse(
        io.BytesIO(raw),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="workspace-export.zip"'},
    )


@router.post("/recurring-scan", dependencies=[Depends(require_plan("pro"))])
def schedule_recurring_scan(_auth: Annotated[AuthContext, Depends(get_auth)]):
    return {
        "enabled": False,
        "plan_required": "pro",
        "message": "Recurring scan scheduling is reserved for Pro and will be activated once background jobs are connected.",
    }
