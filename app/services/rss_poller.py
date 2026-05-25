"""
通用订阅源轮询引擎

feed_type='webpage': 从网页提取 m3u8（通过订阅源配置的解析规则）
feed_type='rss': 标准 RSS 订阅
feed_type='m3u8_direct': 直接 m3u8 URL（无需轮询）
"""
import re
import time
import sqlite3
import threading
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime

from app.db.database import get_db, get_task, create_task, update_task, get_proxy_config

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}


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


def _build_headers(referer: str = "", custom_headers: str = "") -> dict:
    """合并默认 headers + Referer + 自定义 headers"""
    headers = dict(DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer
    if custom_headers:
        for line in custom_headers.split("\n"):
            line = line.strip()
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()
    return headers


def _fetch_url(url: str, referer: str = "", custom_headers: str = "", timeout: int = 15) -> str:
    """通用 URL 请求，支持代理"""
    headers = _build_headers(referer, custom_headers)
    try:
        req = urllib.request.Request(url, headers=headers)
        opener = _get_proxy_opener()
        if opener:
            with opener.open(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8', errors='replace')
        else:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f"[_fetch_url] Failed to fetch {url}: {e}")
        return ""


def _extract_text(html: str, pattern: str) -> str:
    """用正则从 HTML 提取第一个捕获组"""
    if not pattern or not html:
        return ""
    m = re.search(pattern, html)
    return m.group(1).strip() if m else ""


def _clean_title(title: str) -> str:
    """通用标题清理"""
    if not title:
        return ""
    # 去掉末尾的 " - SiteName" 或 " | SiteName"（要求分隔符前后有空格，避免误删 HMN-853 这种番号）
    name = re.sub(r'\s+[-|]\s+[^|]+$', '', title).strip()
    # 如果上面没匹配到，再尝试末尾单独的 " |" 分隔
    if name == title.strip():
        name = re.sub(r'\s*\|\s*[^|]+$', '', title).strip()
    # 去掉文件系统不友好字符
    name = re.sub(r'[\/\\\*,:<>"?|]', '', name).strip()
    return name


def _extract_m3u8(html: str, pattern: str = "") -> str:
    """从页面提取 m3u8 URL，支持自定义正则"""
    if pattern:
        m = re.search(pattern, html)
        if m:
            # 如果有捕获组，取第一个捕获组；否则取完整匹配
            return (m.group(1) if m.lastindex else m.group(0)).rstrip('",\\')
    # 通用 fallback
    m = re.search(r'https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*', html)
    if m:
        return m.group(0).rstrip('",\\')
    # JSON 转义格式
    m = re.search(r'https?://[^"\'<>\s]+\\/[^"\'<>\s]*\.m3u8[^"\'<>\s]*', html)
    if m:
        return m.group(0).replace('\\/', '/').rstrip('",\\')
    return ""


def _extract_video_id(video_url: str, pattern: str = "") -> str:
    """从视频 URL 提取 video_id"""
    if pattern:
        m = re.search(pattern, video_url)
        if m:
            return m.group(1)
    # fallback: 取 URL 最后一段路径
    from urllib.parse import urlparse
    parsed = urlparse(video_url)
    parts = [p for p in parsed.path.strip('/').split('/') if p]
    return parts[-1] if parts else video_url


def _extract_aes_from_manifest(m3u8_url: str, referer: str = "") -> tuple:
    """从 m3u8 manifest 提取 AES-128 key 和 IV"""
    manifest = _fetch_url(m3u8_url, referer=referer)
    if not manifest:
        return "", ""

    key, iv = "", ""
    key_uri_match = re.search(r'URI="([^"]+)"', manifest)
    if key_uri_match:
        base_url = m3u8_url.rsplit('/', 1)[0]
        key_url = base_url + '/' + key_uri_match.group(1)
        try:
            # 使用 binary 模式读取 key 文件
            headers = _build_headers(referer)
            key_req = urllib.request.Request(key_url, headers=headers)
            opener = _get_proxy_opener()
            if opener:
                with opener.open(key_req, timeout=15) as resp:
                    key_bytes = resp.read()
                    key = key_bytes.hex()[:32]
            else:
                with urllib.request.urlopen(key_req, timeout=15) as resp:
                    key_bytes = resp.read()
                    key = key_bytes.hex()[:32]
        except Exception:
            pass

    iv_match = re.search(r'IV=0x([a-fA-F0-9]+)', manifest)
    if iv_match:
        iv = "0x" + iv_match.group(1)

    return key, iv


def _extract_aes_key(html: str, m3u8_url: str, source: dict = None) -> tuple:
    """提取 AES 密钥，优先用页面正则，fallback 到 manifest"""
    cfg = source or {}
    key, iv = "", ""

    key_pattern = cfg.get("key_selector", "")
    iv_pattern = cfg.get("iv_selector", "")

    if key_pattern:
        m = re.search(key_pattern, html)
        if m:
            key = m.group(1)
    if iv_pattern:
        m = re.search(iv_pattern, html)
        if m:
            raw = m.group(1)
            iv = raw if raw.startswith("0x") else "0x" + raw

    # fallback: 从 m3u8 manifest 获取
    if not key and m3u8_url:
        key, iv = _extract_aes_from_manifest(m3u8_url, referer=cfg.get("referer", ""))

    return key, iv


def resolve_video_info(video_url: str, source_config: dict = None, title: str = "") -> dict:
    """
    通用入口：从视频页面 URL 获取完整的下载信息。

    source_config: 订阅源配置 dict，包含解析规则。
                   如果没有传，则使用通用规则（仅提取 m3u8）。
    title: 可选，外部提供的标题（如 RSS item 标题），优先使用。

    返回: {id, name, m3u8_url, key, iv, headers} 或 {}
    """
    cfg = source_config or {}
    referer = cfg.get("referer", "")
    custom_headers = cfg.get("headers", "")

    html = _fetch_url(video_url, referer=referer, custom_headers=custom_headers)
    if not html:
        return {}

    # 标题：优先使用外部传入的（如 RSS 标题），否则从页面提取
    if title:
        name = _clean_title(title)
    else:
        title_pattern = cfg.get("title_selector", r'<title>([^<]+)</title>')
        raw_title = _extract_text(html, title_pattern)
        name = _clean_title(raw_title)

    # 提取 video_id
    video_id = _extract_video_id(video_url, cfg.get("video_id_pattern", ""))

    # 提取 m3u8
    m3u8_url = _extract_m3u8(html, cfg.get("m3u8_selector", ""))
    if not m3u8_url:
        print(f"[resolve_video_info] no m3u8 found in {video_url}, html_len={len(html)}")
        return {}

    # 提取 AES 密钥
    key, iv = _extract_aes_key(html, m3u8_url, source=cfg)

    # 构建 headers 字符串
    headers_str = ""
    if referer:
        headers_str = f"Referer: {referer}"

    return {
        "id": video_id,
        "name": name or video_id,
        "m3u8_url": m3u8_url,
        "key": key,
        "iv": iv,
        "headers": headers_str,
    }


def poll_webpage_source(source: dict) -> list:
    """
    轮询单个网页订阅源（通过 source 中的规则解析）
    source: {id, name, url, feed_type, page_url_pattern, referer, ...}
    返回: 新增任务列表
    """
    url = source["url"]
    referer = source.get("referer", "")
    page_url_pattern = source.get("page_url_pattern", "")

    content = _fetch_url(url, referer=referer, timeout=35)
    if not content:
        return []

    # 从页面提取视频链接列表
    if page_url_pattern:
        video_urls = re.findall(page_url_pattern, content)
    else:
        # 通用 fallback: 提取所有含视频特征的链接
        video_urls = re.findall(r'href="(https?://[^"]+)"', content)
    video_urls = list(dict.fromkeys(video_urls))

    new_tasks = []
    for video_url in video_urls:
        info = resolve_video_info(video_url, source_config=source)
        if not info:
            continue

        vid = info["id"]
        existing = get_task(vid)
        if existing and existing["status"] not in ("failed", "stopped"):
            continue  # 跳过已存在的非失败任务

        task = create_task(vid, info["name"], info["m3u8_url"],
                          info["headers"], info["key"], info["iv"],
                          source_id=source.get("id"))
        if task["status"] in ("waiting",):
            new_tasks.append(task)

    return new_tasks


def poll_rss_source(source: dict) -> list:
    """轮询标准 RSS 订阅源"""
    url = source["url"]

    content = _fetch_url(url, timeout=35)
    if not content:
        return []

    new_tasks = []
    try:
        root = ET.fromstring(content)
        for item in root.iter("item"):
            link = ""
            enclosure = item.find("enclosure")
            if enclosure is not None:
                link = enclosure.get("url", "")
            if not link:
                link_el = item.find("link")
                if link_el is not None:
                    link = link_el.text or ""
            if not link:
                continue

            # 从 RSS item 提取标题（通常比页面 <title> 更干净）
            rss_title = ""
            title_el = item.find("title")
            if title_el is not None:
                rss_title = (title_el.text or "").strip()

            info = resolve_video_info(link, source_config=source, title=rss_title)
            if not info:
                continue

            vid = info["id"]
            existing = get_task(vid)
            if existing and existing["status"] not in ("failed", "stopped"):
                continue

            task = create_task(vid, info["name"], info["m3u8_url"],
                              info["headers"], info["key"], info["iv"],
                              source_id=source.get("id"))
            if task["status"] in ("waiting",):
                new_tasks.append(task)
    except ET.ParseError as e:
        print(f"[poll_rss_source] XML parse error for {url}: {e}")

    return new_tasks


def poll_all_sources() -> list:
    """轮询所有启用的订阅源，根据 feed_type 分发"""
    from app.db.database import list_sources
    sources = list_sources(enabled_only=True)
    all_new = []
    for src in sources:
        try:
            feed_type = src.get("feed_type", "webpage")
            if feed_type == "rss":
                new_tasks = poll_rss_source(src)
            elif feed_type == "m3u8_direct":
                # 直接 m3u8 不需要轮询
                continue
            else:
                new_tasks = poll_webpage_source(src)
            all_new.extend(new_tasks)
        except Exception as e:
            print(f"[rss_poller] Error polling {src['name']}: {e}")
    return all_new


def already_downloaded(video_id: str) -> bool:
    """检查是否已完成或下载中"""
    task = get_task(video_id)
    return task is not None


def fetch_all_video_details(video_urls: list, source_config: dict = None) -> list:
    """并发提取所有视频详情"""
    from concurrent.futures import ThreadPoolExecutor
    results = []

    def fetch_one(video_url):
        info = resolve_video_info(video_url, source_config=source_config)
        return info if info else None

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_one, url): url for url in video_urls}
        for fut in futures:
            r = fut.result()
            if r:
                results.append(r)
    return results
