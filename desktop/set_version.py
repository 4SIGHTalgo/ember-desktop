"""Advance all release version declarations together before a native rebuild."""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
version = sys.argv[1]
if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
    raise SystemExit("Use a semantic version such as 0.1.1.")
for path in (ROOT/"package.json", ROOT/"src-tauri/tauri.conf.json"):
    value = json.loads(path.read_text()); value["version"] = version
    path.write_text(json.dumps(value, indent=2)+"\n")
lock = ROOT/"package-lock.json"
if lock.exists():
    value = json.loads(lock.read_text())
    value["version"] = version
    value["packages"][""]["version"] = version
    lock.write_text(json.dumps(value, indent=2)+"\n")
cargo = ROOT/"src-tauri/Cargo.toml"
cargo.write_text(re.sub(r'^version = "[^"]+"', 'version = "'+version+'"', cargo.read_text(), count=1, flags=re.M))
cargo_lock = ROOT/"src-tauri/Cargo.lock"
if cargo_lock.exists():
    cargo_lock.write_text(re.sub(r'(\[\[package\]\]\nname = "ember-desktop"\nversion = ")[^"]+("\n)',
                                lambda match: match[1]+version+match[2], cargo_lock.read_text(), count=1))
for protocol in (ROOT.parent/"ember_protocol.py", ROOT/"engine/ember_protocol.py"):
    if protocol.exists():
        protocol.write_text(re.sub(r'^VERSION = "[^"]+"', 'VERSION = "'+version+'"', protocol.read_text(), count=1, flags=re.M))
print("Release version declarations aligned:", version)
