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
  list_sessions(user_id, tok)     -> a user's named live sessions, current flagged
  revoke_session(user_id, sid)    -> log out one session by its public id
  revoke_sessions(user_id, keep)  -> log out the rest, or all when keep is empty
"""

import hashlib
import json
import os
import re
import secrets
import time
import urllib.parse
import urllib.request

SESSION_TTL = 30 * 24 * 3600          # 30 days; cookie and row share it
STATE_COOKIE = "fireside_oauth_state"
LINK_COOKIE = "fireside_oauth_link"
CLAIM_COOKIE = "fireside_oauth_claim"
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
    # Proton has no consumer OAuth of its own; SimpleLogin (Proton-owned) is the
    # door. Its sign-in page carries a "Log in with Proton" button, and it hands
    # back a verified email. SimpleLogin documents the 'profile' scope only.
    "simplelogin": {
        "label": "SimpleLogin",
        "authorize": "https://app.simplelogin.io/oauth2/authorize",
        "token": "https://app.simplelogin.io/oauth2/token",
        "identity": "https://app.simplelogin.io/oauth2/userinfo",
        "scope": "profile",
        "id_env": ("FIRESIDE_OAUTH_SIMPLELOGIN_CLIENT_ID", "FIRESIDE_OAUTH_SIMPLELOGIN_SECRET"),
    },
}


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _allow_local():
    """Local username/password accounts. On unless explicitly disabled."""
    v = os.environ.get("FIRESIDE_ALLOW_LOCAL")
    return True if v is None else _truthy(v)


def _digest(password, salt):
    return hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                               bytes.fromhex(salt), 200000).hex()


def normalize_username(username):
    return (username or "").strip().lower()


def account_count(conn):
    """How many local accounts exist. The first one to arrive becomes admin."""
    return conn.execute("SELECT COUNT(*) c FROM local_accounts").fetchone()["c"]


def is_admin(conn, user_id):
    if not user_id:
        return False
    return conn.execute("SELECT 1 FROM admins WHERE user_id=?",
                        (user_id,)).fetchone() is not None


def registration_open(conn):
    """Can a stranger still make an account by themselves?

    No setting row means yes: the default is that a fresh server is claimable,
    and an admin closes the door once the household is set up. A closed door
    never applies to the very first account.
    """
    row = conn.execute("SELECT value FROM settings WHERE `key`='registration'").fetchone()
    if row is None:
        return True
    return str(row["value"]).strip().lower() in ("open", "1", "true", "yes", "on")


def set_registration(conn, open_):
    v = "open" if open_ else "closed"
    conn.execute("DELETE FROM settings WHERE `key`='registration'")
    conn.execute("INSERT INTO settings(`key`,value) VALUES('registration',?)", (v,))
    conn.commit()
    return v


def local_accounts(conn):
    rows = conn.execute("SELECT username,user_id,created_at FROM local_accounts"
                        " ORDER BY created_at").fetchall()
    admins = {r["user_id"] for r in
              conn.execute("SELECT user_id FROM admins").fetchall()}
    return [{"username": r["username"], "user_id": r["user_id"],
             "created_at": r["created_at"], "admin": r["user_id"] in admins}
            for r in rows]


def local_register(conn, username, password):
    """Create a local account. Raises AuthError if the name is taken or weak.

    The first account on a fresh server is always allowed and becomes admin;
    every later account needs registration to be open.
    """
    name = normalize_username(username)
    if len(name) < 3 or len(name) > 64:
        raise AuthError("username must be 3-64 characters")
    if not re.match(r"^[a-z0-9._-]+$", name):
        raise AuthError("username may use letters, digits, dot, dash, underscore")
    if len(password or "") < 8:
        raise AuthError("password must be at least 8 characters")
    first = account_count(conn) == 0
    if not first and not registration_open(conn):
        raise AuthError("registration is closed on this server")
    if conn.execute("SELECT 1 FROM local_accounts WHERE username=?", (name,)).fetchone():
        raise AuthError("username already taken")
    uid = "local:%s" % name
    salt = secrets.token_hex(16)
    conn.execute("INSERT OR IGNORE INTO users(id,name,created_at) VALUES(?,?,?)",
                 (uid, name, _now()))
    conn.execute("INSERT INTO local_accounts(username,user_id,digest,salt,created_at)"
                 " VALUES(?,?,?,?,?)",
                 (name, uid, _digest(password, salt), salt, _now()))
    if first:
        conn.execute("INSERT OR IGNORE INTO admins(user_id,created_at) VALUES(?,?)",
                     (uid, _now()))
    conn.commit()
    return uid


def local_login(conn, username, password):
    """Verify a local account. Raises AuthError without saying which half failed."""
    name = normalize_username(username)
    row = conn.execute("SELECT user_id,digest,salt FROM local_accounts WHERE username=?",
                       (name,)).fetchone()
    if row is None:
        _digest(password or "", secrets.token_hex(16))   # same work, no leak
        raise AuthError("invalid username or password")
    if not secrets.compare_digest(_digest(password or "", row["salt"]), row["digest"]):
        raise AuthError("invalid username or password")
    return row["user_id"]


class AuthError(Exception):
    pass


class AccountExistsError(AuthError):
    """A new social identity collided with an existing account's verified email.

    Carries what the caller needs to ask for proof: the account already in the
    way, the provider, and the pending identity (linked once ownership is
    proven). A verified email is not proof of owning the account.
    """

    def __init__(self, user_id, provider, ident):
        super().__init__("an account with this email already exists")
        self.user_id = user_id
        self.provider = provider
        self.ident = ident


def _default_fetch(req, timeout=20):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def new_state():
    return secrets.token_urlsafe(16)


def _identity(pid, data):
    """Normalize a provider's profile to {subject, name, email, email_verified}.

    email_verified is only ever trusted as the provider reports it; linking by
    email depends on it, so an unverified address links nothing.
    """
    verified = bool(data.get("email_verified"))
    if pid == "github":
        return {"subject": str(data.get("id") or ""),
                "name": data.get("name") or data.get("login") or "",
                "email": data.get("email") or "",
                "email_verified": verified}
    return {"subject": str(data.get("sub") or data.get("id") or ""),
            "name": data.get("name") or data.get("preferred_username") or "",
            "email": data.get("email") or "",
            "email_verified": verified}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---- sessions (rows in sqlite; the cookie only carries the token) -------------

def _touch_after():
    return 300            # refresh last_seen at most this often, per token


def device_label(user_agent):
    """A short human name for a device, from its User-Agent string.

    Best-effort, never trusted for security: it only decides what the sessions
    list says beside a row. An empty UA (curl, an old client) reads as such.
    """
    ua = user_agent or ""
    low = ua.lower()
    if not ua.strip():
        return "Unknown device"
    browser = ""
    for token, name in (("edg/", "Edge"), ("opr/", "Opera"), ("firefox/", "Firefox"),
                        ("crios/", "Chrome"), ("chrome/", "Chrome"),
                        ("safari/", "Safari")):
        if token in low:
            browser = name
            break
    if not browser:
        browser = "Browser"
    kind = ""
    if "android" in low:
        kind = "Android"
    elif "iphone" in low or "ipad" in low or "ipod" in low:
        kind = "iOS"
    elif "windows" in low:
        kind = "Windows"
    elif "mac os x" in low or "macintosh" in low:
        kind = "macOS"
    elif "linux" in low:
        kind = "Linux"
    return "%s on %s" % (browser, kind) if kind else browser


def create_session(conn, user_id, provider, ttl=SESSION_TTL, ua="", ip=""):
    tok = secrets.token_urlsafe(32)
    sid = secrets.token_hex(8)          # a public handle, safe to show and revoke by
    now = int(time.time())
    conn.execute("INSERT INTO sessions"
                 "(token,user_id,provider,created_at,expires_at,sid,user_agent,ip,last_seen)"
                 " VALUES(?,?,?,?,?,?,?,?,?)",
                 (tok, user_id, provider, now, now + ttl, sid, ua or "", ip or "", now))
    conn.commit()
    return tok


def session_row(conn, token):
    """The live session row for a token, or None. Expiry deletes it; last_seen
    is refreshed but not on every request."""
    if not token:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if row is None:
        return None
    now = int(time.time())
    if int(row["expires_at"] or 0) < now:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return None
    if now - int(row["last_seen"] or 0) > _touch_after():
        conn.execute("UPDATE sessions SET last_seen=? WHERE token=?", (now, token))
        conn.commit()
    return row


def session_user(conn, token):
    row = session_row(conn, token)
    return row["user_id"] if row is not None else None


def delete_session(conn, token):
    if token:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()


def list_sessions(conn, user_id, current_token=""):
    """A user's live sessions, newest activity first, each named by its device.

    The token never leaves the server; a row is addressed by its `sid`, and the
    one the caller is using now is flagged so the UI can say 'this device'.
    """
    if not user_id:
        return []
    now = int(time.time())
    rows = conn.execute(
        "SELECT token,sid,provider,created_at,expires_at,user_agent,ip,last_seen"
        " FROM sessions WHERE user_id=?"
        " ORDER BY last_seen DESC, created_at DESC", (user_id,)).fetchall()
    out = []
    for r in rows:
        if int(r["expires_at"] or 0) < now:
            continue
        out.append({
            "id": r["sid"] or "",
            "provider": r["provider"] or "",
            "label": device_label(r["user_agent"]),
            "user_agent": r["user_agent"] or "",
            "ip": r["ip"] or "",
            "created_at": int(r["created_at"] or 0),
            "last_seen": int(r["last_seen"] or 0),
            "current": bool(current_token) and r["token"] == current_token,
        })
    return out


def revoke_session(conn, user_id, sid):
    """Log out ONE session of `user_id`, named by its public id. Scoped by user
    so one account can never revoke another's. Returns True if a row went."""
    if not user_id or not sid:
        return False
    cur = conn.execute("DELETE FROM sessions WHERE user_id=? AND sid=?", (user_id, sid))
    conn.commit()
    return cur.rowcount > 0


def revoke_sessions(conn, user_id, keep_token=""):
    """Log out a user's sessions. Keep the one token when given (log out
    everywhere else); with no token, every session of the user goes.
    Returns how many were removed."""
    if not user_id:
        return 0
    cur = conn.execute("DELETE FROM sessions WHERE user_id=? AND token<>?",
                       (user_id, keep_token or "")) if keep_token else \
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()
    return cur.rowcount


def account_for_verified_email(conn, email):
    """The user that already owns a VERIFIED email, or None.

    Only identities whose provider marked the address verified count here; an
    unverified address never claims a match.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    row = conn.execute(
        "SELECT user_id FROM identities WHERE email=? AND email_verified=1"
        " ORDER BY created_at LIMIT 1", (email,)).fetchone()
    return row["user_id"] if row is not None else None


def upsert_user(conn, provider, ident):
    """Resolve a provider identity to one user id.

    An identity seen before keeps its user. A NEW identity is attached to the
    account it logs into, and never to an account just because the provider
    reports an email that account happens to own.

    A verified email proves control of the mailbox, not of the account: a
    local account's email was never verified, so matching on it would be a
    takeover door - anyone could register victim@x, then the real owner's
    Google login would land in the seeded account. So when a new identity's
    verified email already belongs to a user, we refuse and raise
    AccountExistsError; the caller must ask for that account's own login
    (auth.complete_claim) before we attach. No verified email at all makes a
    fresh account, as before.
    """
    subject = ident["subject"]
    email = (ident.get("email") or "").strip().lower()
    verified = 1 if ident.get("email_verified") else 0
    row = conn.execute("SELECT user_id FROM identities WHERE provider=? AND subject=?",
                       (provider, subject)).fetchone()
    if row is not None:
        uid = row["user_id"]
    else:
        if verified and email:
            owner = account_for_verified_email(conn, email)
            if owner is not None:
                raise AccountExistsError(owner, provider, ident)
        uid = "%s:%s" % (provider, subject)
        conn.execute("INSERT OR IGNORE INTO users(id,name,created_at) VALUES(?,?,?)",
                     (uid, ident.get("name", ""), _now()))
    conn.execute("INSERT OR REPLACE INTO identities"
                 "(provider,subject,user_id,email,email_verified,created_at)"
                 " VALUES(?,?,?,?,?,?)",
                 (provider, subject, uid, email, verified, _now()))
    conn.commit()
    return uid


# --- proving you own an existing account -----------------------------------
#
# When a social login's verified email already belongs to an account, we do not
# merge. We hold the pending identity in a login_claims row, keyed by an opaque
# token, and only attach it once the user proves ownership of the existing
# account - for a local account, by entering its password.

CLAIM_TTL = 900        # seconds; long enough to type a password, no longer


def create_claim(conn, provider, ident, user_id):
    """Stash a social identity that collided with an existing account."""
    token = secrets.token_urlsafe(24)
    now = int(time.time())
    conn.execute(
        "INSERT INTO login_claims"
        "(token,provider,subject,name,email,email_verified,user_id,created_at,expires_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (token, provider, ident["subject"], ident.get("name", ""),
         (ident.get("email") or "").strip().lower(),
         1 if ident.get("email_verified") else 0, user_id, now, now + CLAIM_TTL))
    conn.commit()
    return token


def take_claim(conn, token):
    """Read a pending claim, refusing one that has expired or was consumed."""
    row = conn.execute("SELECT * FROM login_claims WHERE token=?", (token,)).fetchone()
    if row is None:
        raise AuthError("this sign-in has expired; start again")
    if int(row["expires_at"] or 0) < int(time.time()):
        conn.execute("DELETE FROM login_claims WHERE token=?", (token,))
        conn.commit()
        raise AuthError("this sign-in has expired; start again")
    return row


def local_password_proof(conn, user_id, password):
    """True when `password` is the local account password for `user_id`.

    Constant-time compare; a user with no local account always fails here,
    because a social-only account cannot be proven by a password it never had.
    """
    row = conn.execute("SELECT digest,salt FROM local_accounts WHERE user_id=?",
                       (user_id,)).fetchone()
    if row is None:
        return False
    return secrets.compare_digest(_digest(password or "", row["salt"]), row["digest"])


def has_local_account(conn, user_id):
    return conn.execute("SELECT 1 FROM local_accounts WHERE user_id=?",
                        (user_id,)).fetchone() is not None


def claim_methods(conn, user_id):
    """How the owner of an account can prove it: a password it has, and the
    providers already attached to it. Logging in the way the account already
    does IS the proof - a password is only one door, not the door."""
    return {
        "has_password": has_local_account(conn, user_id),
        "providers": linked_providers(conn, user_id),
    }


def complete_claim(conn, token, password):
    """Attach a pending social identity once the account's password is proven.

    Returns the user id on success. Raises AuthError when the account has no
    password to prove with - the other door is complete_claim_via_identity:
    sign in through a provider the account already uses.
    """
    claim = take_claim(conn, token)
    if not has_local_account(conn, claim["user_id"]):
        raise AuthError("that account has no password; sign in the way it "
                        "already does instead")
    if not local_password_proof(conn, claim["user_id"], password):
        raise AuthError("that password does not match the existing account")
    return _finish_claim(conn, token, claim)


def complete_claim_via_identity(conn, token, provider, ident):
    """Attach a pending social identity after proving ownership by signing in
    as the existing account through one of ITS OWN providers.

    The proof is that the identity just authenticated already belongs to the
    claim's account. An identity owned by someone else (or by nobody) proves
    nothing and is refused - otherwise a second social login could seize the
    account.
    """
    claim = take_claim(conn, token)
    subject = ident.get("subject") or ""
    owner = identity_user(conn, provider, subject)
    if owner is None or owner != claim["user_id"]:
        raise AuthError("that login is not one this account already uses")
    return _finish_claim(conn, token, claim)


def _finish_claim(conn, token, claim):
    """Link the pending identity to the claim's account and consume the claim."""
    ident = {"subject": claim["subject"], "name": claim["name"],
             "email": claim["email"],
             "email_verified": bool(claim["email_verified"])}
    link_identity(conn, claim["provider"], ident, claim["user_id"])
    conn.execute("DELETE FROM login_claims WHERE token=?", (token,))
    conn.commit()
    return claim["user_id"]


def identity_user(conn, provider, subject):
    """The user a provider identity already resolves to, or None."""
    row = conn.execute("SELECT user_id FROM identities WHERE provider=? AND subject=?",
                       (provider, subject)).fetchone()
    return row["user_id"] if row is not None else None


def link_identity(conn, provider, ident, user_id):
    """Attach a provider identity to an explicit, already-authenticated user.

    This is the user-initiated door (settings > link an account), not the
    login door. It refuses when the identity already belongs to a DIFFERENT
    account - otherwise a link flow could be used to seize someone's login -
    and re-linking to the same user is idempotent.
    """
    subject = ident["subject"]
    existing = identity_user(conn, provider, subject)
    if existing is not None and existing != user_id:
        raise AuthError("this %s account is already linked to another user" % provider)
    email = (ident.get("email") or "").strip().lower()
    verified = 1 if ident.get("email_verified") else 0
    conn.execute("INSERT OR REPLACE INTO identities"
                 "(provider,subject,user_id,email,email_verified,created_at)"
                 " VALUES(?,?,?,?,?,?)",
                 (provider, subject, user_id, email, verified, _now()))
    conn.commit()
    return user_id


def linked_providers(conn, user_id):
    """Which social providers a user has attached to their account."""
    if not user_id:
        return []
    rows = conn.execute("SELECT DISTINCT provider FROM identities WHERE user_id=?",
                        (user_id,)).fetchall()
    return sorted(r["provider"] for r in rows)


class Auth:
    def __init__(self, conn, fetch=None, providers=None, local=None):
        self.conn = conn
        self.fetch = fetch or _default_fetch
        self.table = providers or PROVIDERS
        self.allow_local = _allow_local() if local is None else bool(local)

    def _creds(self, pid):
        cid, sec = self.table[pid]["id_env"]
        return os.environ.get(cid, ""), os.environ.get(sec, "")

    def providers(self):
        out = []
        if self.allow_local:
            out.append({"id": "local", "label": "Local account", "kind": "local"})
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
        if pid == "github" and cfg.get("emails"):
            # GitHub's /user carries no verified flag; /user/emails does. Fetch it
            # so a github address is only ever linked on a verified one.
            try:
                emails = self._get_json(cfg["emails"], access_token)
                primary = next((e for e in emails if e.get("primary")),
                               emails[0] if emails else None)
                if primary:
                    ident["email"] = primary.get("email") or ident["email"]
                    ident["email_verified"] = bool(primary.get("verified"))
            except Exception:
                pass
        return ident

    def identity_from_code(self, pid, code, redirect_uri):
        """Exchange a callback code and return the normalized identity, without
        touching users or sessions. Used by the claim-proof path, where the
        identity itself is the evidence."""
        if pid not in self.table:
            raise AuthError("unknown provider %s" % pid)
        access = self._exchange(pid, code, redirect_uri)
        ident = self.identity(pid, access)
        if not ident["subject"]:
            raise AuthError("provider %s returned no subject" % pid)
        return ident

    def login(self, pid, code, redirect_uri, ua="", ip=""):
        if pid not in self.table:
            raise AuthError("unknown provider %s" % pid)
        access = self._exchange(pid, code, redirect_uri)
        ident = self.identity(pid, access)
        if not ident["subject"]:
            raise AuthError("provider %s returned no subject" % pid)
        uid = upsert_user(self.conn, pid, ident)
        return uid, create_session(self.conn, uid, pid, ua=ua, ip=ip)

    def link(self, pid, code, redirect_uri, user_id):
        """Finish a link flow: attach this provider to an existing user."""
        if pid not in self.table:
            raise AuthError("unknown provider %s" % pid)
        access = self._exchange(pid, code, redirect_uri)
        ident = self.identity(pid, access)
        if not ident["subject"]:
            raise AuthError("provider %s returned no subject" % pid)
        return link_identity(self.conn, pid, ident, user_id)

