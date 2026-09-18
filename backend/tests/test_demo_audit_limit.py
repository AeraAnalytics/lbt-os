import unittest

from app.config import Settings

BASE_SETTINGS = {
    "supabase_url": "https://example.supabase.co",
    "supabase_service_key": "service-key",
    "clerk_secret_key": "sk_test",
    "clerk_publishable_key": "pk_test_ZXhhbXBsZS5jbGVyay5hY2NvdW50cy5kZXYk",
    "clerk_webhook_secret": "whsec_test",
    "stripe_secret_key": "sk_test",
    "stripe_webhook_secret": "whsec_test",
    "stripe_price_basic": "price_basic",
    "stripe_price_pro": "price_pro",
    "stripe_price_premium": "price_premium",
}


class DemoAuditLimitTests(unittest.TestCase):
    def test_demo_audit_bypass_only_works_outside_production(self):
        dev_settings = Settings(**BASE_SETTINGS, app_env="development", demo_allow_unlimited_audits=True)
        self.assertTrue(dev_settings.should_bypass_audit_monthly_limit)

        prod_settings = Settings(
            **BASE_SETTINGS,
            app_env="production",
            clerk_jwt_audience="https://api.example.com",
            demo_allow_unlimited_audits=True,
        )
        self.assertFalse(prod_settings.should_bypass_audit_monthly_limit)


if __name__ == "__main__":
    unittest.main()
