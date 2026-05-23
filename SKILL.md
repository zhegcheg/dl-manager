---
name: jable-dl-manager
description: >
  Jable.TV 视频下载管理 Skill。
  基于本地 FastAPI 服务（端口 8899），管理 RSS 轮询、N_m3u8DL-RE 下载、ffmpeg 合并、文件转移全流程。
  触发词：jable、jabledl、下载 jable、启动 jable、jable 任务、jable rss、jable 订阅、jable 定时
license: MIT
metadata:
  author: xiaozhi
  version: "2.1.0"
---

# JableDL Manager Skill

## 系统架构

```
主人 ← → 小智（Skill）
           ↓
      FastAPI :8899
           ↓
  ┌─ RSS Poller（APScheduler 每日轮询）
  ├─ N_m3u8DL-RE（下载 TS 片段）
  ├─ ffmpeg（合并 TS → MP4，当 N_m3u8DL-RE 合并失败时自动触发）
  ├─ queue_manager（并发控制，自动启动等待中的任务）
  └─ mover（转移 MP4 → NAS 媒体库）
```

## 服务地址

| 项目 | 地址 |
|------|------|
| **Web UI** | http://localhost:8899/ |
| **健康检查** | `curl http://localhost:8899/api/healthy` |
| **任务列表** | `curl http://localhost:8899/api/tasks` |
| **下载配置** | `curl http://localhost:8899/api/config` |

## 启动服务

```bash
cd ~/.openclaw/workspace/jable-dl-server && python3 server.py
```

服务运行中可随时通过 `curl` 或 Web UI 管理。

## 小智自然语言命令

| 主人说 | 小智行动 |
|--------|---------|
| `启动 jable 下载服务` | 检查端口 8899，若未运行则后台启动 |
| `查看 jable 任务` | `GET /api/tasks`，汇报统计和进行中任务 |
| `停止 jable 下载服务` | `pkill -f "jable-dl-server"` |
| `jable rss 现在检查` | `POST /api/rss/poll`，汇报新增任务数（RSS 后自动启动等待任务） |
| `jable 订阅源列表` | `GET /api/sources` |
| `添加 jable 订阅源 [名称] [URL]` | `POST /api/sources` |
| `jable 修改定时为 [cron]` | `POST /api/scheduler?key=rss_cron&value=<cron>` |
| `jable 立即启动` | `POST /api/start-waiting`，按并发数启动等待任务 |
| `jable 清除所有已完成任务` | 遍历 completed 状态任务，删除记录和文件 |
| `jable 手动下载 [URL]` | 从 URL 提取 m3u8 → 创建任务 → 自动启动下载 |

## 并发控制

- 默认 **最大并发 2 个任务**（可配置）
- 每个任务 **默认 8 线程**（可配置）
- 任务完成或失败后自动触发下一个等待任务
- 可配置下载目录（默认 `~/.jable-dl-server/tasks`）

## 下载配置 API

```bash
# 获取配置
curl http://localhost:8899/api/config

# 修改最大并发数
curl -X POST "http://localhost:8899/api/config?key=max_concurrent&value=3"

# 修改线程数
curl -X POST "http://localhost:8899/api/config?key=thread_count&value=16"

# 修改下载目录（需重启服务生效）
curl -X POST "http://localhost:8899/api/config?key=download_dir&value=/home/zhegcheg/imovie"
```

## RSS 轮询机制

- **Jable 无官方 RSS**，`feed_type='jable'` 通过爬取 jable.tv 页面提取视频列表
- 流程：页面提取 `<a href="/videos/xxx">` → 逐个视频页提取 m3u8_url + AES 密钥 → 写入任务库
- 已下载/下载中/等待中的视频自动跳过
- 轮询完成后自动按并发数启动等待任务
- 默认每日 04:00 执行，可通过 Web UI 修改

## 状态机

```
waiting → downloading → merging → moving → completed
                              ↘ failed
```

## 已知问题处理

### N_m3u8DL-RE 合并失败（1800+ segments bug）
N_m3u8DL-RE 在片段数 >1800 时合并会失败（returncode ≠ 0），文件实际已下载完成。
**自动处理**：检测到 N_m3u8DL-RE 失败且片段数 >80%，自动触发 `merger.py`（ffmpeg concat）完成合并。

### 下载目录结构
- **正确**：`{download_dir}/{task_id}/0____/0000.ts`
- **错误**：`{download_dir}/{task_id}/0000.ts`
（N_m3u8DL-RE 自动创建 `{task_id}/` 子目录，目录需预先创建）

## 核心模块

| 文件 | 职责 |
|------|------|
| `server.py` | FastAPI 入口，端口 8899，常驻进程 |
| `api.py` | HTTP API（任务/订阅源/调度器/RSS/配置） |
| `task_db.py` | SQLite 状态库（任务、订阅源、调度配置、下载配置） |
| `rss_poller.py` | RSS 轮询（Jable 页面爬视频列表） |
| `scheduler.py` | APScheduler 定时调度 |
| `ntrh_downloader.py` | N_m3u8DL-RE 下载 + 进度追踪 |
| `merger.py` | ffmpeg concat demuxer 合并 TS→MP4 |
| `mover.py` | 拷贝 MP4 到 NAS 媒体库 |
| `queue_manager.py` | 并发控制，自动触发下一个任务 |
| `subscriptions.py` | 订阅源 CRUD |

## 数据文件

```
~/.jable-dl-server/
├── state.db          # SQLite（任务、订阅源、调度配置、下载配置）
├── tasks/           # 下载目录（可在配置中修改）
│   └── {task_id}/
│       └── 0____/   # TS 片段
└── logs/            # 日志文件
    └── {task_id}.log
```

## NAS 目标

```
/mnt/fn-nas-imovie/*.mp4
```

## 配置项（数据库 download_config 表）

| 键 | 默认值 | 说明 |
|---|--------|------|
| `download_dir` | `~/.jable-dl-server/tasks` | 下载根目录 |
| `max_concurrent` | `2` | 最大并发下载数 |
| `thread_count` | `8` | 每个任务的下载线程数 |