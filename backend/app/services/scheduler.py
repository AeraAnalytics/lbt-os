"""
Recurring job scheduler — powered by APScheduler.

Jobs:
  - Sunday 23:00 UTC  → sync all connections for Pro/Premium orgs
  - Monday 06:00 UTC  → weekly operating brief (email + SMS) for all Pro/Premium orgs
  - 1st Monday 07:00 UTC (monthly) → AI audit for all Pro/Premium orgs

All jobs are fire-and-forget: errors are logged, never raised.
Scheduler is started on FastAPI startup and shut down cleanly on shutdown.
"""
from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ..auth import get_clerk_user_email, get_clerk_user_phone
from ..config import settings
from ..database import get_db
from .email import send_sync_complete, send_weekly_brief
from .integrations import SUPPORTED_PROVIDERS, sync_all_connections_for_org
from .metrics import get_dashboard_metrics

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SMS helper — fire-and-forget, silently skips if Twilio is not configured
# ---------------------------------------------------------------------------

def _send_sms(*, to: str, body: str) -> None:
    if not all([settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from_number]):
        log.debug("SMS skipped (Twilio not configured): %s", to)
        return
    try:
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        client.messages.create(to=to, from_=settings.twilio_from_number, body=body)
        log.info("SMS sent to %s", to)
    except Exception as exc:
        log.warning("SMS failed (%s): %s", to, exc)


def _weekly_brief_sms(*, to: str, org_name: str, metrics: dict[str, Any]) -> None:
    """Compose and send the Monday morning brief as a short SMS."""
    r = metrics.get("revenue", {})
    leads_m = metrics.get("leads", {})
    revenue    = f"${(r.get('total') or 0):,.0f}"
    margin     = f"{(r.get('margin_pct') or 0):.0f}%"
    conversion = f"{(leads_m.get('conversion_rate_pct') or 0):.0f}%"
    missed     = leads_m.get("missed_follow_ups", 0)
    dashboard  = f"{settings.frontend_url}/app"

    lines = [
        f"LBT OS — Monday brief for {org_name}",
        f"Revenue (30d): {revenue} | Margin: {margin} | Conversion: {conversion}",
    ]
    if missed:
        lines.append(f"⚠ {missed} lead{'s' if missed != 1 else ''} need follow-up")
    lines.append(f"Dashboard: {dashboard}")

    _send_sms(to=to, body="\n".join(lines))


# ---------------------------------------------------------------------------
# Job implementations
# ---------------------------------------------------------------------------

def _job_sync_connections() -> None:
    """Sunday 23:00 UTC — sync all active connections for Pro/Premium orgs."""
    log.info("[scheduler] Starting weekly connection sync")
    db = get_db()
    try:
        orgs = (
            db.table("organizations")
            .select("id, name, clerk_user_id, plan")
            .in_("plan", ["pro", "premium", "enterprise"])
            .execute()
        ).data or []
    except Exception as exc:
        log.error("[scheduler] Failed to fetch orgs for sync: %s", exc)
        return

    for org in orgs:
        org_id       = org["id"]
        org_name     = org["name"]
        clerk_uid    = org["clerk_user_id"]
        provider_key = "unknown"
        try:
            runs = sync_all_connections_for_org(db, org_id, trigger_source="scheduled")
            for run in runs:
                provider_key   = run.get("provider", "")
                provider_label = SUPPORTED_PROVIDERS.get(provider_key, {}).get("label", provider_key.title())
                email = get_clerk_user_email(clerk_uid)
                if email:
                    send_sync_complete(
                        to=email,
                        org_name=org_name,
                        provider_label=provider_label,
                        stats=run.get("stats") or {},
                        status=run.get("status", "unknown"),
                    )
            log.info("[scheduler] Synced %d connections for org %s", len(runs), org_id)
        except Exception as exc:
            log.error("[scheduler] Sync failed for org %s (provider=%s): %s", org_id, provider_key, exc)


def _job_weekly_brief() -> None:
    """Monday 06:00 UTC — email + SMS weekly brief for Pro/Premium orgs."""
    log.info("[scheduler] Sending weekly briefs")
    db = get_db()
    try:
        orgs = (
            db.table("organizations")
            .select("id, name, clerk_user_id, plan")
            .in_("plan", ["pro", "premium", "enterprise"])
            .execute()
        ).data or []
    except Exception as exc:
        log.error("[scheduler] Failed to fetch orgs for weekly brief: %s", exc)
        return

    for org in orgs:
        org_id    = org["id"]
        org_name  = org["name"]
        clerk_uid = org["clerk_user_id"]
        try:
            metrics = get_dashboard_metrics(db, org_id, days=30)
            email   = get_clerk_user_email(clerk_uid)
            phone   = get_clerk_user_phone(clerk_uid)

            if email:
                send_weekly_brief(to=email, org_name=org_name, metrics=metrics)
            if phone:
                _weekly_brief_sms(to=phone, org_name=org_name, metrics=metrics)

            log.info("[scheduler] Weekly brief sent for org %s (email=%s, sms=%s)", org_id, bool(email), bool(phone))
        except Exception as exc:
            log.error("[scheduler] Weekly brief failed for org %s: %s", org_id, exc)


def _job_monthly_audit() -> None:
    """1st Monday of each month, 07:00 UTC — run AI audit for Pro/Premium orgs."""
    log.info("[scheduler] Starting monthly AI audits")
    # Import here to avoid circular import at module load
    from .ai_audit import run_audit
    from .email import send_audit_complete

    db = get_db()
    try:
        orgs = (
            db.table("organizations")
            .select("id, name, industry, clerk_user_id, plan")
            .in_("plan", ["pro", "premium", "enterprise"])
            .execute()
        ).data or []
    except Exception as exc:
        log.error("[scheduler] Failed to fetch orgs for monthly audit: %s", exc)
        return

    for org in orgs:
        org_id    = org["id"]
        org_name  = org["name"]
        clerk_uid = org["clerk_user_id"]
        try:
            report = run_audit(
                db=db,
                org_id=org_id,
                org_name=org_name,
                industry=org.get("industry"),
                plan=org["plan"],
            )
            email = get_clerk_user_email(clerk_uid)
            if email:
                send_audit_complete(to=email, org_name=org_name, report=report)
            log.info("[scheduler] Monthly audit done for org %s (score=%s)", org_id, report.get("health_score"))
        except Exception as exc:
            log.error("[scheduler] Monthly audit failed for org %s: %s", org_id, exc)


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------

_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(timezone="UTC")

    # Sunday 23:00 UTC — connection sync
    _scheduler.add_job(
        _job_sync_connections,
        CronTrigger(day_of_week="sun", hour=23, minute=0, timezone="UTC"),
        id="weekly_sync",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Monday 06:00 UTC — weekly brief (email + SMS)
    _scheduler.add_job(
        _job_weekly_brief,
        CronTrigger(day_of_week="mon", hour=6, minute=0, timezone="UTC"),
        id="weekly_brief",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # 1st Monday of each month, 07:00 UTC — AI audit
    _scheduler.add_job(
        _job_monthly_audit,
        CronTrigger(day_of_week="mon", week="1", hour=7, minute=0, timezone="UTC"),
        id="monthly_audit",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    _scheduler.start()
    log.info("[scheduler] Started — 3 jobs registered (sync/brief/audit)")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("[scheduler] Stopped")
