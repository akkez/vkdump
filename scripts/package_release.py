import argparse
import platform
import plistlib
import shutil
import sqlite3
import subprocess
import os
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "build" / "dist"


def check_bundle(directory: Path) -> None:
    macos = platform.system() == "Darwin"
    suffix = ".exe" if platform.system() == "Windows" else ""
    binary_dir = directory / ("VKdump.app/Contents/MacOS" if macos else "vkdump")
    cli = binary_dir / ("vkdump-cli" + suffix)
    gui = binary_dir / ("vkdump" + suffix)
    if macos:
        with (directory / "VKdump.app/Contents/Info.plist").open("rb") as file:
            info = plistlib.load(file)
        if info["CFBundleExecutable"] != "vkdump" or info.get("LSBackgroundOnly", False):
            raise RuntimeError("The app bundle must launch the GUI")
    expected = {path.name for path in (ROOT / "migrations").glob("*.sql")}
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        env = os.environ | {"QT_QPA_PLATFORM": "offscreen"}
        usage = subprocess.run([str(cli)], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
        if usage.returncode not in (0, 2) or "download-messages" not in usage.stdout:
            raise RuntimeError("CLI command overview is unavailable")
        for name, command in (("cli", [str(cli), "db", "migrate"]), ("gui", [str(gui), "--smoke-test"])):
            database = cwd / f"{name}.db"
            subprocess.run(command, cwd=cwd, env=env | {"DB_PATH": str(database)}, check=True, timeout=60)
            with closing(sqlite3.connect(database)) as connection:
                actual = {row[0] for row in connection.execute("SELECT name FROM _migrations")}
            if actual != expected:
                raise RuntimeError(f"{name}: bundled migrations differ: {actual ^ expected}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["macos-arm64", "macos-x86_64", "windows-x86_64", "linux-x86_64"], required=True)
    args = parser.parse_args()
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "staging"
        staging.mkdir()
        if args.platform.startswith("macos-"):
            shutil.copytree(DIST / "VKdump.app", staging / "VKdump.app", symlinks=True)
            archive = release / f"vkdump-{args.platform}.zip"
            subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", str(staging), str(archive)], check=True)
        else:
            shutil.copytree(DIST / "vkdump", staging / "vkdump", symlinks=True)
            if args.platform.startswith("windows-"):
                archive = Path(shutil.make_archive(str(release / f"vkdump-{args.platform}"), "zip", staging))
            else:
                archive = release / f"vkdump-{args.platform}.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    tar.add(staging / "vkdump", arcname="vkdump")
        extracted = Path(tmp) / "extracted"
        if args.platform.startswith("macos-"):
            subprocess.run(["ditto", "-x", "-k", str(archive), str(extracted)], check=True)
        else:
            shutil.unpack_archive(archive, extracted)
        check_bundle(extracted)
        print(archive)


if __name__ == "__main__":
    main()
