from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import AuthContext, get_auth
from ..database import get_db
from ..models.lead import LeadCreate, LeadOut, LeadUpdate

router = APIRouter(prefix="/leads", tags=["leads"])


@router.get("", response_model=list[LeadOut])
def list_leads(
    auth: Annotated[AuthContext, Depends(get_auth)],
    status: str | None = Query(None),
    source: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    db = get_db()
    query = (
        db.table("leads")
        .select("*")
        .eq("org_id", auth.org_id)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if status:
        query = query.eq("status", status)
    if source:
        query = query.eq("source", source)

    return query.execute().data


@router.post("", response_model=LeadOut, status_code=201)
def create_lead(body: LeadCreate, auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    data = body.model_dump(exclude_none=True)
    data["org_id"] = auth.org_id
    result = db.table("leads").insert(data).execute()
    return result.data[0]


@router.get("/{lead_id}", response_model=LeadOut)
def get_lead(lead_id: str, auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    result = (
        db.table("leads")
        .select("*")
        .eq("id", lead_id)
        .eq("org_id", auth.org_id)   # tenant isolation
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Lead not found.")
    return result.data


@router.patch("/{lead_id}", response_model=LeadOut)
def update_lead(
    lead_id: str,
    body: LeadUpdate,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    from datetime import datetime, timezone
    db = get_db()
    update_data = body.model_dump(exclude_none=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update.")

    # Capture old status for history before updating
    old_status = None
    new_status = update_data.get("status")
    if new_status:
        existing = (
            db.table("leads")
            .select("status")
            .eq("id", lead_id)
            .eq("org_id", auth.org_id)
            .maybe_single()
            .execute()
        )
        if existing.data:
            old_status = existing.data.get("status")

    # Serialize datetimes to ISO strings for Supabase
    for k, v in update_data.items():
        if hasattr(v, "isoformat"):
            update_data[k] = v.isoformat()

    now = datetime.now(timezone.utc).isoformat()
    if new_status and new_status != old_status:
        update_data["stage_changed_at"] = now

    result = (
        db.table("leads")
        .update(update_data)
        .eq("id", lead_id)
        .eq("org_id", auth.org_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Lead not found.")

    if new_status and new_status != old_status:
        try:
            db.table("lead_stage_history").insert({
                "org_id":      auth.org_id,
                "lead_id":     lead_id,
                "from_status": old_status,
                "to_status":   new_status,
                "changed_by":  auth.user_id,
                "changed_at":  now,
            }).execute()
        except Exception:
            pass  # history is best-effort — don't fail the update

    return result.data[0]


@router.delete("/{lead_id}", status_code=204)
def delete_lead(lead_id: str, auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    db.table("leads").delete().eq("id", lead_id).eq("org_id", auth.org_id).execute()


@router.post("/{lead_id}/convert", response_model=dict)
def convert_lead_to_customer(lead_id: str, auth: Annotated[AuthContext, Depends(get_auth)]):
    """
    Mark a lead as won and create a customer record.
    Returns the new customer.
    """
    from datetime import datetime, timezone
    db = get_db()

    lead = (
        db.table("leads")
        .select("*")
        .eq("id", lead_id)
        .eq("org_id", auth.org_id)
        .maybe_single()
        .execute()
    )
    if not lead.data:
        raise HTTPException(status_code=404, detail="Lead not found.")
    if lead.data["status"] == "won":
        raise HTTPException(status_code=400, detail="Lead already converted.")

    now = datetime.now(timezone.utc).isoformat()
    db.table("leads").update({"status": "won", "converted_at": now}).eq("id", lead_id).execute()

    customer = db.table("customers").insert({
        "org_id":  auth.org_id,
        "lead_id": lead_id,
        "name":    lead.data["name"],
        "email":   lead.data.get("email"),
        "phone":   lead.data.get("phone"),
    }).execute()

    return {"customer": customer.data[0]}
