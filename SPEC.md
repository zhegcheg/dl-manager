# Jable Download Manager - 技术规格

## 架构

```
┌─────────────────────────────┐
│  小智 (OpenClaw Agent)      │
│  - RSS 抓取                 │
│  - 任务推送 (本地 HTTP)      │
│  - 监听 SSE 完成事件         │
└──────────┬──────────────────┘
           │ HTTP POST / SSE
           ▼
┌─────────────────────────────┐
│  JableDL Server (本机:8899) │
│  - FastAPI                  │
│  - N_m3u8DL-RE 下载器       │
│  - ffmpeg 合并器            │
│  - SQLite 状态库            │
│  - Web UI (Vue)             │
└─────────────────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  /mnt/fn-nas-imovie/        │
│  (媒体库目录)                │
└─────────────────────────────┘
```

## 任务状态机

```
waiting → downloading → merging → moving → completed
                              ↘ failed
```

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `POST /api/tasks` | 创建任务 | m3u8_url, name, headers, key |
| `GET /api/tasks` | 任务列表 | filter=status |
| `GET /api/tasks/{id}` | 任务详情 | 含 progress, speed, stage |
| `DELETE /api/tasks/{id}` | 删除任务 | kill 进程 + 清理 |
| `GET /api/tasks/{id}/logs` | 实时日志 | SSE 流 |
| `GET /api/events` | 全局事件 | download-success/failed/stopped |

## 任务字段

```json
{
  "id": "ntrh-021",
  "name": "NTRH-021 被睡走就睡回來 被睡走的妻子...",
  "m3u8_url": "https://xxx.m3u8",
  "headers": "Referer: https://jable.tv/...",
  "key": "0a42765b28a3b247a0424317b8bdc657",
  "iv": "0xc5537ce953bc7bd79d357c4be6536634",
  "status": "downloading",
  "stage": "downloading",
  "progress": 75,
  "speed": "2.5MB/s",
  "segments": "1918/2558",
  "chunks": null,
  "move_speed": null,
  "move_elapsed": null,
  "error": null,
  "created_at": "2026-05-23T06:50:00Z",
  "updated_at": "2026-05-23T06:51:00Z",
  "completed_at": null,
  "file": "/home/zhegcheg/.openclaw/workspace/jable-dl-server/tasks/ntrh-021/ntrh-021.mp4",
  "final_path": null
}
```

## Stage 详情

| stage | 关键字段 |
|-------|---------|
| waiting | - |
| downloading | progress, speed, segments |
| merging | progress, chunks (done/total) |
| moving | move_speed, move_elapsed |
| completed | file, final_path, total_time |
| failed | error |

## N_m3u8DL-RE 调用方式

```bash
./N_m3u8DL-RE "<m3u8_url>" \
  --save-dir "<task_dir>" \
  --save-name "<id>" \
  --custom-hls-key "<key>" \
  --custom-hls-iv "<iv>" \
  -H "Referer: https://jable.tv/" \
  -H "User-Agent: Mozilla/5.0..." \
  --log-level DEBUG \
  --log-file-path "<log_file>" \
  --thread-count 8
```

进度解析：解析日志中的 `Vid Kbps: XX%` 行。

## 合并流程

1. 下载产生 T0000.ts ~ T0025.ts + 临时 .ts.tmp 文件
2. 清理 .ts.tmp 和 core 文件
3. ffmpeg concat demuxer 合并为 single.mp4
4. 重命名为最终名称
5. 转移到 /mnt/fn-nas-imovie/
6. 清理 task 目录

## Web UI 页面

- 任务列表页 (`/`)：卡片展示所有任务
  - 状态标签（waiting/downloading/merging/moving/completed/failed）
  - 进度条（下载/合并/转移）
  - 速度显示
  - 创建时间
- 任务详情页 (`/task/{id}`)：实时日志滚动
- 自动刷新：下载中任务每 3 秒轮询

## 文件结构

```
jable-dl-server/
├── server.py           # FastAPI 主入口
├── task_db.py          # SQLite 操作
├── ntrh_downloader.py  # N_m3u8DL-RE 包装器
├── merger.py           # ffmpeg 合并
├── mover.py            # 文件转移
├── api.py              # HTTP API 路由
├── web/
│   ├── index.html      # 单页应用
│   ├── style.css
│   └── app.js          # Vue 组件
├── run.sh              # 启动脚本
└── requirements.txt
```