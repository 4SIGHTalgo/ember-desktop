"""Run the native bundler without accidentally treating empty Apple secrets as a P12."""
import argparse
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("--target", required=True)
parser.add_argument("--bundles", required=True)
args = parser.parse_args()
environment = dict(os.environ)
for key in ("APPLE_CERTIFICATE", "APPLE_CERTIFICATE_PASSWORD", "APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID"):
    if not environment.get(key):
        environment.pop(key, None)
production = environment.get("EMBER_PRODUCTION_SIGNING", "false").lower() == "true"
if args.target.endswith("apple-darwin"):
    if production and not all(environment.get(key) for key in ("APPLE_CERTIFICATE", "APPLE_CERTIFICATE_PASSWORD", "APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID")):
        raise SystemExit("Publisher-signed Mac releases require the configured Apple certificate and notarization credentials.")
    if not environment.get("APPLE_CERTIFICATE"):
        environment["APPLE_SIGNING_IDENTITY"] = "-"
elif "windows" in args.target:
    environment.pop("APPLE_SIGNING_IDENTITY", None)
    if production:
        signing = json.loads((ROOT/"src-tauri/tauri.conf.json").read_text()).get("bundle", {}).get("windows", {})
        if not signing.get("certificateThumbprint") and not signing.get("signCommand"):
            raise SystemExit("Configure Windows publisher signing before requesting a production-signed build.")
executable = ROOT/("node_modules/.bin/tauri.cmd" if os.name == "nt" else "node_modules/.bin/tauri")
subprocess.run([str(executable), "build", "--target", args.target, "--bundles", args.bundles], env=environment, cwd=ROOT, check=True)
