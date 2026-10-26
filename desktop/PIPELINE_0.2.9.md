# Ember 0.2.9 — closed-pick submission recovery

Closed picks can replace the submit button with “Remove Unavailable Picks” and
remove their own stake/odds controls. Version 0.2.8 treated that native layout as
unsupported, preventing another valid selection from reaching stake authorization.

This release identifies closed picks from agreeing native contract/status,
visible provider markers and explicit unfunded Single-bet evidence. It removes
them individually only while the entire slip is unfunded, rechecking between
removals. Open and suspended picks are preserved. Cleanup runs during staging,
signal readiness and pre-claim validation. A late closure aborts submission;
funding/identity ambiguity and uncertain prior submissions remain blocked.

The closed-pick fixture reproduces the provider test IDs, native selection-only
tracking and the blocked submit control. Both source and frozen workers pass the
actual scanner/Django/TLS/WSS/Chrome pipeline: two closed picks removed, then four
exact claims and four synthetic acceptances across the approved market families.
Coverage includes failed removal, unknown funding, stop/uncertainty, and picks
closing after claim or arming. Detailed local readiness reasons remain compatible
with the existing server reporting protocol.

Pre-release gates: 326 public tests, 1,266 private tests (five skipped), 174 pinned
engine/dashboard checks and 16 connected-pipeline tests passed. No formula,
freshness limit, price floor, economic reservation or replay guard was weakened.
No live wagers were used for verification.

Stop monitoring before updating. Review saved settings after restart; press Start
only when you want to enable monitoring and execution.

## Stable release acceptance

- [Release CI](https://github.com/4SIGHTalgo/ember-desktop/actions/runs/40340886594)
  and [source CI](https://github.com/4SIGHTalgo/ember-desktop/actions/runs/40340880986)
  passed Windows x64 and both Mac architectures. One Windows local-fixture
  navigation failure and one Intel Mac busy-DMG failure passed on a single
  failed-job rerun with unchanged source and checks; initial failures remain
  visible in Actions history.
- All 24 assets match their GitHub digests and checksum sidecars. Updater
  signatures verify for all platforms. Packaged versions/architectures agree,
  private strategy modules are absent, and the native slip reader and closed-pick
  cleanup fragment match the verified source.
- All three stable updater targets are at 100% rollout. Upgrade checks through
  0.2.8 return exact signed 0.2.9 artifacts; current 0.2.9 checks return no update.
  The production installation guide shows 0.2.9.
- The verified installer upgraded Windows to native version 0.2.9.0 and worker
  0.2.9. The exact installed worker passed the connected closed-pick fixture:
  two closed picks removed, four server-sized claims and four synthetic provider
  acceptances. Existing execution history, profiles, shortcut and settings survived.
  The regular app was signed in with a current feed and left stopped, with no
  pending update. No live wager was used for release verification.
