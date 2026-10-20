"""Stripe - the first registered payment provider.

This is the only file in the tree that talks to Stripe. The host knows nothing
about it: the deployment registers it by import path
(`FIRESIDE_PAYMENT_PROVIDER=payments_stripe`) and the key is read from the
environment at that moment, never from this file and never from the repository.

It speaks the Stripe REST API over `requests` (already a host dependency), so no
extra package is needed. The honesty rule is host/payments.py's: `charge` returns
ok=True only when Stripe says the charge actually settled, and `confirm` returns a
subscription only when Stripe says the checkout is complete and paid. Anything
else (requires_action, processing, a declined card, an abandoned session) is not
money and records nothing.

Test vs live is the key: a `sk_test_` key charges test cards, a `sk_live_` key
charges real ones. Card entry belongs in the client; until that lands, `charge`
accepts a `payment_method` from the caller and, in test mode only, falls back to
Stripe's canned test card. The recurring tier uses Stripe-hosted Checkout, so the
client never sees a card at all - it just opens the URL.
"""

import os

import requests

from host.payments import Checkout, Plan, Receipt, Subscription

STRIPE_API = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com").rstrip("/")

# The plans this provider can sell. The provider is the sole authority on price.
# The platform tier is the recurring one; character capacity is not a plan - it is
# bought one-shot through charge(). Storage add-on blocks are not sold yet, because
# we do not enforce a storage quota until that is decided.
DEFAULT_PLANS = [
    Plan(id="platform", label="fireside platform", amount_cents=300,
         currency="usd", interval="month",
         description="Server, updates, and 2GB storage. The platform only."),
]


class StripeProvider:
    id = "stripe"

    def __init__(self, secret_key, api_base=STRIPE_API, timeout=20, post=None,
                 plans=None):
        self.key = secret_key
        self.api_base = (api_base or STRIPE_API).rstrip("/")
        self.timeout = timeout
        self._post = post or requests.post
        self._plans = list(plans) if plans is not None else list(DEFAULT_PLANS)

    # -- helpers -------------------------------------------------------------

    def _auth(self):
        # Stripe uses HTTP basic auth with the secret key as the username.
        return (self.key, "")

    def _get(self, path, params=None):
        fn = getattr(self._post, "get", None)
        if callable(fn):
            return fn(self.api_base + path, params=params, auth=self._auth(),
                      timeout=self.timeout)
        return requests.get(self.api_base + path, params=params,
                            auth=self._auth(), timeout=self.timeout)

    def live(self):
        return self.key.startswith("sk_live_")

    def account(self):
        """GET /v1/account - proves the key is real at startup, no charge made."""
        return self._get("/v1/account")

    # -- character capacity (one-shot) ---------------------------------------

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

    # -- the recurring platform tier -----------------------------------------

    def plans(self):
        return list(self._plans)

    def _plan(self, plan_id):
        for p in self._plans:
            if p.id == plan_id:
                return p
        return None

    def checkout(self, plan_id, meta=None):
        """Create a Stripe-hosted Checkout Session for a plan, return its URL."""
        meta = meta or {}
        plan = self._plan(plan_id)
        if plan is None:
            return Checkout(False, detail="unknown plan %r" % plan_id)
        return_url = (meta.get("return_url") or "").strip()
        if return_url:
            sep = "&" if "?" in return_url else "?"
            success_url = return_url + sep + "checkout={CHECKOUT_SESSION_ID}"
        else:
            success_url = ""
        data = {
            "mode": "subscription" if plan.interval else "payment",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": plan.currency,
            "line_items[0][price_data][unit_amount]": str(plan.amount_cents),
            "line_items[0][price_data][product_data][name]": plan.label or plan.id,
            "metadata[user]": str(meta.get("user", "")),
            "metadata[plan]": plan.id,
        }
        if plan.interval:
            data["line_items[0][price_data][recurring][interval]"] = plan.interval
        if success_url:
            data["success_url"] = success_url
            data["cancel_url"] = return_url
        try:
            r = self._post(self.api_base + "/v1/checkout/sessions", data=data,
                           auth=self._auth(), timeout=self.timeout)
        except requests.RequestException as e:
            return Checkout(False, detail="stripe unreachable: %s" % e)
        try:
            j = r.json()
        except ValueError:
            return Checkout(False, detail="stripe returned non-JSON (http %d)"
                            % r.status_code)
        if r.status_code != 200:
            msg = (j.get("error") or {}).get("message") or ("http %d" % r.status_code)
            return Checkout(False, detail=msg)
        return Checkout(True, j.get("url", ""), j.get("id", ""))

    def confirm(self, reference):
        """Read a Checkout Session back and say whether it is paid + active."""
        ref = (reference or "").strip()
        if not ref:
            return Subscription(False, detail="no checkout reference")
        r = self._get("/v1/checkout/sessions/" + ref, params={"expand[]": "subscription"})
        try:
            j = r.json()
        except ValueError:
            return Subscription(False, detail="stripe returned non-JSON (http %d)"
                                % r.status_code)
        if r.status_code != 200:
            msg = (j.get("error") or {}).get("message") or ("http %d" % r.status_code)
            return Subscription(False, reference=ref, detail=msg)
        md = j.get("metadata") or {}
        plan = md.get("plan", "")
        user = md.get("user", "")
        complete = j.get("status") == "complete"
        paid = j.get("payment_status") in ("paid", "no_payment_required")
        sub = j.get("subscription") or {}
        if isinstance(sub, str) or not sub:
            sub = {}
        period_end = int(sub.get("current_period_end") or 0)
        status = sub.get("status") or ("complete" if complete else j.get("status", ""))
        if not complete:
            return Subscription(False, user=user, plan=plan, status=j.get("status", ""),
                                reference=ref, detail="checkout not complete")
        if not paid:
            return Subscription(False, user=user, plan=plan, status=status,
                                reference=ref, detail="not paid")
        return Subscription(True, user=user, plan=plan, status=status or "active",
                            current_period_end=period_end, reference=ref,
                            detail="ok")


def from_env():
    """Build the provider from $FIRESIDE_STRIPE_KEY, or None if unset."""
    key = os.environ.get("FIRESIDE_STRIPE_KEY", "").strip()
    if not key:
        return None
    if not key.startswith("sk_"):
        raise ValueError("FIRESIDE_STRIPE_KEY is not a Stripe secret key (sk_...)")
    return StripeProvider(key)
