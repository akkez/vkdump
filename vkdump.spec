import os
import sys
from pathlib import Path

from vkdump import __version__

root = Path(SPECPATH).resolve()
target = os.environ.get("VKDUMP_BUILD_TARGET", "both")
executables = []
binaries = []
datas = []

for kind in ("gui", "cli"):
    if target not in (kind, "both"):
        continue
    gui = kind == "gui"
    name = "vkdump" if gui else "vkdump-cli"
    data = [(str(root / "migrations"), "migrations"),
            (str(root / "src/vkdump/modules/save_chat/templates"), "vkdump/modules/save_chat/templates")]
    if gui:
        data.append((str(root / "assets"), "assets"))
    analysis = Analysis(
        [str(root / f"{kind}_main.py")],
        pathex=[str(root / "src")],
        binaries=[],
        datas=data,
        hiddenimports=[],
        hookspath=[],
        runtime_hooks=[],
        excludes=[] if gui else ["PySide6", "shiboken6"],
        noarchive=False,
    )
    pyz = PYZ(analysis.pure)
    icon = root / "assets" / ("icon.ico" if sys.platform == "win32" else "icon.icns")
    exe = EXE(
        pyz,
        analysis.scripts,
        [],
        exclude_binaries=True,
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=not gui,
        disable_windowed_traceback=False,
        icon=str(icon) if gui else None,
    )
    executables.append(exe)
    binaries.extend(analysis.binaries)
    datas.extend(analysis.datas)

coll = COLLECT(*executables, binaries, datas, name="vkdump", strip=False, upx=False)

if sys.platform == "darwin" and target in ("gui", "both"):
    app = BUNDLE(
        coll,
        name="VKdump.app",
        icon=str(root / "assets" / "icon.icns"),
        bundle_identifier="ru.akkez.vkdump",
        version=__version__,
        info_plist={
            "CFBundleDisplayName": "VKdump",
            "CFBundleExecutable": "vkdump",
            "LSBackgroundOnly": False,
            "NSHighResolutionCapable": True,
        },
    )
