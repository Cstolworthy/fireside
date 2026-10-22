"""A stored Stripe Price is better than an inline one.

Inline `price_data` works, but it leaves an anonymous one-off price behind on
every checkout: the dashboard fills with them, renewals cannot be grouped by
product, and the Customer Portal has nothing coherent to reason about. When a
deployment has real Price ids we send those instead; when it does not, checkout
must still work.
"""

import json
import unittest

import payments_stripe as ps


class _Post:
    """Capture the form body Stripe would have received."""

    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {"id": "cs_test_1", "url": "https://checkout/x"}

    def __call__(self, url, data=None, auth=None, timeout=None):
        self.calls.append((url, dict(data or {})))
        return ps._Response(200, json.dumps(self.payload))


def _provider(post, **kw):
    return ps.StripeProvider("sk_test_1", post=post, **kw)


class TestPriceObjects(unittest.TestCase):
    def test_a_stored_price_is_sent_as_a_price_id(self):
        post = _Post()
        p = _provider(post, plans=[ps.Plan(id="platform", label="fireside platform",
                                           amount_cents=300, interval="month",
                                           price="price_abc")])
        out = p.checkout("platform", {"user": "u1"})
        self.assertTrue(out.ok)
        url, body = post.calls[-1]
        self.assertTrue(url.endswith("/v1/checkout/sessions"))
        self.assertEqual(body.get("line_items[0][price]"), "price_abc")
        self.assertNotIn("line_items[0][price_data][unit_amount]", body)

    def test_no_price_id_still_prices_inline(self):
        post = _Post()
        p = _provider(post)
        self.assertTrue(p.checkout("platform", {"user": "u1"}).ok)
        _, body = post.calls[-1]
        self.assertNotIn("line_items[0][price]", body)
        self.assertEqual(body["line_items[0][price_data][unit_amount]"], "300")
        self.assertEqual(body["line_items[0][price_data][recurring][interval]"], "month")

    def test_a_cart_mixes_configured_and_inline_lines(self):
        post = _Post()
        p = _provider(post, plans=[
            ps.Plan(id="platform", label="fireside platform", amount_cents=300,
                    interval="month", price="price_plat"),
            ps.Plan(id="storage", label="fireside storage +5 GB", amount_cents=100,
                    interval="month", price="")])
        out = p.checkout(None, {"user": "u1", "items": [
            {"plan": "platform", "quantity": 1}, {"plan": "storage", "quantity": 2}]})
        self.assertTrue(out.ok, out.detail)
        _, body = post.calls[-1]
        self.assertEqual(body["line_items[0][price]"], "price_plat")
        self.assertEqual(body["line_items[1][price_data][unit_amount]"], "100")
        self.assertEqual(body["line_items[1][quantity]"], "2")

    def test_env_attaches_price_ids(self):
        plans = ps._plans_from_env({"FIRESIDE_STRIPE_PRICE_PLATFORM": " price_p ",
                                    "FIRESIDE_STRIPE_PRICE_STORAGE": "price_s"})
        got = {x.id: x.price for x in plans}
        self.assertEqual(got, {"platform": "price_p", "storage": "price_s"})

    def test_no_env_leaves_prices_empty(self):
        plans = ps._plans_from_env({})
        self.assertEqual([x.price for x in plans], ["", ""])
        self.assertEqual(plans[0].amount_cents, 300)


class TestCreatePrices(unittest.TestCase):
    def test_one_product_and_price_per_plan(self):
        seen = []

        class Post:
            def __init__(self):
                self.n = 0

            def __call__(self, url, data=None, auth=None, timeout=None):
                self.n += 1
                seen.append((url, dict(data or {})))
                if url.endswith("/v1/products"):
                    return ps._Response(200, json.dumps({"id": "prod_%d" % (self.n // 2)}))
                return ps._Response(200, json.dumps({"id": "price_%d" % self.n}))

        made = ps.create_prices(_provider(Post()))
        self.assertEqual([m["plan"] for m in made], ["platform", "storage"])
        self.assertTrue(all(m["price"].startswith("price_") for m in made))
        price_calls = [d for u, d in seen if u.endswith("/v1/prices")]
        self.assertEqual(price_calls[0]["recurring[interval]"], "month")
        self.assertEqual(price_calls[0]["unit_amount"], "300")


if __name__ == "__main__":
    unittest.main()
