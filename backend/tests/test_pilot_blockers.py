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

    def eq(self, _col, value):
        self._key = value
        return self

    def maybe_single(self):
        return self

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
            row = self.rows.get(self._key)
            return FakeResult({"status": row[0], "processed_at": row[1]} if row else None)
        if op == "update":
            if self._key in self.rows:
                old_status, old_ts = self.rows[self._key]
                self.rows[self._key] = (payload.get("status", old_status), old_ts)
            return FakeResult(None)
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

    def test_non_duplicate_db_errors_still_raise(self):
        class BoomDb(StatusDb):
            def execute(self):
                raise Exception("connection reset")

        with self.assertRaises(Exception):
            stripe_service.claim_webhook_event(BoomDb(), "evt_5")


if __name__ == "__main__":
    unittest.main()
