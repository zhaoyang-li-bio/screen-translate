#!/usr/bin/env bash
# 在 Ubuntu / Debian 上打包成单文件可执行程序（产物：dist/ScreenTranslate）
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1; then
  echo "缺少 python3，请先执行：sudo apt install python3 python3-pip python3-tk" >&2
  exit 1
fi

echo "==> 检查系统依赖"
MISSING=""
python3 -c "import tkinter" 2>/dev/null || MISSING="$MISSING python3-tk"
command -v xclip >/dev/null 2>&1 || MISSING="$MISSING xclip"
if [ -n "$MISSING" ]; then
  echo "缺少：$MISSING"
  echo "请执行：sudo apt install$MISSING"
  exit 1
fi

echo "==> 安装 PyInstaller"
python3 -m pip install --upgrade pip pyinstaller

echo "==> 打包"
python3 -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name ScreenTranslate \
  linux/screen_translate_linux.py

chmod +x dist/ScreenTranslate
echo
echo "完成：dist/ScreenTranslate"
echo "直接运行：./dist/ScreenTranslate"
