import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request

from ..config import settings
from ..models.visitor_event import VisitorEventCreate

MAX_METADATA_KEYS = 40
MAX_METADATA_VALUE_LENGTH = 500
SENSITIVE_KEY_PARTS = ("password", "token", "secret", "key", "authorization", "cookie")


def sanitize_metadata(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_METADATA_VALUE_LENGTH]
    if isinstance(value, list):
        return [sanitize_metadata(item, depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in list(value.items())[:MAX_METADATA_KEYS]:
            key_text = str(key)[:80]
            if any(part in key_text.lower() for part in SENSITIVE_KEY_PARTS):
                sanitized[key_text] = "[redacted]"
            else:
                sanitized[key_text] = sanitize_metadata(child, depth + 1)
        return sanitized
    return str(value)[:MAX_METADATA_VALUE_LENGTH]


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


def _hash_ip(ip_address: str) -> str | None:
    if not ip_address:
        return None
    salt = settings.api_secret or settings.supabase_service_key
    return hashlib.sha256(f"{salt}:{ip_address}".encode()).hexdigest()


def _optional_user_id(request: Request) -> str | None:
    user_id = request.headers.get("x-lbt-user-id")
    return user_id[:128] if user_id else None


def build_event_row(body: VisitorEventCreate, request: Request) -> dict[str, Any]:
    return {
        "event_type": body.event_type,
        "visitor_id": body.visitor_id,
        "session_id": body.session_id,
        "path": body.path,
        "source": body.source,
        "metadata": sanitize_metadata(body.metadata),
        "user_agent": request.headers.get("user-agent", "")[:500],
        "referrer": request.headers.get("referer", "")[:500] or None,
        "ip_hash": _hash_ip(_client_ip(request)),
        "clerk_user_id": _optional_user_id(request),
        "occurred_at": (body.occurred_at or datetime.now(timezone.utc)).isoformat(),
    }


def record_event(db, body: VisitorEventCreate, request: Request) -> dict[str, Any]:
    row = build_event_row(body, request)
    result = db.table("visitor_events").insert(row).execute()
    if isinstance(result, dict):
        return {"ok": True, "event": row}
    data = getattr(result, "data", None) or []
    return {"ok": True, "event": data[0] if data else row}


def list_recent_events(db, limit: int = 50) -> list[dict[str, Any]]:
    result = (
        db.table("visitor_events")
        .select("*")
        .order("occurred_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def event_summary(db, days: int = 7) -> dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = (
        db.table("visitor_events")
        .select("event_type, visitor_id, occurred_at")
        .gte("occurred_at", since)
        .execute()
    )
    rows = result.data or []
    by_type: dict[str, int] = {}
    visitors: set[str] = set()
    for row in rows:
        event_type = row.get("event_type") or "unknown"
        by_type[event_type] = by_type.get(event_type, 0) + 1
        if row.get("visitor_id"):
            visitors.add(row["visitor_id"])
    return {
        "days": days,
        "total_events": len(rows),
        "unique_visitors": len(visitors),
        "by_type": by_type,
    }

