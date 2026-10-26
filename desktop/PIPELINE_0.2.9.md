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
