"""
Stripe integration service.

Handles:
  - Creating Stripe customers for organizations
  - Creating checkout sessions for subscription upgrades
  - Processing webhooks to sync subscription state
"""
from __future__ import annotations

import datetime
import logging

import stripe
from supabase import Client

from ..config import PLAN_PRICE_MAP, settings

log = logging.getLogger(__name__)

stripe.api_key = settings.stripe_secret_key


def _configured_price_id(plan: str) -> str:
    price_id = PLAN_PRICE_MAP.get(plan)
    if not price_id:
        raise ValueError(f"Unknown plan: {plan}")
    if not price_id.startswith("price_") or "placeholder" in price_id.lower():
        raise ValueError(
            f"Stripe Price ID for {plan} is not configured. "
            f"Set STRIPE_PRICE_{plan.upper()} to the recurring Stripe Price ID."
        )
    return price_id


def get_or_create_stripe_customer(db: Client, org_id: str, org_name: str, email: str) -> str:
    """Return existing Stripe customer ID or create a new one."""
    org = db.table("organizations").select("stripe_customer_id").eq("id", org_id).single().execute()

    if org.data.get("stripe_customer_id"):
        return org.data["stripe_customer_id"]

    customer = stripe.Customer.create(
        name=org_name,
        email=email,
        metadata={"lbt_org_id": org_id},
    )
    log.info("stripe_customer_created", extra={"org_id": org_id, "customer_id": customer.id})

    db.table("organizations").update({"stripe_customer_id": customer.id}).eq("id", org_id).execute()
    return customer.id


def create_checkout_session(
    db: Client,
    org_id: str,
    org_name: str,
    user_email: str,
    plan: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """Create a Stripe Checkout session and return the URL."""
    price_id = _configured_price_id(plan)

    customer_id = get_or_create_stripe_customer(db, org_id, org_name, user_email)

    success_separator = "&" if "?" in success_url else "?"
    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{success_url}{success_separator}session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=cancel_url,
        client_reference_id=org_id,
        allow_promotion_codes=True,
        metadata={"lbt_org_id": org_id, "plan": plan},
        subscription_data={"metadata": {"lbt_org_id": org_id, "plan": plan}},
    )
    log.info("checkout_session_created", extra={"org_id": org_id, "plan": plan, "session_id": session.id})
    return session.url


def verify_checkout_session(db: Client, org_id: str, session_id: str) -> dict:
    """
    Retrieve a Stripe Checkout Session after redirect, verify ownership, and sync
    the subscription immediately so the app does not depend on webhook timing.
    """
    session = stripe.checkout.Session.retrieve(session_id, expand=["subscription"])

    # stripe-python 15.x uses attribute access, not .get()
    metadata = session.metadata or {}
    session_org_id = session.client_reference_id or (metadata.get("lbt_org_id") if isinstance(metadata, dict) else None)
    if session_org_id != org_id:
        raise ValueError("Checkout session does not belong to this organization.")

    subscription = session.subscription
    if subscription:
        if isinstance(subscription, str):
            subscription = stripe.Subscription.retrieve(subscription)
        sync_subscription(db, subscription)

    sub_id = getattr(subscription, "id", None) if subscription and not isinstance(subscription, str) else subscription

    return {
        "id": session.id,
        "status": session.status,
        "payment_status": session.payment_status,
        "plan": metadata.get("plan") if isinstance(metadata, dict) else None,
        "subscription_id": sub_id,
    }


def create_portal_session(db: Client, org_id: str, return_url: str) -> str:
    """Return a Stripe billing portal URL for the org."""
    org = db.table("organizations").select("stripe_customer_id").eq("id", org_id).single().execute()
    customer_id = org.data.get("stripe_customer_id")
    if not customer_id:
        raise ValueError("No Stripe customer on file. Subscribe first.")

    session = stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url)
    return session.url


def handle_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify and parse a Stripe webhook. Raises stripe.SignatureVerificationError on bad sig."""
    return stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)


# How long a "processing" claim is honored before another worker may take it
# over. Stripe retries sequentially with backoff, so a recent claim means a
# delivery is still in flight somewhere; an old one means it died.
_WEBHOOK_CLAIM_TAKEOVER_SECONDS = 600


def claim_webhook_event(db: Client, event_id: str) -> str:
    """
    Claim a Stripe event for processing (TW-079 status-gated idempotency).

    Returns:
        "new"       — this caller owns the event and must process it.
        "duplicate" — already processed; skip.
        "inflight"  — claimed very recently by another in-flight delivery.
                      The caller MUST answer with a retryable status (5xx),
                      never 200 — a 200 tells Stripe the event was delivered
                      and it will not retry a crashed worker's event (TW-087).

    Only events marked "processed" are skipped. A failed dispatch is marked
    "failed" (see mark_webhook_failed), so Stripe's retry reprocesses it
    instead of being swallowed as a duplicate.
    """
    try:
        db.table("stripe_events").insert(
            {"stripe_event_id": event_id, "status": "processing"}
        ).execute()
        return "new"
    except Exception as exc:
        error_text = str(exc).lower()
        if "duplicate key" not in error_text and "unique constraint" not in error_text:
            raise

    result = (
        db.table("stripe_events")
        .select("status, processed_at")
        .eq("stripe_event_id", event_id)
        .maybe_single()
        .execute()
    )
    row = result.data if result and result.data else None
    if not row:
        # Lost the insert race but no row is visible yet — process; the
        # mark_webhook_processed update below is idempotent.
        return "new"

    # NOT NULL column; the fallback is purely defensive.
    status = (row.get("status") or "processed").lower()
    if status == "processed":
        return "duplicate"
    if status == "processing" and _claim_age_seconds(row.get("processed_at")) < _WEBHOOK_CLAIM_TAKEOVER_SECONDS:
        return "inflight"

    # Take over a stale "processing" claim or a "failed" claim. This MUST be a
    # single conditional update (not check-then-act): concurrent workers racing
    # on the same stale row must not both win. Zero affected rows means someone
    # else took it or finished it first -> treat as inflight, do not process.
    now_iso = _utcnow_iso()
    if status == "failed":
        # A failed claim is by definition not in flight — the worker died.
        # Take over immediately regardless of claim age.
        taken = (
            db.table("stripe_events")
            .update({"status": "processing", "processed_at": now_iso})
            .eq("stripe_event_id", event_id)
            .eq("status", "failed")
            .execute()
        )
    else:
        cutoff_iso = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=_WEBHOOK_CLAIM_TAKEOVER_SECONDS)
        ).isoformat()
        taken = (
            db.table("stripe_events")
            .update({"status": "processing", "processed_at": now_iso})
            .eq("stripe_event_id", event_id)
            .eq("status", "processing")
            .lt("processed_at", cutoff_iso)
            .execute()
        )
    taken_rows = taken.data if taken and taken.data else None
    return "new" if taken_rows else "inflight"


def mark_webhook_processed(db: Client, event_id: str) -> None:
    """Mark a Stripe event fully processed — future deliveries are duplicates."""
    db.table("stripe_events").update({"status": "processed"}).eq(
        "stripe_event_id", event_id
    ).execute()


def mark_webhook_failed(db: Client, event_id: str) -> None:
    """Mark a Stripe event failed so Stripe's retry reprocesses it (TW-079)."""
    db.table("stripe_events").update({"status": "failed"}).eq(
        "stripe_event_id", event_id
    ).execute()


def _utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string (for claim timestamps)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _claim_age_seconds(claimed_at) -> float:
    """Age of a claim timestamp in seconds; unparseable -> treated as stale."""
    if not claimed_at:
        return float("inf")
    try:
        if isinstance(claimed_at, str):
            claimed_at = datetime.datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
        if claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - claimed_at).total_seconds()
    except Exception:
        return float("inf")


def sync_subscription(db: Client, subscription: stripe.Subscription) -> None:
    """
    Sync a Stripe subscription to the organizations table.
    Called from the webhook handler on created/updated/checkout events.
    """
    org_id = subscription.metadata.get("lbt_org_id")
    plan   = subscription.metadata.get("plan", "basic")

    if not org_id:
        log.warning("sync_subscription_missing_org_id", extra={"sub_id": getattr(subscription, "id", None)})
        return

    status_map = {
        "active":             "active",
        "trialing":           "active",
        "past_due":           "past_due",
        "canceled":           "inactive",
        "unpaid":             "past_due",
        "incomplete":         "inactive",
        "incomplete_expired": "inactive",
    }
    internal_status = status_map.get(subscription.status, "inactive")

    db.table("organizations").update({
        "stripe_subscription_id": subscription.id,
        "subscription_status":    internal_status,
        "plan":                   plan,
    }).eq("id", org_id).execute()

    log.info(
        "subscription_synced",
        extra={
            "org_id": org_id,
            "plan": plan,
            "status": internal_status,
            "sub_id": subscription.id,
        },
    )
