# fireside v2 - playback and sync

Status: the client-side model for step 5 of the build order. Derived from Chris's
end-to-end playback walkthrough (2026-10-17). Experience first: the walkthrough is
the spec, the mechanism is what it implies.

## The walkthrough

A user has a converted book. They:

1. select it from their library and load it;
2. toggle **auto-play next chapter** - if the next chapter exists from the same
   model and voice, it plays straight through; a gap stops playback at the
   boundary;
3. listen to 1:16 on the Windows client, then open the Android client and hit
   play - **the Windows client stops immediately**;
4. if the chapter isn't local, the client pulls it and starts only after it has
   **fully downloaded**;
5. offline is a pull of already-converted chapters and **never triggers a
   render**.

## What that implies

### One authoritative position, per user per book

Position is a single record per user per book:

    {chapter_index, offset_seconds, updated_at, owner_device_id}

`updated_at` is stamped by the server, never by a client clock. Writes:

- **pause / stop / seek** write immediately and exactly (a clean stop resumes
  where it stopped);
- **while playing**, a heartbeat writes every ~15s;
- a hard kill therefore loses at most one heartbeat (~15s). 1:16 landing mid-beat
  isn't a designed seek, it's where the last heartbeat fell.

The book-level position (chapter + offset) is what makes "1:16 on the PC, 1:16
on the phone" the same place.

### Handoff is a single-owner claim

Only one device plays a book at a time. "Play" claims ownership:

1. the client asks the server to claim `owner_device_id`;
2. the server records the new owner and **pushes stop** to the previous owner's
   live session;
3. the previous owner stops immediately (that is the Windows-stops-when-Android-
   plays behavior) and writes its exact position before yielding.

This is why the two clients never both play the same book.

### Auto-advance only over sequential chapters

Advance to the next chapter **only** when it is sequential: same book, next
index, same model + voice, already rendered. Then playback is seamless.

A gap (missing or differently-voiced next chapter) **pauses at the boundary**. It
does not skip a missing chapter and does not start an unrendered one.

### Download, verify, then play

Before a chapter plays, it must be present and verified:

- a chapter audio ref = `{chapter_id, storage_ref, bytes, checksum, duration_s,
  provider, voice, model}`;
- the client fetches, checks `checksum` and `bytes`, and only then marks it
  **ready** and plays it.

There is no partial play of a chapter. "It only starts after it fully downloads"
applies online and offline alike - the client materializes the whole chapter
first, whether it came over the wire or from local disk.

### Prefetch and offline are different promises

- **Prefetch** (default on): while chapter N plays, fetch N+1 if it is
  sequential. Bounded by the same verify gate. This is invisible, best-effort
  insulation from a bad network.
- **Make available offline** (explicit): pull **every** chapter of the book to
  the device for listening without a connection. It is **pull-only**:

  - it downloads what is already converted;
  - it reports chapters that are not converted yet as not-downloadable;
  - it **never** kicks off a render and never spends server-side render cost on
    its own. Rendering is always an explicit user action in the conversion
    workflow.

The phone is never a renderer. Client = player + cache.

### Conflict is explicit

If two devices both think they own playback ("laptop says X, phone says Y"), the
user picks one. Picking one makes it the owner and stops the other - the same
stop-push as a normal handoff, just user-initiated.

### Cache: bytes are shared, position is not

- Audio blobs are content-addressed by `checksum`, so two books (or two users)
  that resolve to the same bytes share the stored blob. The cache is global on
  the server's storage layer.
- Library, position, and offline selections are **per-user**. Sharing happens at
  the storage contract, not at the account.

## Still inferred (confirm before building)

- **Sleep timer / speed control**: not in the walkthrough. Assume playback speed
  is per-client UI state and a sleep timer is a client nicety, neither part of
  the synced model.
- **Chapter bookmarks**: the model keeps one position (chapter + offset). If
  per-chapter bookmarks are wanted, it's an additive table, not a change to the
  resume path.
- **The "pull then play" gate vs streaming**: the walkthrough says fully
  download first. That is deliberate (verify-before-play), and it is the reason
  prefetch exists.
