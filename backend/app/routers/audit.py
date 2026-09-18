from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from ..auth import AuthContext, get_auth, get_clerk_user_email, require_plan
from ..config import settings
from ..database import get_db
from ..limiter import enforce_user_limit, limiter
from ..services.ai_audit import (
    PLAN_AUDIT_LIMITS,
    count_audits_this_month,
    get_latest_audit,
    run_audit,
)
from ..services.audit_pdf import generate_audit_pdf
from ..services.email import send_audit_complete

router = APIRouter(prefix="/audit", tags=["ai-audit"])

# Plan labels for upsell messaging
_NEXT_PLAN = {"basic": "Growth ($129/mo)", "pro": "Scale ($299/mo)"}
_PLAN_NAMES = {"basic": "Starter", "pro": "Growth", "premium": "Scale", "enterprise": "Enterprise"}


def _audit_limit_detail(plan: str, cap: int) -> str:
    next_plan = _NEXT_PLAN.get(plan, "a higher tier")
    return (
        f"{_PLAN_NAMES.get(plan, plan.title())} plan includes {cap} AI audit{'s' if cap != 1 else ''}/month. "
        f"Upgrade to {next_plan} for more audits and full recommendations."
    )


@router.post("/run")
@limiter.limit(settings.audit_rate_limit)
def trigger_audit(
    request: Request,
    auth: Annotated[AuthContext, Depends(get_auth)],
    _user_limit: Annotated[None, Depends(enforce_user_limit(settings.audit_rate_limit))],  # TW-078: per-verified-user
    background_tasks: BackgroundTasks,
):
    """
    Trigger a fresh AI audit.

    - basic:      3 audits/month, truncated report (3 insights, no recs)
    - pro:        20 audits/month, full report via Ollama/OpenAI mini
    - premium:    unlimited, full report via GPT-4o
    - enterprise: unlimited, full report via GPT-4o
    """
    db = get_db()

    cap = PLAN_AUDIT_LIMITS.get(auth.plan)   # None means unlimited
    if cap is not None and not settings.should_bypass_audit_monthly_limit:
        used = count_audits_this_month(db, auth.org_id)
        if used >= cap:
            raise HTTPException(
                status_code=402,
                detail=_audit_limit_detail(auth.plan, cap),
            )

    org = db.table("organizations").select("name, industry").eq("id", auth.org_id).single().execute()
    if not org.data:
        raise HTTPException(status_code=404, detail="Organization not found.")

    report = run_audit(
        db=db,
        org_id=auth.org_id,
        org_name=org.data["name"],
        industry=org.data.get("industry"),
        plan=auth.plan,
    )

    # Send audit-complete email in the background (non-blocking)
    org_name = org.data["name"]
    user_id  = auth.user_id
    background_tasks.add_task(_email_audit_complete, user_id, org_name, report)

    return report


def _email_audit_complete(user_id: str, org_name: str, report: dict) -> None:
    email = get_clerk_user_email(user_id)
    if email:
        send_audit_complete(to=email, org_name=org_name, report=report)


@router.get("/latest")
def get_latest(auth: Annotated[AuthContext, Depends(get_auth)]):
    """Return the most recent audit. Truncated for free tier."""
    db = get_db()
    report = get_latest_audit(db, auth.org_id, plan=auth.plan)
    if not report:
        raise HTTPException(
            status_code=404,
            detail="No audit report yet. Run your first AI audit to get insights.",
        )
    return report


@router.get("/history")
def audit_history(
    auth: Annotated[AuthContext, Depends(get_auth)],
    limit: int = Query(10, ge=1, le=50),
):
    """Return past audit summaries for trend tracking. Growth plan and above."""
    if auth.plan == "basic":
        raise HTTPException(
            status_code=402,
            detail=_audit_limit_detail("basic", PLAN_AUDIT_LIMITS["basic"]),
        )
    db = get_db()
    result = (
        db.table("audit_reports")
        .select("id, period_start, period_end, health_score, generated_at, model_used")
        .eq("org_id", auth.org_id)
        .order("generated_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data


@router.get("/latest/export.pdf", dependencies=[Depends(require_plan("pro"))])
def export_latest_pdf(auth: Annotated[AuthContext, Depends(get_auth)]):
    """
    Download the latest audit as a branded PDF.
    Requires Pro or Premium plan.
    """
    db = get_db()
    report = get_latest_audit(db, auth.org_id, plan=auth.plan)
    if not report:
        raise HTTPException(status_code=404, detail="No audit report to export.")

    org = db.table("organizations").select("name").eq("id", auth.org_id).single().execute()
    org_name = org.data["name"] if org.data else "Your Business"

    pdf_bytes = generate_audit_pdf(report, org_name)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="lbt-audit-report.pdf"'},
    )
