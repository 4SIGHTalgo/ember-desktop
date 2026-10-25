"""Inspect packaged resources without opening a sportsbook browser or credentials."""
from pathlib import Path
import platform
from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parent
system, machine = platform.system(), platform.machine().lower()
if system == "Windows":
    name = "ember-worker-x86_64-pc-windows-msvc.exe"
elif system == "Darwin":
    name = "ember-worker-"+("aarch64" if machine in {"arm64", "aarch64"} else "x86_64")+"-apple-darwin"
else:
    raise SystemExit("Verify the native sidecar on its Windows/macOS build host.")
binary = ROOT/"src-tauri/binaries"/name
archive = CArchiveReader(str(binary))
names = set(archive.toc)
if not {"reveal_next_set.js", "start_draftkings.ps1"} <= names:
    raise SystemExit("Required browser resources are missing from the worker bundle.")
modules = set(archive.open_embedded_archive("PYZ.pyz").toc)
if not {"execution_policy", "conservative_policy", "tt_market_semantics", "tt_market_exec", "wager_sizing", "wager_submission", "ember_protocol", "ember_opportunity", "ember_history", "ember_market_readiness", "ember_browser_lifecycle", "psutil", "ember_provider_login", "ember_browser_preparation"} <= modules:
    raise SystemExit("The worker is missing the updated submission or sizing modules.")
for name in names:
    if any(part in name.replace('\\', '/').split('/') for part in (".env", ".chrome-profile", ".ttt_token.json", "private")):
        raise SystemExit("A private resource was included in the worker bundle.")
print("Worker resources and updated submission modules verified; no private environment, token cache, or browser profile is bundled.")
