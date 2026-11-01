# PyInstaller spec - one-folder build (LGPL libraries stay replaceable, docs/ARCHITECTURE.md §12).
# Build with: python packaging/build.py
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hidden = collect_submodules("can.interfaces") + collect_submodules("flashtool")
datas = collect_data_files("flashtool", includes=["templates/*.yaml"]) + collect_data_files("odxtools")

a = Analysis(
    ["entry.py"],
    pathex=[".."],
    hiddenimports=hidden,
    datas=datas,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtPdf"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="flashtool", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="flashtool")
