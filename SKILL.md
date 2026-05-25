---
name: dl-manager
description: >
  视频下载管理工具（DL Manager），基于 FastAPI + yt-dlp + ffmpeg。
  管理订阅源轮询、m3u8 下载、实时进度追踪、自动合并、NAS 转移全流程。
  触发词：dl、dl-manager、视频下载、下载管理、jable、订阅、下载队列
license: MIT
metadata:
  author: xiaozhi
  version: "4.0.0"
---

# DL Manager Skill

## 系统架构

```
主人 ← → 小智（Skill）
           ↓
      FastAPI :8899 (server.py → app/main.py)
           ↓
  ┌─ RSS Poller（Jable 页面抓取 + RSS 订阅轮询，APScheduler 定时）
  ├─ yt-dlp（子进程下载 m3u8，GIL 隔离，断点续传，代理支持）
  ├─ queue.py（优先级队列 + 并发控制 + 指数退避重试）
  ├─ merger.py（ffmpeg concat copy / re-encode 合并 TS→MP4）
  ├─ mover.py（异步复制 MP4 → NAS，dd/Python 带进度）
  └─ events.py（SSE 事件总线，2s 脏标记广播）
```

## 服务地址

| 项目 | 地址 |
|------|------|
| **Web UI** | http://localhost:8899/ |
| **健康检查** | `curl http://localhost:8899/healthy` |
| **任务列表** | `curl http://localhost:8899/api/tasks` |
| **SSE 推送** | `GET /api/tasks/events` |
| **播放视频** | `/player.html?id=<task_id>` |
| **GitHub** | https://github.com/zhegcheg/dl-manager |

## 服务管理

```bash
# Docker（推荐）
docker compose up -d
docker compose logs -f
docker compose restart

# 源码运行
python server.py

# 查看日志
docker compose logs --tail 100 -f dl-manager
```

## 小智自然语言命令

| 主人说 | 小智行动 |
|--------|---------|
| `启动 dl 下载服务` | 检查服务状态，若未运行则 `docker compose up -d` |
| `查看下载任务` | `GET /api/tasks`，汇报统计和进行中任务 |
| `停止 dl 下载服务` | `docker compose stop` |
| `重启 dl 下载服务` | `docker compose restart` |
| `现在检查订阅` | `POST /api/rss/poll`，汇报新增任务数 |
| `订阅源列表` | `GET /api/sources` |
| `添加订阅源 [名称] [URL]` | `POST /api/sources` (body: name, url, feed_type) |
| `修改并发数为 [n]` | `POST /api/config/apply` (body: {max_concurrent: n}) |
| `批量启动` | `POST /api/start-waiting` |
| `添加视频 [URL]` | `POST /api/tasks/from-url` (body: {url: "..."}) |
| `查看下载目录` | `ls <download_dir>` |
| `查看 NAS 文件` | `ls /mnt/fn-nas-imovie/` |
| `查看队列状态` | `GET /api/queue/status` |

## 功能概览

| 功能 | 说明 |
|------|------|
| 📡 订阅源管理 | Jable TV 页面抓取 + 标准 RSS，自动提取 m3u8 和 AES 密钥 |
| ⬇️ 并发队列 | 优先级队列（priority DESC, created_at ASC），可配置并发数 1-10，yt-dlp 子进程下载 |
| 📊 实时进度 | SSE 推送，百分比 + 速度 + 分片数，2s 广播间隔 |
| 🔗 自动合并 | ffmpeg concat copy（快）→ re-encode（保底），编码跳变检测 |
| 📤 NAS 转移 | dd (Linux) / Python 原生复制 (Windows)，异步带进度 |
| 🌐 代理支持 | HTTP / SOCKS5，用于 yt-dlp 下载和 RSS 轮询 |
| ▶️ 网页播放 | 内置播放器，Range 206 流式传输 |
| 📋 双视图 | 卡片网格 / 紧凑列表，支持分页（24/36/72/108 条） |
| 🔄 批量操作 | 全选、批量开始/暂停/重试/删除 |
| 🔁 智能重试 | 自动重试 3 次（指数退避 1/5/15 分钟），手动重试无限 |
| ⏰ 定时轮询 | APScheduler cron 定时（默认 04:00 Asia/Shanghai） |
| 🔧 断点续传 | 重启自动恢复：下载中断→重置 waiting，合并/转移中断→后台恢复 |

## 核心模块

| 文件 | 职责 |
|------|------|
| `server.py` | 入口：uvicorn 启动 FastAPI，端口 8899 |
| `app/main.py` | FastAPI 应用工厂，生命周期管理（启动恢复+SSE广播） |
| `app/events.py` | SSE 事件总线：mark_dirty() 标记变更，broadcast_worker 2s 广播 |
| `app/db/database.py` | SQLite（WAL），tasks/sources/scheduler_config/download_config/proxy_config 表 |
| `app/routers/tasks.py` | 任务 CRUD + SSE 端点 + 日志流 + from-url/from-m3u8 创建 |
| `app/routers/sources.py` | 订阅源 CRUD + RSS 手动触发 |
| `app/routers/config.py` | 配置/代理/队列状态/媒体流（Range 206） |
| `app/services/downloader.py` | yt-dlp 子进程下载：GIL 隔离、--progress-template JSON 解析、DownloadThread 封装 |
| `app/services/merger.py` | ffmpeg 合并：编码跳变检测 → concat copy → re-encode 保底 |
| `app/services/mover.py` | 异步转移：dd (Linux) / Python 复制 (Windows)，带进度汇报 |
| `app/services/queue.py` | 优先级队列、并发控制、指数退避重试、启动恢复 cleanup_finished |
| `app/services/rss_poller.py` | Jable 页面抓取（urllib+代理）、m3u8/AES 密钥提取、多源轮询 |
| `app/services/scheduler.py` | APScheduler BackgroundScheduler，cron 定时 RSS |
| `web/index.html` | Vue 3 前端：任务/订阅/设置三标签页 |
| `web/app.js` | 前端逻辑：SSE 订阅、状态管理、API 调用、KiB/s 速度单位支持 |
| `web/player.html` | 视频播放器页面 |

## API 端点速查

### 任务管理
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/tasks` | 任务列表（`?status=` 过滤） |
| GET | `/api/tasks/events` | SSE 实时推送 |
| GET | `/api/tasks/{id}` | 任务详情 |
| POST | `/api/tasks` | 创建任务（需提供 id, name, m3u8_url） |
| POST | `/api/tasks/from-url` | 从视频页 URL 自动解析创建 |
| POST | `/api/tasks/from-m3u8` | 从 m3u8 URL 直接创建 |
| POST | `/api/tasks/{id}/start` | 开始下载 |
| POST | `/api/tasks/{id}/stop` | 暂停 |
| POST | `/api/tasks/{id}/retry` | 手动重试（不限次数） |
| DELETE | `/api/tasks/{id}` | 删除（清理文件+日志） |
| PATCH | `/api/tasks/{id}` | 更新（优先级） |
| GET | `/api/tasks/{id}/logs` | 实时日志 SSE |
| POST | `/api/start-waiting` | 启动等待队列 |

### 订阅源
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/sources` | 列表 |
| POST | `/api/sources` | 添加 |
| PUT/PATCH | `/api/sources/{id}` | 更新 |
| DELETE | `/api/sources/{id}` | 删除 |
| POST | `/api/rss/poll` | 手动触发轮询 |

### 配置
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/config` | 获取全部配置 |
| POST | `/api/config` | 更新单个配置项 |
| POST | `/api/config/batch` | 批量更新 |
| POST | `/api/config/apply` | 更新 + 动态调整队列 |
| GET/POST | `/api/proxy` | 代理配置 |
| GET | `/api/queue/status` | 队列状态 |
| GET/POST | `/api/scheduler` | 调度配置 |

### 媒体
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/media/by-id/{task_id}` | 按任务 ID 流式播放（Range 206） |
| GET | `/api/media/{filename}` | 按文件名播放 NAS 视频 |

## 配置项

| 键 | 说明 | 默认值 | 范围 |
|----|------|--------|------|
| `download_dir` | 下载目录 | `~/.jable-dl-server/tasks` | - |
| `temp_dir` | 临时目录 | `~/.jable-dl-server/temp` | - |
| `max_concurrent` | 最大并发数 | `2` | 1-10 |
| `thread_count` | 每任务线程数 | `8` | 1-16 |
| `move_to_nas` | NAS 转移开关 | `true` | true/false |
| `rss_cron` | 定时 cron | `0 4 * * *` | 标准 cron |
| `rss_enabled` | 定时开关 | `true` | true/false |
| `proxy.enabled` | 代理开关 | `false` | true/false |
| `proxy.type` | 代理类型 | `http` | http/socks5 |
| `proxy.host` | 代理主机 | `""` | - |
| `proxy.port` | 代理端口 | `7890` | 1-65535 |

## 数据文件

```
~/.jable-dl-server/
├── state.db          # SQLite（WAL 模式，busy_timeout=30s）
├── tasks/            # 下载目录
│   └── {task_id}.mp4 # 完成后平铺
├── temp/             # 临时分片目录
└── logs/             # 任务日志
```

## NAS 目标

```
/mnt/fn-nas-imovie/{视频标题}.mp4
```

## 进度追踪机制

基于 yt-dlp `--progress-template` 输出：
- yt-dlp 运行在独立子进程（subprocess.Popen），与主进程 GIL 完全隔离
- 监控线程通过 `proc.stdout.readline()` 读取 JSON 进度行
- 每 2 秒更新一次 DB（POLL_INTERVAL=2s）
- 提取：进度百分比、下载速度、分片进度（fragment_index/fragment_count）
- SSE 广播间隔 2s（events.py broadcast_worker）
- 系统统计缓存 4s TTL，避免多 SSE 订阅者重复计算 psutil
- 前端通过 `/api/tasks/events` SSE 接收实时更新
- 速度单位支持：MB/s, KB/s（标准） + MiB/s, KiB/s, GiB/s（yt-dlp 二进制单位）

> 注：`--concurrent-fragments` 仅对 DASH 多文件下载有效，HLS/m3u8 始终顺序下载分片

## Web UI 操作

| 区域 | 操作 |
|------|------|
| 添加视频 | ➕ 按钮 → 弹窗 → Jable 页面 URL 或 m3u8 URL |
| 任务列表 | ☐ 全选 → 批量开始/暂停/重试/删除 |
| 视图切换 | 右上角 ☰（列表）/ ▦（卡片） |
| 筛选栏 | 全部/等待/下载中/合并中/转移中/已完成/失败/已暂停 |
| 设置 | 下载目录、临时目录、并发数、线程数、NAS、定时、代理 |
| 已完成任务 | ▶ 播放（浏览器流式播放） |
| 分页 | 底部分页器，24/36/72/108 条每页 |
| 统计栏 | 进行中/已完成/失败/等待 + 总速度 MB/s |
