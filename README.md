# DL Manager

> 视频下载管理工具，基于 yt-dlp + ffmpeg 实现 m3u8 视频下载、合并、自动转移至 NAS。

![status](https://img.shields.io/badge/status-active-brightgreen)
![Python](https://img.shields.io/badge/python-3.10+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109-green)
![Vue](https://img.shields.io/badge/Vue-3-42b883)
![yt-dlp](https://img.shields.io/badge/yt--dlp-latest-orange)

---

## 功能特性

| 功能 | 说明 |
|------|------|
| 📡 订阅源管理 | 支持 Jable TV 页面抓取和标准 RSS 订阅，定时轮询自动发现新视频 |
| ⬇️ 批量下载 | yt-dlp 子进程下载（GIL 隔离），可配置并发数（上限 10），支持断点续传 |
| 📊 实时进度 | SSE 推送任务状态：百分比、下载速度、分片数，2s 广播间隔 |
| 🔗 自动合并 | ffmpeg concat copy 快速合并 TS 为 MP4，编码跳变时自动 re-encode |
| 📤 NAS 转移 | 下载完成后异步复制到 NAS（dd/Python 原生，带进度追踪） |
| 🌐 代理支持 | HTTP / SOCKS5 代理，解决网络限制 |
| ▶️ 网页播放 | 内置播放器，支持 Range 请求流式播放 NAS 视频 |
| 📋 双视图 | 网格卡片 / 紧凑列表一键切换，支持分页，组件化架构 |
| 🔄 批量操作 | 全选、批量开始 / 暂停 / 重试 / 删除 |
| 🎯 优先级队列 | 支持任务优先级（-100~100），高优先级先下载 |
| 🔁 智能重试 | 自动重试 3 次（指数退避 1min/5min/15min），手动重试无限次 |
| ⏰ 定时调度 | APScheduler cron 定时 RSS 轮询（默认每日 04:00） |
| 🔧 断点续传 | 服务重启自动恢复中断任务（下载/合并/转移） |

## 快速开始

### 方式一：Docker 部署（推荐）

```bash
git clone https://github.com/zhegcheg/dl-manager.git
cd dl-manager

# 创建数据目录
mkdir -p ~/.jable-dl-server

# 启动（host 网络模式）
docker compose up -d

# 查看日志
docker compose logs -f
```

> 容器配置了 `restart: always`，系统重启后自动恢复。

### 方式二：源码运行

**前置条件：**
- Python 3.10+
- ffmpeg（用于 TS 合并）
- 可选：NAS 挂载点

```bash
git clone https://github.com/zhegcheg/dl-manager.git
cd dl-manager

# 安装依赖
pip install -r requirements.txt

# 启动服务
python server.py
```

### 访问

打开浏览器访问 **http://localhost:8899**

## 项目结构

```
dl-manager/
├── server.py                  # 入口：uvicorn 启动 FastAPI
├── app/
│   ├── main.py                # FastAPI 应用工厂 + 生命周期
│   ├── events.py              # SSE 事件总线（脏标记广播）
│   ├── db/
│   │   └── database.py        # SQLite 操作（WAL 模式）
│   ├── routers/
│   │   ├── tasks.py           # 任务 CRUD + SSE 端点 + 日志流
│   │   ├── sources.py         # 订阅源管理 + RSS 触发
│   │   └── config.py          # 配置/代理/队列/媒体流
│   └── services/
│       ├── downloader.py      # yt-dlp 下载（多线程分片+断点续传）
│       ├── merger.py          # ffmpeg 合并（copy/re-encode 双策略）
│       ├── mover.py           # 异步文件转移到 NAS
│       ├── queue.py           # 优先级队列 + 并发控制 + 智能重试
│       ├── rss_poller.py      # Jable 页面抓取 + RSS 订阅轮询
│       └── scheduler.py       # APScheduler 定时调度
├── web/
│   ├── index.html             # Vue 3 主页面（任务/订阅/设置）
│   ├── app.js                 # 前端逻辑
│   ├── style.css              # 样式
│   ├── components/            # Vue 组件拆分
│   │   ├── LogsView.js        # 日志面板组件
│   │   ├── SettingsView.js    # 设置面板组件
│   │   └── SourcesView.js     # 订阅源面板组件
│   └── player.html            # 视频播放器
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 配置说明

在页面「⚙ 设置」中可配置：

| 配置项 | 说明 | 默认值 | 范围 |
|--------|------|--------|------|
| 下载目录 | 视频下载存储路径 | `~/.jable-dl-server/tasks` | - |
| 临时目录 | 分片下载暂存路径 | `~/.jable-dl-server/temp` | - |
| 最大并发数 | 同时下载的任务数 | `2` | 1-10 |
| 每任务线程数 | 每个任务的下载线程数 | `8` | 1-16 |
| NAS 转移 | 完成后自动复制到 NAS | `true` | on/off |
| 代理 | HTTP/SOCKS5 代理配置 | 关闭 | - |
| RSS 定时 | cron 表达式 | `0 4 * * *` | 标准 cron |

## 架构

```
┌─────────────────────────────────────────────────┐
│                 Vue 3 前端 (web/)                │
│   任务列表 · 订阅源管理 · 设置 · 播放器          │
└────────────────────┬────────────────────────────┘
                     │ HTTP / SSE
┌────────────────────┴────────────────────────────┐
│              FastAPI 后端 (app/)                  │
│  ┌──────────┬───────────┬──────────┐            │
│  │  tasks   │  sources  │  config  │  routers   │
│  └────┬─────┴─────┬─────┴────┬─────┘            │
│       │           │          │                   │
│  ┌────┴────┬──────┴────┬─────┴──────┐           │
│  │downloader│ merger  │  mover     │  services  │
│  │(yt-dlp) │(ffmpeg) │ (dd/copy)  │            │
│  └────┬────┴──────┬───┴─────┬──────┘           │
│       │           │         │                    │
│  ┌────┴───────────┴─────────┴──────┐            │
│  │      queue (优先级+并发控制)      │            │
│  │      events (SSE 事件总线)       │            │
│  │      scheduler (APScheduler)    │            │
│  └────────────────┬────────────────┘            │
│                   │                              │
│  ┌────────────────┴────────────────┐            │
│  │     SQLite (state.db, WAL)      │            │
│  └─────────────────────────────────┘            │
└─────────────────────────────────────────────────┘
                     │
        ┌────────────┴────────────┐
        │   NAS (/mnt/fn-nas-*)   │
        └─────────────────────────┘
```

## 技术栈

| 层 | 技术 |
|----|------|
| 后端框架 | Python 3.10+, FastAPI 0.109, Uvicorn |
| 下载引擎 | yt-dlp（子进程模式，GIL 隔离，断点续传，代理支持） |
| 视频合并 | ffmpeg（concat copy 优先，libx264 re-encode 保底） |
| 文件转移 | dd (Linux) / Python 原生复制 (Windows)，异步带进度 |
| 数据库 | SQLite（WAL 模式，busy_timeout 30s） |
| 定时调度 | APScheduler（BackgroundScheduler + CronTrigger） |
| 前端 | Vue 3 (CDN)，无构建步骤 |
| 实时通信 | SSE（Server-Sent Events），脏标记广播，2s 间隔 |
| 容器化 | Docker + docker-compose（host 网络模式） |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/healthy` | 健康检查 |
| GET | `/api/tasks` | 任务列表（`?status=` 过滤） |
| GET | `/api/tasks/events` | SSE 实时任务推送 |
| GET | `/api/tasks/{id}` | 任务详情 |
| POST | `/api/tasks` | 创建任务 |
| POST | `/api/tasks/from-url` | 从视频页 URL 自动创建任务 |
| POST | `/api/tasks/from-m3u8` | 从 m3u8 URL 直接创建任务 |
| POST | `/api/tasks/{id}/start` | 开始下载 |
| POST | `/api/tasks/{id}/stop` | 暂停任务 |
| POST | `/api/tasks/{id}/retry` | 手动重试（不限次数） |
| DELETE | `/api/tasks/{id}` | 删除任务（清理文件） |
| PATCH | `/api/tasks/{id}` | 更新任务（优先级等） |
| GET | `/api/tasks/{id}/logs` | 历史日志查询（分页） |
| POST | `/api/start-waiting` | 启动等待队列中的任务 |
| GET | `/api/sources` | 订阅源列表 |
| POST | `/api/sources` | 添加订阅源 |
| PUT/PATCH | `/api/sources/{id}` | 更新订阅源 |
| DELETE | `/api/sources/{id}` | 删除订阅源 |
| POST | `/api/rss/poll` | 手动触发 RSS 轮询 |
| GET | `/api/config` | 获取全部配置 |
| POST | `/api/config` | 更新单个配置项 |
| POST | `/api/config/batch` | 批量更新配置 |
| POST | `/api/config/apply` | 更新配置 + 动态调整队列 |
| GET/POST | `/api/proxy` | 代理配置读写 |
| GET | `/api/queue/status` | 队列状态 |
| GET | `/api/scheduler` | 调度配置 |
| POST | `/api/scheduler` | 更新调度配置 |
| GET | `/api/media/by-id/{id}` | 按任务 ID 流式播放视频（Range 206） |
| GET | `/api/media/{path}` | 按文件名播放 NAS 视频 |

## 数据存储

```
~/.jable-dl-server/
├── state.db          # SQLite 数据库（WAL 模式）
├── logs/             # 任务日志（每个任务一个 .log 文件）
├── tasks/            # 下载目录（可配置）
│   └── {task_id}.mp4 # 下载完成的视频
└── temp/             # 临时目录（分片暂存）
```

## License

MIT
