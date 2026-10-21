"""minimax - MiniMax T2A. Needs a key + group id. The voice used in the demo clip.

Pricing is per model and not verified here, so this provider reports UNPRICED
(None). Unpriced renders; the meter marks the row `unpriced` instead of guessing."""

import json
import urllib.parse
import urllib.request

from . import Audio, Field, Voice, report
from ._text import chunk_text

DEFAULT_MODEL = "speech-02-hd"


class MiniMax:
    id = "minimax"
    ext = ".mp3"
    mime = "audio/mpeg"
    needs_key = True

    def settings(self):
        return [Field("key", "MiniMax key", "password"),
                Field("group", "Group ID", "text", help="The GroupId your MiniMax key belongs to.")]

    def list_voices(self, cred):
        return voices_minimax(cred.get("key", ""), cred.get("group", ""))

    def chunks(self, text):
        return len(chunk_text(text))

    def split(self, text):
        return chunk_text(text)

    def cost_per_1m_chars(self, cred):
        return None  # per model, unverified -> unpriced, not free

    def synth(self, text, voice, opts, cred):
        opts = opts or {}
        model = opts.get("model") or DEFAULT_MODEL
        speed = float(opts.get("speed", 1.0) or 1.0)
        key, group = cred.get("key", ""), cred.get("group", "")
        parts = chunk_text(text)
        buf = bytearray()
        for n, p in enumerate(parts, 1):
            buf += synth_minimax(p, voice, key, group, model, speed)
            report(opts, n, len(parts))
        return Audio(bytes(buf), self.ext, self.mime, None)


def voices_minimax(key: str, group: str):
    # Best-effort. MiniMax voice ids look like English_Gentle-voiced_man.
    url = "https://api.minimax.chat/v1/get_voice?GroupId=%s" % urllib.parse.quote(group)
    req = urllib.request.Request(url, data=b"{}", headers={
        "Authorization": "Bearer %s" % key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
        vs = d.get("system_voice") or d.get("voice_list") or []
        out = [Voice(v.get("voice_id"), v.get("voice_name") or v.get("voice_id"))
               for v in vs if v.get("voice_id")]
        if out:
            return out
    except Exception:
        pass
    return [
        Voice("English_Gentle-voiced_man", "English Gentle (voiced man)", "en", "male"),
        Voice("English_CalmWoman", "English Calm (woman)", "en", "female"),
        Voice("English_Trustworth_Man", "English Trustworthy (man)", "en", "male"),
        Voice("English_radiant_girl", "English Radiant (girl)", "en", "female"),
    ]


def synth_minimax(text: str, voice: str, key: str, group: str, model: str = DEFAULT_MODEL,
                  speed: float = 1.0) -> bytes:
    url = "https://api.minimax.chat/v1/t2a_v2?GroupId=%s" % urllib.parse.quote(group)
    body = json.dumps({
        "model": model, "text": text, "stream": False,
        "voice_setting": {"voice_id": voice, "speed": speed, "vol": 1.0, "pitch": 0},
        "audio_setting": {"sample_rate": 32000, "format": "mp3"},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer %s" % key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode())
    audio = (d.get("data") or {}).get("audio")
    if not audio:
        raise ValueError("minimax returned no audio: %s" % str(d)[:200])
    return bytes.fromhex(audio)

