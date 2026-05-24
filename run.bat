@echo off
chcp 65001 >nul
:: DL Manager 启动脚本 (Windows)

cd /d "%~dp0"

:: 设置 NAS 媒体目录（默认 D:\nas，可修改为你实际的路径）
if not defined NAS_MEDIA_DIR (
    set "NAS_MEDIA_DIR=D:\nas"
)

echo NAS_MEDIA_DIR=%NAS_MEDIA_DIR%

:: 安装依赖
pip install -q -r requirements.txt

:: 启动服务
python server.py
