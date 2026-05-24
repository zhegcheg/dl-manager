---
name: dl-manager
description: >
  视频下载管理工具（原 JableDL Manager），基于本地 FastAPI 服务（端口 8899）。
  管理 RSS 轮询、N_m3u8DL-RE 下载、实时进度追踪、自动合并、文件转移全流程。
  触发词：dl、dl-manager、视频下载、下载管理、jable、ntrh、订阅、下载队列
license: MIT
metadata:
  author: xiaozhi
  version: "3.0.0"
---

# DL Manager Skill

## 系统架构

```
主人 ← → 小智（Skill）
           ↓
      FastAPI :8899
           ↓
  ┌─ RSS Poller（订阅源轮询，默认每日 04:00）
  ├─ N_m3u8DL-RE（下载 TS 片段 + 自动合并为 MP4）
  ├─ queue_manager（并发控制 + 队列调度 + 重试机制）
  ├─ watch_process（实时解析 stdout，追踪进度/速度）
  └─ mover（复制 MP4 → NAS 媒体库）
```

## 服务地址

| 项目 | 地址 |
|------|------|
| **Web UI** | http://localhost:8899/ |
| **健康检查** | `curl http://localhost:8899/api/healthy` |
| **任务列表** | `curl http://localhost:8899/api/tasks` |
| **播放视频** | `/player?file=文件名.mp4` |
| **GitHub** | https://github.com/zhegcheg/dl-manager |

## 服务管理（systemd）

```bash
# 启动服务
systemctl --user start jable-dl-server.service

# 停止服务
systemctl --user stop jable-dl-server.service

# 重启服务
systemctl --user restart jable-dl-server.service

# 查看状态
systemctl --user status jable-dl-server.service

# 查看日志
journalctl --user-unit jable-dl-server.service -f
```

> ⚠️ 已配置开机自启（`enable`），无需手动启动。

## 小智自然语言命令

| 主人说 | 小智行动 |
|--------|---------|
| `启动 dl 下载服务` | 检查 jable-dl-server.service 状态，若未运行则拉起 |
| `查看下载任务` | `GET /api/tasks`，汇报统计和进行中任务 |
| `停止 dl 下载服务` | `systemctl --user stop jable-dl-server.service` |
| `重启 dl 下载服务` | `systemctl --user restart jable-dl-server.service` |
| `现在检查订阅` | `POST /api/rss/poll`，汇报新增任务数 |
| `订阅源列表` | `GET /api/sources` |
| `添加订阅源 [名称] [URL]` | `POST /api/sources` |
| `修改并发数为 [n]` | `POST /api/config/apply` |
| `批量启动` | `POST /api/start-waiting` |
| `查看下载目录` | `ls /home/zhegcheg/imovie/tasks/` |
| `查看 NAS 文件` | `ls /mnt/fn-nas-imovie/` |

## 功能概览

| 功能 | 说明 |
|------|------|
| 📡 订阅源管理 | 支持 Jable TV 页面抓取和标准 RSS |
| ⬇️ 并发队列 | 可配置并发数，按序调度（先进先出） |
| 📊 实时进度 | 百分比 + 速度 + 分片数，每秒刷新 |
| 🔗 自动合并 | N_m3u8DL-RE 自带合并，无需额外工具 |
| 📤 NAS 转移 | 复制到 NAS 并跟踪进度/速度 |
| ▶️ 网页播放 | 内置 Video.js 播放器 |
| 📋 双视图 | 卡片/列表切换 |
| 🔄 批量操作 | 全选、批量开始/暂停/重试/删除 |
| 🔄 重试机制 | 卡死任务自动重试（最多 3 次） |
| ⏰ 定时轮询 | APScheduler 定时 RSS |

## 并发控制

- `max_concurrent` 可动态调整（改小即时停掉多余任务，改大自动拉入新任务）
- 所有操作（开始/重试/批量）均经过队列，不超限
- 任务完成后自动启动下一个等待任务

## Web UI 操作

| 区域 | 操作 |
|------|------|
| 任务列表 | ☐ 全选 → 批量开始/暂停/重试/删除 |
| 视图切换 | 右上角 ☰（列表）/ ▦（卡片） |
| 设置 | 下载目录、并发数、线程数、定时调度 |
| 已完成任务 | ▶ 播放（在浏览器中观看） |
| 进度条 | 显示百分比 + 实时速度 |
| 统计栏 | 进行中/已完成/失败/等待 + 总速度 |

## 进度追踪机制

基于 MediaGo 方案，实时解析 N_m3u8DL-RE 的 stdout：

```
N_m3u8DL-RE 输出:  Vid Kbps ━━━  434/2558  16.97%  340.51MB/2.00GB  1.06MBps
                       ↓            ↓        ↓                    ↓
                      段数正则      百分比正则                   速度正则
```

- 正则：`([\d.]+%)`, `([\d.]+[GMK]Bps)`, `(\d+)/(\d+)`
- 前端轮询 1s 刷新

## 核心模块

| 文件 | 职责 |
|------|------|
| `server.py` | FastAPI 入口，端口 8899 |
| `api.py` | HTTP API（任务/订阅源/配置/媒体播放） |
| `task_db.py` | SQLite（WAL 模式，含重试机制） |
| `ntrh_downloader.py` | N_m3u8DL-RE 封装 + stdout 解析 |
| `queue_manager.py` | 队列调度 + 并发控制 |
| `rss_poller.py` | RSS 订阅轮询 |
| `scheduler.py` | APScheduler 定时 |
| `mover.py` | NAS 转移（copy2 + unlink） |
| `web/index.html` | Vue 3 前端主页面 |
| `web/app.js` | 前端逻辑 |
| `web/style.css` | 样式 |
| `web/player.html` | 视频播放器页面 |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/tasks` | 任务列表 |
| POST | `/api/tasks/{id}/start` | 开始单个任务 |
| POST | `/api/tasks/{id}/stop` | 暂停单个任务 |
| POST | `/api/tasks/{id}/retry` | 重试单个任务（最多 3 次） |
| DELETE | `/api/tasks/{id}` | 删除任务 |
| POST | `/api/start-waiting` | 批量启动等待任务 |
| GET | `/api/config` | 获取配置 |
| POST | `/api/config/apply` | 批量更新配置 + 动态调整队列 |
| GET | `/api/media/{filename}` | 播放 NAS 视频文件 |
| POST | `/api/rss/poll` | 手动触发 RSS 轮询 |
| GET | `/api/sources` | 订阅源列表 |
| GET | `/api/scheduler` | 调度配置 |
| GET | `/player.html` | 播放器页面 |

## 数据文件

```
~/.jable-dl-server/
├── state.db          # SQLite（WAL 模式）
├── tasks/            # 下载目录
│   └── task_id.mp4   # 完成后平铺到根目录
└── logs/             # 日志
```

## NAS 目标

```
/mnt/fn-nas-imovie/*.mp4（以原标题命名）
```

## 配置项

| 键 | 默认值 | 上限 |
|---|--------|------|
| `download_dir` | `~/.jable-dl-server/tasks` | - |
| `max_concurrent` | `2` | 10 |
| `thread_count` | `8` | 16 |
