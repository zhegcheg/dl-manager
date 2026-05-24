"""
Jable RSS 轮询 + 多订阅源支持

feed_type='jable': 从 Jable 页面提取 m3u8_url（无官方 RSS）
  - 从页面 <meta property="og:url"> 提取 video_id
  - 从页面 title 提取标题
  - 从页面 m3u8 URL 提取 m3u8_url + AES 密钥

feed_type='rss': 标准 RSS（6dylan6_jdpro 等）
  - enclosure 或 link 里的 URL 即为视频页 URL
  - 从视频页 URL 提取 video_id，再获取 m3u8_url
"""
import re
import time
import sqlite3
import subprocess
import threading
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

from app.db.database import get_db, get_task, create_task, update_task, get_proxy_config

# Jable 页面解析
JABLE_PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

REFERER = "Referer: https://jable.tv/\r\nUser-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36\r\n"

def _get_proxy_opener():
    """根据 proxy_config 返回配置了代理的 urllib opener，或 None"""
    try:
        cfg = get_proxy_config()
        if cfg.get("enabled") != "true":
            return None
        proxy_type = cfg.get("type", "http")
        host = cfg.get("host", "").strip()
        port = cfg.get("port", "7890").strip()
        if not host:
            return None
        proxy_url = f"{proxy_type}://{host}:{port}"
        proxy_handler = urllib.request.ProxyHandler({
            "http": proxy_url,
            "https": proxy_url,
        })
        return urllib.request.build_opener(proxy_handler)
    except Exception as e:
        print(f"[_get_proxy_opener] Failed to build proxy opener: {e}")
        return None

def fetch_jable_page(video_url: str, timeout: int = 15) -> str:
    """抓取 Jable 视频页面 HTML（使用 urllib 兼容 Windows/Linux）"""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        req = urllib.request.Request(video_url, headers=headers)
        opener = _get_proxy_opener()
        if opener:
            with opener.open(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8', errors='replace')
        else:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f"[fetch_jable_page] Failed to fetch {video_url}: {e}")
        return ""

def extract_jable_info(video_url: str) -> dict:
    """
    从 Jable 视频页面提取:
      - video_id
      - name (标题)
      - m3u8_url
      - key, iv (AES 加密密钥)
      - headers
    """
    html = fetch_jable_page(video_url)
    if not html:
        return {}

    # 提取 title
    title_match = re.search(r'<title>([^<]+)</title>', html)
    raw_title = title_match.group(1).strip() if title_match else ""
    # 清理标题：去掉后缀 " - Jable.TV..."
    name = re.sub(r'\s*-\s*Jable\.TV.*$', '', raw_title).strip()
    name = re.sub(r'\s*\|\s*免费高清AV在線看\s*\|\s*J片\s*AV看到飽\s*$', '', name).strip()
    # 去掉特殊字符
    name = re.sub(r'[\/\\\*,:<>"?|]', '', name).strip()

    # 提取 m3u8 URL（保留查询参数，某些 CDN token 是必需的）
    m3u8_match = re.search(r'https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*', html)
    m3u8_url = m3u8_match.group(0) if m3u8_match else ""
    m3u8_url = m3u8_url.rstrip('",\\')

    if not m3u8_url:
        # 尝试从 JSON 转义格式中找
        json_match = re.search(r'https?://[^"\'<>\s]+\\/[^"\'<>\s]*\.m3u8[^"\'<>\s]*', html)
        if json_match:
            m3u8_url = json_match.group(0).replace('\\/', '/').rstrip('",\\')

    if not m3u8_url:
        print(f"[extract_jable_info] no m3u8 found in {video_url}, html_len={len(html)}")
        return {}

    # 提取 AES 密钥 (从 m3u8 URL 所在 div 或 script 里找 key)
    # 简单策略：从页面里找 #EXT-X-KEY 或 key URI
    key_match = re.search(r'(?:crypto|key|iv)["\']?\s*[:=]\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
    key = ""
    iv = ""

    # 尝试从 mushroomtrack 域名相关脚本找 AES 密钥
    # 常见模式：acd275713d84de10.ts (key 文件名即 16 字节 hex)
    aes_key_match = re.search(r'acd([a-f0-9]{24})\.ts', html)
    if aes_key_match:
        full_hex = "acd" + aes_key_match.group(1)
        key = full_hex[:32]

    iv_match = re.search(r'0x([a-f0-9]{32})', html)
    if iv_match:
        iv = "0x" + iv_match.group(1)

    # video_id 从 URL 提取
    video_id_match = re.search(r'/videos/([^/]+)', video_url)
    video_id = video_id_match.group(1) if video_id_match else ""

    return {
        "id": video_id,
        "name": name or video_id,
        "m3u8_url": m3u8_url,
        "key": key,
        "iv": iv,
        "headers": REFERER
    }

def fetch_jable_m3u8_key(m3u8_url: str) -> tuple[str, str]:
    """从 m3u8 manifest 提取 AES-128 key URI 和 IV"""
    opener = None
    try:
        req = urllib.request.Request(m3u8_url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://jable.tv/",
        })
        opener = _get_proxy_opener()
        if opener:
            with opener.open(req, timeout=15) as resp:
                manifest = resp.read().decode('utf-8', errors='replace')
        else:
            with urllib.request.urlopen(req, timeout=15) as resp:
                manifest = resp.read().decode('utf-8', errors='replace')
    except Exception:
        manifest = ""

    key, iv = "", ""
    key_uri_match = re.search(r'URI="([^"]+)"', manifest)
    if key_uri_match:
        # 从 m3u8_url 提取 base path，构建完整 key URL
        base_url = m3u8_url.rsplit('/', 1)[0]
        key_url = base_url + '/' + key_uri_match.group(1)
        try:
            key_req = urllib.request.Request(key_url, headers={
                "Referer": "https://jable.tv/",
            })
            if opener:
                with opener.open(key_req, timeout=15) as resp:
                    key_hex = resp.read().hex()
                    key = key_hex[:32] if len(key_hex) >= 32 else key_hex
            else:
                with urllib.request.urlopen(key_req, timeout=15) as resp:
                    key_hex = resp.read().hex()
                    key = key_hex[:32] if len(key_hex) >= 32 else key_hex
        except Exception:
            pass

    iv_match = re.search(r'IV=0x([a-fA-F0-9]+)', manifest)
    if iv_match:
        iv = "0x" + iv_match.group(1)

    return key, iv

def poll_jable_source(source: dict) -> list[dict]:
    """
    轮询单个 Jable 订阅源（从页面抓取视频列表）
    source: {id, name, url, feed_type}
    返回: 新增任务列表
    """
    name = source["name"]
    url = source["url"]

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        })
        opener = _get_proxy_opener()
        if opener:
            with opener.open(req, timeout=35) as resp:
                content = resp.read().decode('utf-8', errors='replace')
        else:
            with urllib.request.urlopen(req, timeout=35) as resp:
                content = resp.read().decode('utf-8', errors='replace')
    except Exception:
        return []

    # 从页面提取视频链接列表
    video_urls = re.findall(r'href="(https://jable\.tv/videos/[^"]+)"', content)
    # 去重
    video_urls = list(dict.fromkeys(video_urls))

    new_tasks = []
    for video_url in video_urls:
        info = extract_jable_info(video_url)
        if not info or not info.get("m3u8_url"):
            continue

        # 获取 AES 密钥（如果页面没有）
        if not info.get("key"):
            key, iv = fetch_jable_m3u8_key(info["m3u8_url"])
            info["key"] = key
            info["iv"] = iv

        # 检查是否已存在（已完成/下载中/等待中 → 跳过）
        vid = info["id"]
        existing = get_task(vid)
        if existing and existing["status"] not in ("failed", "stopped"):
            continue  # 跳过已存在的非失败任务

        # 写入数据库（去重，已存在则更新 key/iv）
        task = create_task(vid, info["name"], info["m3u8_url"],
                          info["headers"], info["key"], info["iv"])
        if task["status"] in ("waiting",):  # 新建或重新等待的任务才算新增
            new_tasks.append(task)

    return new_tasks

def poll_all_sources() -> list[dict]:
    """轮询所有启用的订阅源"""
    from app.db.database import list_sources
    sources = list_sources(enabled_only=True)
    all_new = []
    for src in sources:
        try:
            new_tasks = poll_jable_source(src)
            all_new.extend(new_tasks)
        except Exception as e:
            print(f"[rss_poller] Error polling {src['name']}: {e}")
    return all_new

def already_downloaded(video_id: str) -> bool:
    """检查是否已完成或下载中"""
    task = get_task(video_id)
    return task is not None

def fetch_all_video_details(video_urls: list) -> list[dict]:
    """并发提取所有视频详情"""
    results = []
    def fetch_one(video_url):
        info = extract_jable_info(video_url)
        if info.get("m3u8_url"):
            if not info.get("key"):
                key, iv = fetch_jable_m3u8_key(info["m3u8_url"])
                info["key"] = key
                info["iv"] = iv
            return info
        return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_one, url): url for url in video_urls}
        for fut in futures:
            r = fut.result()
            if r:
                results.append(r)
    return results