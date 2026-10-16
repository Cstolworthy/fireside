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

## Drift already present (fix when the loader lands)

- `server.py` dispatches on `provider` with if/elif in three places (voices, say,
  render). Collapse to a registry lookup; keep behavior identical.
- `local` has no `list_voices` and its `synth` is `(text, out_path)`, not
  `(text, voice, opts, cred)`. It needs an explicit "no voices, zero cost" shim.
- `synth_azure` drops `model`; `synth_minimax` takes one. `opts` carries both.
- The `PROVIDERS` dict holds only `ext`/`mime`; fold it into the module.

## Not in scope here

- The loader (discovery from a `providers/` dir, isolation, third-party install).
  Later, and it is expansion, not revenue: do not let it eat the hosted-billing
  path.
- Offline. Web pre-fetch is a service-worker cache the OS can evict, so web
  offline is best-effort. Durable offline belongs to the native app. Held.
