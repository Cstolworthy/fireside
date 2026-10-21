"""The plan catalog: what a user can be on, and what each includes.

One place turns quota's policy constants and the payment provider's price list
into the shape the screens read. It names two tiers and one add-on:

  free      the sign-up default; an allowance, no price
  platform  the paid tier that funds the server; its price is the provider's
  storage   an add-on sold by the block; its price is the provider's too

Prices are never invented here. A tier or a block with no provider price reports
`amount_cents: None` and the screen says the price is not set yet, which is the
honest state until a payment provider is registered.
"""

from host import quota

STORAGE_ADDON = quota.STORAGE_ADDON


def _price_of(provider, plan_id):
    """(amount_cents, currency, interval) a provider charges for `plan_id`, or
    (None, 'usd', interval-guess) when there is no provider or no such plan."""
    fn = getattr(provider, "plans", None)
    if not callable(fn):
        return None, "usd", None
    try:
        plans = fn() or []
    except Exception:
        return None, "usd", None
    for p in plans:
        d = p.dict() if hasattr(p, "dict") else dict(p)
        if d.get("id") == plan_id:
            return d.get("amount_cents"), d.get("currency", "usd"), d.get("interval")
    return None, "usd", None


def catalog(provider=None):
    """The offering, ready to render.

    `tiers` are exclusive choices (you are on exactly one); `storage` is an
    add-on you buy N blocks of, on top of any tier.
    """
    plat_cents, plat_cur, plat_int = _price_of(provider, quota.PLATFORM_PLAN)
    blk_cents, blk_cur, blk_int = _price_of(provider, STORAGE_ADDON)
    configured = provider is not None
    return {
        "payments_configured": configured,
        "tiers": [
            {"id": quota.FREE_PLAN, "label": "Free", "amount_cents": 0,
             "currency": "usd", "interval": None,
             "included_bytes": quota.FREE_ALLOWANCE_BYTES,
             "description": "Enough for a few books. Storage is shown, not billed."},
            {"id": quota.PLATFORM_PLAN, "label": "fireside platform",
             "amount_cents": plat_cents, "currency": plat_cur,
             "interval": plat_int or "month",
             "included_bytes": quota.PLATFORM_ALLOWANCE_BYTES,
             "description": "The server, updates, and 2 GB of storage."},
        ],
        "storage": {
            "addon": STORAGE_ADDON,
            "block_bytes": quota.STORAGE_BLOCK_BYTES,
            "amount_cents": blk_cents, "currency": blk_cur,
            "interval": blk_int or "month",
            "description": "Add storage in blocks, monthly. Buy as many as you need.",
        },
    }
