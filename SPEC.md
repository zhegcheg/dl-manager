# DL Manager - 技术规格

## 架构

```
┌──────────────────────────────────────────────────────┐
│  Vue 3 前端 (web/)                                    │
│  index.html (任务/订阅/设置) · player.html (播放)     │
│  components/ (LogsView · SettingsView · SourcesView)  │
└───────────────────────┬──────────────────────────────┘
                        │ HTTP REST + SSE
┌───────────────────────┴──────────────────────────────┐
│  FastAPI 后端 (server.py → app/main.py)               │
│                                                       │
│  Routers: tasks · sources · config                    │
│  Services: downloader(子进程) · merger · mover        │
│            queue · rss_poller · scheduler             │
│  Events: SSE 广播 (events.py, 2s 间隔)                │
│  Storage: SQLite (database.py, WAL mode)              │
└───────────────────────┬──────────────────────────────┘
          │                    │
   yt-dlp (subprocess)    NAS (/mnt/fn-nas-imovie/)
   + ffmpeg
```

## 任务状态机

```
                    ┌──────────┐
                    │ waiting  │ ← 创建 / 自动重试(退避) / 手动重试
                    └────┬─────┘
                         │ try_start_next() (优先级排序)
                         ▼
                   ┌─────────────┐
                   │ downloading │ ← yt-dlp 子进程下载
                   └──────┬──────┘
                          │ 下载完成
                          ▼
                    ┌──────────┐
                    │ merging  │ ← ffmpeg concat copy / re-encode
                    └────┬─────┘
                         │ 合并完成
                         ▼
                    ┌──────────┐
                    │ moving   │ ← 异步复制到 NAS (dd/Python)
                    └────┬─────┘
                         │ 转移完成
                         ▼
                    ┌───────────┐
                    │ completed │
                    └───────────┘

  任何阶段 ──→ failed (错误)
  任何运行阶段 ──→ stopped (用户暂停/停止)

  重启恢复:
    downloading → waiting (断点续传)
    merging → 后台恢复合并
    moving → 后台恢复转移
```

## 任务字段

```json
{
  "id": "julia-021",
  "name": "视频标题",
  "m3u8_url": "https://cdn.example.com/video/index.m3u8",
  "headers": "Referer: https://jable.tv/\r\nUser-Agent: ...",
  "key": "0a42765b28a3b247a0424317b8bdc657",
  "iv": "0xc5537ce953bc7bd79d357c4be6536634",
  "status": "downloading",
  "stage": "downloading",
  "progress": 75.5,
  "speed": "2.50MB/s",
  "segments": "1918/2558",
  "chunks": "",
  "move_speed": "",
  "move_elapsed": "",
  "error": "",
  "retry_count": 0,
  "retry_after": "",
  "priority": 0,
  "download_dir": "",
  "created_at": "2026-05-23T06:50:00Z",
  "updated_at": "2026-05-23T06:51:00Z",
  "completed_at": null,
  "file": "/root/.jable-dl-server/tasks/julia-021.mp4",
  "final_path": "/mnt/fn-nas-imovie/视频标题.mp4"
}
```

## Stage 详情

| stage | 关键字段 | 说明 |
|-------|---------|------|
| `waiting` | priority, retry_after | 等待调度，支持优先级和退避时间 |
| `downloading` | progress, speed, segments | yt-dlp 下载中，实时进度 |
| `merging` | progress | ffmpeg concat copy 合并中 |
| `merging_reencode` | progress | ffmpeg re-encode 合并中（编码跳变时） |
| `moving` | progress, move_speed, move_elapsed | 异步转移到 NAS |
| `completed` | file, final_path, move_speed="done" | 全部完成 |
| `failed` | error | 失败（含错误信息） |
| `stopped` | error | 用户暂停/停止 |

## 下载流程 (yt-dlp 子进程)

```python
# downloader.py - start_download()
cmd = [
    sys.executable, '-m', 'yt_dlp', m3u8_url,
    '-o', f'{temp_dir}/{task_id}.%(ext)s',
    '--concurrent-fragments', str(thread_count),
    '--continue',                        # 断点续传
    '--merge-output-format', 'mp4',      # 自动合并
    '--console-title',                   # 进度条写控制台标题
    '--retries', '10',
    '--fragment-retries', '10',
    '--socket-timeout', '30',
    '--newline',
    '--progress-template',               # JSON 进度输出到 stdout
    'download:{"progress":"...","speed":"...","frag_idx":"...","frag_cnt":"...","eta":"..."}',
]
proc = subprocess.Popen(cmd, stdout=PIPE, stderr=PIPE, text=True)
```

**架构**: yt-dlp 运行在独立子进程（subprocess.Popen），与主进程 GIL 完全隔离，Web UI 不受下载影响

**进度解析**: 监控线程通过 `proc.stdout.readline()` 读取 JSON 进度行，每 2 秒（POLL_INTERVAL=2）更新一次 DB

**代理支持**: 根据 proxy_config 设置 `ydl_opts['proxy']`（http/socks5）

> 注：`--concurrent-fragments` 仅对 DASH 多文件下载有效，HLS/m3u8 始终顺序下载分片

## 合并流程 (ffmpeg)

```
merger.py - merge_ts_to_mp4()

1. 采样 ffprobe 检测编码跳变（首/中/尾三个 TS）
2. 若无跳变:
   → concat copy + aac_adtstoasc（最快，-c:v copy -c:a copy）
   → 失败则 fallback 到 re-encode
3. 若有跳变:
   → 直接 re-encode（libx264 preset=fast crf=23 + aac 128k + faststart）
```

## 转移流程 (mover.py)

```
move_to_media_library() → 启动后台线程

Linux:  dd bs=4M status=progress → 解析 stderr 获取进度
Windows: Python 原生 4MB chunk 复制 → 计算进度

完成后:
  - 更新 status=completed, final_path=NAS路径
  - 删除源文件 (unlink)
  - 清理 download_dir/{task_id}/ 目录
```

**文件名处理**: CIFS 文件名上限 255 字符，超过 200 字符自动截断到 120

## 队列管理 (queue.py)

### 优先级调度

```python
# try_start_next() 排序规则
order_by = "priority DESC, created_at ASC"
# 高优先级先下载，同优先级按创建时间
```

### 并发控制

- `max_concurrent` 可配置 1-10，动态调整
- `apply_max_concurrent(new_limit)`: 调小时停掉最后启动的多余任务
- 所有操作（start/retry/batch）均经过队列检查

### 智能重试（指数退避）

```python
RETRY_BACKOFF = [60, 300, 900]  # 1min, 5min, 15min

# 自动重试: retry_count < 3，每次失败增加退避时间
# 手动重试: 重置 retry_count=0，不受限制
```

### 启动恢复 (cleanup_finished)

```
服务启动时扫描所有任务:
  downloading → waiting（进程已死，yt-dlp 支持断点续传）
  moving (未完成) → 后台线程重新转移
  merging → 后台线程重新合并
  failed (含"合并"错误) → 后台线程重试合并
```

## SSE 实时推送 (events.py)

```
工作线程调用 mark_dirty() → 设置脏标记
broadcast_worker (asyncio) → 每 2s 检查脏标记
  → 有变更: 查询全部任务列表 → 推送给所有 SSE 订阅者
  → 无变更: 跳过
系统统计: get_system_stats() 结果缓存 4s TTL，避免多 SSE 订阅者重复计算 psutil
SSE 端点: GET /api/tasks/events
  → 连接时立即推送一次当前列表
  → 每 15s 发送心跳保持连接
```

## RSS 轮询 (rss_poller.py)

### Jable 页面抓取

```
fetch_jable_page(video_url) → HTML
extract_jable_info(video_url) → {id, name, m3u8_url, key, iv, headers}

提取策略:
  - title: <title> 标签 → 清理后缀
  - m3u8: 正则匹配 https?://...m3u8
  - AES key: 正则匹配 crypto/key/iv 或 mushroomtrack 域名 hex
  - video_id: 从 URL /videos/{id} 提取
```

### 代理支持

```python
_get_proxy_opener() → urllib opener (配置了 ProxyHandler)
# 用于 RSS 轮询和页面抓取（yt-dlp 有独立的代理配置）
```

### 去重

```python
# 检查 video_id 是否已存在
existing = get_task(vid)
if existing and existing["status"] not in ("failed", "stopped"):
    continue  # 跳过已存在的非失败任务
```

## 数据库设计 (SQLite)

### 表结构

```sql
-- 任务表
tasks (
    id TEXT PRIMARY KEY,          -- 视频 ID
    name TEXT NOT NULL,           -- 标题
    m3u8_url TEXT NOT NULL,       -- m3u8 播放列表 URL
    headers TEXT DEFAULT '',      -- 自定义 HTTP 头
    key TEXT DEFAULT '',          -- HLS AES 密钥 (hex)
    iv TEXT DEFAULT '',           -- HLS AES IV (hex)
    status TEXT DEFAULT 'waiting',-- 状态
    stage TEXT DEFAULT 'waiting', -- 阶段
    progress REAL DEFAULT 0,      -- 进度 0-100
    speed TEXT DEFAULT '',        -- 下载速度
    segments TEXT DEFAULT '',     -- 分片进度 "1918/2558"
    move_speed TEXT DEFAULT '',   -- 转移速度
    move_elapsed TEXT DEFAULT '', -- 转移耗时
    error TEXT DEFAULT '',        -- 错误信息
    retry_count INTEGER DEFAULT 0,-- 重试次数
    retry_after TEXT DEFAULT '',  -- 退避截止时间
    priority INTEGER DEFAULT 0,   -- 优先级 -100~100
    download_dir TEXT DEFAULT '', -- 自定义下载目录
    file TEXT DEFAULT '',         -- 本地文件路径
    final_path TEXT DEFAULT '',   -- NAS 最终路径
    created_at TEXT, updated_at TEXT, completed_at TEXT
)

-- 订阅源表
subscription_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, url TEXT,
    feed_type TEXT DEFAULT 'jable',
    enabled INTEGER DEFAULT 1,
    created_at TEXT, updated_at TEXT
)

-- 调度配置 (key-value)
scheduler_config (key TEXT PRIMARY KEY, value TEXT)
  -- rss_cron: "0 4 * * *"
  -- rss_enabled: "true"

-- 下载配置 (key-value)
download_config (key TEXT PRIMARY KEY, value TEXT)
  -- download_dir, temp_dir, max_concurrent, thread_count, move_to_nas

-- 代理配置 (key-value)
proxy_config (key TEXT PRIMARY KEY, value TEXT)
  -- enabled, type, host, port
```

### SQLite 配置

```python
PRAGMA journal_mode=WAL        # 写前日志，支持并发读写
PRAGMA busy_timeout=30000      # 锁等待 30 秒
```

## 媒体流 (config.py)

```
GET /api/media/by-id/{task_id}
  → 从 task 的 file/final_path 获取文件路径
  → 支持 Range 请求 (HTTP 206 Partial Content)
  → 64KB chunk 流式传输
  → Content-Type: video/mp4

GET /api/media/{filename}
  → 从 NAS MEDIA_DIR 提供文件
  → FileResponse
```

## 前端架构

```
Vue 3 (CDN, 无构建) → 单页应用 + 组件化拆分

三个标签页（独立组件）:
  1. 任务列表 (tasks) - 统计栏 + 筛选栏 + 卡片/列表视图 + 分页
  2. 订阅源 (sources) - SourcesView.js 组件
  3. 设置 (settings) - SettingsView.js 组件
  日志面板: LogsView.js 组件

实时通信:
  SSE (/api/tasks/events) → 任务变更自动刷新
  连接断开自动重连 (5s)

速度单位: 支持 MB/s, KB/s (标准) 和 MiB/s, KiB/s, GiB/s (yt-dlp 二进制单位)

添加视频弹窗:
  - Jable 模式: 输入视频页 URL → POST /api/tasks/from-url
  - m3u8 模式: 输入 m3u8 URL + 名称 + headers → POST /api/tasks/from-m3u8
```

## Docker 部署

```dockerfile
# Dockerfile
FROM python:3.10-slim
RUN apt-get install -y curl ffmpeg   # ffmpeg 用于合并
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8899
CMD ["python3", "server.py"]
```

```yaml
# docker-compose.yml
network_mode: "host"
volumes:
  - tasks:/app/tasks
  - data:/root/.jable-dl-server
  - nas:/mnt/fn-nas-imovie
environment:
  TZ: Asia/Shanghai
```

## 文件结构

```
dl-manager/
├── server.py                       # uvicorn 入口
├── app/
│   ├── main.py                     # FastAPI 工厂 + lifespan
│   ├── events.py                   # SSE 事件总线
│   ├── db/
│   │   └── database.py             # SQLite CRUD
│   ├── routers/
│   │   ├── tasks.py                # /api/tasks/*
│   │   ├── sources.py              # /api/sources/* + /api/rss/*
│   │   └── config.py               # /api/config/* + /api/proxy/* + /api/media/*
│   └── services/
│       ├── downloader.py           # yt-dlp 下载
│       ├── merger.py               # ffmpeg 合并
│       ├── mover.py                # 文件转移
│       ├── queue.py                # 队列调度
│       ├── rss_poller.py           # RSS/Jable 轮询
│       └── scheduler.py            # APScheduler
├── web/
│   ├── index.html                  # 主页面
│   ├── app.js                      # 前端逻辑
│   ├── style.css                   # 样式
│   ├── components/                 # Vue 组件拆分
│   │   ├── LogsView.js             # 日志面板
│   │   ├── SettingsView.js         # 设置面板
│   │   └── SourcesView.js          # 订阅源面板
│   └── player.html                 # 播放器
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
