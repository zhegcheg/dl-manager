#!/bin/bash
# DL Manager 启动脚本

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# 安装依赖
pip3 install -q -r requirements.txt

# 启动服务
python3 server.py