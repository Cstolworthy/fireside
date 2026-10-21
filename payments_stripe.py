"""Stripe - the first registered payment provider.

This is the only file in the tree that talks to Stripe. The host knows nothing
about it: the deployment registers it by import path
(`FIRESIDE_PAYMENT_PROVIDER=payments_stripe`) and the key is read from the
environment at that moment, never from this file and never from the repository.

It speaks the Stripe REST API over the standard library (urllib), so it adds no
package to the tree: a clean CI checkout with nothing installed still imports it.
The honesty rule is host/payments.py's: `charge` returns
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

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from host.payments import Checkout, Plan, Receipt, Subscription

STRIPE_API = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com").rstrip("/")


def _basic_auth(key):
    # Stripe uses HTTP basic auth with the secret key as the username.
    return "Basic " + base64.b64encode((key + ":").encode()).decode()


class _Response:
    """The slice of a `requests` response the provider reads: status + .json()."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return json.loads(self._body or "{}")


def _http_post(url, data=None, auth=None, timeout=None):
    body = urllib.parse.urlencode(data or {}, doseq=True).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if auth:
        req.add_header("Authorization", _basic_auth(auth[0]))
    try:
        with urllib.request.urlopen(req, timeout=timeout or 20) as r:
            return _Response(getattr(r, "status", 200), r.read().decode())
    except urllib.error.HTTPError as e:
        # Stripe error bodies (card declined, etc.) arrive as HTTP 4xx with JSON.
        return _Response(e.code, e.read().decode())


def _http_get(url, params=None, auth=None, timeout=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, method="GET")
    if auth:
        req.add_header("Authorization", _basic_auth(auth[0]))
    try:
        with urllib.request.urlopen(req, timeout=timeout or 20) as r:
            return _Response(getattr(r, "status", 200), r.read().decode())
    except urllib.error.HTTPError as e:
        return _Response(e.code, e.read().decode())

# The plans this provider can sell. The provider is the sole authority on price.
# The platform tier is the recurring one; character capacity is not a plan - it is
# bought one-shot through charge(). The storage add-on is the second recurring
# plan: a monthly block you buy N of, which the host records as an add-on rather
# than a tier. Its byte size lives in host/quota.py (STORAGE_BLOCK_BYTES).
DEFAULT_PLANS = [
    Plan(id="platform", label="fireside platform", amount_cents=300,
         currency="usd", interval="month",
         description="Server, updates, and 2GB storage. The platform only."),
    Plan(id="storage", label="fireside storage +5 GB", amount_cents=100,
         currency="usd", interval="month",
         description="Add 5 GB of storage, monthly. Buy as many blocks as you need."),
]


class StripeProvider:
    id = "stripe"

    def __init__(self, secret_key, api_base=STRIPE_API, timeout=20, post=None,
                 plans=None):
        self.key = secret_key
        self.api_base = (api_base or STRIPE_API).rstrip("/")
        self.timeout = timeout
        self._post = post or _http_post
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
        return _http_get(self.api_base + path, params=params,
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
        except (urllib.error.URLError, OSError) as e:
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
        """Create a Stripe-hosted Checkout Session, return its URL.

        Two shapes. A single plan: `plan_id` with `meta['quantity']` buying more
        than one of it (storage blocks). A cart: `meta['items']` is a list of
        `{"plan": id, "quantity": n}` line items, each priced from this
        provider's own price list. The tier and its storage add-on go out as one
        subscription, so a single payment settles both at once.
        """
        meta = meta or {}
        items = meta.get("items") or None
        if items:
            return self._checkout_items(items, meta)
        plan = self._plan(plan_id)
        if plan is None:
            return Checkout(False, detail="unknown plan %r" % plan_id)
        try:
            quantity = max(1, int(meta.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
        return_url = (meta.get("return_url") or "").strip()
        if return_url:
            sep = "&" if "?" in return_url else "?"
            success_url = return_url + sep + "checkout={CHECKOUT_SESSION_ID}"
        else:
            success_url = ""
        data = {
            "mode": "subscription" if plan.interval else "payment",
            "line_items[0][quantity]": str(quantity),
            "line_items[0][price_data][currency]": plan.currency,
            "line_items[0][price_data][unit_amount]": str(plan.amount_cents),
            "line_items[0][price_data][product_data][name]": plan.label or plan.id,
            "metadata[user]": str(meta.get("user", "")),
            "metadata[plan]": plan.id,
            "metadata[quantity]": str(quantity),
        }
        if plan.interval:
            data["line_items[0][price_data][recurring][interval]"] = plan.interval
        if success_url:
            data["success_url"] = success_url
            data["cancel_url"] = return_url
        return self._session(data)

    def _checkout_items(self, items, meta):
        """A cart: one line item per entry, all on one subscription session.

        Only names this provider actually prices are accepted - an unknown plan
        refuses the whole session rather than charging a guessed amount. The
        items are echoed into metadata so confirm() can apply each one.
        """
        out = []
        for it in items:
            pid = (it or {}).get("plan") or ""
            plan = self._plan(pid)
            if plan is None:
                return Checkout(False, detail="unknown plan %r" % pid)
            try:
                qty = max(1, int(it.get("quantity") or 1))
            except (TypeError, ValueError):
                qty = 1
            out.append({"plan": plan.id, "quantity": qty, "interval": plan.interval,
                        "amount_cents": plan.amount_cents, "currency": plan.currency,
                        "label": plan.label or plan.id})
        if not out:
            return Checkout(False, detail="empty cart")
        recurring = all(bool(o["interval"]) for o in out)
        if not recurring:
            return Checkout(False, detail="cart mixes recurring and one-time items")
        return_url = (meta.get("return_url") or "").strip()
        if return_url:
            sep = "&" if "?" in return_url else "?"
            success_url = return_url + sep + "checkout={CHECKOUT_SESSION_ID}"
        else:
            success_url = ""
        data = {
            "mode": "subscription",
            "metadata[user]": str(meta.get("user", "")),
            "metadata[items]": json.dumps([{"plan": o["plan"], "quantity": o["quantity"]}
                                           for o in out]),
        }
        for i, o in enumerate(out):
            b = "line_items[%d]" % i
            data[b + "[quantity]"] = str(o["quantity"])
            data[b + "[price_data][currency]"] = o["currency"]
            data[b + "[price_data][unit_amount]"] = str(o["amount_cents"])
            data[b + "[price_data][product_data][name]"] = o["label"]
            data[b + "[price_data][recurring][interval]"] = o["interval"]
        if success_url:
            data["success_url"] = success_url
            data["cancel_url"] = return_url
        return self._session(data)

    def _session(self, data):
        try:
            r = self._post(self.api_base + "/v1/checkout/sessions", data=data,
                           auth=self._auth(), timeout=self.timeout)
        except (urllib.error.URLError, OSError) as e:
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
        items = []
        raw = md.get("items")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    items = [{"plan": str((x or {}).get("plan", "")),
                              "quantity": int((x or {}).get("quantity") or 1)}
                             for x in parsed if (x or {}).get("plan")]
            except (ValueError, TypeError):
                items = []
        try:
            quantity = int(md.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
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
                            detail="ok", quantity=quantity, items=items)


def from_env():
    """Build the provider from $FIRESIDE_STRIPE_KEY, or None if unset."""
    key = os.environ.get("FIRESIDE_STRIPE_KEY", "").strip()
    if not key:
        return None
    if not key.startswith("sk_"):
        raise ValueError("FIRESIDE_STRIPE_KEY is not a Stripe secret key (sk_...)")
    return StripeProvider(key)
