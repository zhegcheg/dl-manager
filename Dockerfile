FROM python:3.10-slim

WORKDIR /app

# 安装系统依赖: ffmpeg (视频合并) + pv (文件转移进度监控)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ffmpeg pv \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY . .

# 创建必要目录
RUN mkdir -p logs tasks

EXPOSE 8899

CMD ["python3", "server.py"]
