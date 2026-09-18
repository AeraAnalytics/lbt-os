import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.visitor_events import sanitize_metadata


class InsertResult:
    def __init__(self, data):
        self.data = data


class FakeVisitorEventsTable:
    def __init__(self):
        self.inserted = []

    def insert(self, row):
        self.inserted.append(row)
        return self

    def execute(self):
        return InsertResult(self.inserted)


class FakeVisitorEventsDb:
    def __init__(self):
        self.visitor_events = FakeVisitorEventsTable()

    def table(self, name):
        if name != "visitor_events":
            raise AssertionError(f"unexpected table: {name}")
        return self.visitor_events


class VisitorEventTests(unittest.TestCase):
    def test_capture_public_visitor_event(self):
        db = FakeVisitorEventsDb()
        client = TestClient(app)

        with patch("app.routers.visitor_events.get_db", return_value=db):
            response = client.post(
                "/api/v1/visitor-events",
                json={
                    "event_type": "test_ping",
                    "visitor_id": "visitor_12345",
                    "session_id": "session_12345",
                    "path": "/",
                    "source": "test",
                    "metadata": {"button": "smoke-test"},
                },
                headers={"user-agent": "test-agent", "referer": "https://example.com/"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["ok"])
        inserted = db.visitor_events.inserted[0]
        self.assertEqual(inserted["event_type"], "test_ping")
        self.assertEqual(inserted["metadata"]["button"], "smoke-test")
        self.assertEqual(inserted["user_agent"], "test-agent")
        self.assertEqual(inserted["referrer"], "https://example.com/")
        self.assertTrue(inserted["ip_hash"])

    def test_metadata_redacts_secret_like_keys(self):
        sanitized = sanitize_metadata({
            "business_name": "Aera Analytics",
            "api_token": "should-not-store",
            "nested": {"password": "nope"},
        })

        self.assertEqual(sanitized["business_name"], "Aera Analytics")
        self.assertEqual(sanitized["api_token"], "[redacted]")
        self.assertEqual(sanitized["nested"]["password"], "[redacted]")


if __name__ == "__main__":
    unittest.main()

