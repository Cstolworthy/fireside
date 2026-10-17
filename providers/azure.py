"""azure - Azure AI Speech. Needs a key + region. Great voice menu (hundreds).

Price is the pay-as-you-go list rate; it drops only at commitment tiers. The host
zeroes the recorded cost when the key is the user's own (BYO)."""

import html
import json
import re
import urllib.request

from . import Audio, Field, Voice, report
from ._text import chunk_text

PRICE_PER_1M_USD = 15.0


class Azure:
    id = "azure"
    ext = ".mp3"
    mime = "audio/mpeg"
    needs_key = True

    def settings(self):
        return [Field("key", "Azure key", "password",
                      help="Azure AI Speech key. Well-voiced, hundreds of voices."),
                Field("region", "Region", "text", default="eastus")]

    def list_voices(self, cred):
        return voices_azure(cred.get("key", ""), cred.get("region", "eastus"))

    def chunks(self, text):
        return len(chunk_text(text))

    def cost_per_1m_chars(self, cred):
        return PRICE_PER_1M_USD

    def synth(self, text, voice, opts, cred):
        opts = opts or {}
        rate = opts.get("rate", "0%")
        key, region = cred.get("key", ""), cred.get("region", "eastus")
        parts = chunk_text(text)
        buf = bytearray()
        for n, p in enumerate(parts, 1):
            buf += synth_azure(p, voice, key, region, rate)
            report(opts, n, len(parts))
        cost = PRICE_PER_1M_USD * len(text) / 1e6
        return Audio(bytes(buf), self.ext, self.mime, round(cost, 6))


def voices_azure(key: str, region: str):
    url = "https://%s.tts.speech.microsoft.com/cognitiveservices/voices/list" % region
    req = urllib.request.Request(url, headers={"Ocp-Apim-Subscription-Key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    out = []
    for v in data:
        if "Neural" not in v.get("VoiceType", ""):
            continue
        locale = v.get("Locale", "")
        gender = v.get("Gender", "")
        name = _voice_name(v.get("DisplayName") or v.get("ShortName", ""))
        # deliberately no personality in the label: we do not apply a style, so
        # naming one would claim something the audio does not do
        label = "%s (%s, %s)" % (name, locale, gender)
        out.append(Voice(v.get("ShortName", ""), label, locale, gender))
    # English first (that is what gets read here), then everything else; both alphabetical
    out.sort(key=lambda x: (0 if x.locale.startswith("en") else 1, x.locale, x.id))
    return out


def _voice_name(dn: str) -> str:
    """'Microsoft Jenny Online (Natural) - English (United States)' -> 'Jenny'.

    Azure's DisplayName is a mouthful and its ShortName is a slug; the voice list
    is unusable if you have to read either one 900 times.
    """
    s = (dn or "").strip()
    s = re.sub(r"^Microsoft\s+", "", s, flags=re.I)
    s = s.split(" - ")[0]
    s = re.sub(r"\s*\([^)]*\)", "", s).strip()
    s = re.sub(r"\s+Online$", "", s, flags=re.I).strip()
    if not s or s == dn:
        # DisplayName was missing and we got a ShortName like en-US-JennyMultilingualNeural
        m = re.match(r"^[a-z]{2}-[A-Za-z]{2,}(-(.+))?$", s or dn or "")
        if m and m.group(2):
            s = re.sub(r"Neural$", "", m.group(2)) or s
    return s or dn


def synth_azure(text: str, voice: str, key: str, region: str, rate: str = "0%") -> bytes:
    url = "https://%s.tts.speech.microsoft.com/cognitiveservices/v1" % region
    ssml = (
        "<speak version='1.0' xml:lang='en-US'>"
        "<voice name='%s'>"
        "<prosody rate='%s'>%s</prosody>"
        "</voice></speak>" % (voice, rate, html.escape(text))
    )
    req = urllib.request.Request(url, data=ssml.encode("utf-8"), headers={
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
        "User-Agent": "reader-local",
    })
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()

