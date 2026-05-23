# DL Manager

> 视频下载管理工具，基于 N_m3u8DL-RE 实现 m3u8 视频下载、合并、自动转移。

![screenshot](https://img.shields.io/badge/status-active-brightgreen)
![Python](https://img.shields.io/badge/python-3.10+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-latest-green)
![Vue](https://img.shields.io/badge/Vue-3-42b883)

---

## 功能

| 功能 | 说明 |
|------|------|
| 📡 订阅源管理 | 支持 Jable TV 页面抓取和标准 RSS |
| ⬇️ 批量下载 | 可配置并发数，自动队列调度 |
| 📊 实时进度 | 百分比、速度、分片数，每秒刷新 |
| 🔗 自动合并 | 下载完成后自动合并 TS 为 MP4 |
| 📤 NAS 转移 | 自动复制到 NAS（CIFS 挂载） |
| ▶️ 网页播放 | 内置播放器，直接观看已下载视频 |
| 📋 双视图 | 网格列表 / 紧凑列表 一键切换 |
| 🔄 批量操作 | 全选、批量开始/暂停/重试/删除 |
| ⏰ 定时轮询 | 定时 RSS 订阅，自动添加新任务 |

## 快速开始

### 方式一：Docker 部署（推荐）

```bash
# 克隆
git clone https://github.com/zhegcheg/dl-manager.git
cd dl-manager

# 创建数据目录
mkdir -p ~/.jable-dl-server

# 启动
docker compose up -d

# 查看日志
docker compose logs -f
```

> 可用 `docker compose restart` 重启，容器配置了 `restart: always`，系统重启后自动启动。

### 方式二：源码直接运行

前置条件：
- Python 3.10+
- N_m3u8DL-RE（下载引擎，放到 `/tmp/N_m3u8DL-RE`）
- 推荐：飞牛 NAS（或任意 CIFS 挂载点）

### 安装

```bash
# 克隆
git clone https://github.com/zhegcheg/dl-manager.git
cd dl-manager

# 安装依赖
pip install -r requirements.txt

# 启动
python3 server.py
```

### 访问

打开浏览器访问 `http://localhost:8899`

## 配置说明

在页面「⚙ 设置」中可配置：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| 下载目录 | 视频下载路径 | `/home/zhegcheg/imovie/tasks` |
| 最大并发数 | 同时下载的任务数（上限 10） | `2` |
| 每任务线程数 | 每个任务的下载线程数（上限 16） | `8` |
| NAS 目标 | 完成后的文件转移路径 | `/mnt/fn-nas-imovie/` |

## 架构

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  Vue 3 前端  │────▶│  FastAPI 后端 │────▶│  SQLite 数据库 │
│  web/index  │     │  api.py      │     │  state.db    │
└─────────────┘     └──────┬───────┘     └──────────────┘
                           │
                    ┌──────┴───────┐
                    │ 队列管理器    │
                    │ queue_manager│
                    └──────┬───────┘
                           │
                    ┌──────┴───────┐
                    │ N_m3u8DL-RE  │
                    │ 下载引擎     │
                    └──────────────┘
```

## 技术栈

- **后端**: Python 3, FastAPI, Uvicorn, SQLite
- **前端**: Vue 3 (CDN), 原生 HTML/CSS
- **下载**: N_m3u8DL-RE (m3u8 下载合并)
- **存储**: 本地磁盘 + CIFS NAS 挂载

## License

MIT
