# DL Manager

视频下载管理工具，基于 N_m3u8DL-RE 实现 m3u8 视频下载、合并、自动转移到 NAS。

## 功能

- 订阅源管理（Jable TV 等）
- 批量下载队列（可配置并发数）
- 实时进度追踪（速度、百分比、分片数）
- 下载完成后自动合并并转移到 NAS
- 网页内播放已下载视频
- 列表/卡片双视图
- 定时 RSS 轮询

## 快速开始

```bash
pip install -r requirements.txt
python3 server.py
```

访问 http://localhost:8899

## 技术栈

- 后端：Python + FastAPI + SQLite
- 前端：Vue 3 + 原生 HTML/CSS
- 下载引擎：N_m3u8DL-RE
