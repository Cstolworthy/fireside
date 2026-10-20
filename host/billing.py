"""Recurring platform subscriptions, kept on the server.

The platform tier is separate from character capacity: it pays for the server and
storage and buys nothing else (docs/PRICING.md). The host holds only the state the
payment provider reported - it never computes a price or a period. One active
subscription per user for now; the provider is the authority on status.
"""

from host.db import now


def get(conn, uid):
    r = conn.execute("SELECT * FROM subscriptions WHERE user_id=?",
                     (uid,)).fetchone()
    if not r:
        return None
    return {"user_id": r["user_id"], "plan": r["plan"], "status": r["status"],
            "provider": r["provider"], "reference": r["reference"],
            "current_period_end": r["current_period_end"],
            "updated_at": r["updated_at"]}


def upsert(conn, uid, plan, status, provider="", reference="",
           current_period_end=0):
    """Record what the provider said. Merges into the one row a user has."""
    conn.execute(
        "INSERT INTO subscriptions (user_id, plan, status, provider, reference, "
        "current_period_end, updated_at, created_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET plan=excluded.plan, "
        "status=excluded.status, provider=excluded.provider, "
        "reference=excluded.reference, "
        "current_period_end=excluded.current_period_end, "
        "updated_at=excluded.updated_at",
        (uid, plan, status, provider, reference, int(current_period_end or 0),
         now(), now()))
    conn.commit()
    return get(conn, uid)


def active(conn, uid):
    """True when the user's subscription is currently paid/active."""
    s = get(conn, uid)
    return bool(s and s["status"] in ("active", "trialing"))
