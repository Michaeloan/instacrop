$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$taskPython = Join-Path $PSScriptRoot '.runtime\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    Write-Host '首次使用，正在创建项目独立环境……'
    python -m venv .runtime
    if ($LASTEXITCODE -ne 0) { throw '无法创建 Python 环境，请确认已安装 Python 3.10 或更新版本。' }
}
& $taskPython -c 'import cv2, numpy, PIL, webview' 2>$null
if ($LASTEXITCODE -ne 0) {
    & $taskPython -m pip install -r requirements-desktop.txt
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络后重试。' }
}
& $taskPython desktop.py
