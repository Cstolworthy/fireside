"""Social sign-in for the host: OAuth authorization-code flow + server sessions.

One deployment-agnostic shape: the host owns the session, the provider owns the
identity. No plugin is involved, no provider is imported by name - the table
below is data. A provider only exists if its client id and secret are present in
the environment, so a deployment without credentials simply offers no buttons,
and no secret ever lives in the repo.

Contract:
  providers()                     -> [{id, label, start}] for the configured set
  authorize_url(pid, redirect, s) -> the URL to send the browser to
  login(pid, code, redirect)      -> (user_id, session_token) after a callback
  session_user(token)             -> user_id or None (expiry enforced)
"""

import json
import os
import secrets
import time
import urllib.parse
import urllib.request

SESSION_TTL = 30 * 24 * 3600          # 30 days; cookie and row share it
STATE_COOKIE = "fireside_oauth_state"
SESSION_COOKIE = "fireside_session"

# provider table: data, not code paths. id_env names the two secrets to read.
PROVIDERS = {
    "github": {
        "label": "GitHub",
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        "identity": "https://api.github.com/user",
        "emails": "https://api.github.com/user/emails",
        "scope": "read:user user:email",
        "id_env": ("FIRESIDE_OAUTH_GITHUB_CLIENT_ID", "FIRESIDE_OAUTH_GITHUB_SECRET"),
    },
    "google": {
        "label": "Google",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "identity": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
        "id_env": ("FIRESIDE_OAUTH_GOOGLE_CLIENT_ID", "FIRESIDE_OAUTH_GOOGLE_SECRET"),
    },
    "microsoft": {
        "label": "Microsoft",
        "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "identity": "https://graph.microsoft.com/oidc/userinfo",
        "scope": "openid email profile",
        "id_env": ("FIRESIDE_OAUTH_MICROSOFT_CLIENT_ID", "FIRESIDE_OAUTH_MICROSOFT_SECRET"),
    },
}


class AuthError(Exception):
    pass


def _default_fetch(req, timeout=20):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def new_state():
    return secrets.token_urlsafe(16)


def _identity(pid, data):
    """Normalize a provider's profile to {subject, name, email}."""
    if pid == "github":
        return {"subject": str(data.get("id") or ""),
                "name": data.get("name") or data.get("login") or "",
                "email": data.get("email") or ""}
    return {"subject": str(data.get("sub") or data.get("id") or ""),
            "name": data.get("name") or data.get("preferred_username") or "",
            "email": data.get("email") or ""}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---- sessions (rows in sqlite; the cookie only carries the token) -------------

def create_session(conn, user_id, provider, ttl=SESSION_TTL):
    tok = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute("INSERT INTO sessions(token,user_id,provider,created_at,expires_at)"
                 " VALUES(?,?,?,?,?)", (tok, user_id, provider, now, now + ttl))
    conn.commit()
    return tok


def session_user(conn, token):
    if not token:
        return None
    row = conn.execute("SELECT user_id, expires_at FROM sessions WHERE token=?",
                       (token,)).fetchone()
    if row is None:
        return None
    if int(row["expires_at"]) < int(time.time()):
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return None
    return row["user_id"]


def delete_session(conn, token):
    if token:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()


def upsert_user(conn, provider, ident):
    uid = "%s:%s" % (provider, ident["subject"])
    row = conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO users(id,name,created_at) VALUES(?,?,?)",
                     (uid, ident.get("name", ""), _now()))
        conn.commit()
    return uid


class Auth:
    def __init__(self, conn, fetch=None, providers=None):
        self.conn = conn
        self.fetch = fetch or _default_fetch
        self.table = providers or PROVIDERS

    def _creds(self, pid):
        cid, sec = self.table[pid]["id_env"]
        return os.environ.get(cid, ""), os.environ.get(sec, "")

    def providers(self):
        out = []
        for pid, cfg in self.table.items():
            cid, sec = self._creds(pid)
            if cid and sec:
                out.append({"id": pid, "label": cfg["label"],
                            "start": "/auth/%s/start" % pid})
        return out

    def authorize_url(self, pid, redirect_uri, state):
        cfg = self.table[pid]
        cid, _ = self._creds(pid)
        if not cid:
            raise AuthError("provider %s is not configured" % pid)
        q = {"client_id": cid, "redirect_uri": redirect_uri, "scope": cfg["scope"],
             "state": state, "response_type": "code"}
        if pid == "google":
            q["access_type"] = "online"
            q["prompt"] = "select_account"
        return cfg["authorize"] + "?" + urllib.parse.urlencode(q)

    def _exchange(self, pid, code, redirect_uri):
        cfg = self.table[pid]
        cid, sec = self._creds(pid)
        data = urllib.parse.urlencode({
            "client_id": cid, "client_secret": sec, "code": code,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code"}).encode()
        req = urllib.request.Request(cfg["token"], data=data, headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"})
        body = json.loads(self.fetch(req).decode("utf-8"))
        tok = body.get("access_token")
        if not tok:
            raise AuthError("no access_token from %s: %s" % (pid, body.get("error", "unknown")))
        return tok

    def _get_json(self, url, token):
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + token, "Accept": "application/json",
            "User-Agent": "fireside"})
        return json.loads(self.fetch(req).decode("utf-8"))

    def identity(self, pid, access_token):
        cfg = self.table[pid]
        ident = _identity(pid, self._get_json(cfg["identity"], access_token))
        if pid == "github" and not ident["email"] and cfg.get("emails"):
            try:
                emails = self._get_json(cfg["emails"], access_token)
                primary = next((e["email"] for e in emails if e.get("primary")),
                               emails[0]["email"] if emails else "")
                ident["email"] = primary or ""
            except Exception:
                pass
        return ident

    def login(self, pid, code, redirect_uri):
        if pid not in self.table:
            raise AuthError("unknown provider %s" % pid)
        access = self._exchange(pid, code, redirect_uri)
        ident = self.identity(pid, access)
        if not ident["subject"]:
            raise AuthError("provider %s returned no subject" % pid)
        uid = upsert_user(self.conn, pid, ident)
        return uid, create_session(self.conn, uid, pid)
