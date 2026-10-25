# 在 Windows 上打包成单文件 exe（产物：dist/ScreenTranslate.exe）
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "==> 安装 PyInstaller"
python -m pip install --upgrade pip pyinstaller

Write-Host "==> 打包"
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name ScreenTranslate `
  windows/screen_translate.py

Write-Host ""
Write-Host "完成：dist\ScreenTranslate.exe"
