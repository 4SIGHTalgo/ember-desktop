# Ember 0.3.2 — monitoring continues after a skipped bet

Release status: stable. 0.3.2 is published at 100% on all three updater targets.

On 2026-11-02 to 11-04, 0.3.1 ended its own run three times after a bet it did
not submit, because it could not verify the single slip empty afterwards
(#1039, #1064, #1103). Every later signal was then missed. Once the run had
stopped, nothing kept the DraftKings session active, and DraftKings signed the
account out for inactivity.

- The contract stays blocked, as before: it is never released or retried, and
  it is journaled as a miss. The run now continues. Every later signal already
  has to verify the single slip before it can arm, and the idle slip upkeep
  removes unfunded leftovers. If a stake Ember armed could not be cleared, the
  run still stops, because only you can check a funded slip.
- While the slip still says "Placing your bets", the receipt wait runs up to 30
  seconds after the click instead of 12. Ember never clicks again during that
  wait. Closeout #1017 was placed but still showed that state at 12.9 seconds.
  Any other state keeps the 12-second bound, and a placing state that lasts past
  30 seconds stays unconfirmed.

No formula, price floor, freshness limit, reservation or replay guard changed.
An unconfirmed submission still blocks Start until it is cleared.

Gates: the new `test_wager_submission` checks that the placing state is
awaited and bounded and that the normal bound still applies. `test_ember_client`
checks three things: a leftover pick blocks only its contract, the next contract
arms only on a verified slip, and a stake left in the slip still stops the run.
These tests fail against 0.3.1. Shared modules are byte-identical to the private
source (`fb3e221`), apart from the 8 known public variants. This release was
gated on tests and 3-OS CI only, with no device rehearsal.

## Stable release acceptance

- Source CI [run 41775428461](https://github.com/4SIGHTalgo/ember-desktop/actions/runs/41775428461)
  at `cb1382e` passed Windows x64 and both Macs. Run 41774308906 at `ab627c9`
  had failed one Windows navigation fixture, because the fixed 0.5 s sleep was
  shorter than a new tab's renderer start. The test now waits on the gate for
  each document. No gate was loosened.
- [Release workflow 41778391659](https://github.com/4SIGHTalgo/ember-desktop/actions/runs/41778391659)
  for tag `v0.3.2` passed all three platforms on the first attempt, including
  the Windows WebView event gate, and refreshed the Homebrew cask to 0.3.2.
- All 24 assets match their GitHub digests and checksum sidecars.
- Updater: the three signed artifacts minisign-verified against the pinned key,
  bound to 0.3.2, and were promoted to stable 100% (release IDs 66, 67 and 68).
  Public verification at 2026-11-04 05:35:07 UTC:
  - Upgrade checks from 0.1.13 through 0.3.1 return the exact 0.3.2 signatures.
  - 0.3.2 checks return HTTP 204.
  - Each download returns HTTP 200 with the exact length.

| Target | Release ID | Bytes | SHA-256 |
| --- | --- | --- | --- |
| Windows x64 | 66 | 30,569,295 | `aa1a5f7d15533f18a966fa53844b6df60155ce8c4b8b6d7ef5e8f007eb9fe337` |
| Apple Silicon | 67 | 27,453,737 | `6f93e294dd35d8ef8906f91f16d88093fe47250418b57d59eb976b4ddce228d7` |
| Intel Mac | 68 | 28,640,969 | `867b81a256c6b8167c84eeb68ae9c7ca0a92d6d6735fcad49bbab4235f348fce` |

- Website version `65478040-971c-4f9f-be06-1fa41557bfa2` serves the 0.3.2
  install guide as a scoped change on top of the live version.
  - Live guide checks passed at 390 and 1280 pixels.
  - `releases/latest` links return the 0.3.2 installers, and `/health/` is ok.
- The service minimum stays at 0.3.1, and no server change was needed.