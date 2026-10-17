"""Default quote/report for a speech plugin that only knows its per-M chars rate.

A plugin that has real pricing logic (tiers, minimums, a sale connection markup)
overrides these. A plugin that does not still answers the contract honestly: a
rate of None means unpriced, and an unpriced run reports `unknown`, never zero.
"""

from host.contracts import Quote, Report


class AccountingMixin:
    def quote(self, ctx):
        ctx = ctx or {}
        chars = int(ctx.get("chars_est") or 0)
        try:
            rate = self.cost_per_1m_chars(ctx.get("cred"))
        except Exception:
            rate = None
        price = (chars / 1_000_000.0) * rate if rate is not None else 0.0
        return Quote(chars_est=chars, price=round(price, 6),
                     currency=ctx.get("currency", "USD"), eligible=True,
                     authoritative=rate is not None)

    def report(self, ctx):
        ctx = ctx or {}
        return Report(charge="unknown", chars_actual=ctx.get("chars_actual"))
