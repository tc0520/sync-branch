#!/bin/bash
# 一键构建分发包：编译 Mac 壳 -> 刷新 python 拷贝 -> 签名 -> 打 zip
# 用法: ./scripts/build.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)

APP="dist/分支同步面板.app"

# 1. 重新编译 Mac 原生壳（arm64 + x86_64 通用二进制）
if command -v swiftc >/dev/null 2>&1; then
    echo "编译 Swift 壳..."
    swiftc -O -target arm64-apple-macos12.0  macos/main.swift -o /tmp/sb_arm64
    swiftc -O -target x86_64-apple-macos12.0 macos/main.swift -o /tmp/sb_x86_64
    lipo -create -output "$APP/Contents/MacOS/launcher" /tmp/sb_arm64 /tmp/sb_x86_64
    rm -f /tmp/sb_arm64 /tmp/sb_x86_64
    echo "  ✓ 已更新 $APP/Contents/MacOS/launcher"
else
    echo "  ! 未找到 swiftc，跳过壳编译（沿用现有二进制）"
fi

# 2. 把核心源码刷新到两份分发拷贝（永远只改根目录的 sync-branches-ui.py）
cp sync-branches-ui.py "$APP/Contents/Resources/"
cp sync-branches-ui.py dist/windows/
echo "  ✓ 已刷新 .app 与 windows/ 内的 sync-branches-ui.py"

# 3. ad-hoc 签名（Apple Silicon 必须）
codesign --force --deep -s - "$APP"
echo "  ✓ 已签名"

# 4. 打发给同事的 zip（app + windows + 用户说明）
STAGE=/tmp/sb_pkg/分支同步面板
rm -rf /tmp/sb_pkg && mkdir -p "$STAGE"
cp -R "$APP" dist/windows dist/README.md "$STAGE/"
rm -f dist/分支同步面板.zip
ditto -c -k --keepParent "$STAGE" dist/分支同步面板.zip
rm -rf /tmp/sb_pkg
echo "  ✓ 已生成 dist/分支同步面板.zip ($(du -h dist/分支同步面板.zip | cut -f1 | tr -d ' '))"

# 5. Windows 专用包：全英文路径，避免部分 Windows 解压工具显示中文文件名乱码
WIN_STAGE=/tmp/sb_win_pkg/branch-sync-panel-windows
rm -rf /tmp/sb_win_pkg && mkdir -p "$WIN_STAGE"
cp dist/windows/sync-branches-ui.py "$WIN_STAGE/"
cp dist/windows/分支同步面板.bat "$WIN_STAGE/start.bat"
cp dist/README.md "$WIN_STAGE/README.md"
rm -f dist/branch-sync-panel-windows.zip
(cd /tmp/sb_win_pkg && COPYFILE_DISABLE=1 zip -rq "$ROOT/dist/branch-sync-panel-windows.zip" branch-sync-panel-windows)
rm -rf /tmp/sb_win_pkg
echo "  ✓ 已生成 dist/branch-sync-panel-windows.zip ($(du -h dist/branch-sync-panel-windows.zip | cut -f1 | tr -d ' '))"
echo "完成。"
