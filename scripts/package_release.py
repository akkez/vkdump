import argparse
import plistlib
import shutil
import sqlite3
import subprocess
import os
import re
import tarfile
import tempfile
import tomllib
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "build" / "dist"


def check_bundle(directory: Path, platform_name: str) -> None:
    macos = platform_name.startswith('macos-')
    suffix = ".exe" if platform_name.startswith('windows-') else ""
    binary_dir = directory / "VKdump.app/Contents/MacOS" if macos else directory
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
        for name, command in (("cli", [str(cli), "db", "migrate"]), ("gui", [str(gui), "--smoke-test"])):
            database = cwd / f"{name}.db"
            run_env = env | {"DB_PATH": str(database)}
            if name == "cli":
                usage = subprocess.run([str(cli)], cwd=cwd, env=run_env, capture_output=True, text=True, timeout=60)
                if usage.returncode not in (0, 2) or "download-messages" not in usage.stdout:
                    raise RuntimeError("CLI command overview is unavailable")
            subprocess.run(command, cwd=cwd, env=run_env, check=True, timeout=60)
            with closing(sqlite3.connect(database)) as connection:
                actual = {row[0] for row in connection.execute("SELECT name FROM _migrations")}
            if actual != expected:
                raise RuntimeError(f"{name}: bundled migrations differ: {actual ^ expected}")


def version_tag(raw: str = '') -> str:
    if not raw:
        raw = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    tag = raw if raw.startswith('v') else f'v{raw}'
    if not re.fullmatch(r'v\d+(?:\.\d+){1,2}(?:[-+][A-Za-z0-9.-]+)?', tag):
        raise ValueError('Expected a version tag such as v0.3')
    return tag


def package_bundle(platform_name: str, tag: str, dist: Path = DIST, release: Path = ROOT / 'release') -> Path:
    folder_name = f'vkdump-{version_tag(tag)}'
    release.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "staging"
        staging.mkdir()
        bundle = staging / folder_name
        if platform_name.startswith("macos-"):
            shutil.copytree(dist / "VKdump.app", bundle / "VKdump.app", symlinks=True)
            archive = release / f"vkdump-{platform_name}.zip"
            subprocess.run(["ditto", "-c", "-k", "--norsrc", "--keepParent", str(bundle), str(archive)], check=True)
        else:
            shutil.copytree(dist / "vkdump", bundle, symlinks=True)
            if platform_name.startswith("windows-"):
                archive = Path(shutil.make_archive(str(release / f"vkdump-{platform_name}"), "zip", staging))
            else:
                archive = release / f"vkdump-{platform_name}.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    tar.add(bundle, arcname=folder_name)
        extracted = Path(tmp) / "extracted"
        if platform_name.startswith("macos-"):
            subprocess.run(["ditto", "-x", "-k", str(archive), str(extracted)], check=True)
        else:
            shutil.unpack_archive(archive, extracted)
        check_bundle(extracted / folder_name, platform_name)
        return archive


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["macos-arm64", "macos-x86_64", "windows-x86_64", "linux-x86_64"], required=True)
    parser.add_argument('--version', default=os.environ.get('RELEASE_TAG', ''), help='Release tag; defaults to the project version for build-only runs.')
    args = parser.parse_args()
    print(package_bundle(args.platform, args.version))


if __name__ == "__main__":
    main()
