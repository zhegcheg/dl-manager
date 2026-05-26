# DL Manager

基于 yt-dlp + ffmpeg 的 m3u8 视频下载管理平台，支持网页端管理、订阅源自动轮询、实时下载进度和 NAS 转移。

## 一键部署

```bash
docker pull zhegcheg/dl-manager:latest

docker run -d --name dl-manager \
  --network host \
  -v ~/dl-manager/tasks:/app/tasks \
  -v ~/.dl-manager:/root/.dl-manager \
  -v /mnt/nas:/mnt/nas \
  -e NAS_MEDIA_DIR=/mnt/nas \
  -e TZ=Asia/Shanghai \
  zhegcheg/dl-manager:latest
```

自定义端口（默认 8899）：
```bash
docker run ... -e PORT=9900 ...
```

## 主要功能

| 功能 | 说明 |
|------|------|
| 📡 订阅源 | 支持网页抓取 + RSS 订阅，定时轮询 |
| ⬇️ 下载 | yt-dlp 子进程下载，配置并发数 1-10 |
| 📊 实时进度 | SSE 推送，2s 间隔，百分比+速度 |
| 🔗 自动合并 | ffmpeg concat copy，编码跳变自动 re-encode |
| 📤 NAS 转移 | 下载完成后异步复制到 NAS，带进度 |
| ▶️ 网页播放 | 内置播放器，支持 Range 流式播放 |
| 🔄 批量操作 | 全选、批量开始/暂停/重试/删除 |
| 🎯 优先级队列 | 优先级 -100~100，高优先先下载 |
| 🔁 智能重试 | 自动 3 次（指数退避），手动无限次 |
| 🔧 断点续传 | 服务重启自动恢复中断任务 |

## 配置参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `PORT` | 服务端口 | `8899` |
| `TZ` | 时区 | `Asia/Shanghai` |
| `NAS_MEDIA_DIR` | NAS 挂载点（容器内路径） | `/mnt/nas` |

## 数据持久化

- `~/.dl-manager/` — 数据库、日志、配置文件
- `~/dl-manager/tasks/` — 下载目录（TS 分片 + MP4）

## 技术栈

Python 3.10+ · FastAPI · Vue 3 (CDN) · yt-dlp · ffmpeg · SQLite · APScheduler · Docker