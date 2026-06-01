"""Open a URL in the user's default browser, preserving the fragment.

macOS LaunchServices (and everything that funnels through it —
``webbrowser.open``, ``open <url>``, ``open -a Firefox <url>``,
``osascript "open location ..."``) silently strips the ``#fragment``
from ``file://`` URLs before handing them to the browser. The only
reliable workaround is to invoke the browser binary directly so the
URL passes byte-for-byte.

This module:

1. resolves the default browser bundle id from LaunchServices,
2. locates the corresponding .app via Spotlight,
3. reads the bundle's Info.plist to find the actual executable,
4. spawns it with the URL as a CLI argument.

When *anything* in that chain fails (or we're not on macOS) it
falls back to ``webbrowser.open``, which still opens the page — just
without the anchor. Strictly better than no-op.
"""
from __future__ import annotations

import logging
import plistlib
import subprocess
import sys
import webbrowser
from pathlib import Path

logger = logging.getLogger(__name__)


def _macos_default_browser_binary() -> Path | None:
    """Return the path to the executable inside the default-browser
    .app bundle, or None when detection fails at any step.
    """
    plist = (
        Path.home()
        / "Library/Preferences/com.apple.LaunchServices"
        / "com.apple.launchservices.secure.plist"
    )
    try:
        with open(plist, "rb") as f:
            data = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return None
    bundle_id: str | None = None
    for h in data.get("LSHandlers", []):
        if h.get("LSHandlerURLScheme") == "https":
            bundle_id = h.get("LSHandlerRoleAll") or h.get("LSHandlerRoleViewer")
            break
    if not bundle_id or bundle_id == "-":
        return None
    try:
        result = subprocess.run(
            ["mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'"],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    for line in result.stdout.splitlines():
        app = Path(line.strip())
        if not app.is_dir():
            continue
        info_plist = app / "Contents/Info.plist"
        try:
            with open(info_plist, "rb") as f:
                info = plistlib.load(f)
        except (OSError, plistlib.InvalidFileException):
            continue
        exe = info.get("CFBundleExecutable")
        if not exe:
            continue
        bin_path = app / "Contents/MacOS" / exe
        if bin_path.is_file():
            return bin_path
    return None


def open_url(uri: str, *, want_fragment: bool = False) -> None:
    """Open ``uri`` in the user's default browser.

    When ``want_fragment`` is True and we're on macOS, route through
    the browser binary directly so the ``#fragment`` survives. On
    every other platform — and on macOS when binary detection fails
    — fall back to ``webbrowser.open``.
    """
    if want_fragment and sys.platform == "darwin":
        bin_path = _macos_default_browser_binary()
        if bin_path is not None:
            try:
                subprocess.Popen(
                    [str(bin_path), uri],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return
            except OSError:
                logger.warning("direct-browser launch failed for %s", bin_path)
    webbrowser.open(uri)
