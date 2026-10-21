"""Outbound transactional email: one verb, send().

A deployment with no mailer configured sends nothing and says so. That is
deliberate: a password reset link is only worth minting if it can reach an
inbox, so the flow is honestly *unavailable*, not silently broken, until a
sender exists.

Resend is the first provider because it is one POST and a free tier. It is a
provider, not the shape: send() is the seam, so a second sender (Postmark, SES)
drops in behind it without the caller changing.
"""

import json
import os
import urllib.request

API = "https://api.resend.com/emails"


def _env(name):
    return (os.environ.get(name) or "").strip()


def configured():
    """True when a sender and a from-address are both present."""
    return bool(_env("FIRESIDE_RESEND_API_KEY") and _env("FIRESIDE_MAIL_FROM"))


def send(to, subject, text, html=None, fetch=None):
    """Send one message. Returns True when the provider accepted it, False when
    no mailer is configured or the send failed.

    Never raises: mail is a side channel, and a dead one must not take the
    request that triggered it down with it. A caller that needs to know whether
    a link actually went out checks the return value; a caller that only needs
    the request to survive ignores it.
    """
    if not configured():
        return False
    payload = {
        "from": _env("FIRESIDE_MAIL_FROM"),
        "to": [to],
        "subject": subject,
        "text": text,
    }
    if html:
        payload["html"] = html
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + _env("FIRESIDE_RESEND_API_KEY"),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "fireside",
        },
    )
    try:
        (fetch or (lambda r: urllib.request.urlopen(r, timeout=15).read()))(req)
        return True
    except Exception:
        return False
