$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$taskPython = Join-Path $PSScriptRoot '.runtime\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { $taskPython = 'python' }
& $taskPython -m pip install -r requirements-desktop.txt
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
& $taskPython -m PyInstaller --noconfirm --distpath release/v1.0.0 --workpath build/v1.0.0 PolaScan.spec
if ($LASTEXITCODE -ne 0) { throw '打包失败' }
& $taskPython package_release.py
if ($LASTEXITCODE -ne 0) { throw '压缩包生成失败' }
