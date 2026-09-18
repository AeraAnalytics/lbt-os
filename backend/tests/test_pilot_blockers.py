"""Tests for the TW-078 / TW-079 pilot-blocker fixes (Rex Ryder's audit)."""
import base64
import json
import unittest

from fastapi import HTTPException
from starlette.requests import Request

from app.auth import AuthContext
from app.limiter import (
    _rate_limit_key,
    _user_limiter,
    enforce_user_limit,
)
from app.services import stripe_service


def _unsigned_jwt(sub: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{payload}.sig"


def _request_with_bearer(sub: str) -> Request:
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Bearer {_unsigned_jwt(sub)}".encode())],
        "client": ("203.0.113.7", 1234),
    }
    return Request(scope)


class FakeResult:
    def __init__(self, data):
        self.data = data


class StatusDb:
    """In-memory fake of the stripe_events table."""

    def __init__(self):
        self.rows: dict[str, tuple[str, str]] = {}

    def table(self, _name):
        self._conds: list = []
        return self

    def insert(self, payload):
        self._op = ("insert", payload)
        return self

    def select(self, _cols):
        self._op = ("select", None)
        return self

    def update(self, payload):
        self._op = ("update", payload)
        return self

    def eq(self, col, value):
        self._conds.append(("eq", col, value))
        return self

    def in_(self, col, values):
        self._conds.append(("in", col, list(values)))
        return self

    def lt(self, col, value):
        self._conds.append(("lt", col, value))
        return self

    def maybe_single(self):
        return self

    def _match(self, event_id, row):
        for kind, col, value in self._conds:
            actual = event_id if col == "stripe_event_id" else (row[0] if col == "status" else row[1])
            if kind == "eq" and actual != value:
                return False
            if kind == "in" and actual not in value:
                return False
            if kind == "lt" and not (actual < value):
                return False
        return True

    def execute(self):
        op, payload = self._op
        if op == "insert":
            import datetime

            event_id = payload["stripe_event_id"]
            if event_id in self.rows:
                raise Exception("duplicate key value violates unique constraint")
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.rows[event_id] = (payload.get("status", "processed"), now)
            return FakeResult(None)
        if op == "select":
            for event_id, row in self.rows.items():
                if self._match(event_id, row):
                    return FakeResult({"status": row[0], "processed_at": row[1]})
            return FakeResult(None)
        if op == "update":
            matched = []
            for event_id, row in self.rows.items():
                if self._match(event_id, row):
                    new_ts = payload.get("processed_at", row[1])
                    self.rows[event_id] = (payload.get("status", row[0]), new_ts)
                    matched.append({"stripe_event_id": event_id})
            return FakeResult(matched)
        raise AssertionError(f"unexpected op {op}")


class RateLimitKeyTests(unittest.TestCase):
    """TW-078: the slowapi bucket key must not come from unverified JWT claims."""

    def test_key_ignores_bearer_sub_claim(self):
        # Attacker mints a fresh unsigned sub per request — key must NOT follow it.
        key_a = _rate_limit_key(_request_with_bearer("attacker-sub-1"))
        key_b = _rate_limit_key(_request_with_bearer("attacker-sub-2"))
        self.assertEqual(key_a, "203.0.113.7")
        self.assertEqual(key_b, "203.0.113.7")
        self.assertNotIn("attacker-sub", key_a)

    def test_key_without_auth_header_is_ip(self):
        scope = {"type": "http", "headers": [], "client": ("198.51.100.9", 443)}
        self.assertEqual(_rate_limit_key(Request(scope)), "198.51.100.9")


class UserLimitTests(unittest.IsolatedAsyncioTestCase):
    """TW-078: per-verified-user limits enforced after get_auth."""

    def setUp(self):
        _user_limiter.reset()

    def tearDown(self):
        _user_limiter.reset()

    async def test_allows_up_to_limit_then_429s(self):
        check = enforce_user_limit("2/minute")
        auth = AuthContext(user_id="user-1", org_id="org-1")
        await check(auth=auth)
        await check(auth=auth)
        with self.assertRaises(HTTPException) as ctx:
            await check(auth=auth)
        self.assertEqual(ctx.exception.status_code, 429)

    async def test_buckets_are_per_user(self):
        check = enforce_user_limit("1/minute")
        await check(auth=AuthContext(user_id="user-1", org_id="org-1"))
        # A different verified user gets their own bucket.
        await check(auth=AuthContext(user_id="user-2", org_id="org-1"))
        with self.assertRaises(HTTPException):
            await check(auth=AuthContext(user_id="user-1", org_id="org-1"))

    def test_bad_period_rejected(self):
        with self.assertRaises(ValueError):
            enforce_user_limit("5/fortnight")


class WebhookClaimTests(unittest.TestCase):
    """TW-079: status-gated idempotency — failed events must be reprocessable."""

    def test_new_then_duplicate_after_processed(self):
        db = StatusDb()
        self.assertEqual(stripe_service.claim_webhook_event(db, "evt_1"), "new")
        stripe_service.mark_webhook_processed(db, "evt_1")
        self.assertEqual(stripe_service.claim_webhook_event(db, "evt_1"), "duplicate")

    def test_failed_event_is_reprocessed_on_retry(self):
        """The exact TW-079 bug: a 500'd event must NOT be swallowed as a duplicate."""
        db = StatusDb()
        # First delivery: claim, dispatch raises, mark failed (mirrors the router).
        self.assertEqual(stripe_service.claim_webhook_event(db, "evt_2"), "new")
        stripe_service.mark_webhook_failed(db, "evt_2")
        # Stripe retry: must reprocess, not skip.
        self.assertEqual(stripe_service.claim_webhook_event(db, "evt_2"), "new")
        stripe_service.mark_webhook_processed(db, "evt_2")
        self.assertEqual(stripe_service.claim_webhook_event(db, "evt_2"), "duplicate")

    def test_recent_inflight_claim_is_not_reprocessed(self):
        db = StatusDb()
        self.assertEqual(stripe_service.claim_webhook_event(db, "evt_3"), "new")
        # A second delivery while the first is still in flight: skip.
        self.assertEqual(stripe_service.claim_webhook_event(db, "evt_3"), "inflight")

    def test_stale_processing_claim_is_taken_over(self):
        db = StatusDb()
        db.rows["evt_4"] = ("processing", "2020-01-01T00:00:00+00:00")
        self.assertEqual(stripe_service.claim_webhook_event(db, "evt_4"), "new")

    def test_takeover_refreshes_claim_timestamp(self):
        """Rosa MAJOR #1: the takeover must re-anchor the 600s window."""
        db = StatusDb()
        old_ts = "2020-01-01T00:00:00+00:00"
        db.rows["evt_7"] = ("processing", old_ts)
        self.assertEqual(stripe_service.claim_webhook_event(db, "evt_7"), "new")
        self.assertNotEqual(db.rows["evt_7"][1], old_ts)
        # A third delivery now sees a FRESH claim -> inflight, not a second takeover.
        self.assertEqual(stripe_service.claim_webhook_event(db, "evt_7"), "inflight")

    def test_failed_claim_taken_over_immediately(self):
        """A 'failed' claim is by definition not in flight — no staleness wait."""
        import datetime

        db = StatusDb()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        db.rows["evt_8"] = ("failed", now)
        self.assertEqual(stripe_service.claim_webhook_event(db, "evt_8"), "new")

    def test_concurrent_takeover_single_winner(self):
        """Rosa MAJOR #2: two workers racing a stale claim — exactly one wins."""
        db = StatusDb()
        db.rows["evt_6"] = ("processing", "2020-01-01T00:00:00+00:00")
        self.assertEqual(stripe_service.claim_webhook_event(db, "evt_6"), "new")
        # Worker 2 built its conditional update from the same stale read;
        # after worker 1's takeover refreshed processed_at, it matches 0 rows.
        stale_cutoff = "2020-01-01T00:10:00+00:00"
        lost = (
            db.table("stripe_events")
            .update({"status": "processing", "processed_at": "2026-09-17T00:00:00+00:00"})
            .eq("stripe_event_id", "evt_6")
            .eq("status", "processing")
            .lt("processed_at", stale_cutoff)
            .execute()
        )
        self.assertEqual(lost.data, [])

    def test_non_duplicate_db_errors_still_raise(self):
        class BoomDb(StatusDb):
            def execute(self):
                raise Exception("connection reset")

        with self.assertRaises(Exception):
            stripe_service.claim_webhook_event(BoomDb(), "evt_5")


class WebhookRouterTests(unittest.IsolatedAsyncioTestCase):
    """TW-087: the router must answer 'inflight' with a retryable 5xx, never 200."""

    async def test_inflight_returns_503_not_200(self):
        import datetime
        from unittest.mock import patch

        from app.routers import stripe_webhooks

        db = StatusDb()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # Simulate a crashed worker: fresh "processing" claim, never marked.
        db.rows["evt_9"] = ("processing", now)

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        scope = {
            "type": "http",
            "headers": [(b"stripe-signature", b"sig")],
            "client": ("1.2.3.4", 1234),
        }
        request = Request(scope, receive=receive)

        fake_event = {"id": "evt_9", "type": "customer.subscription.deleted"}
        with (
            patch.object(stripe_webhooks, "handle_webhook", return_value=fake_event),
            patch.object(stripe_webhooks, "get_db", return_value=db),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await stripe_webhooks.stripe_webhook(request)
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_duplicate_still_returns_200(self):
        import datetime
        from unittest.mock import patch

        from fastapi.responses import JSONResponse

        from app.routers import stripe_webhooks

        db = StatusDb()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        db.rows["evt_10"] = ("processed", now)

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        scope = {
            "type": "http",
            "headers": [(b"stripe-signature", b"sig")],
            "client": ("1.2.3.4", 1234),
        }
        request = Request(scope, receive=receive)

        fake_event = {"id": "evt_10", "type": "customer.subscription.deleted"}
        with (
            patch.object(stripe_webhooks, "handle_webhook", return_value=fake_event),
            patch.object(stripe_webhooks, "get_db", return_value=db),
        ):
            response = await stripe_webhooks.stripe_webhook(request)
        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()


def _iter_full_routes(app):
    """Yield (full_path, route) pairs, expanding lazy include_router output."""
    for route in app.routes:
        if hasattr(route, "original_router"):
            prefix = getattr(route.include_context, "prefix", "") or ""
            for inner in route.original_router.routes:
                yield prefix + (getattr(inner, "path", "") or ""), inner
        else:
            yield getattr(route, "path", "") or "", route


class EndpointWiringTests(unittest.TestCase):
    """Rosa MAJOR #3: assert enforce_user_limit is actually attached to the
    costly endpoints and resolves through the verified get_auth dependency."""

    PATHS = [
        ("POST", "/api/v1/audit/run"),
        ("POST", "/api/v1/strategy/ask"),
        ("POST", "/api/v1/strategy/search-competitors"),
        ("POST", "/api/v1/strategy/analyze-competitors"),
        ("POST", "/api/v1/messages/channels/{channel_id}/ask"),
    ]

    def test_costly_endpoints_enforce_per_user_limit(self):
        from app.auth import get_auth
        from app.main import app

        by_path = {}
        for full_path, route in _iter_full_routes(app):
            methods = getattr(route, "methods", set()) or set()
            for method in methods:
                by_path[(method, full_path)] = route

        for method, path in self.PATHS:
            with self.subTest(path=path):
                route = by_path.get((method, path))
                self.assertIsNotNone(route, f"route {method} {path} not found")
                user_limit_deps = [
                    dep
                    for dep in route.dependant.dependencies
                    if getattr(dep.call, "__name__", "") == "_enforce"
                ]
                self.assertTrue(
                    user_limit_deps,
                    f"{method} {path} is missing the enforce_user_limit dependency",
                )
                for dep in user_limit_deps:
                    self.assertTrue(
                        any(sub.call is get_auth for sub in dep.dependencies),
                        f"{method} {path}: user limit does not resolve through get_auth",
                    )
