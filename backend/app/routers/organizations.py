from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from ..auth import AuthContext, UserContext, get_auth, get_user
from ..database import get_db
from ..models.organization import (
    DemoBootstrapRequest,
    DemoReseedRequest,
    OrgCreate,
    OrgUpdate,
)
from ..services.demo_data import (
    bootstrap_demo_org,
    reset_org_operating_data,
    seed_org_data,
)
from ..services.templates import get_template, list_templates
from ..services.workspace import get_workspace_status_payload

router = APIRouter(prefix="/organizations", tags=["organizations"])


def _safe_count(db, table: str, org_id: str) -> int:
    try:
        return len(db.table(table).select("id").eq("org_id", org_id).limit(1).execute().data or [])
    except Exception as exc:
        if "Could not find the table" in str(exc):
            return 0
        raise


@router.post("", status_code=201)
def create_organization(
    body: OrgCreate,
    user: Annotated[UserContext, Depends(get_user)],
):
    """
    Called once during onboarding. Creates the org keyed to the Clerk user.
    Idempotent — returns existing org if already created.
    """
    db = get_db()
    existing = (
        db.table("organizations")
        .select("*")
        .eq("clerk_user_id", user.user_id)
        .maybe_single()
        .execute()
    )
    if existing is not None and existing.data:
        return existing.data

    result = db.table("organizations").insert({
        "clerk_user_id": user.user_id,
        "clerk_org_id":  user.user_id,
        "name":          body.name,
        "industry":      body.industry,
        "city":          body.city,
        "state":         body.state,
    }).execute()
    return result.data[0]


@router.get("/me")
def get_my_org(auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    result = db.table("organizations").select("*").eq("id", auth.org_id).single().execute()
    return result.data


@router.patch("/me")
def update_my_org(
    body: OrgUpdate,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    db = get_db()
    update_data = body.model_dump(exclude_none=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update.")
    result = db.table("organizations").update(update_data).eq("id", auth.org_id).execute()
    return result.data[0]


@router.get("/templates")
def get_templates():
    return list_templates()


@router.get("/templates/{industry}")
def get_industry_template(industry: str):
    template = get_template(industry)
    if not template:
        raise HTTPException(status_code=404, detail=f"No template for industry '{industry}'")
    return template


@router.post("/bootstrap-demo", status_code=201)
def bootstrap_demo(
    body: DemoBootstrapRequest,
    user: Annotated[UserContext, Depends(get_user)],
):
    template = get_template(body.industry)
    if not template:
        raise HTTPException(status_code=404, detail=f"No template for industry '{body.industry}'")

    db = get_db()
    existing = (
        db.table("organizations")
        .select("id, name")
        .eq("clerk_user_id", user.user_id)
        .maybe_single()
        .execute()
    )
    if existing is not None and existing.data:
        org_id = existing.data["id"]
        activity_counts = {
            "leads": _safe_count(db, "leads", org_id),
            "customers": _safe_count(db, "customers", org_id),
            "sales": _safe_count(db, "sales", org_id),
            "expenses": _safe_count(db, "expenses", org_id),
            "connections": _safe_count(db, "integration_connections", org_id),
        }
        has_existing_activity = any(activity_counts.values())
        if has_existing_activity and not body.replace_existing:
            raise HTTPException(
                status_code=409,
                detail="This workspace already has data. Confirm replacement before loading demo records.",
            )

    demo_name = body.name or template.get("sample_org_names", [template["label"]])[0]
    result = bootstrap_demo_org(
        db,
        user_id=user.user_id,
        name=demo_name,
        industry=body.industry,
        city=body.city,
        state=body.state,
        seed=body.seed,
    )
    return result


@router.get("/workspace-status")
def get_workspace_status(auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    payload = get_workspace_status_payload(db, auth.org_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return payload


@router.post("/demo/reseed")
def reseed_demo_workspace(
    body: DemoReseedRequest,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    db = get_db()
    status_payload = get_workspace_status_payload(db, auth.org_id)
    if not status_payload:
        raise HTTPException(status_code=404, detail="Organization not found.")
    if status_payload["has_connections"]:
        raise HTTPException(status_code=409, detail="Disconnect live data sources before reseeding demo data.")

    industry = body.industry or status_payload["organization"].get("industry")
    if industry:
        db.table("organizations").update({"industry": industry}).eq("id", auth.org_id).execute()
    reset_org_operating_data(db, auth.org_id)
    summary = seed_org_data(db, auth.org_id, industry, seed=body.seed)
    return {"workspace_mode": "demo", "seed_summary": summary}


@router.post("/demo/clear")
def clear_demo_workspace(auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    status_payload = get_workspace_status_payload(db, auth.org_id)
    if not status_payload:
        raise HTTPException(status_code=404, detail="Organization not found.")
    if status_payload["has_connections"]:
        raise HTTPException(status_code=409, detail="Disconnect live data sources before clearing demo data.")

    reset_org_operating_data(db, auth.org_id)
    return {"workspace_mode": "blank", "cleared": True}
