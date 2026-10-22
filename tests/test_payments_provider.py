"""The Stripe provider and how a payment provider is loaded.

No network: `StripeProvider` takes an injectable `post`, so the response shape is
driven by a fake here. The one property that matters is honest: ok=True only when
Stripe reports the charge settled. A declined card, requires_action, or a 4xx is
not money and must credit nothing.
"""

import os
import time
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


class TestCheckoutSessions(unittest.TestCase):
    """The one place a buyer is sent to pay. Two things must hold every time."""

    def _p(self, fp):
        return payments_stripe.StripeProvider("rk_live_x", post=fp,
                                              plans=payments_stripe.DEFAULT_PLANS)

    def test_a_single_plan_session_opts_out_of_managed_payments(self):
        fp = _FakePost(_Resp(200, {"id": "cs_1", "url": "https://pay.stripe/cs_1"}))
        c = self._p(fp).checkout("platform", {"user": "u1",
                                              "return_url": "https://readfireside.com/"})
        self.assertTrue(c.ok)
        self.assertEqual(c.reference, "cs_1")
        data = fp.calls[0]["data"]
        # Stripe defaults Managed Payments on for some accounts; left implicit the
        # session 400s for a missing product tax_code and Stripe becomes the
        # merchant of record. We sell as ourselves, so the session says so.
        self.assertEqual(data["managed_payments[enabled]"], "false")
        self.assertEqual(data["mode"], "subscription")

    def test_a_cart_session_also_opts_out(self):
        fp = _FakePost(_Resp(200, {"id": "cs_2", "url": "https://pay.stripe/cs_2"}))
        c = self._p(fp).checkout(None, {"user": "u1", "return_url": "https://readfireside.com/",
                                        "items": [{"plan": "platform", "quantity": 1},
                                                  {"plan": "storage", "quantity": 2}]})
        self.assertTrue(c.ok)
        data = fp.calls[0]["data"]
        self.assertEqual(data["managed_payments[enabled]"], "false")
        self.assertEqual(data["line_items[1][quantity]"], "2")

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

    def test_restricted_key_is_accepted(self):
        # A restricted key (rk_) is a valid server credential with fewer
        # granted permissions; it must register, and live mode is the mode
        # segment, not the sk_/rk_ prefix.
        os.environ["FIRESIDE_STRIPE_KEY"] = "rk_test_dummy"
        p = payments_stripe.from_env()
        self.assertIsNotNone(p)
        self.assertFalse(p.live())
        os.environ["FIRESIDE_STRIPE_KEY"] = "rk_live_dummy"
        self.assertTrue(payments_stripe.from_env().live())

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


class TestWebhookSignature(unittest.TestCase):
    """The webhook is public, so its signature is the only authentication."""

    SECRET = "whsec_test_abc"

    def _sig(self, raw, t=None, secret=None):
        import hashlib
        import hmac as _hmac
        t = t if t is not None else int(time.time())
        signed = (str(t) + ".").encode() + raw
        v1 = _hmac.new((secret or self.SECRET).encode(), signed,
                       hashlib.sha256).hexdigest()
        return "t=%d,v1=%s" % (t, v1)

    def test_a_real_signature_verifies(self):
        raw = b'{"type":"checkout.session.completed"}'
        self.assertTrue(payments_stripe.verify_signature(
            raw, self._sig(raw), self.SECRET))

    def test_tampered_body_or_wrong_secret_fails(self):
        raw = b'{"a":1}'
        sig = self._sig(raw)
        self.assertFalse(payments_stripe.verify_signature(b'{"a":2}', sig,
                                                          self.SECRET))
        self.assertFalse(payments_stripe.verify_signature(raw, sig, "whsec_other"))

    def test_a_stale_timestamp_is_refused(self):
        raw = b'{}'
        old = int(time.time()) - 3600
        self.assertFalse(payments_stripe.verify_signature(
            raw, self._sig(raw, t=old), self.SECRET))

    def test_missing_pieces_are_not_a_pass(self):
        self.assertFalse(payments_stripe.verify_signature(b'{}', "", self.SECRET))
        self.assertFalse(payments_stripe.verify_signature(b'{}', "t=1", self.SECRET))

    def test_verify_webhook_needs_a_configured_secret(self):
        p = payments_stripe.StripeProvider("sk_test_x", webhook_secret="")
        ev, err = p.verify_webhook(b"{}", "t=1,v1=x")
        self.assertIsNone(ev)
        self.assertIn("webhook secret", err)

    def test_verify_webhook_returns_the_event_only_when_signed(self):
        p = payments_stripe.StripeProvider("sk_test_x", webhook_secret=self.SECRET)
        raw = b'{"type":"checkout.session.completed","data":{"object":{"id":"cs_1"}}}'
        ev, err = p.verify_webhook(raw, self._sig(raw))
        self.assertEqual(err, "")
        self.assertEqual(ev["type"], "checkout.session.completed")
        self.assertIsNone(p.verify_webhook(raw, "t=1,v1=deadbeef")[0])

    def test_only_a_completed_checkout_names_a_session(self):
        p = payments_stripe.StripeProvider("sk_test_x", webhook_secret=self.SECRET)
        done = {"type": "checkout.session.completed",
                "data": {"object": {"id": "cs_1"}}}
        self.assertEqual(p.webhook_reference(done), "cs_1")
        other = {"type": "invoice.paid", "data": {"object": {"id": "in_1"}}}
        self.assertEqual(p.webhook_reference(other), "")


if __name__ == "__main__":
    unittest.main()
