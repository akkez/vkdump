# PyInstaller spec for the vkdump GUI. Run via: python build.py
import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821 — injected by PyInstaller

a = Analysis(
    [str(SPEC_DIR / "gui_main.py")],
    pathex=[str(SPEC_DIR / "src")],
    binaries=[],
    datas=[(str(SPEC_DIR / "migrations"), "migrations")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="vkdump-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="vkdump-gui",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="vkdump-gui.app",
        icon=None,
        bundle_identifier="com.vkdump.gui",
    )
