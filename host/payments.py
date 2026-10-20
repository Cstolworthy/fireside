"""Payment providers - the one door money enters through.

The host never takes payment itself. A provider turns a requested purchase (an
amount in USD) into a settled charge, or refuses. No provider ships by default,
so the credits route refuses: purchases stay held until a real provider is
configured (Chris, 2026-10-20 - "hold sales until the provider lands"). Adding
Stripe later is registering an object here; the host is untouched.

A provider must be honest: `charge` returns a Receipt whose `ok` says whether the
money actually moved. The host credits characters only on ok, and the receipt's
reference is what an operator reconciles a purchase against.
"""

__all__ = ["Receipt", "register", "get", "configured", "REGISTRY"]


class Receipt:
    __slots__ = ("ok", "reference", "detail")

    def __init__(self, ok, reference="", detail=""):
        self.ok = bool(ok)
        self.reference = reference or ""
        self.detail = detail or ""

    def dict(self):
        return {"ok": self.ok, "reference": self.reference, "detail": self.detail}


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
