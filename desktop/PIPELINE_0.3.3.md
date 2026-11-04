# Ember 0.3.3 — a warm connection for each signal's first server check

Release status: stable. 0.3.3 is published at 100% on all three updater targets.

Signal #1115 (2026-11-04 06:03 UTC) had a 4.26 s lifetime. Ember clicked the
pick and it landed in the slip. The plan then ran out of time just before its
server claim, so Ember removed the pick without submitting, while its odds were
still valid. The first server check of that turn had taken about one second.

The local trace since 11/02 shows that this first authorize of a turn took a
median of 704 ms on the execution thread's idle (cold) HTTPS connection. The
same request inside a turn took a median of 79 ms. While idle, Ember now sends
one read-only status request at most every 20 seconds on that thread's
connection. That keeps the connection and the access token ready, and returns
about 600 ms to each signal. The request touches no lease, signal, browser or
wager state, its errors are ignored, and its timeout is 1 second. When a
cleanup after a skipped bet fails, the local trace now also keeps why the bet
was skipped.

No formula, freshness limit, lease, price floor, reservation or replay guard
changed.

Gates: `test_ember_client` checks that there is at most one status request per
interval with failures ignored, that an idle turn keeps the connection warm, and
that the skip reason survives a failed cleanup. All three fail against 0.3.2.
Shared modules are byte-identical to the private source (`ff368a7`), apart from
the 8 known public variants. Gated on tests and 3-OS CI only, with no device
rehearsal.

## Stable release acceptance

- Source CI [run 41785087983](https://github.com/4SIGHTalgo/ember-desktop/actions/runs/41785087983)
  at `5f68fbc` passed Windows x64 and both Macs on the first attempt.
- [Release workflow 41787029340](https://github.com/4SIGHTalgo/ember-desktop/actions/runs/41787029340)
  for tag `v0.3.3` passed all three platforms on the first attempt and refreshed
  the Homebrew cask.
- All 24 assets match their GitHub digests and checksum sidecars.
- Updater: the three artifacts minisign-verified, were bound to 0.3.3, and were
  promoted to stable 100% (release IDs 69, 70 and 71). Public verification at
  2026-11-04 06:54:58 UTC:
  - Upgrade checks from 0.1.13 through 0.3.2 return the exact 0.3.3 signatures.
  - 0.3.3 checks return HTTP 204.
  - Each download returns HTTP 200 with the exact length.
- Website version `304e434e-b96b-479a-80c9-f0dafb4c5dd0` serves the 0.3.3 guide
  as a scoped change.
  - Live guide checks passed.
  - `releases/latest` links return the 0.3.3 installers, and `/health/` is ok.