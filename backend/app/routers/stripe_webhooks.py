"""
Stripe webhook router.

IMPORTANT: The webhook endpoint receives raw bytes — never parse the body
with FastAPI's JSON machinery or signature verification will fail.

Webhook events handled:
  - customer.subscription.created
  - customer.subscription.updated
  - customer.subscription.deleted
  - checkout.session.completed
"""
import logging
from typing import Annotated

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..auth import AuthContext, get_auth, get_clerk_user_email
from ..config import settings
from ..database import get_db
from ..services.stripe_service import (
    claim_webhook_event,
    create_checkout_session,
    create_portal_session,
    handle_webhook,
    mark_webhook_failed,
    mark_webhook_processed,
    sync_subscription,
    verify_checkout_session,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/billing")
_ALLOWED_CHECKOUT_PLANS = {"basic", "pro", "premium"}


@router.post("/checkout")
def start_checkout(
    plan: str,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    """Create a Stripe Checkout session for a plan upgrade."""
    if plan not in _ALLOWED_CHECKOUT_PLANS:
        raise HTTPException(status_code=400, detail="Unsupported billing plan.")
    db = get_db()
    org = db.table("organizations").select("name").eq("id", auth.org_id).single().execute()

    user_email = get_clerk_user_email(auth.user_id) or f"{auth.user_id}@lbt-os.app"
    try:
        url = create_checkout_session(
            db=db,
            org_id=auth.org_id,
            org_name=org.data["name"],
            user_email=user_email,
            plan=plan,
            success_url=f"{settings.frontend_url}/app/billing?upgraded={plan}",
            cancel_url=f"{settings.frontend_url}/app/billing",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except stripe.StripeError as exc:
        log.exception("checkout_session_failed", extra={"org_id": auth.org_id, "plan": plan})
        message = getattr(exc, "user_message", None) or str(exc)
        raise HTTPException(status_code=502, detail=f"Stripe checkout failed: {message}")
    return {"checkout_url": url}


@router.post("/portal")
def billing_portal(auth: Annotated[AuthContext, Depends(get_auth)]):
    """Return a Stripe Customer Portal URL for managing subscription."""
    db = get_db()
    try:
        url = create_portal_session(db, auth.org_id, return_url=f"{settings.frontend_url}/app/billing")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"portal_url": url}


@router.get("/checkout/session/{session_id}")
def checkout_session_status(
    session_id: str,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    """Verify a completed Checkout Session and sync the org subscription."""
    db = get_db()
    try:
        return verify_checkout_session(db, auth.org_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except stripe.StripeError as exc:
        log.exception("checkout_session_verify_failed", extra={"org_id": auth.org_id, "session_id": session_id})
        message = getattr(exc, "user_message", None) or getattr(exc, "error", {}).get("message") or str(exc)
        raise HTTPException(status_code=502, detail=f"Stripe checkout verification failed: {message}")
    except Exception:
        log.exception("checkout_session_verify_unexpected", extra={"org_id": auth.org_id, "session_id": session_id})
        raise HTTPException(status_code=500, detail="Unexpected error verifying checkout session.")


@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request):
    """
    Raw Stripe webhook receiver. Excluded from auth — Stripe signs it via HMAC.
    Returns 400 on bad signature so Stripe will retry.
    Returns 200 on duplicate (idempotent).
    """
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = handle_webhook(payload, sig_header)
    except stripe.SignatureVerificationError:
        log.warning("webhook_signature_invalid", extra={"sig_present": bool(sig_header)})
        raise HTTPException(status_code=400, detail="Invalid Stripe signature.")

    event_id   = event.get("id", "unknown")
    event_type = event.get("type", "unknown")

    log.info("webhook_received", extra={"event_id": event_id, "event_type": event_type})

    db = get_db()

    # TW-079: status-gated idempotency. Only events marked "processed" are
    # skipped — a failed dispatch is marked "failed" so Stripe's retry
    # reprocesses it instead of being swallowed as a duplicate.
    if event_id != "unknown":
        claim = claim_webhook_event(db, event_id)
        if claim == "duplicate":
            log.info("webhook_duplicate", extra={"event_id": event_id, "event_type": event_type})
            return JSONResponse({"received": True, "duplicate": True})
        if claim == "inflight":
            log.info("webhook_inflight", extra={"event_id": event_id, "event_type": event_type})
            return JSONResponse({"received": True, "inflight": True})

    try:
        _dispatch(db, event_type, event)
        if event_id != "unknown":
            mark_webhook_processed(db, event_id)
        log.info("webhook_processed", extra={"event_id": event_id, "event_type": event_type})
    except Exception:
        if event_id != "unknown":
            mark_webhook_failed(db, event_id)
        log.exception("webhook_processing_failed", extra={"event_id": event_id, "event_type": event_type})
        # Return 500 so Stripe retries
        raise HTTPException(status_code=500, detail="Webhook processing error.")

    return JSONResponse({"received": True})


def _dispatch(db, event_type: str, event: dict) -> None:
    """Route a verified, deduplicated event to the appropriate handler."""
    if event_type in ("customer.subscription.created", "customer.subscription.updated"):
        sync_subscription(db, event["data"]["object"])

    elif event_type == "customer.subscription.deleted":
        sub    = event["data"]["object"]
        org_id = sub.get("metadata", {}).get("lbt_org_id")
        if org_id:
            db.table("organizations").update({
                "subscription_status": "inactive",
                "plan": "basic",
            }).eq("id", org_id).execute()
        else:
            log.warning("subscription_deleted_no_org", extra={"sub_id": sub.get("id")})

    elif event_type == "checkout.session.completed":
        session = event["data"]["object"]
        sub_id  = session.get("subscription")
        if sub_id:
            sub = stripe.Subscription.retrieve(sub_id)
            sync_subscription(db, sub)
        else:
            log.warning("checkout_completed_no_subscription", extra={"session_id": session.get("id")})

    else:
        log.debug("webhook_unhandled_type", extra={"event_type": event_type})
