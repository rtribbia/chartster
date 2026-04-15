# PyInstaller spec for Chartster.
# Build with: pyinstaller Chartster.spec
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

excludes = [
    # Qt modules chartster doesn't use; pyside6-essentials already drops most,
    # but these sneak in via hidden imports otherwise.
    "PySide6.QtNetwork",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtXml",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPrintSupport",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    # Stdlib modules we don't use
    "tkinter",
    "unittest",
    "test",
    "pydoc",
    "pdb",
    "email",
    "http",
    "xmlrpc",
]

a = Analysis(
    ["chartster/gui.py"],
    pathex=[],
    binaries=[],
    datas=[("chartster/assets", "chartster/assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Chartster",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Chartster",
)

app = BUNDLE(
    coll,
    name="Chartster.app",
    icon=None,
    bundle_identifier="net.chartster.app",
)
