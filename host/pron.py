"""Per-user pronunciation fixes: the words a voice gets wrong.

A rule is {"from": word, "to": respelling}. `from` is the word exactly as it
appears in the text; `to` is how it should sound, spelled out for the engine
(Hermione -> Her-my-oh-nee, Qui-Gon -> Kwai-Gon). The host applies the list to
the chapter text before any speech engine sees it, so every engine gets the fix,
including the local one that cannot take phonemes.

Rules are per user and global across their books, stored as JSON in
user_prefs.pron.
"""

import hashlib
import json
import re

from host.db import now


def load(conn, uid):
    """This user's list, tidied: [{'from': word, 'to': respelling}].

    Anything malformed is dropped rather than raised: one bad row must not take
    down a render.
    """
    row = conn.execute("SELECT pron FROM user_prefs WHERE user_id=?",
                       (uid or "",)).fetchone()
    raw = (row["pron"] if row else "") or ""
    try:
        rules = json.loads(raw) if raw else []
    except Exception:
        return []
    if not isinstance(rules, list):
        return []
    out = []
    for r in rules:
        frm = str((r or {}).get("from", "")).strip()
        to = str((r or {}).get("to", "")).strip()
        if frm and to:
            out.append({"from": frm, "to": to})
    return out


def save(conn, uid, rules):
    """Tidy and store the list: drop blanks, drop duplicate words, keep order.

    Updates the existing prefs row in place so the other columns (default
    voice/connection) survive; only inserts when the user has no row yet.
    """
    clean, seen = [], set()
    for r in rules or []:
        frm = str((r or {}).get("from", "")).strip()
        to = str((r or {}).get("to", "")).strip()
        if frm and to and frm.lower() not in seen:
            seen.add(frm.lower())
            clean.append({"from": frm, "to": to})
    blob = json.dumps(clean, ensure_ascii=False)
    row = conn.execute("SELECT user_id FROM user_prefs WHERE user_id=?",
                       (uid,)).fetchone()
    if row:
        conn.execute("UPDATE user_prefs SET pron=?,updated_at=? WHERE user_id=?",
                     (blob, now(), uid))
    else:
        conn.execute(
            "INSERT INTO user_prefs(user_id,pron,updated_at) VALUES(?,?,?)",
            (uid, blob, now()))
    conn.commit()
    return clean


def _regex(rules):
    """One alternation, longest word first, whole-word where the word has edges."""
    alts = []
    for r in sorted(rules or [], key=lambda r: -len(r["from"])):
        frm = r["from"]
        e = re.escape(frm)
        if re.match(r"\w", frm[0]):
            e = r"(?<!\w)" + e
        if re.match(r"\w", frm[-1]):
            e = e + r"(?!\w)"
        alts.append(e)
    return re.compile("|".join(alts), re.I) if alts else None


def apply(text, rules):
    """Swap whole words for their say-as respelling, case-insensitive. The
    replacement goes in literally, never read back as a regex."""
    pat = _regex(rules)
    if not text or pat is None:
        return text
    repl = {r["from"].lower(): r["to"] for r in rules}
    return pat.sub(lambda m: repl.get(m.group(0).lower(), m.group(0)), text)


def token_for(text, rules):
    """A short tag for just the rules that match `text`, for the audio cache key.

    Only the matching rules go in, so adding a rule for a word that lives in one
    chapter does not invalidate chapters where that word never appears. Empty
    means no rule applies, and the chapter keeps whatever audio it already had.
    """
    pat = _regex(rules)
    if not pat or not text:
        return ""
    found = {m.group(0).lower() for m in pat.finditer(text)}
    use = [r for r in rules if r["from"].lower() in found]
    if not use:
        return ""
    raw = "|".join("%s>%s" % (r["from"].lower(), r["to"])
                   for r in sorted(use, key=lambda r: r["from"].lower()))
    return "p" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]


def impact(conn, storage, user_id, book_id=None, split_for=None):
    """Chapters already voiced that the current rules make out of date.

    For every book the user owns, look at each rendition - one voiced copy of a
    chapter, one voice. A rendition is stale when the pronunciation tag it was
    made with no longer matches the tag the rules produce for that chapter's
    text. That is the exact test the renderer uses to decide what to redo.

    `split_for(provider)` returns a provider's chunk function, so the report can
    also say how much audio is genuinely new: only the chunks with no cached
    audio for the current rules get synthesised, the rest are reused. Without a
    split function the whole chapter counts, a safe upper bound.

    Grouped by book and by (provider, voice, model, speed) so the caller can
    re-encode each voice with a real render call, scoped to just these chapters.
    """
    from host import library, jobs

    rules = load(conn, user_id)
    q = "SELECT id,title FROM books WHERE user_id=?"
    args = [user_id]
    if book_id:
        q += " AND id=?"
        args.append(book_id)
    books = []
    chapters_affected = 0
    chars_to_synth = 0
    est_seconds = 0.0
    for b in conn.execute(q, tuple(args)).fetchall():
        chapters = library.chapters(conn, b["id"])
        by_idx = {c["idx"]: c for c in chapters}
        rends = {}
        for r in library.renditions(conn, b["id"]):
            rends.setdefault(r["idx"], []).append(r)
        stale = set()
        groups = {}
        for c in chapters:
            has_default = bool(c.get("audio_ref"))
            rs = rends.get(c["idx"], [])
            if not rs and not has_default:
                continue  # never voiced: nothing to redo
            try:
                text = storage.get(c["text_ref"]).decode("utf-8")
            except Exception:
                text = ""
            tag = token_for(text, rules)
            if not rs and has_default:
                # legacy row with audio but no rendition entry; treat the
                # chapter's own columns as the one voiced copy.
                rs = [{"provider": c.get("provider") or "",
                       "voice": c.get("voice") or "", "voice_label": "",
                       "model": c.get("model") or "",
                       "speed": c.get("speed") or 1.0,
                       "pron": c.get("pron") or ""}]
            for r in rs:
                if (r.get("pron") or "") == tag:
                    continue
                prov = r.get("provider") or ""
                voice = r.get("voice") or ""
                model = r.get("model") or ""
                speed = float(r.get("speed") or 1.0)
                label = r.get("voice_label") or ""
                split = split_for(prov) if split_for else None
                if callable(split):
                    sc = jobs.stale_chars(conn, storage, text, rules, split,
                                          prov, voice, model, speed)
                else:
                    sc = len(text)
                g = groups.setdefault((prov, voice, model, speed, label),
                                      {"chapters": [], "chars_to_synth": 0})
                g["chapters"].append(c["idx"])
                g["chars_to_synth"] += sc
                stale.add(c["idx"])
        if not stale:
            continue
        rends_out = []
        book_chars = 0
        book_seconds = 0.0
        for (prov, voice, model, speed, label), g in groups.items():
            secs = g["chars_to_synth"] / jobs.synth_rate(prov)
            book_chars += g["chars_to_synth"]
            book_seconds += secs
            rends_out.append({"provider": prov, "voice": voice, "model": model,
                              "speed": speed, "voice_label": label,
                              "chapters": sorted(g["chapters"]),
                              "chars_to_synth": g["chars_to_synth"],
                              "est_seconds": round(secs, 1)})
        books.append({
            "id": b["id"], "title": b["title"],
            "chapters": [{"idx": i,
                          "title": (by_idx.get(i) or {}).get("title") or ""}
                         for i in sorted(stale)],
            "renditions": rends_out,
            "chars_to_synth": book_chars,
            "est_seconds": round(book_seconds, 1),
        })
        chapters_affected += len(stale)
        chars_to_synth += book_chars
        est_seconds += book_seconds
    return {"books": books, "books_affected": len(books),
            "chapters_affected": chapters_affected,
            "chars_to_synth": chars_to_synth,
            "est_seconds": round(est_seconds, 1)}
