# Ember 0.2.5 official location rechecks

Includes the native slip-identity and provider-error recovery changes documented
in `PROVIDER_RECOVERY_0.2.4.md`. 0.2.4 native builds passed but remained a draft
while the requested location follow-up was included in this release.

The desktop gate now uses the existing official-location helper. A recognized
failure banner can open exactly one visible, unambiguous same-origin `/auth`
Recheck link through a normal trusted WebDriver click. Unrelated, external,
disabled, hidden, duplicate and changed-purpose controls are rejected.

Wager execution remains paused during verification. Native sign-in, can-bet and
wagering-grant evidence must return on a completed document after the settling
hold. An absent warning alone does not restore permission. Only that positive
permission observation enables one attempt for a later, separate expiry episode.
Driver reconnection retains the reserved attempt; a lost click reply is not
replayed. Stop cancels an attempt before dispatch.

This does not change coordinates, fake GeoComply acceptance or extend a provider's
verification lifetime. Authentication, MFA and CAPTCHA remain user actions. No
previous wager or expired signal is replayed after a location check.
