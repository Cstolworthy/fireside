"""The v2 host, served: HTTP routes over the registry, library, jobs, ledger.

One deployment-agnostic server. Running at home and running hosted differ only
in auth mode and in whose credential is used; the routes are identical. Nothing
here imports a plugin by name - it goes through the registry.

Endpoints (JSON unless noted):
  GET  /health                              what this deployment loaded
  GET  /plugins                             readers + speech + storage, with fields
  GET  /library?user=                        the user's books
  POST /import   {user, kind, url|data_b64, filename}   reader -> book
  POST /render   {user, book, provider, voice, model, chapters?}   -> {job_id}
  GET  /job?id=                              job status
  GET  /progress?user=&book=                 per-chapter title, chars, rendered status
  GET  /text?book=&idx=                      the chapter text (story page, no audio)
  GET  /chapter?book=&idx=                   raw audio bytes (verify-before-play)
  GET  /position?user=&book=                 current position
  POST /position {user, book, chapter_index, offset_seconds, device}
  POST /claim    {user, book, device}        take ownership; stops the other
  GET  /stop?user=&book=&device=             has this device been told to stop
  POST /ack      {user, book, device}        confirm the stop
"""

import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from host.host import Host
from host import accounting, auth, credentials, db, jobs, library, playback
from sources._fetch import fetch as _fetch

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
HOST_VERSION = "2.0"


class App:
    def __init__(self, plugins_root=None, db_path=None, storage_id="local",
                 sale_connection=None, rate_limiter=None, hosted=False,
                 require_auth=False):
        self.host = Host(plugins_root)
        self.db_path = db_path
        self.conn = db.connect(db_path)
        self.storage_id = storage_id
        self.sale_connection = sale_connection
        self.rate_limiter = rate_limiter
        self.hosted = hosted
        self.require_auth = require_auth
        self.auth = auth.Auth(self.conn)

    def _uid(self, q, body):
        """The signed-in user wins over any user id the client passed in."""
        return ((q or {}).get("_user") or (body or {}).get("user")
                or (q or {}).get("user") or "")

    @property
    def storage(self):
        return self.host.storage[self.storage_id].obj

    # ---- registry -------------------------------------------------------------

    def health(self, q, body):
        h = self.host.health()
        h["hosted"] = self.hosted
        h["storage_backend"] = self.storage_id
        h["db_backend"] = db.backend_name()
        return 200, h

    def _fields(self, o):
        fn = getattr(o, "settings", None)
        if not callable(fn):
            return []
        try:
            raw = fn()
        except Exception:
            return []
        out = []
        for f in raw:                            # one shape for the UI: name + kind
            d = f.dict() if hasattr(f, "dict") else dict(f)
            if "name" not in d and "key" in d:
                d["name"] = d["key"]
            if "kind" not in d and "type" in d:
                d["kind"] = d["type"]
            out.append(d)
        return out

    def plugins(self, q, body):
        out = {"readers": [], "speech": [], "storage": []}
        for p in self.host.readers:
            out["readers"].append({"id": p.id, "label": p.label, "order": p.order,
                                   "settings": self._fields(p.obj)})
        for pid, p in self.host.speech.items():
            o = p.obj
            out["speech"].append({"id": pid, "label": p.label,
                                  "needs_key": bool(getattr(o, "needs_key", True)),
                                  "ext": getattr(o, "ext", ""),
                                  "settings": self._fields(o)})
        for pid, p in self.host.storage.items():
            out["storage"].append({"id": pid, "info": p.obj.info()})
        return 200, out

    def voices(self, q, body):
        provider = body.get("provider") or q.get("provider") or ""
        if provider not in self.host.speech:
            return 400, {"error": "unknown provider"}
        cred = body.get("cred") or {}
        raw = self.host.speech[provider].obj.list_voices(cred)
        return 200, {"voices": [v.dict() if hasattr(v, "dict") else dict(v) for v in raw]}

    def ui(self, q, body):
        """Serve the browser client. The host owns its own UI; plugins ship none."""
        with open(os.path.join(UI_DIR, "index.html"), "r", encoding="utf-8") as f:
            page = f.read().replace("__HOST_VERSION__", HOST_VERSION)
        return 200, {"_html": page}

    # ---- library --------------------------------------------------------------

    def library_list(self, q, body):
        return 200, library.list_books(self.conn, self._uid(q, None))

    def import_book(self, q, body):
        user = self._uid(q, body)
        req = {"kind": body.get("kind", "url"), "url": body.get("url"),
               "filename": body.get("filename"),
               "data": base64.b64decode(body["data_b64"]) if body.get("data_b64") else None}
        cred = body.get("cred") or {}
        plugin = self._pick_reader(req)
        if plugin is None:
            return 400, {"error": "no reader matched"}
        book = plugin.obj.read(req, self._make_fetch(plugin.obj, cred), cred)
        bid = library.add_book(self.conn, self.storage, user, book)
        return 200, {"book_id": bid, "title": getattr(book, "title", ""),
                     "chapters": len(getattr(book, "chapters", []))}

    def _pick_reader(self, req):
        for p in self.host.readers:
            try:
                if p.obj.matches(req):
                    return p
            except Exception:
                continue
        return None

    def _make_fetch(self, o, cred):
        hosts = tuple(o.credential_hosts() or []) if callable(
            getattr(o, "credential_hosts", None)) else ()
        tr = o.transport() if callable(getattr(o, "transport", None)) else None

        def f(url, **kw):
            return _fetch(url, cred=cred, credential_hosts=hosts, transport=tr)
        return f

    # ---- render ---------------------------------------------------------------

    def render(self, q, body):
        user = self._uid(q, body)
        book_id = body["book"]
        provider = body["provider"]
        voice = body.get("voice", "")
        model = body.get("model", "")
        speed = float(body.get("speed", 1.0) or 1.0)
        only = body.get("chapters")
        if provider not in self.host.speech:
            return 400, {"error": "unknown provider"}
        if self.rate_limiter is not None and not self.rate_limiter.allow(user):
            return 429, {"error": "rate limited"}
        speech = self.host.speech[provider].obj
        cred = body.get("cred") or credentials.get_credential(self.conn, user, provider)
        rows = library.chapters(self.conn, book_id)
        est = sum(c["chars"] or 0 for c in rows)
        todo = [c["idx"] for c in rows]
        if only is not None:
            todo = [i for i in todo if i in set(only)]
        sale = accounting.sale_connection_matches(self.sale_connection, provider, voice, model)
        res_id = None
        if sale:
            quote = speech.quote({"chars_est": est, "cred": cred})
            if not getattr(quote, "eligible", True):
                return 402, {"error": "not eligible",
                             "reason": getattr(quote, "refusal_reason", "")}
            price = float(getattr(quote, "price", 0.0) or 0.0)
            if price > 0:
                try:
                    res_id = accounting.reserve(self.conn, user, price,
                                                getattr(quote, "currency", "USD"))
                except accounting.Insufficient as e:
                    return 402, {"error": "insufficient balance", "detail": str(e)}
        jid = jobs.start_job(self.conn, user, book_id, speech.id, voice, model,
                             total=len(todo))
        opts = {"model": model, "speed": speed}

        def _work():
            # A render can run for minutes; it gets its own DB connection so it
            # never blocks the request thread and the client can poll /job.
            c = db.connect(self.db_path)
            try:
                jobs.render_book(c, self.storage, speech, user, book_id, voice,
                                 model, speed, cred=cred, opts=opts, only=only,
                                 job_id=jid)
                if sale and res_id:
                    rep = speech.report({"chars_actual": est, "cred": cred, "job_id": jid})
                    accounting.settle(c, res_id, rep)
            finally:
                c.close()

        threading.Thread(target=_work, daemon=True).start()
        return 200, {"job_id": jid, "total": len(todo)}

    def job(self, q, body):
        j = jobs.job(self.conn, q.get("id", ""))
        return (200, j) if j else (404, {"error": "no job"})

    def progress(self, q, body):
        ch = library.chapters(self.conn, q.get("book", ""))
        return 200, {"chapters": [
            {"idx": c["idx"], "title": c["title"], "chars": c["chars"],
             "rendered": bool(c["audio_ref"]),
             "audio_ref": c["audio_ref"], "audio_bytes": c["audio_bytes"],
             "provider": c["provider"], "voice": c["voice"], "model": c["model"]}
            for c in ch]}

    def text(self, q, body):
        """The chapter's text, for the now-playing page. Not the audio path."""
        try:
            idx = int(q["idx"])
            t = library.chapter_text(self.conn, self.storage, q["book"], idx)
        except (KeyError, ValueError):
            return 404, {"error": "no chapter"}
        rows = [c for c in library.chapters(self.conn, q["book"]) if c["idx"] == idx]
        return 200, {"text": t, "title": rows[0]["title"] if rows else ""}

    def chapter(self, q, body):
        idx = int(q["idx"])
        rows = [r for r in library.chapters(self.conn, q["book"]) if r["idx"] == idx]
        if not rows:
            return 404, {"error": "no chapter"}
        r = rows[0]
        if not r["audio_ref"]:
            return 409, {"error": "not rendered"}
        data = self.storage.get(r["audio_ref"])
        return 200, {"_raw": data, "_mime": "audio/mpeg", "bytes": len(data),
                     "checksum": r["audio_ref"]}

    # ---- playback -------------------------------------------------------------

    def get_position(self, q, body):
        return 200, playback.get_position(self.conn, self._uid(q, None), q["book"]) or {}

    def put_position(self, q, body):
        return 200, playback.put_position(self.conn, self._uid(q, body), body["book"],
                                          body["chapter_index"], body["offset_seconds"],
                                          body.get("device", ""))

    def claim(self, q, body):
        return 200, playback.claim(self.conn, self._uid(q, body), body["book"], body["device"])

    def stop(self, q, body):
        return 200, {"stop": playback.stop_pending(self.conn, self._uid(q, None), q["book"],
                                                   q["device"])}

    def ack(self, q, body):
        playback.ack_stop(self.conn, self._uid(q, body), body["book"], body["device"])
        return 200, {"ok": True}

    # ---- credentials (hosted; values never leave) -----------------------------

    def put_credential(self, q, body):
        credentials.set_credential(self.conn, self._uid(q, body), body["provider"],
                                   body.get("data") or {})
        return 200, {"ok": True, "providers": credentials.providers(self.conn, self._uid(q, body))}

    # ---- auth (social sign-in; the session lives in sqlite) ------------------

    def auth_providers(self, q, body):
        return 200, {"providers": self.auth.providers()}

    def me(self, q, body):
        uid = (q or {}).get("_user") or ""
        return 200, {"user": uid, "authenticated": bool(uid)}


GET_ROUTES = {
    "/": "ui", "/index.html": "ui",
    "/health": "health", "/plugins": "plugins", "/library": "library_list",
    "/job": "job", "/progress": "progress", "/chapter": "chapter",
    "/text": "text",
    "/position": "get_position", "/stop": "stop",
    "/auth/providers": "auth_providers", "/me": "me",
}
POST_ROUTES = {
    "/import": "import_book", "/render": "render", "/voices": "voices",
    "/position": "put_position", "/claim": "claim", "/ack": "ack",
    "/credential": "put_credential",
}

# reachable without a session even when auth is required: the page, its health,
# the sign-in surface, and the browser's cache of who it is.
PUBLIC_ROUTES = {"/", "/index.html", "/health", "/auth/providers", "/me"}

# auth sub-routes the handler serves directly (they set cookies / redirect),
# so they are not in the GET/POST dispatch tables above. Declared here so the
# browser client's served-route guard can see them too.
AUTH_ROUTES = {"/auth/logout"}


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "fireside-host/2"

        # ---- cookies + redirects ---------------------------------------------
        def _cookie(self, name):
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v
            return ""

        def _base_url(self):
            env = os.environ.get("FIRESIDE_BASE_URL")
            if env:
                return env.rstrip("/")
            return "http://" + (self.headers.get("Host") or "127.0.0.1:8788")

        def _cookie_str(self, name, value, path="/", max_age=None):
            c = "%s=%s; Path=%s; HttpOnly; SameSite=Lax" % (name, value, path)
            if max_age is not None:
                c += "; Max-Age=%d" % max_age
            if self._base_url().startswith("https"):
                c += "; Secure"
            return c

        def _redirect(self, location, cookies=None):
            self.send_response(302)
            self.send_header("Location", location)
            for c in (cookies or []):
                self.send_header("Set-Cookie", c)
            self.end_headers()

        def _token(self):
            tok = self._cookie(auth.SESSION_COOKIE)
            if not tok:
                authz = self.headers.get("Authorization", "")
                if authz.startswith("Bearer "):
                    tok = authz[7:]
            return tok

        def _auth_route(self, parts, parsed):
            from urllib.parse import parse_qs
            q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            pid, action = parts[1], parts[2]
            if pid not in app.auth.table:
                return self.send_error(404)
            try:
                redirect_uri = self._base_url() + "/auth/%s/callback" % pid
                if action == "start":
                    state = auth.new_state()
                    url = app.auth.authorize_url(pid, redirect_uri, state)
                    return self._redirect(url, [self._cookie_str(
                        auth.STATE_COOKIE, state, path="/auth", max_age=600)])
                if action == "callback":
                    if q.get("error"):
                        return self.send_error(400, "oauth error: %s" % q["error"])
                    if not q.get("state") or q["state"] != self._cookie(auth.STATE_COOKIE):
                        return self.send_error(400, "bad oauth state")
                    uid, token = app.auth.login(pid, q.get("code", ""), redirect_uri)
                    return self._redirect("/", [
                        self._cookie_str(auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL),
                        self._cookie_str(auth.STATE_COOKIE, "", path="/auth", max_age=0)])
            except auth.AuthError as e:
                return self._json(401, {"error": str(e)})
            return self.send_error(404)

        def _dispatch(self, routes):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            path = u.path.rstrip("/") or "/"
            parts = path.strip("/").split("/")
            if (self.command == "GET" and len(parts) == 3 and parts[0] == "auth"):
                return self._auth_route(parts, u)
            if path == "/auth/logout" and self.command == "POST":
                auth.delete_session(app.conn, self._token())
                return self._redirect("/", [self._cookie_str(
                    auth.SESSION_COOKIE, "", max_age=0)])
            if path in ("/auth/local/register", "/auth/local/login") \
                    and self.command == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                except ValueError:
                    return self.send_error(400)
                try:
                    if path.endswith("register"):
                        uid = auth.local_register(app.conn, body.get("username", ""),
                                                  body.get("password", ""))
                    else:
                        uid = auth.local_login(app.conn, body.get("username", ""),
                                               body.get("password", ""))
                except auth.AuthError as e:
                    return self._json(401, {"error": str(e)})
                token = auth.create_session(app.conn, uid, "local")
                return self._json(200, {"ok": True, "user": uid},
                                  cookie=self._cookie_str(
                                      auth.SESSION_COOKIE, token,
                                      max_age=auth.SESSION_TTL))
            name = routes.get(path)
            if name is None:
                return self.send_error(404)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            body = {}
            if self.command == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    try:
                        body = json.loads(self.rfile.read(n).decode("utf-8"))
                    except ValueError:
                        return self.send_error(400)
            q["_user"] = auth.session_user(app.conn, self._token()) or ""
            if app.require_auth and path not in PUBLIC_ROUTES and not q["_user"]:
                return self._json(401, {"error": "auth required"})
            try:
                status, payload = getattr(app, name)(q, body)
            except Exception as e:
                return self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})
            if isinstance(payload, dict) and "_html" in payload:
                raw = payload["_html"].encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif isinstance(payload, dict) and "_raw" in payload:
                raw = payload["_raw"]
                self.send_response(status)
                self.send_header("Content-Type", payload.get("_mime", "application/octet-stream"))
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Checksum", payload.get("checksum", ""))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self._json(status, payload)

        def _json(self, status, payload, cookie=None):
            b = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._dispatch(GET_ROUTES)

        def do_POST(self):
            self._dispatch(POST_ROUTES)

        def log_message(self, *args):
            pass

    return Handler


def serve(app=None, port=8788, host="127.0.0.1"):
    app = app or App()
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    return app, httpd


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 for this machine; 0.0.0.0 to share on the LAN")
    ap.add_argument("--plugins", default=None)
    ap.add_argument("--data", default=None,
                    help="data dir for the DB and blobs (default: $FIRESIDE_DATA)")
    ap.add_argument("--hosted", action="store_true")
    ap.add_argument("--require-auth", action="store_true",
                    help="reject requests with no session (auto-on when --hosted)")
    args = ap.parse_args()
    if args.data:                      # one knob sets DB + blob root alike
        os.environ["FIRESIDE_DATA"] = args.data
    require = (args.require_auth or args.hosted
               or _truthy(os.environ.get("FIRESIDE_REQUIRE_AUTH")))
    app, httpd = serve(App(plugins_root=args.plugins, hosted=args.hosted,
                           require_auth=require), args.port, args.host)
    print("fireside v2 host on http://%s:%d" % (args.host, httpd.server_address[1]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


