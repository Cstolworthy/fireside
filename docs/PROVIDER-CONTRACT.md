# Speech provider contract (v1)

The app already speaks three engines (local, azure, minimax), dispatched by an
if/elif on `provider` in three places. That is a seam nobody named, and unnamed
seams drift. This file names it. The loader is later; the contract is the part
that stops the three from growing apart in the meantime.

## The interface

One provider = one module exposing:

1. `id` - stable slug (`local`, `azure`, `minimax`, ...). It is the root of the
   audio cache key and the meter tag. Never rename it once audio exists under it,
   or the cache orphans and the meter loses the row.
2. `list_voices(cred) -> [Voice]`, `Voice = {id, label, locale, gender}`.
   `label` is what a human reads, not the vendor's raw string (see `_voice_name`).
3. `synth(text, voice, opts, cred) -> Audio`,
   `Audio = {bytes, ext, mime, cost_usd}`. `bytes` is one self-contained file:
   we cache it to disk and concatenate it, so no streams and no sidecar headers.
4. `cost_per_1m_chars(cred) -> float | None` - `None` means unpriced, not free.
5. `auth` - how `cred` arrives. Two doors, one slot: `paste-key` (now) and
   `oauth` (later). The provider receives a *resolved* credential; it never holds
   the login and never sees which door it came through.

## Credentials stay host-side

The provider is handed a value, not a vault. Paste-key and oauth are two ways to
fill `cred`; the provider cannot tell them apart, so adding oauth later touches
no provider code.

## Cost must be declared

The meter is only honest if each provider states its own price. `local` = 0.
`azure` = $15/1M (pay-as-you-go; drops only at commitment tiers). `minimax` =
per model. When the key is the user's (BYO), our cost is 0 and the meter must
record chars at 0 cost, or a BYO user's bundle is burnt paying our price for
speech we did not buy. Unpriced providers render, but the meter marks the row
`unpriced` instead of guessing a number.

## Drift, named and closed (v1.7)

The three engines now live in `providers/` on this contract: one module each
(`local.py`, `azure.py`, `minimax.py`), plus the registry in `providers/__init__.py`.

- `server.py` asks `get_provider(provider)` instead of if/elif in three places.
- `local` exposes `list_voices` (one tone) and a `synth` that returns bytes at
  zero cost, so it is no longer a special case.
- `opts` carries `rate`/`speed`/`model` for every provider; azure ignores model,
  minimax uses it, and neither call site can drift from the other.
- The old `PROVIDERS` dict is gone; `ext`/`mime`/`needs_key` live on the module.
- `cost_per_1m_chars`: local 0, azure 15 (pay-as-you-go), minimax `None`
  (unpriced, not free). `/api/say` returns `cost_usd`; the host zeroes it for a
  BYO key.
- `render_chapter_file` and `/api/say` share one provider call, write the returned
  bytes once, and report progress through the same `report(n,total)` callback, so
  the Play button and Render-all cannot drift apart.

`tests/test_providers.py` checks all of it with no network and no fixture (it
builds a two-chapter EPUB in a temp dir and runs the real server).

Still later: the loader (discovery from a `providers/` dir, isolation, install).
A new provider only imports `Audio`/`Voice` and is added to `REGISTRY`; nothing
else in the app changes.

## Not in scope here

- The loader (discovery from a `providers/` dir, isolation, third-party install).
  Later, and it is expansion, not revenue: do not let it eat the hosted-billing
  path.
- Offline. Web pre-fetch is a service-worker cache the OS can evict, so web
  offline is best-effort. Durable offline belongs to the native app. Held.
