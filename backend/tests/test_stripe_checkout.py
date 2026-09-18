import unittest
from unittest.mock import patch

from app.services import stripe_service


class StripeCheckoutTests(unittest.TestCase):
    def test_placeholder_price_id_is_rejected_before_checkout(self):
        with patch.dict(stripe_service.PLAN_PRICE_MAP, {"pro": "price_placeholder"}):
            with self.assertRaises(ValueError) as ctx:
                stripe_service._configured_price_id("pro")

        self.assertIn("STRIPE_PRICE_PRO", str(ctx.exception))

    def test_real_price_id_is_accepted(self):
        with patch.dict(stripe_service.PLAN_PRICE_MAP, {"pro": "price_123456789"}):
            self.assertEqual(stripe_service._configured_price_id("pro"), "price_123456789")

    def test_unknown_plan_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            stripe_service._configured_price_id("enterprise")

        self.assertIn("Unknown plan", str(ctx.exception))

    def test_checkout_session_must_belong_to_org(self):
        # stripe-python 15.x returns attribute-style objects, not dicts.
        fake_session = type("Session", (), {
            "id": "cs_test_123",
            "client_reference_id": "other_org",
            "metadata": {"lbt_org_id": "other_org", "plan": "pro"},
            "subscription": None,
            "status": "complete",
            "payment_status": "paid",
        })()

        with patch.object(stripe_service.stripe.checkout.Session, "retrieve", return_value=fake_session):
            with self.assertRaises(ValueError) as ctx:
                stripe_service.verify_checkout_session(None, "org_123", "cs_test_123")

        self.assertIn("does not belong", str(ctx.exception))

    def test_checkout_session_syncs_subscription_for_matching_org(self):
        fake_subscription = type("Sub", (), {
            "id": "sub_123",
            "status": "active",
            "metadata": {"lbt_org_id": "org_123", "plan": "pro"},
        })()
        fake_session = type("Session", (), {
            "id": "cs_test_123",
            "client_reference_id": "org_123",
            "metadata": {"lbt_org_id": "org_123", "plan": "pro"},
            "subscription": fake_subscription,
            "status": "complete",
            "payment_status": "paid",
        })()

        with patch.object(stripe_service.stripe.checkout.Session, "retrieve", return_value=fake_session), \
             patch.object(stripe_service, "sync_subscription") as sync:
            result = stripe_service.verify_checkout_session(None, "org_123", "cs_test_123")

        sync.assert_called_once_with(None, fake_subscription)
        self.assertEqual(result["plan"], "pro")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["payment_status"], "paid")


if __name__ == "__main__":
    unittest.main()
