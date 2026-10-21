"""Recurring platform subscriptions, kept on the server.

The platform tier is separate from character capacity: it pays for the server and
storage and buys nothing else (docs/PRICING.md). The host holds only the state the
payment provider reported - it never computes a price or a period. One active
subscription per user for now; the provider is the authority on status.

Add-ons are separate rows: a storage block is a monthly thing you buy N of, and
it adds bytes to whichever tier you are on instead of replacing it. The provider
is still the authority on status and period; this module holds the count.
"""

from host.db import now

STORAGE_ADDON = "storage"


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


def assign_free(conn, uid):
    """Put a new account on the free tier, once.

    Called when a user row is first created so sign-ups start at 500 MB. An
    account that already has any subscription row is left alone, and a missing
    row keeps failing open to the platform allowance (quota.allowance_bytes), so
    accounts that predate the free tier are not silently cut down to 500 MB.
    """
    if get(conn, uid) is not None:
        return get(conn, uid)
    return upsert(conn, uid, "free", "active", provider="", reference="")


def addons(conn, uid):
    """{addon: row} for the user's add-ons."""
    out = {}
    for r in conn.execute("SELECT * FROM addons WHERE user_id=?", (uid,)):
        out[r["addon"]] = {"addon": r["addon"], "quantity": r["quantity"],
                           "status": r["status"], "provider": r["provider"],
                           "reference": r["reference"],
                           "current_period_end": r["current_period_end"]}
    return out


def upsert_addon(conn, uid, addon, quantity, status, provider="", reference="",
                 current_period_end=0):
    """Record how many of an add-on the provider says are paid for."""
    conn.execute(
        "INSERT INTO addons (user_id, addon, quantity, status, provider, "
        "reference, current_period_end, updated_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id, addon) DO UPDATE SET quantity=excluded.quantity, "
        "status=excluded.status, provider=excluded.provider, "
        "reference=excluded.reference, "
        "current_period_end=excluded.current_period_end, "
        "updated_at=excluded.updated_at",
        (uid, addon, int(quantity or 0), status, provider, reference,
         int(current_period_end or 0), now(), now()))
    conn.commit()
    return addons(conn, uid).get(addon)


def blocks(conn, uid):
    """Paid storage blocks the account currently holds (an active add-on)."""
    a = addons(conn, uid).get(STORAGE_ADDON)
    if not a or a["status"] not in ("active", "trialing"):
        return 0
    try:
        return max(0, int(a["quantity"] or 0))
    except (TypeError, ValueError):
        return 0


def list_all(conn):
    """{user_id: row} for every account with a subscription row. An account
    with no row is on no tier here and fails open to the platform allowance."""
    out = {}
    for r in conn.execute("SELECT * FROM subscriptions"):
        out[r["user_id"]] = {"user_id": r["user_id"], "plan": r["plan"],
                             "status": r["status"], "provider": r["provider"],
                             "reference": r["reference"],
                             "current_period_end": r["current_period_end"]}
    return out
