# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [
    ('app.ico', '.'),
    ('nav_parse.png', '.'),
    ('nav_parse_on.png', '.'),
    ('nav_import.png', '.'),
    ('nav_import_on.png', '.'),
    ('nav_convert.png', '.'),
    ('nav_convert_on.png', '.'),
    ('nav_monitor.png', '.'),
    ('nav_monitor_on.png', '.'),
]
binaries = [('PCAN-Basic\\x64\\PCANBasic.dll', '.')]
hiddenimports = ['live_monitor', 'pcan_config', 'uds', 'can.interfaces.pcan', 'can.interfaces.virtual', 'tkinterdnd2']
# tkdnd 的 tcl 脚本与 dll 必须保持原目录结构带进包，否则运行期 require 失败
datas += collect_data_files('tkinterdnd2')
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['dbc_parser.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='BusLens',
    icon='app.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
