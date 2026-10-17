"""The v2 host skeleton: a plugin registry and a health endpoint.

The host owns auth, library, jobs, storage, the credit ledger and the plugin
registry. This file is only the first brick - the registry, the discovery it
sits on, and the one endpoint that says whether the deployment came up whole.
Jobs, the library, storage and the ledger land on top of it, and the old v1 app
keeps running next to it until each v2 piece passes the same tests.

Nothing here imports a plugin by name. A plugin that announces a kind but does
not answer to that kind's contract is a broken deployment, and the host says so
loudly at startup rather than failing later on a request.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from host.pluginhost import KINDS, PluginError, discover

DEFAULT_PLUGINS_ROOT = os.environ.get("FIRESIDE_PLUGINS_ROOT", "plugins")
DEFAULT_PORT = int(os.environ.get("FIRESIDE_HOST_PORT", "8788"))

# the methods the host will actually call on each kind
_REQUIRED_METHODS = {
    "reader": ("matches", "read"),
    "speech": ("list_voices", "synth", "quote", "report"),
    "storage": ("put", "get", "delete"),
}


class Host:
    def __init__(self, plugins_root=None):
        self.plugins_root = plugins_root or DEFAULT_PLUGINS_ROOT
        self.reload()

    def reload(self):
        """(Re)discover every kind and refuse to come up half-built."""
        self.readers = discover("reader", self.plugins_root)     # ordered list
        self.speech = discover("speech", self.plugins_root)      # id -> plugin
        self.storage = discover("storage", self.plugins_root)    # id -> plugin
        self._check_contracts()
        return self

    def _check_contracts(self):
        for kind in KINDS:
            found = self.readers if kind == "reader" else getattr(self, kind)
            plugins = found if isinstance(found, list) else list(found.values())
            for p in plugins:
                for method in _REQUIRED_METHODS[kind]:
                    if not callable(getattr(p.obj, method, None)):
                        raise PluginError(
                            f"{kind} plugin {p.id!r} has no {method}()")

    def health(self):
        """The one endpoint that must never lie: what this deployment loaded."""
        return {
            "ok": bool(self.readers),        # no reader means no path from input to book
            "plugins_root": self.plugins_root,
            "readers": [{"id": p.id, "order": p.order, "version": p.version}
                        for p in self.readers],
            "speech": sorted(self.speech),
            "storage": sorted(self.storage),
        }


def make_handler(host):
    class Handler(BaseHTTPRequestHandler):
        server_version = "fireside-host/2"

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path in ("/", "/health"):
                body = json.dumps(host.health()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, *args):        # the host logs its own way; stdlib is loud
            pass

    return Handler


def serve(host=None, port=None):
    host = host or Host()
    httpd = ThreadingHTTPServer(("127.0.0.1", port or DEFAULT_PORT),
                                make_handler(host))
    return host, httpd


if __name__ == "__main__":
    host, httpd = serve()
    print(f"fireside v2 host on http://127.0.0.1:{httpd.server_address[1]}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
