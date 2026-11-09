"""Validated browser preferences and discoverable managed profiles."""

import os
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode

BROWSERS = {"chromium": "Bundled Chromium", "chrome": "Google Chrome", "msedge": "Microsoft Edge"}
RESERVED_PROFILES = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}
EXTENSION_ID = "mnldpjghemdjnoefihnifnpfhlaihamk"


def browser_settings(value):
    browser, profile = value.get("browser", "chromium"), value.get("browserProfile", "")
    if browser not in BROWSERS:
        raise ValueError("browser must be chromium, chrome, or msedge")
    if not isinstance(profile, str) or (profile and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", profile) or profile.upper() in RESERVED_PROFILES)):
        raise ValueError("Browser profile must contain at most 64 letters, numbers, underscores or hyphens and cannot use a Windows device name")
    if profile.startswith("local-") and (value.get("browserMode") != "attached" or browser == "chromium"):
        raise ValueError("An installed browser profile requires Existing signed-in browser mode and Chrome or Edge")
    return {"browser": browser, "browserProfile": profile}


def browser_executable(browser):
    if browser not in {"chrome", "msedge"}:
        return None
    vendor = "Google/Chrome/Application" if browser == "chrome" else "Microsoft/Edge/Application"
    filename = "chrome.exe" if browser == "chrome" else "msedge.exe"
    for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        root = os.environ.get(variable)
        if root and (candidate := Path(root) / vendor / filename).is_file():
            return candidate
    return None


def extension_directory():
    source = Path(__file__).resolve().parents[1]
    return (source if (source / "browser").is_dir() else source.parent) / "browser/extension"


def installed_profiles(browser):
    local = os.environ.get("LOCALAPPDATA")
    if not local or browser not in {"chrome", "msedge"}:
        return []
    root = Path(local) / ("Google/Chrome/User Data" if browser == "chrome" else "Microsoft/Edge/User Data")
    try:
        state = json.loads((root / "Local State").read_text(encoding="utf-8"))
        cache = state.get("profile", {}).get("info_cache", {})
        if not isinstance(cache, dict):
            return []
    except (OSError, ValueError, AttributeError):
        return []
    profiles = []
    for directory, metadata in cache.items():
        if not isinstance(metadata, dict) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,100}", directory):
            continue
        profile = root / directory
        if not profile.is_dir() or profile.resolve().parent != root.resolve():
            continue
        ident = "local-" + hashlib.sha256(f"{browser}\0{directory}".encode()).hexdigest()[:24]
        installed = False
        for filename in ("Preferences", "Secure Preferences"):
            try:
                preferences = json.loads((profile / filename).read_text(encoding="utf-8"))
                entry = preferences.get("extensions", {}).get("settings", {}).get(EXTENSION_ID, {})
                installed = installed or bool(entry) and entry.get("state", 1) == 1
            except (OSError, ValueError, AttributeError):
                pass
        profiles.append({"id": ident, "directory": directory,
                         "name": str(metadata.get("shortcut_name") or metadata.get("name") or directory)[:120],
                         "account": str(metadata.get("user_name") or "")[:254], "extensionInstalled": installed})
    return sorted(profiles, key=lambda item: (item["directory"] != "Default", item["directory"]))


def open_installed_profile(browser, profile_id):
    profile = next((item for item in installed_profiles(browser) if item["id"] == profile_id), None)
    executable = browser_executable(browser)
    if not profile or not executable:
        raise ValueError("The selected installed browser profile is no longer available")
    if profile["extensionInstalled"]:
        label = profile["name"] + (" · " + profile["account"] if profile["account"] else "")
        fragment = urlencode({"localProfileId": profile_id, "profileName": label})
        url = f"chrome-extension://{EXTENSION_ID}/options.html#{fragment}"
    else:
        url = "edge://extensions/" if browser == "msedge" else "chrome://extensions/"
    subprocess.Popen([str(executable), "--profile-directory=" + profile["directory"], url],
                     cwd=executable.parent, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return {"profileId": profile_id, "setupRequired": not profile["extensionInstalled"],
            "extensionDirectory": str(extension_directory())}


def setup_installed_profile(browser, profile_id):
    profile = next((item for item in installed_profiles(browser) if item["id"] == profile_id), None)
    executable = browser_executable(browser)
    if not profile or not executable:
        raise ValueError("The selected installed browser profile is no longer available")
    extension = extension_directory()
    helper = extension.parent.parent / "native/browser-extension-setup.exe"
    if not helper.is_file():
        raise ValueError("Automatic browser setup is unavailable in this installation")
    label = profile["name"] + (" · " + profile["account"] if profile["account"] else "")
    try:
        result = subprocess.run([str(helper), str(executable), profile["directory"], str(extension), profile_id, label],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=90,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        # The browser may inherit this pipe and print its own launch message.
        # Our helper writes one structured report as its final nonempty line.
        line = next((line for line in reversed(result.stdout[-65536:].splitlines()) if line.strip()), "")
        report = json.loads(line)
        if not isinstance(report, dict) or not (
                isinstance(report.get("error"), str) or
                all(isinstance(report.get(key), bool) for key in ("extensionLoaded", "paired"))):
            raise ValueError("Invalid browser setup report")
    except subprocess.TimeoutExpired:
        raise ValueError("Browser setup timed out. Close its setup window and retry when the browser is ready.") from None
    except (OSError, ValueError):
        raise ValueError("The browser setup helper could not finish. Keep its setup window visible and retry.") from None
    if result.returncode or report.get("error"):
        raise ValueError(report.get("error") or "The browser setup helper failed")
    return {"profileId": profile_id, **report}


def browser_catalog(home, bridge):
    data = []
    for browser, name in BROWSERS.items():
        profiles = home / "browser-profiles" / browser
        installed = browser == "chromium" or browser_executable(browser) is not None
        existing = installed_profiles(browser)
        for profile in existing:
            profile["connected"] = any(item.get("browser") == browser and item.get("localProfileId") == profile["id"] for item in bridge.catalog())
        data.append({"id": browser, "name": name, "installed": installed,
                     "installedProfiles": existing,
                     "profiles": sorted(path.name for path in profiles.iterdir() if path.is_dir()) if profiles.is_dir() else []})
    return {"data": data, "connections": bridge.catalog()}
