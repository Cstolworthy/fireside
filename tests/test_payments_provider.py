"""The Stripe provider and how a payment provider is loaded.

No network: `StripeProvider` takes an injectable `post`, so the response shape is
driven by a fake here. The one property that matters is honest: ok=True only when
Stripe reports the charge settled. A declined card, requires_action, or a 4xx is
not money and must credit nothing.
"""

import os
import unittest

from host import payments
import payments_stripe


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakePost:
    """Records the call and returns a canned response."""

    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def __call__(self, url, data=None, auth=None, timeout=None):
        self.calls.append({"url": url, "data": data, "auth": auth})
        return self.resp


class TestStripeCharge(unittest.TestCase):
    def test_succeeded_is_a_receipt_of_real_money(self):
        fp = _FakePost(_Resp(200, {"id": "pi_123", "status": "succeeded"}))
        p = payments_stripe.StripeProvider("sk_test_x", post=fp)
        r = p.charge(5.0, {"user": "u1", "chars": 254712})
        self.assertTrue(r.ok)
        self.assertEqual(r.reference, "pi_123")
        # 5.00 USD -> 500 cents, and the metadata rides along for reconciliation
        self.assertEqual(fp.calls[0]["data"]["amount"], 500)
        self.assertEqual(fp.calls[0]["data"]["currency"], "usd")
        self.assertEqual(fp.calls[0]["data"]["metadata[chars]"], "254712")

    def test_requires_action_credits_nothing(self):
        fp = _FakePost(_Resp(200, {"id": "pi_9", "status": "requires_action"}))
        p = payments_stripe.StripeProvider("sk_test_x", post=fp)
        r = p.charge(5.0, {})
        self.assertFalse(r.ok)
        self.assertIn("not settled", r.detail)

    def test_card_error_is_refused_with_reason(self):
        fp = _FakePost(_Resp(402, {"error": {"message": "Your card was declined."}}))
        p = payments_stripe.StripeProvider("sk_test_x", post=fp)
        r = p.charge(5.0, {})
        self.assertFalse(r.ok)
        self.assertEqual(r.detail, "Your card was declined.")

    def test_zero_and_bad_amounts_buy_nothing(self):
        p = payments_stripe.StripeProvider("sk_test_x", post=_FakePost(_Resp(200, {})))
        self.assertFalse(p.charge(0, {}).ok)
        self.assertFalse(p.charge("abc", {}).ok)

    def test_live_key_without_a_payment_method_refuses(self):
        p = payments_stripe.StripeProvider("sk_live_x",
                                           post=_FakePost(_Resp(200, {})))
        r = p.charge(5.0, {})          # no payment_method, and no test fallback
        self.assertFalse(r.ok)
        self.assertIn("no payment method", r.detail)

    def test_test_key_falls_back_to_a_test_card(self):
        fp = _FakePost(_Resp(200, {"id": "pi_1", "status": "succeeded"}))
        p = payments_stripe.StripeProvider("sk_test_x", post=fp)
        p.charge(1.0, {})
        self.assertEqual(fp.calls[0]["data"]["payment_method"], "pm_card_visa")

    def test_client_supplied_payment_method_wins(self):
        fp = _FakePost(_Resp(200, {"id": "pi_1", "status": "succeeded"}))
        p = payments_stripe.StripeProvider("sk_test_x", post=fp)
        p.charge(1.0, {"payment_method": "pm_from_client"})
        self.assertEqual(fp.calls[0]["data"]["payment_method"], "pm_from_client")


class TestFromEnv(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FIRESIDE_STRIPE_KEY", None)
        os.environ.pop("FIRESIDE_PAYMENT_PROVIDER", None)
        payments.REGISTRY.clear()

    def test_no_key_registers_nothing(self):
        self.assertIsNone(payments_stripe.from_env())

    def test_non_secret_key_is_rejected(self):
        os.environ["FIRESIDE_STRIPE_KEY"] = "pk_test_notasecret"
        with self.assertRaises(ValueError):
            payments_stripe.from_env()

    def test_load_registers_the_named_provider(self):
        os.environ["FIRESIDE_PAYMENT_PROVIDER"] = "payments_stripe"
        os.environ["FIRESIDE_STRIPE_KEY"] = "sk_test_dummy"
        self.assertIsNone(payments.get())
        p = payments.load()
        self.assertIsNotNone(p)
        self.assertTrue(payments.configured())
        self.assertIs(payments.get("stripe"), p)

    def test_load_without_a_provider_name_is_a_noop(self):
        self.assertIsNone(payments.load())
        self.assertFalse(payments.configured())


if __name__ == "__main__":
    unittest.main()
