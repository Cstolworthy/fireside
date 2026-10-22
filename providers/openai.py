"""openai - any OpenAI-compatible /v1/audio/speech server.

Point base_url at a speech server on your own machine (Kokoro, Speaches,
openedai-speech, ...). It runs on your hardware, so the cost is 0 and it never
touches a meter. No key required; if the server wants one, paste it and it goes
out as a Bearer token.

This is deliberately generic: one provider for the whole OpenAI TTS shape, so a
new local engine is a URL, not a code change.
"""

import json
import time
import urllib.error
import urllib.request

from . import Audio, Field, Voice, report
from ._text import chunk_text

DEFAULT_BASE = "http://127.0.0.1:8880"
DEFAULT_VOICE = "af_heart"

# Seconds to wait between tries, and per try. Total worst case ~50s, but the
# common case is a cold tunnel that answers on the second knock.
_RETRY_WAITS = (1.0, 2.5)
_LIST_TIMEOUT = 12


def _why(e):
    """A short reason a human can act on, not a repr of an exception."""
    code = getattr(e, "code", None)
    if code == 401:
        return "server rejected the key"
    if code == 403:
        return "key not allowed"
    if code == 404:
        return "no voice list at that address"
    if code:
        return "server said %s" % code
    if isinstance(e, urllib.error.URLError) or "timed out" in str(e).lower():
        return "server did not answer"
    return "server did not answer"


class OpenAICompat:
    id = "openai"
    ext = ".mp3"
    mime = "audio/mpeg"
    needs_key = False

    def settings(self):
        return [Field("base_url", "Server URL", "text", default=DEFAULT_BASE,
                      help="Any OpenAI-compatible /v1/audio/speech server on your LAN "
                           "(Kokoro, Speaches, ...). Runs on your hardware, so it costs nothing."),
                Field("key", "Key", "password",
                      help="Bearer token, if your server wants one. Sent as "
                           "Authorization: Bearer <key>.")]

    def _base(self, cred):
        return (cred.get("base_url") or DEFAULT_BASE).rstrip("/")

    def list_voices(self, cred):
        hdr = {}
        if cred.get("key"):
            # a gated server (our tower) needs the token to answer the list too,
            # not just synth; without it the menu silently falls back to one voice
            hdr["Authorization"] = "Bearer " + cred["key"]
        data = None
        # A self-hosted server behind a tunnel can be cold on the first call and
        # answer a second later (the tower's funnel does exactly this). One slow
        # list used to cost the user every voice but the default, so try again
        # before deciding the server is really gone.
        for attempt, wait in enumerate(_RETRY_WAITS + (None,)):
            try:
                req = urllib.request.Request(self._base(cred) + "/v1/audio/voices",
                                             headers=hdr)
                with urllib.request.urlopen(req, timeout=_LIST_TIMEOUT) as r:
                    data = json.loads(r.read().decode())
                break
            except Exception as e:
                last = e
                if wait is not None:
                    time.sleep(wait)
        if data is None:
            # Still nothing. Keep speaking (the synth call may well work), but say
            # so out loud in the menu instead of pretending one voice is the list.
            why = _why(last)
            return [Voice(DEFAULT_VOICE,
                          "%s (voice list unavailable: %s)" % (DEFAULT_VOICE, why))]
        out = []
        for v in data.get("voices", []):
            vid = v.get("id") or v.get("name")
            if not vid:
                continue
            out.append(Voice(vid, v.get("name") or vid))
        out.sort(key=lambda x: x.id)
        if not out:
            return [Voice(DEFAULT_VOICE,
                          "%s (server returned an empty voice list)" % DEFAULT_VOICE)]
        return out

    def chunks(self, text):
        return len(chunk_text(text))

    def split(self, text):
        return chunk_text(text)

    def cost_per_1m_chars(self, cred):
        return 0.0  # runs on your own machine

    def synth(self, text, voice, opts, cred):
        opts = opts or {}
        speed = float(opts.get("speed", 1.0) or 1.0)
        model = opts.get("model") or "kokoro"
        parts = chunk_text(text)
        buf = bytearray()
        for n, p in enumerate(parts, 1):
            buf += self._one(p, voice or DEFAULT_VOICE, speed, model, cred)
            report(opts, n, len(parts))
        return Audio(bytes(buf), self.ext, self.mime, 0.0)

    def _one(self, text, voice, speed, model, cred):
        body = json.dumps({
            "model": model, "input": text, "voice": voice,
            "response_format": "mp3", "speed": speed, "stream": False,
        }).encode()
        hdr = {"Content-Type": "application/json"}
        if cred.get("key"):
            hdr["Authorization"] = "Bearer " + cred["key"]
        req = urllib.request.Request(self._base(cred) + "/v1/audio/speech",
                                     data=body, headers=hdr)
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.read()

