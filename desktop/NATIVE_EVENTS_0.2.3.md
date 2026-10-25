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
