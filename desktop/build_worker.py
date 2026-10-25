"""Build the Python sidecar on its native OS, without private files or sessions."""
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import tomllib
import runpy

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
ENGINE = DESKTOP/"engine" if (DESKTOP/"engine/ember_client.py").exists() else ROOT
machine = platform.machine().lower()
system = platform.system()
if system == "Windows" and machine in {"amd64", "x86_64"}:
    target, suffix = "x86_64-pc-windows-msvc", ".exe"
elif system == "Darwin" and machine in {"arm64", "aarch64", "x86_64"}:
    target, suffix = ("aarch64" if machine != "x86_64" else "x86_64") + "-apple-darwin", ""
else:
    raise SystemExit("Build this sidecar on a supported Windows/macOS architecture.")
sys.path.insert(0, str(ROOT))
from ember_protocol import VERSION
versions = [json.loads((DESKTOP / path).read_text())["version"] for path in ("package.json", "src-tauri/tauri.conf.json")]
versions.append(tomllib.loads((DESKTOP/"src-tauri/Cargo.toml").read_text())["package"]["version"])
if any(version != VERSION for version in versions):
    raise SystemExit("Worker and desktop versions differ; align release versions before building.")
if runpy.run_path(str(ENGINE/"ember_protocol.py"))["VERSION"] != VERSION:
    raise SystemExit("The pinned engine version differs; refresh the engine snapshot before building.")
if ENGINE != ROOT and (ENGINE/"ember_client.py").read_bytes().replace(b"\r\n", b"\n") != (ROOT/"ember_client.py").read_bytes().replace(b"\r\n", b"\n"):
    raise SystemExit("The pinned client is stale; run desktop/snapshot_engine.py before building.")
binary_dir = DESKTOP / "src-tauri/binaries"
binary_dir.mkdir(parents=True, exist_ok=True)
arguments = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
             "--name", "ember-worker-" + target, "--distpath", str(binary_dir),
             "--workpath", str(DESKTOP/"build/worker"), "--specpath", str(DESKTOP/"build"),
             "--paths", str(ENGINE),               "--collect-all", "selenium", "--collect-all", "keyring",
              "--hidden-import", "tt_market_semantics",
              "--hidden-import", "draftkings_login",
             "--add-data", str(ENGINE/"reveal_next_set.js") + ":.",
             "--add-data", str(ENGINE/"start_draftkings.ps1") + ":.",
             str(ENGINE/"ember_client.py")]
# Keep the checkout's research modules out of PyInstaller's import precedence.
# The entry point and every browser dependency must resolve from this snapshot.
subprocess.run(arguments, check=True, cwd=ENGINE, env=dict(os.environ, PYTHONPATH=str(ENGINE)))
print("Native worker built:", binary_dir / ("ember-worker-" + target + suffix))
