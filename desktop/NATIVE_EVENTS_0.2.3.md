# Ember 0.2.3 corrective release

The published 0.2.2 worker passed its startup and browser fixtures, but the native
UI failed installed verification: `plugin:event|listen` was denied by the Tauri
ACL. `capabilities/default.json` existed locally and was ignored by `*.json`, so
clean CI builds omitted it. Worker authentication/feed success did not establish
a functioning UI. The 0.2.2 updater rollout was withdrawn; artifacts are retained.

0.2.3 tracks the existing main-window capability file and adds a post-build Windows
WebView2 gate before publication. Windows UI Automation must observe the version
header set by the real packaged worker's ready event through the native event
ACL, stopped execution, disabled Start before sign-in, and a successful native
close. It uses temporary
application data, never signs in, and never opens a sportsbook browser.

The 0.2.2 staging, exact-leg, scheduler, and multi-match board implementation is
retained. Updater signatures are distinct from OS publisher signing/notarization.

Local verification retained the failed 0.2.2 ACL check. The 0.2.3 root rerun had
six Chrome lifecycle/locked-file errors in unchanged tests while the native build
was running; the affected seven-test lifecycle class passed in isolation. The
earlier 271-test release preflight was clean. These are distinct runs.

Hosted Windows exposed no CDP debug endpoint despite running WebView2 children.
The native gate now reads the actual accessibility tree, without requiring a
debugging transport. The production tag is unchanged; workflow-only retries use
the pinned current test harness against binaries built from that tag. A final
quiet local discovery passed all 271 tests; the native UI gate passed separately.

## Published and installed verification

- [Release run 40215076594](https://github.com/4SIGHTalgo/ember-desktop/actions/runs/40215076594)
  passed all three platforms and Homebrew; native source CI run `40215076405`
  also passed. The source tag stays at `e70c266`; the passing workflow/native
  accessibility harness is `e5fac99`. The failed attempts remain recorded.
- All 24 public asset/sidecar digests and all three version-bound updater
  signatures were independently verified. Mac app/worker versions and native
  architectures matched; private strategy modules were absent from the workers.
- Production updater metadata serves 0.2.3 to older clients and returns no update
  for 0.2.3. The live download guide and Homebrew cask point to 0.2.3.
- An existing Windows installation upgraded through the real native updater.
  Its executable, packaged worker and actual UI ready event all report 0.2.3.
  Durable ledgers, existing journal records, browser profile and shortcut survived.
- The installed app received fresh board/feed data from the compatible server.
  A zero-opportunity scan-only check observed provider sign-in/native permission
  and reached a verified event; it finished without a submission. The normal
  installed app was then left stopped. An independent location field remained
  unknown; no independently verified location is claimed.
- The production reader accepted an empty live slip during read-only inspection.
  **Populated live-provider exact-leg identity remains unverified.** No live wager
  was placed by this verification.

| Signed updater target | SHA-256 |
| --- | --- |
| Windows x64 | `7569035683dd9f52a629aaa08450ccdcf94f44a50a2efde82fdec82e7b767ae4` |
| Intel Mac | `aef9f272c04784af3a354b831c926d5d7bbac3b345aa678e0f435090c950ff93` |
| Apple Silicon | `4cf008a280f8fc27913f8bf814a30ea9c6e04404432000ae3bee2ad7f51ecca2` |
