# PyInstaller spec for the vkdump CLI. Run via: python build.py
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821 — injected by PyInstaller

a = Analysis(
    [str(SPEC_DIR / "cli_main.py")],
    pathex=[str(SPEC_DIR / "src")],
    binaries=[],
    datas=[(str(SPEC_DIR / "migrations"), "migrations")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["PySide6", "shiboken6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="vkdump",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=None,
)
