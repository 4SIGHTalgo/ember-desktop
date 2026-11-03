# Ember 0.3.1 — falling-price reference windows and live DraftKings repairs

Release status: stable. Version 0.3.1 is published at 100% on all three updater
targets, and the service now requires 0.3.1 or later.

Reference windows now open only while conservative EV is positive at the set
anchor. After that they accept only an unchanged or lower price while EV stays
positive. Any observed price rise, or EV no longer positive, permanently ends the
window: a rising price means the market moved against the selection, which the
frozen pre-set probability cannot see. The window kind is now
`pregame_non_increasing`. Version 0.3.0 rejects it and fails closed, which means
no signal is acted on until this update is installed. The client ledger, slip
reads and market readiness keep the lowest observed price as a ceiling, and the
server claim checks the same ceiling.

DraftKings repairs from live runs:
- Closeout signals reach the Moneyline board, including DraftKings' renamed
  "Winner" and "Match Winner" sections. Non-table-tennis pages are left after a
  grace period, so reading My Bets is never interrupted.
- Ember routes between matches in place in one tab, because a freshly opened
  DraftKings event tab stalled for 20 to 30 seconds in live runs. It closes the
  tabs of finished matches while always keeping one open, and it reattaches to
  the last worked tab.
- A location failure that persists is rechecked after 2, 4, 8 and 16 minutes,
  then every 30 minutes, instead of pausing wagering for hours. A confirmed
  wagering permission resets that schedule.
- Session keep-alive continues while Ember waits, never clicks through an
  overlay, and backs off after a click that never returns. A sign-out during a
  run raises an alert.
- PICK CLOSED at submission is a miss for that contract only, and the run
  continues. The contract is never retried.
- Driver commands outlast DraftKings' long main-thread tasks (25 s), and a
  clicked leg must land in the slip before the plan reads it.
- Execution-error misses record only the exception class and Ember's own code
  location. Per-command timing and stage marks also record no page text.
- A no-wager rehearsal mode arms the server-sized slip and never dispatches the
  wager command. It is used for release acceptance.

Source: the private client at d9e03c5 (worker 9B3F045A) ran live for about
10.3 hours on 2026-11-01 and 11-02. It placed 3 wagers (Points Spread, Games
Spread, Game 3) with no wrong submissions. The first closeout that reached the
Moneyline board failed closed on a locked button. This release ports that engine:
10 changed modules are byte-identical copies, 2 are new (`ember_tabs`,
`ember_navigation`), and 4 public variants (`ember_client`, `ember_browser_lifecycle`,
`ember_market_readiness`, `tt_market_exec`) carry the same changes line for
line. No formula, freshness limit, price floor, economic reservation or replay
guard was weakened, and the public worker still contains no strategy model.

Pre-release gates and acceptance:

- Source CI [run 41406971572](https://github.com/4SIGHTalgo/ember-desktop/actions/runs/41406971572)
  passed at `0921c83` on Windows x64 and both Mac architectures. It covered
  every Windows browser tier, the worker bundle check (tab and navigation
  modules present, no private strategy module), the packaged startup smoke with
  headed Chrome on all three hosts, the native builds, and the Windows WebView
  event gate. Earlier runs on this branch caught one test that relied on
  `window_handles` order and two fixtures tied to runner speed. All three were
  fixed in the tests; no gate was loosened.
- The CI-built Windows installer `Ember_0.3.1_x64-setup.exe` (SHA-256
  `bc870871482bd772d73d2b158986e8d569d84f4fe810d59b2c4a3b2ceb86cb96`) upgraded
  the installed 0.3.0 in place with exit code 0. It reports native version
  0.3.1, and its worker SHA-256 is
  `2a503dc96e39a3313ee95044d656f14aa67650512a08d332b56bf6eba08c74ca`. The
  execution ledger, opportunity journal and browser profile were preserved.
- No-wager rehearsal on that installed build (2026-11-02, 15:57–21:37 UTC): two
  live signals reached an armed, server-sized slip and stopped before the wager
  command. One was a closeout on the Moneyline board ($0.87), the other a Game 4
  ($1.23). After the Game 4 abort, Ember could not verify the single slip empty,
  so it blocked that contract and journaled a miss; nothing was submitted.
  Other signals expired while Ember routed to their match, as on the private
  worker.
- Live run on that installed build, started from the app at 2026-11-03 05:56
  UTC: in its first 2.6 hours, signal #1051 (Game 4, $0.46 at a 1.79–1.85 price
  band) was submitted 2.4 s after receipt and confirmed placed. The other two
  signals at the selected level were misses, with no submission. There were no
  unconfirmed submissions and no wrong submissions. The ledger holds one placed
  attempt for that contract, and the journal synced it as placed.
- Known limitation: on the private worker, closeout #1017 (2026-11-02 07:41 UTC)
  was still showing DraftKings' "PLACING YOUR BETS" when Ember's 12-second
  receipt wait ended. Ember recorded it as unconfirmed and stopped, as designed.
  DraftKings had placed it, and the owner confirmed this in My Bets. The client
  has no in-app way to clear an unconfirmed submission, so after one, Start
  stays blocked until the owner clears it.

## Stable release acceptance

- [Release workflow 41601231961](https://github.com/4SIGHTalgo/ember-desktop/actions/runs/41601231961)
  for tag `v0.3.1` (`0921c83`) passed Windows x64, Intel Mac and Apple Silicon,
  including the Windows WebView event gate, and refreshed the Homebrew cask. It
  passed on the first attempt, with no rerun.
- All 24 assets match their GitHub digests and their checksum sidecars. The
  sidecars of the versioned asset names record the runner's temporary path, not
  the bare file name, so only the alias sidecars (for example
  `Ember-Setup-x64.exe.sha256`) work with `sha256sum -c`.
- The Homebrew cask is at 0.3.1, with both DMG digests matching.
- Updater: the three signed artifacts minisign-verified against the pinned key,
  bound to 0.3.1, and were promoted to stable 100% (release IDs 63, 64 and 65).
  Public verification at 2026-11-03 08:27:09 UTC:
  - Upgrade checks from 0.1.13, 0.2.1 through 0.2.9, and 0.3.0 return the exact
    0.3.1 signatures.
  - 0.3.1 checks return HTTP 204.
  - Each download returns HTTP 200 with the exact length.

| Target | Release ID | Bytes | SHA-256 |
| --- | --- | --- | --- |
| Windows x64 | 63 | 30,568,329 | `f7e675e66e5e1c54ff6edab14b625ce87496a00e337f38ec420c3ca050818f2e` |
| Apple Silicon | 64 | 27,464,802 | `5a6b6c512d9c58005458ad30a6fd6f74ccafe13cd387d49b37e746fff3f5aa82` |
| Intel Mac | 65 | 28,640,598 | `f4135992f27da667483ed84d0ddbf1b8d0a25d20958ec58363104c7cdee3855d` |

- The service minimum is now 0.3.1, so 0.3.0 is told to update rather than
  receiving windows it rejects. The web and signal API services were restarted,
  and the installed 0.3.1 client reconnected to the signal stream.
- Website version `4c32b99c-3548-4001-9b7e-45168f184f54` serves the 0.3.1
  install guide and the Bet Hub's current reference-window wording. It was a
  scoped change on top of the live version; every other page and asset was
  kept. Live guide checks passed at 390 and 1280 pixels, `releases/latest`
  links return the 0.3.1 installers, and `/health/` is ok.
- The laptop runs the CI build of the same commit. The release-built installer
  was not installed, because that would have meant stopping a live run in
  progress.
