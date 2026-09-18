"""
Admin router — platform management endpoints.

Access is restricted to Clerk user IDs listed in ADMIN_USER_IDS env var.
All endpoints require a valid Clerk JWT plus admin authorization.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..auth import _verify_clerk_jwt
from ..config import settings
from ..database import get_db

router = APIRouter(prefix="/admin", tags=["admin"])
bearer_scheme = HTTPBearer()


def _admin_user_ids() -> set[str]:
    raw = settings.admin_user_ids.strip()
    if not raw:
        return set()
    return {uid.strip() for uid in raw.split(",") if uid.strip()}


def _safe_admin_query(execute_fn, default):
    try:
        result = execute_fn()
        return result.data or default
    except Exception as exc:
        if "Could not find the table" in str(exc):
            return default
        raise


async def get_admin_auth(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
) -> dict:
    payload = _verify_clerk_jwt(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing sub claim.")
    if user_id not in _admin_user_ids():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return {"user_id": user_id}


# ---------- /admin/me ----------

@router.get("/me")
async def admin_me(admin: Annotated[dict, Depends(get_admin_auth)]):
    """Check whether the current user has admin access."""
    return {"is_admin": True, "user_id": admin["user_id"]}


# ---------- /admin/stats ----------

@router.get("/stats")
async def admin_stats(admin: Annotated[dict, Depends(get_admin_auth)]):
    """Platform-wide aggregate stats."""
    db = get_db()

    orgs = db.table("organizations").select("id, plan, subscription_status, created_at").execute()
    rows = orgs.data or []

    plan_counts: dict[str, int] = {}
    for row in rows:
        plan = row.get("plan", "basic") or "basic"
        plan_counts[plan] = plan_counts.get(plan, 0) + 1

    # Orgs created in the last 30 days
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    recent = sum(1 for r in rows if (r.get("created_at") or "") >= cutoff)

    # Count active integrations
    conn_rows = _safe_admin_query(
        lambda: db.table("integration_connections").select("id, status").execute(),
        [],
    )
    active_conns = sum(1 for c in conn_rows if c.get("status") == "connected")

    # Count total audits run
    audits = db.table("audit_reports").select("id").execute()
    total_audits = len(audits.data or [])

    return {
        "total_organizations": len(rows),
        "plan_breakdown": plan_counts,
        "new_orgs_last_30_days": recent,
        "active_integrations": active_conns,
        "total_audits_run": total_audits,
    }


@router.get("/integrations/health")
async def admin_integration_health(
    admin: Annotated[dict, Depends(get_admin_auth)],
    limit: int = Query(20, ge=1, le=100),
):
    db = get_db()
    connections = _safe_admin_query(
        lambda: db.table("integration_connections").select(
            "id, org_id, provider, label, status, last_synced_at, last_sync_status, last_sync_error, external_account_name, updated_at"
        ).order("updated_at", desc=True).limit(limit * 3).execute(),
        [],
    )
    recent_runs = _safe_admin_query(
        lambda: db.table("integration_sync_runs").select(
            "id, org_id, connection_id, provider, trigger_source, status, stats, error, started_at, finished_at"
        ).order("started_at", desc=True).limit(limit).execute(),
        [],
    )
    recent_failures = [run for run in recent_runs if run.get("status") == "failed"][:limit]

    provider_breakdown: dict[str, dict[str, int]] = {}
    for connection in connections:
        provider = connection.get("provider") or "unknown"
        bucket = provider_breakdown.setdefault(provider, {"total": 0, "connected": 0, "error": 0, "disconnected": 0})
        bucket["total"] += 1
        status_key = connection.get("status") or "connected"
        if status_key not in bucket:
            bucket[status_key] = 0
        bucket[status_key] += 1

    unhealthy_connections = [
        connection
        for connection in connections
        if connection.get("status") == "error" or connection.get("last_sync_status") == "failed"
    ][:limit]

    return {
        "provider_breakdown": provider_breakdown,
        "recent_runs": recent_runs,
        "recent_failures": recent_failures,
        "unhealthy_connections": unhealthy_connections,
    }


# ---------- /admin/organizations ----------

@router.get("/organizations")
async def admin_list_organizations(
    admin: Annotated[dict, Depends(get_admin_auth)],
    search: str | None = Query(None),
    plan: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List all organizations with optional filters."""
    db = get_db()
    # fix #8: clerk_user_id is PII — omit from the list view
    query = db.table("organizations").select(
        "id, name, industry, city, state, plan, subscription_status, created_at"
    )
    if plan:
        query = query.eq("plan", plan)
    if search:
        query = query.ilike("name", f"%{search}%")

    result = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    return {"organizations": result.data or [], "total": len(result.data or [])}


@router.get("/organizations/{org_id}")
async def admin_get_organization(
    org_id: str,
    admin: Annotated[dict, Depends(get_admin_auth)],
):
    """Get a single organization with full detail."""
    db = get_db()
    result = (
        db.table("organizations")
        .select("*")
        .eq("id", org_id)
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return result.data


@router.patch("/organizations/{org_id}/plan")
async def admin_update_org_plan(
    org_id: str,
    body: dict,
    admin: Annotated[dict, Depends(get_admin_auth)],
):
    """Manually override an organization's plan."""
    new_plan = body.get("plan")
    if new_plan not in ("basic", "pro", "premium", "enterprise"):
        raise HTTPException(status_code=400, detail="plan must be basic, pro, premium, or enterprise.")
    db = get_db()
    old_org = (
        db.table("organizations")
        .select("plan")
        .eq("id", org_id)
        .maybe_single()
        .execute()
    )
    old_plan = old_org.data.get("plan") if old_org.data else None
    result = (
        db.table("organizations")
        .update({"plan": new_plan})
        .eq("id", org_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Organization not found.")
    try:
        from datetime import datetime, timezone
        db.table("plan_audit_log").insert({
            "org_id":     org_id,
            "changed_by": admin["user_id"],
            "from_plan":  old_plan,
            "to_plan":    new_plan,
            "changed_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:
        pass
    return {"ok": True, "org_id": org_id, "plan": new_plan}


@router.patch("/organizations/{org_id}/status")
async def admin_update_org_status(
    org_id: str,
    body: dict,
    admin: Annotated[dict, Depends(get_admin_auth)],
):
    """Override subscription_status for an organization."""
    new_status = body.get("subscription_status")
    if new_status not in ("active", "trialing", "past_due", "canceled", "unpaid"):
        raise HTTPException(status_code=400, detail="Invalid subscription_status value.")
    db = get_db()
    result = (
        db.table("organizations")
        .update({"subscription_status": new_status})
        .eq("id", org_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return {"ok": True, "org_id": org_id, "subscription_status": new_status}
