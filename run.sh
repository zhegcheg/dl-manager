#!/bin/bash
# DL Manager 启动脚本

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# 设置 NAS 媒体目录（默认 /mnt/fn-nas-imovie）
export NAS_MEDIA_DIR="${NAS_MEDIA_DIR:-/mnt/fn-nas-imovie}"

# 安装依赖
pip3 install -q -r requirements.txt

# 启动服务
python3 server.py