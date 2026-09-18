import io
import unittest

from app.auth import AuthContext, get_auth
from app.config import Settings
from app.limiter import limiter as shared_limiter
from app.main import app
from app.services import manual_import, messaging, stripe_service
from fastapi.testclient import TestClient


class FakeUploadFile:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._data
        return self._data[:size]


class DuplicateInsertDb:
    def table(self, _name: str):
        return self

    def insert(self, _payload):
        return self

    def execute(self):
        raise Exception("duplicate key value violates unique constraint")


class SuccessInsertDb(DuplicateInsertDb):
    def execute(self):
        return {"status": "ok"}


class SecurityFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_csv_upload_size_is_bounded(self):
        oversized = FakeUploadFile(b"a" * (manual_import.MAX_CSV_FILE_BYTES + 1))
        with self.assertRaises(Exception) as ctx:
            await manual_import._read_upload_bytes(oversized)
        self.assertEqual(ctx.exception.status_code, 413)

    async def test_csv_formula_cells_are_neutralized(self):
        rows = manual_import._csv_rows(
            b"name,notes,phone\n"
            b"=cmd,\"@sum(1,1)\",+13035550101\n"
        )
        self.assertEqual(rows[0]["name"], "'=cmd")
        self.assertEqual(rows[0]["notes"], "'@sum(1,1)")
        self.assertEqual(rows[0]["phone"], "+13035550101")

    def test_stripe_webhook_duplicate_events_are_ignored(self):
        self.assertFalse(stripe_service.record_webhook_event(DuplicateInsertDb(), "evt_123"))
        self.assertTrue(stripe_service.record_webhook_event(SuccessInsertDb(), "evt_123"))

    def test_production_requires_clerk_audience_by_default(self):
        with self.assertRaises(ValueError):
            Settings(
                supabase_url="https://example.supabase.co",
                supabase_service_key="service-key",
                clerk_secret_key="sk_test",
                clerk_publishable_key="pk_test_ZXhhbXBsZS5jbGVyay5hY2NvdW50cy5kZXYk",
                clerk_webhook_secret="whsec_test",
                stripe_secret_key="sk_test",
                stripe_webhook_secret="whsec_test",
                stripe_price_basic="price_basic",
                stripe_price_pro="price_pro",
                stripe_price_premium="price_premium",
                app_env="production",
            )

    def test_production_requires_valid_stripe_config(self):
        with self.assertRaises(ValueError):
            Settings(
                supabase_url="https://example.supabase.co",
                supabase_service_key="service-key",
                clerk_secret_key="sk_test",
                clerk_publishable_key="pk_test_ZXhhbXBsZS5jbGVyay5hY2NvdW50cy5kZXYk",
                clerk_webhook_secret="whsec_test",
                clerk_jwt_audience="lbt-os",
                stripe_secret_key="placeholder",
                stripe_webhook_secret="whsec_test",
                stripe_price_basic="price_basic",
                stripe_price_pro="price_pro",
                stripe_price_premium="price_premium",
                app_env="production",
            )

    def test_production_requires_trusted_hosts(self):
        settings = Settings(
            supabase_url="https://example.supabase.co",
            supabase_service_key="service-key",
            clerk_secret_key="sk_test",
            clerk_publishable_key="pk_test_ZXhhbXBsZS5jbGVyay5hY2NvdW50cy5kZXYk",
            clerk_webhook_secret="whsec_test",
            clerk_jwt_audience="lbt-os",
            stripe_secret_key="sk_test_123",
            stripe_webhook_secret="whsec_123",
            stripe_price_basic="price_basic",
            stripe_price_pro="price_pro",
            stripe_price_premium="price_premium",
            app_env="production",
        )
        with self.assertRaises(ValueError):
            settings.parsed_trusted_hosts

    def test_development_cors_allows_localhost_and_loopback_vite_hosts(self):
        settings = Settings(
            supabase_url="https://example.supabase.co",
            supabase_service_key="service-key",
            clerk_secret_key="sk_test",
            clerk_publishable_key="pk_test_ZXhhbXBsZS5jbGVyay5hY2NvdW50cy5kZXYk",
            clerk_webhook_secret="whsec_test",
            stripe_secret_key="sk_test",
            stripe_webhook_secret="whsec_test",
            stripe_price_basic="price_basic",
            stripe_price_pro="price_pro",
            stripe_price_premium="price_premium",
            app_env="development",
        )
        self.assertIn("http://localhost:5173", settings.parsed_cors_origins)
        self.assertIn("http://127.0.0.1:5173", settings.parsed_cors_origins)

    def test_message_upload_rejects_extension_spoofing(self):
        with self.assertRaises(Exception) as ctx:
            messaging._validate_file_upload(
                "report.pdf",
                "pdf",
                b"MZ executable data",
                "application/pdf",
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_message_upload_allows_octet_stream_only_for_signed_office_files(self):
        with self.assertRaises(Exception) as ctx:
            messaging._validate_file_upload(
                "image.png",
                "png",
                b"\x89PNG\r\n\x1a\n",
                "application/octet-stream",
            )
        self.assertEqual(ctx.exception.status_code, 400)

        messaging._validate_file_upload(
            "report.docx",
            "docx",
            b"PK\x03\x04" + b"x" * 32,
            "application/octet-stream",
        )

    def test_main_uses_shared_limiter_instance(self):
        self.assertIs(app.state.limiter, shared_limiter)

    def test_manual_import_route_rejects_spoofed_extension(self):
        client = TestClient(app)
        app.dependency_overrides[get_auth] = lambda: AuthContext("user_123", "org_123", "pro")
        try:
            response = client.post(
                "/api/v1/integrations/manual-import?entity_type=leads",
                files={"file": ("contacts.csv.exe", io.BytesIO(b"name\nAlice\n"), "text/csv")},
            )
        finally:
            app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 400)
        self.assertIn("CSV", response.json()["detail"])

    def test_audit_route_endpoint_is_rate_limited(self):
        route = next(route for route in app.routes if getattr(route, "path", None) == "/api/v1/audit/run")
        self.assertTrue(hasattr(route.endpoint, "__wrapped__"))


if __name__ == "__main__":
    unittest.main()
