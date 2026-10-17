"""Client core: verify-before-play, offline plan, sequential auto-advance.

The client never renders and never trusts bytes off the wire. A chapter is
materialized and verified (hash + size) before it is marked ready, online or
offline. Offline is a pull of what is already converted - it reports the rest as
not-downloadable and never triggers a render.
"""

import hashlib


class VerifyError(Exception):
    """Bytes did not match the ref. Never play unverified audio."""


def verify(data, checksum, size=None):
    if size is not None and len(data) != size:
        raise VerifyError("size %d != expected %d" % (len(data), size))
    got = hashlib.sha256(data).hexdigest()
    if got != checksum:
        raise VerifyError("checksum %s != expected %s" % (got, checksum))
    return True


def offline_plan(chapters):
    """Split chapters into what can be pulled now and what has not been converted.

    `chapters` = [{idx, audio_ref, provider, voice, model}]. Pull-only: a chapter
    with no audio_ref is reported, never queued for a server render.
    """
    downloadable, not_ready = [], []
    for c in sorted(chapters, key=lambda c: c["idx"]):
        (downloadable if c.get("audio_ref") else not_ready).append(c["idx"])
    return {"downloadable": downloadable, "not_ready": not_ready}


class Player:
    """The playback state machine a client shell drives. Pure and offline."""

    def __init__(self, chapters):
        self.chapters = sorted(chapters, key=lambda c: c["idx"])
        self.ready = set()
        self.chapter_index = 0
        self.offset_seconds = 0.0

    def _chapter(self, idx):
        for c in self.chapters:
            if c["idx"] == idx:
                return c
        return None

    def accept(self, idx, data):
        """Verify and mark a chapter ready. Raises VerifyError on a bad blob."""
        c = self._chapter(idx)
        if c is None:
            raise KeyError("no chapter %s" % idx)
        verify(data, c["audio_ref"], c.get("audio_bytes"))
        self.ready.add(idx)
        return True

    def is_ready(self, idx):
        return idx in self.ready

    def can_play(self, idx):
        return self.is_ready(idx)          # verify-before-play: never stream

    def resume_at(self, chapter_index, offset_seconds):
        """Adopt a server position. Only plays if that chapter is verified."""
        self.chapter_index = int(chapter_index)
        self.offset_seconds = float(offset_seconds)
        return self.can_play(self.chapter_index)

    def next_chapter(self):
        """Next chapter only if sequential: same provider/voice/model, verified."""
        cur = self._chapter(self.chapter_index)
        nxt = self._chapter(self.chapter_index + 1)
        if cur is None or nxt is None:
            return None
        same_voice = (nxt.get("provider"), nxt.get("voice"), nxt.get("model")) == \
                     (cur.get("provider"), cur.get("voice"), cur.get("model"))
        if not same_voice or not self.is_ready(nxt["idx"]):
            return None                    # a gap pauses at the boundary
        return nxt["idx"]
