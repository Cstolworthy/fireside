"""local - offline stand-in. A spoken-length tone. No key, no network, cost 0.
It exists to prove the pipeline and to try the UI without paying anyone."""

import io
import struct
import wave

from . import Audio, Voice, report


class Local:
    id = "local"
    ext = ".wav"
    mime = "audio/wav"
    needs_key = False

    def list_voices(self, cred):
        return [Voice("tone", "Offline tone (no speech)")]

    def chunks(self, text):
        return 1

    def cost_per_1m_chars(self, cred):
        return 0.0

    def synth(self, text, voice, opts, cred):
        secs = max(1.0, min(240.0, len(text) / 14.0))
        rate = 16000
        frames = int(secs * rate)
        buf = bytearray()
        for i in range(frames):
            # slow warble so it is obvious this is a stand-in, not real speech
            v = int(3000 * (0.5 + 0.5 * (i % 800) / 800.0))
            buf += struct.pack("<h", v)
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(buf))
        report(opts, 1, 1)
        return Audio(out.getvalue(), self.ext, self.mime, 0.0)
