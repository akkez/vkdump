"""Build the vkdump GUI into a standalone bundle for the host OS.

Usage:
    python build.py                # full clean build
    python build.py --no-clean     # incremental (faster, may reuse stale artifacts)

Output goes to build/dist/vkdump-gui/. PyInstaller cannot cross-compile, so a
Windows .exe must be built on Windows; a macOS .app on macOS; etc.
"""
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "vkdump-gui.spec"
BUILD_DIR = ROOT.parent / "build"
DIST_DIR = BUILD_DIR / "dist"
WORK_DIR = BUILD_DIR / "work"


def _run(cmd: list[str]) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, cwd=ROOT)


def _ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit(
            "PyInstaller is not installed. Run:\n"
            "  .venv/bin/pip install -e '.[dev]'\n"
            "or:\n"
            "  .venv/bin/pip install pyinstaller"
        )


def _clean() -> None:
    for d in (DIST_DIR, WORK_DIR):
        if d.exists():
            print(f"removing {d}", flush=True)
            shutil.rmtree(d)


def _binary_path() -> Path:
    name = "vkdump-gui"
    base = DIST_DIR / name
    if platform.system() == "Windows":
        return base / f"{name}.exe"
    if platform.system() == "Darwin":
        app = DIST_DIR / f"{name}.app"
        if app.exists():
            return app / "Contents" / "MacOS" / name
    return base / name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-clean", action="store_true", help="Skip wiping build/")
    args = parser.parse_args()

    _ensure_pyinstaller()

    if not args.no_clean:
        _clean()

    BUILD_DIR.mkdir(exist_ok=True)
    _run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(SPEC),
            "--distpath",
            str(DIST_DIR),
            "--workpath",
            str(WORK_DIR),
            "--noconfirm",
        ]
    )

    binary = _binary_path()
    print()
    print(f"Built for {platform.system()} {platform.machine()}")
    print(f"Binary: {binary}")
    if not binary.exists():
        print("WARNING: expected binary not found at the path above.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
