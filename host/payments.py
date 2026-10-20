"""Payment providers - the one door money enters through.

The host never takes payment itself. A provider turns a requested purchase into a
settled charge, or refuses. No provider ships by default, so the routes that would
sell refuse: purchases stay held until a real provider is configured (Chris,
2026-10-20 - "hold sales until the provider lands"). Adding Stripe is registering
an object here; the host is untouched.

A provider must be honest. `charge` returns a Receipt whose `ok` says whether the
money actually moved; the host credits characters only on ok, and the receipt's
reference is what an operator reconciles a purchase against. The same rule holds
for the recurring tier: `checkout` returns a URL to pay at, and `confirm` returns
a Subscription whose `ok` says the money settled - the host records a subscription
only on ok.

Two things money buys, two shapes:

  * character capacity - one-shot. `charge(usd, meta) -> Receipt`.
  * the platform tier   - recurring. `plans()`, `checkout(plan_id, meta)`,
                          `confirm(reference)`.

A provider need not implement the recurring trio; the host checks with getattr and
says "payments not configured" if the method is absent.
"""

import importlib
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["Receipt", "Plan", "Checkout", "Subscription",
           "register", "get", "configured", "load", "REGISTRY"]


@dataclass
class Receipt:
    ok: bool = False
    reference: str = ""
    detail: str = ""

    def dict(self):
        return {"ok": self.ok, "reference": self.reference, "detail": self.detail}


@dataclass
class Plan:
    """A recurring (or one-time) thing the provider can sell. The provider is the
    sole authority on price and interval; the host only lists what it is handed."""
    id: str = ""
    label: str = ""
    amount_cents: int = 0
    currency: str = "usd"
    interval: Optional[str] = None      # "month"/"year" recurring; None = one-time
    description: str = ""

    def dict(self):
        return {"id": self.id, "label": self.label, "amount_cents": self.amount_cents,
                "currency": self.currency, "interval": self.interval,
                "description": self.description}


@dataclass
class Checkout:
    """Where to send a buyer to pay. `url` is the hosted payment page."""
    ok: bool = False
    url: str = ""
    reference: str = ""
    detail: str = ""

    def dict(self):
        return {"ok": self.ok, "url": self.url, "reference": self.reference,
                "detail": self.detail}


@dataclass
class Subscription:
    """What `confirm` says about a completed checkout. `ok` means paid/active."""
    ok: bool = False
    user: str = ""
    plan: str = ""
    status: str = ""
    current_period_end: int = 0
    reference: str = ""
    detail: str = ""

    def dict(self):
        return {"ok": self.ok, "user": self.user, "plan": self.plan,
                "status": self.status, "current_period_end": self.current_period_end,
                "reference": self.reference, "detail": self.detail}


# id -> provider object. Empty until an operator registers one.
REGISTRY = {}


def register(provider):
    """Add a payment provider. `provider.id` is its stable slug."""
    REGISTRY[provider.id] = provider
    return provider


def get(provider_id=None):
    if provider_id:
        return REGISTRY.get(provider_id)
    return next(iter(REGISTRY.values()), None)


def configured():
    return bool(REGISTRY)


def load(dotted=None):
    """Register the provider named by $FIRESIDE_PAYMENT_PROVIDER (or `dotted`).

    The core never names a provider: the operator points at a module, the host
    imports it and asks for `from_env()`. A module with no key configured returns
    None and nothing is registered, so an unconfigured deployment keeps refusing
    purchases. Returns the provider object, or None.
    """
    dotted = dotted or os.environ.get("FIRESIDE_PAYMENT_PROVIDER", "").strip()
    if not dotted:
        return None
    mod = importlib.import_module(dotted)
    provider = mod.from_env()
    if provider is not None:
        register(provider)
    return provider
