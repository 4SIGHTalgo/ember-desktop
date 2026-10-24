# Ember — live table-tennis signal client

Ember is the free, open-source desktop client for
[getembersignals.com](https://getembersignals.com). It connects your own
DraftKings browser to the Ember signal service and executes authorized,
fail-closed wagers on live table-tennis matches — in your browser, with your
account, under your explicit control.

**Receiving signals requires an active Ember subscription.** The client is
free to download and use; the signal service is a paid product. Without a
subscription the app runs but receives no signals and places no bets.

## Requirements

- Windows 10/11 (x64) or macOS 12+ (Apple Silicon or Intel)
- Google Chrome
- A DraftKings account in a legal market (you keep full control; Ember never
  uploads your sportsbook credentials)
- An Ember account with an active subscription

## Install

### macOS (Homebrew)

```sh
brew install --cask 4sightalgo/ember/ember
```

Or download the disk image directly:

```sh
wget https://github.com/4SIGHTalgo/ember-desktop/releases/latest/download/Ember-aarch64.dmg      # Apple Silicon
wget https://github.com/4SIGHTalgo/ember-desktop/releases/latest/download/Ember-x64.dmg          # Intel
```

### Windows (wget)

```sh
wget https://github.com/4SIGHTalgo/ember-desktop/releases/latest/download/Ember-Setup-x64.exe
```

`curl` (preinstalled on Windows 10/11) works the same:

```sh
curl.exe -LO https://github.com/4SIGHTalgo/ember-desktop/releases/latest/download/Ember-Setup-x64.exe
```

Then run the downloaded installer. Each release also publishes `.sha256`
checksum files: `wget` the matching sidecar and verify with
`sha256sum -c Ember-Setup-x64.exe.sha256`.

## First run

1. Launch Ember and sign in. Your browser opens an Ember authorization page —
   approve it. (Ember uses a device-authorization flow; your password is never
   entered into the app.)
2. Ember opens a dedicated Chrome profile. Sign in to DraftKings there and
   complete any SMS/location checks — exactly as you would normally.
3. Choose your safety settings (maximum bankroll risk percent, conservative
   level, Kelly fraction) and press **Start**.

While monitoring, Ember receives execution plans over an authenticated,
continuously re-verified connection and only ever submits a wager when the
exact authorized selection, price floor, stake, and deadline all still match.
Anything ambiguous — a moved price, an expired authorization, an unclear
balance — aborts the bet instead of guessing.

## Updating

Ember checks for signed updates on its own and installs them after your
approval. Updates are served by the Ember service with staged rollout and
signature verification; the app refuses unsigned updates.

## Privacy and security

- No secrets in this repository. Sign-in uses rotating tokens stored in your
  OS keychain (Windows Credential Manager / macOS Keychain); access tokens
  live only in process memory.
- Sportsbook credentials, cookies, and location data never leave your
  computer.
- Every wager decision is journaled locally and reconciled against the
  service so a crash never silently replays or hides an attempt.
- Found a security issue? Please see [SECURITY.md](SECURITY.md).

## Build from source

Prerequisites: Node 24, Python 3.11, Rust (stable), a Chrome install.

```sh
python desktop/build_worker.py     # PyInstaller sidecar from the pinned engine
npm ci                             # in desktop/
python desktop/build_native.py     # Tauri bundle for your OS
```

The bundled worker is verified before packaging: no private files, token
caches, or browser profiles may ride along (`desktop/verify_bundle.py`).

## License

[MIT](LICENSE). The Ember signal service, signal generation, and server
software are separate products of 4SIGHTalgo and are not part of this
repository.
