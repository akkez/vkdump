"""Build the vkdump GUI and CLI into standalone bundles for the host OS.

Usage:
    python build.py                       # build both targets, clean first
    python build.py --target gui          # build only the GUI bundle
    python build.py --target cli          # build only the CLI binary
    python build.py --no-clean            # incremental (faster, may reuse stale artifacts)

Output goes to build/dist/. PyInstaller cannot cross-compile, so a Windows
.exe must be built on Windows; a macOS .app on macOS; etc.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BUILD_DIR = ROOT / "build"
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


def _binary_path(target: str) -> Path:
    """Best-effort path to the produced binary for reporting."""
    name = "vkdump" if target == "gui" else "vkdump-cli"
    if platform.system() == "Windows":
        return DIST_DIR / "vkdump" / f"{name}.exe"
    if platform.system() == "Darwin":
        app = DIST_DIR / "VKdump.app"
        if app.is_dir():
            return app / "Contents" / "MacOS" / name
    return DIST_DIR / "vkdump" / name


def _build(target: str) -> None:
    spec = ROOT / "vkdump.spec"
    os.environ["VKDUMP_BUILD_TARGET"] = target
    _run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(spec),
            "--distpath",
            str(DIST_DIR),
            "--workpath",
            str(WORK_DIR),
            "--noconfirm",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        choices=["gui", "cli", "both"],
        default="both",
        help="Which bundle to build (default: both)",
    )
    parser.add_argument("--no-clean", action="store_true", help="Skip wiping build/")
    args = parser.parse_args()

    _ensure_pyinstaller()

    if not args.no_clean:
        _clean()
    BUILD_DIR.mkdir(exist_ok=True)

    targets = ["gui", "cli"] if args.target == "both" else [args.target]
    _build(args.target)
    ok = True
    for t in targets:
        binary = _binary_path(t)
        print(f"  -> {binary}")
        if not binary.exists():
            ok = False
            print(f"WARNING: {t} binary not found at expected path.", file=sys.stderr)

    print()
    print(f"Built for {platform.system()} {platform.machine()}")
    print(f"Dist dir: {DIST_DIR}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
