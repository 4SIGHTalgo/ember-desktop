"""Attach only to the explicitly retained, locally owned Windows validation browser."""
import json
import os
from pathlib import Path
import subprocess


def validation_ticket(data_dir):
    value = os.environ.get("EMBER_DEVICE_BROWSER_TICKET")
    if not value:
        return None
    if os.name != "nt":
        raise RuntimeError("Retained Windows browser tickets cannot be used on another platform")
    ticket = json.loads(Path(value).read_text())
    expected = (Path(data_dir)/"validation-browser").resolve()
    if Path(ticket.get("profile", "")).resolve() != expected:
        raise RuntimeError("The retained ticket is not for the dedicated validation profile")
    if type(ticket.get("port")) is not int or not 1024 <= ticket["port"] <= 65535:
        raise RuntimeError("Invalid validation browser port")
    return ticket


def attach_validation_browser(ticket):
    import draftkings_login as login
    from selenium.webdriver.chrome.options import Options
    from driver_lifecycle import create_owned_chrome
    launcher = Path(login.__file__).with_name("start_draftkings.ps1")
    # The existing launcher proves the listening Chrome process uses this exact
    # dedicated profile. It refuses another process, personal profile, or public bind.
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(launcher),
                             "-BrowserOnly", "-DeferNavigation", "-ProfileDirectory", ticket["profile"],
                             "-Port", str(ticket["port"])], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Retained browser ownership validation failed")
    options = Options()
    options.page_load_strategy = "none"
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{ticket['port']}")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    driver = create_owned_chrome(options, preserve_external_browser=True)
    login.allow_location_permissions(driver)
    return driver
