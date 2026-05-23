#!/bin/bash
# JableDL Server 启动脚本

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# 安装依赖
pip3 install -q -r requirements.txt

# 确保 N_m3u8DL-RE 可执行
chmod +x /tmp/N_m3u8DL-RE 2>/dev/null

# 启动服务
python3 server.py