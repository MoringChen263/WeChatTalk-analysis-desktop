# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

datas = [('skills', 'skills')]
hiddenimports = ['onnxruntime', 'cv2', 'windows_capture', 'sqlite3']
datas += collect_data_files('qfluentwidgets')
datas += collect_data_files('rapidocr_onnxruntime')
hiddenimports += collect_submodules('app')
hiddenimports += collect_submodules('rapidocr_onnxruntime')


a = Analysis(
    ['app/main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'transformers', 'laya', 'matplotlib', 'pandas', 'tkinter', 'numpy.f2py', 'scipy', 'tensorflow', 'qfluentwidgets.multimedia', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='jev-chat-analyzer',
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
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='jev-chat-analyzer',
)
