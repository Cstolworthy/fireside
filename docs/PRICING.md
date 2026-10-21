# Pricing policy (fireside)

This is the business rule the accounting epics (#28, #39, #43, #44) are built
against. It settles one question the spec left implicit: where the margin lives.

## The rule

Two lines, each covers its own cost. Nothing sells below cost.

1. **Character capacity** (the designated sale connection) sells at
   **provider rate + our fee**. The fee covers the per-transaction payment
   fee (Stripe, ~3-6%) plus a small spread for infrastructure and support.
2. **The platform tier** (recurring) pays for the server, storage, and updates,
   and stays cheap. It is *not* a per-character charge and buys nothing but the
   platform — that is stated explicitly at purchase.

**BYO-key self-host stays the zero-margin floor.** A user who runs the open
source server with their own provider key pays nothing to us and is not
subsidised by anyone.

## Why not sell characters at raw provider cost

Selling at cost loses money on every sale at any volume:

- the payment fee is per-transaction, so it is a fixed toll on each purchase;
- provider cost scales with use, so a cost-exact price can never be ahead;
- if the platform tier were made to carry the whole margin instead, it would be
  priced for the heaviest user — exactly the one who costs the most.

The earlier draft of #43/#44 had the sale rate set equal to the provider rate
("no markup or fee adjustment"). That number was set to zero by omission, not by
policy. Chris's call on 2026-10-16: put the policy here, keep the arithmetic.

## How this fits the spec without changing it

The spec is right that the host stays dumb and the plugin is the sole authority
on price (#33, #43, #44). So the fee is **not** applied by the host. It lives
inside the rate the designated sale connection's plugin reports:

- the **sale connection** is a Fireside-authored plugin wrapping the chosen
  provider; it reports a rate that already includes our fee;
- every other connection is pass-through and reports the raw provider rate;
- `characters = floor(payment / reported_rate * 1_000_000)` from #43 is then
  correct exactly as written — no host-side markup, no host-side fee math.

The user sees the rate and the number of characters it buys. The provider's
public price is a lower bound they can compare against; the difference is the
fee, which we do not hide behind a fake "credit" abstraction.

## What stays out of scope

- Currency conversion. Rate and sale currency must match (#44).
- Retrofitting fee onto already-purchased characters. Purchased quantities are
  fixed; later price changes affect future purchases only (#45).
- Local/open-source engines. If a self-hosted TTS engine is wired in as a
  provider, its rate is 0 and it can be the sale connection at 0 + fee; that is
  the real supply lever against Azure's commitment-tier wall.

## Mechanics: the character ledger (2026-10-20)

The unit of the credit ledger is the character, and the plugin is the authority
on it, so the host stays dumb:

- The sale connection's plugin reports `sale_rate_per_1m` = (provider rate +
  per-1M-chars infra fee) * markup. Both the infra fee and the markup are fields
  on the plugin's own config screen; the host never computes a price.
- A payment of `payment` USD buys `characters = floor(payment / rate * 1_000_000)`
  and the balance becomes characters. The rounding is floored, so the fraction
  the money cannot fund lands on us, not the buyer.
- A metered run holds its estimated characters before it starts (the host holds
  `Quote.price` in `Quote.currency`), the plugin reports the real count after,
  and a run the balance cannot cover is refused up front. An `unknown` report
  stays held and visibly pending, never guessed.

Payments sit behind `host/payments.py`: no provider ships, so the purchase route
refuses ("purchases are held until a payment provider lands") until an operator
registers one. `GET /credits` is read-only (balance, rate, bundles);
`POST /credits/purchase` is the only route that turns a payment into characters.

Still to build: the payment provider once it is registered (the tier and storage
screens ship first, priced from the provider when one exists).

## A settled checkout lands twice (2026-10-21)

A buyer who pays and then closes the tab must still get their plan, so there are
two ways the same sale is recorded, and both are safe:

- `POST /billing/confirm` is the browser's fast path. It needs a session and the
  Checkout Session id, and it re-reads the session from the provider.
- `POST /webhook/<provider>` is the provider's own server-to-server call. It is
  public by necessity, so the *signature* is the whole authentication: the body
  is not read until the provider verifies it. A forged or replayed body changes
  nothing.

The two write the same rows (both writers are upserts), so it does not matter
which arrives first, and the provider's retries are harmless. A render is only
metered against the sale connection, never a BYO-key connection, and a fresh
render pays only for characters it actually synthesises.

To turn Stripe on: set `FIRESIDE_PAYMENT_PROVIDER=payments_stripe`,
`FIRESIDE_STRIPE_KEY=sk_...` (test or live), and `FIRESIDE_STRIPE_WEBHOOK_SECRET=whsec_...`
for the endpoint registered at `/webhook/stripe` in the Stripe dashboard. With
no key, or no webhook secret, purchases refuse and the webhook refuses; the
honest default is off.

## The plan model (2026-10-21)

Two tiers and one add-on, named in `host/quota.py` and priced by the provider:

| plan | included | price |
|------|----------|-------|
| free | 500 MB | none; the sign-up default |
| platform | 2 GB | provider's monthly price |
| storage | +5 GB per block | provider's monthly price per block |

An account on **free** is put there when its user row is first created
(`auth._start_on_free_tier`). An account with **no plan row at all** (created
before the free tier existed) fails open to the platform allowance rather than
being cut to 500 MB - a plan name we do not recognise must never lock a user out
of their own library.

**Storage blocks are an add-on, not a tier.** They live in their own table and
add bytes on top of whatever tier you are on; buying blocks never moves you off
platform/free. `GET /plans` returns the tiers and the block for the screen;
`GET /storage` reports `storage_blocks` and the summed `allowance_bytes`.

**A comp is a plan row with `provider='admin'`.** An admin sets an account's tier
from the plans screen (`POST /admin/plan`); nothing downstream can tell a comped
row from a paid one except that field.

### Does the monthly cover the cost of hosting a user?

The marginal cost of one user is small; the fixed cost of the box is not, so the
tier must be read against how many people are on it.

- Blob storage: ~$0.018/GB/mo (hot LRS, US). 2 GB stored is ~4c/mo.
- Egress: $0.087/GB after the first 100 GB/mo. A heavy listener (a few hours a
day) is maybe 20-30c/mo.
- Per-user marginal, all in: **roughly $0.15-0.35/mo.**
- Fixed: one always-on Container App replica + a small MySQL Flexible Server is
  on the order of **$55-60/mo** together, shared across everyone.

So at the $3/mo platform tier a user's own cost is covered with room to spare,
but the fixed cost is carried by the crowd: **~20 subs break even on the box,
~50 leave a real margin.** A 5 GB storage block costs us ~9c/mo and sells for
$1/mo. These fixed figures are list-price estimates from the Azure calculator,
not a measured bill; the honest next step is to check them against the live
invoice once there are users. The egress figure is the one to watch, since
streaming scales with listening, not with storage.

## Storage: a held book bills its own bytes

Chris's rule (2026-10-20), and the launch model: **drop a book, and it consumes
storage.** Every distinct blob a user's shelf names - chapter text, the audio,
its renditions, its cover - counts against that user's plan, once per blob.

- **No sharing discount.** Two people holding the same book each bill its bytes.
  Content addressing still keeps one physical copy on disk; billing is per shelf,
  never per copy. This was a deliberate simplification: the refcount/auto-release
  model was more code and more ways to be wrong, and it charged nobody for the
  common case.
- **The allowance is the account's plan plus any storage blocks** (free 500 MB,
  platform 2 GB, +5 GB per block). An account with no plan row fails open to the
  platform tier, so it is never locked out of its own library.
- **A write that would cross the allowance is refused up front** (import, cover,
  render). Existing bytes are never silently removed to make room.
- **Promote-to-shared is parked.** Handing a book to the shared catalog so it
  stops billing you is a real later feature; nothing here releases storage
  automatically.

Cost lever: because storage is a pluggable backend, Azure Blob lifecycle rules
move rarely-read books down through hot -> cool -> cold -> archive. Blob storage
at the hot tier is ~$0.018/GB/mo (LRS, US), cool ~$0.01, archive ~$0.002, so the
bytes are rounding error next to the server; the tier's real cost is the box.
