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
  GET  /progress?user=&book=                 per-chapter rendered status
  GET  /chapter?book=&idx=                   raw audio bytes (verify-before-play)
  GET  /position?user=&book=                 current position
  POST /position {user, book, chapter_index, offset_seconds, device}
  POST /claim    {user, book, device}        take ownership; stops the other
  GET  /stop?user=&book=&device=             has this device been told to stop
  POST /ack      {user, book, device}        confirm the stop
"""

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from host.host import Host
from host import accounting, credentials, db, jobs, library, playback
from sources._fetch import fetch as _fetch


class App:
    def __init__(self, plugins_root=None, db_path=":memory:", storage_id="local",
                 sale_connection=None, rate_limiter=None, hosted=False):
        self.host = Host(plugins_root)
        self.conn = db.connect(db_path)
        self.storage_id = storage_id
        self.sale_connection = sale_connection
        self.rate_limiter = rate_limiter
        self.hosted = hosted

    @property
    def storage(self):
        return self.host.storage[self.storage_id].obj

    # ---- registry -------------------------------------------------------------

    def health(self, q, body):
        h = self.host.health()
        h["hosted"] = self.hosted
        h["storage_backend"] = self.storage_id
        return 200, h

    def _fields(self, o):
        fn = getattr(o, "settings", None)
        if not callable(fn):
            return []
        try:
            return [f.dict() if hasattr(f, "dict") else dict(f) for f in fn()]
        except Exception:
            return []

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

    # ---- library --------------------------------------------------------------

    def library_list(self, q, body):
        return 200, library.list_books(self.conn, q.get("user", ""))

    def import_book(self, q, body):
        user = body["user"]
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
        user = body["user"]
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
        est = sum(c["chars"] or 0 for c in library.chapters(self.conn, book_id))
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
        jid = jobs.render_book(self.conn, self.storage, speech, user, book_id,
                               voice, model, speed, cred=cred,
                               opts={"model": model, "speed": speed}, only=only)
        if sale and res_id:
            rep = speech.report({"chars_actual": est, "cred": cred, "job_id": jid})
            accounting.settle(self.conn, res_id, rep)
        return 200, {"job_id": jid}

    def job(self, q, body):
        j = jobs.job(self.conn, q.get("id", ""))
        return (200, j) if j else (404, {"error": "no job"})

    def progress(self, q, body):
        ch = library.chapters(self.conn, q.get("book", ""))
        return 200, {"chapters": [
            {"idx": c["idx"], "rendered": bool(c["audio_ref"]),
             "audio_ref": c["audio_ref"], "audio_bytes": c["audio_bytes"],
             "provider": c["provider"], "voice": c["voice"], "model": c["model"]}
            for c in ch]}

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
        return 200, playback.get_position(self.conn, q["user"], q["book"]) or {}

    def put_position(self, q, body):
        return 200, playback.put_position(self.conn, body["user"], body["book"],
                                          body["chapter_index"], body["offset_seconds"],
                                          body.get("device", ""))

    def claim(self, q, body):
        return 200, playback.claim(self.conn, body["user"], body["book"], body["device"])

    def stop(self, q, body):
        return 200, {"stop": playback.stop_pending(self.conn, q["user"], q["book"],
                                                   q["device"])}

    def ack(self, q, body):
        playback.ack_stop(self.conn, body["user"], body["book"], body["device"])
        return 200, {"ok": True}

    # ---- credentials (hosted; values never leave) -----------------------------

    def put_credential(self, q, body):
        credentials.set_credential(self.conn, body["user"], body["provider"],
                                   body.get("data") or {})
        return 200, {"ok": True, "providers": credentials.providers(self.conn, body["user"])}


GET_ROUTES = {
    "/health": "health", "/plugins": "plugins", "/library": "library_list",
    "/job": "job", "/progress": "progress", "/chapter": "chapter",
    "/position": "get_position", "/stop": "stop",
}
POST_ROUTES = {
    "/import": "import_book", "/render": "render", "/position": "put_position",
    "/claim": "claim", "/ack": "ack", "/credential": "put_credential",
}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "fireside-host/2"

        def _dispatch(self, routes):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            path = u.path.rstrip("/") or "/"
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
            try:
                status, payload = getattr(app, name)(q, body)
            except Exception as e:
                return self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})
            if isinstance(payload, dict) and "_raw" in payload:
                raw = payload["_raw"]
                self.send_response(status)
                self.send_header("Content-Type", payload.get("_mime", "application/octet-stream"))
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Checksum", payload.get("checksum", ""))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self._json(status, payload)

        def _json(self, status, payload):
            b = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
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


def serve(app=None, port=8788):
    app = app or App()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    return app, httpd


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--plugins", default=None)
    ap.add_argument("--hosted", action="store_true")
    args = ap.parse_args()
    app, httpd = serve(App(plugins_root=args.plugins, hosted=args.hosted), args.port)
    print("fireside v2 host on http://127.0.0.1:%d" % httpd.server_address[1])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
