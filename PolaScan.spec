# Desktop portable build. Public program assets only; no user photo is bundled.
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs
from pathlib import Path

datas = [('web', 'web')]
datas += [('tools/haarcascades', 'tools/haarcascades')]
datas += collect_data_files('webview')
datas += collect_data_files('pillow_heif')
a = Analysis(['desktop.py'], pathex=[], binaries=[], datas=datas,
             hiddenimports=['webview.platforms.edgechromium','webview.platforms.winforms','pillow_heif'],
             hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['tkinter','matplotlib','pandas','PyQt5','PyQt6','PySide2','PySide6','pytest'],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz,a.scripts,[],exclude_binaries=True,name='InstaCrop',debug=False,
          bootloader_ignore_signals=False,strip=False,upx=False,console=False,
          disable_windowed_traceback=False,icon='assets/polascan.ico',version='assets/version.txt')
coll = COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='InstaCrop')
