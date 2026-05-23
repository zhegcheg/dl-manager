FROM python:3.10-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 下载 N_m3u8DL-RE
RUN curl -sL "https://github.com/nilaoda/N_m3u8DL-RE/releases/latest/download/N_m3u8DL-RE_Linux_x64.tar.gz" \
    | tar -xz -C /usr/local/bin/ N_m3u8DL-RE && \
    chmod +x /usr/local/bin/N_m3u8DL-RE

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY . .

# 创建必要目录
RUN mkdir -p logs tasks

EXPOSE 8899

CMD ["python3", "server.py"]
