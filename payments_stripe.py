"""Stripe - the first registered payment provider.

This is the only file in the tree that talks to Stripe. The host knows nothing
about it: the deployment registers it by import path
(`FIRESIDE_PAYMENT_PROVIDER=payments_stripe`) and the key is read from the
environment at that moment, never from this file and never from the repository.

It speaks the Stripe REST API over `requests` (already a host dependency), so no
extra package is needed. The honesty rule is host/payments.py's: `charge` returns
ok=True only when Stripe says the charge actually settled. Anything else
(requires_action, processing, a declined card) is not money and gets ok=False, and
the host credits no characters.

Test vs live is the key: a `sk_test_` key charges test cards, a `sk_live_` key
charges real ones. Card entry belongs in the client (Stripe.js / Checkout); until
that lands, `charge` accepts a `payment_method` from the caller and, in test mode
only, falls back to Stripe's canned test card.
"""

import os

import requests

from host.payments import Receipt

STRIPE_API = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com").rstrip("/")


class StripeProvider:
    id = "stripe"

    def __init__(self, secret_key, api_base=STRIPE_API, timeout=20, post=None):
        self.key = secret_key
        self.api_base = (api_base or STRIPE_API).rstrip("/")
        self.timeout = timeout
        self._post = post or requests.post

    # -- helpers -------------------------------------------------------------

    def _auth(self):
        # Stripe uses HTTP basic auth with the secret key as the username.
        return (self.key, "")

    def live(self):
        return self.key.startswith("sk_live_")

    def account(self):
        """GET /v1/account - proves the key is real at startup, no charge made."""
        return self._post(self.api_base + "/v1/account", auth=self._auth(),
                          timeout=self.timeout)

    def _pm(self, meta):
        """The payment method to confirm with.

        A client sends `payment_method` (a token/element). If it does not, we only
        ever fall back to Stripe's canned test card, and only under a test key -
        a live key with no payment method must refuse, not guess.
        """
        pm = (meta or {}).get("payment_method")
        if pm:
            return pm
        if not self.live():
            return os.environ.get("STRIPE_TEST_PAYMENT_METHOD", "pm_card_visa")
        return ""

    def charge(self, usd, meta=None):
        meta = meta or {}
        try:
            amount = int(round(float(usd) * 100))
        except (TypeError, ValueError):
            return Receipt(False, "", "bad amount")
        if amount <= 0:
            return Receipt(False, "", "amount must be greater than zero")

        pm = self._pm(meta)
        if not pm:
            return Receipt(False, "", "no payment method supplied")

        data = {
            "amount": amount,
            "currency": "usd",
            "confirm": "true",
            "payment_method": pm,
            "description": meta.get("description", "fireside character credits"),
            "metadata[user]": str(meta.get("user", "")),
            "metadata[provider]": str(meta.get("provider", "")),
            "metadata[chars]": str(meta.get("chars", "")),
        }
        try:
            r = self._post(self.api_base + "/v1/payment_intents", data=data,
                           auth=self._auth(), timeout=self.timeout)
        except requests.RequestException as e:
            return Receipt(False, "", "stripe unreachable: %s" % e)
        try:
            j = r.json()
        except ValueError:
            return Receipt(False, "", "stripe returned non-JSON (http %d)"
                           % r.status_code)
        if r.status_code != 200:
            msg = (j.get("error") or {}).get("message") or ("http %d" % r.status_code)
            return Receipt(False, "", msg)

        status = j.get("status", "")
        if status == "succeeded":
            return Receipt(True, j.get("id", ""), "succeeded")
        # requires_action / processing / requires_payment_method: the money has
        # NOT settled, so the host must not credit characters.
        return Receipt(False, j.get("id", ""), "not settled (status=%s)" % status)


def from_env():
    """Build the provider from $FIRESIDE_STRIPE_KEY, or None if unset."""
    key = os.environ.get("FIRESIDE_STRIPE_KEY", "").strip()
    if not key:
        return None
    if not key.startswith("sk_"):
        raise ValueError("FIRESIDE_STRIPE_KEY is not a Stripe secret key (sk_...)")
    return StripeProvider(key)
