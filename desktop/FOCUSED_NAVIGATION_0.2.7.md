# Ember 0.2.7 focused navigation — 2026-10-26

One navigation arbiter now owns which live match the browser is on. It is
chosen from the stage catalog itself — an open entry window with an unstaged
leg first, then unstaged matches nearest set break — with a sticky focus, a
2+ point takeover margin, and the server's preparation hint as the tie-break.
Slip staging never navigates; it clicks only on the focused event's page, so
every live mapped match gets its legs pre-staged before its window opens and
a qualifying signal needs only claim and confirm. A signal turn that must
still cross matches gets one real 2 s navigation budget while reveal waits
keep their short yield slice.

Transient browser faults no longer discard queued signals: each signal's own
expiry plus the revoked lease remain the fail-closed authority, and a
possibly-submitted contract is still only ever resolved as `uncertain`. Idle
polling is paced (a queued signal still wakes immediately) and the slip read
is reused within a tick. Every `browser_fault` in the local lifecycle log now
carries the short operation label that preceded it. The panel shows a
computable estimate with its "Entry closed" stage, and the board names live
matches that lack verified history mapping. Wire schemas, the server
protocol, and every wager gate are unchanged.

## Verification

- Local Windows battery: client/history/opportunity/device/stream/preparation/
  readiness/lifecycle-recovery/semantics/exec/policy/login/live suites, board,
  location rechecks, native smoke, slip/staging/scheduling/location/dashboard
  browser tiers, provider-login/recovery/startup browser tiers, and the
  packaged-worker startup gates all pass. The worker bundle builds and
  verifies with no private modules.
- Real-browser scheduling fixtures: two events rotate through the arbiter,
  both pre-stage, and both signals place in either FIFO order; the idle-staged
  mode places in 187/375 ms receive-to-submit.
- Public CI run `40261148895` passed all three native platforms. Release run
  `40261790472` (retry of `40261167614`, whose Windows job hit the known
  timing-sensitive `(mode='slow')` scheduler flake also seen once at 0.2.6)
  passed all three platforms and the Homebrew tap.
- All 24 release asset/sidecar digests verified locally and again on the
  server; all three updater signatures verified against the version-bound
  public key with macOS architectures and bundled worker architecture
  confirmed; the packaged Mac workers report 0.2.7, exclude private modules,
  and embed the exact verified native slip reader.

## Stable rollout

Production updater rows 51 (windows-x86_64), 52 (darwin-aarch64), 53
(darwin-x86_64) are enabled, stable, 100% rollout. Authenticated checks:
versions 0.1.13 through 0.2.6 receive the signed 0.2.7 update, 0.2.7 returns
204, and artifact downloads match the registered byte lengths. The live
downloads guide serves the 0.2.7 instructions and links resolve to the
verified artifacts. The first release attempt remains visible in history as
a failed run; it is not represented as green.
