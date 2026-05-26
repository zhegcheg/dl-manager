"""
SQLite 任务状态管理
"""
import os
import re
import sqlite3
import time
from pathlib import Path
from datetime import datetime
from typing import Optional

DB_PATH = Path.home() / ".dl-manager" / "state.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 初始化时设置一次 PRAGMA，后续连接只需继承
def _init_pragmas():
    """一次性设置 WAL 和 busy_timeout"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.close()

_init_pragmas()

def get_db():
    """获取 SQLite 连接（轻量级，PRAGMA 已在初始化时设置）"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def init():
    """创建所有数据库表"""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            m3u8_url TEXT NOT NULL,
            headers TEXT DEFAULT '',
            key TEXT DEFAULT '',
            iv TEXT DEFAULT '',
            status TEXT DEFAULT 'waiting',
            stage TEXT DEFAULT 'waiting',
            progress REAL DEFAULT 0,
            speed TEXT DEFAULT '',
            segments TEXT DEFAULT '',
            chunks TEXT DEFAULT '',
            move_speed TEXT DEFAULT '',
            move_elapsed TEXT DEFAULT '',
            error TEXT DEFAULT '',
            retry_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            file TEXT DEFAULT '',
            final_path TEXT DEFAULT '',
            priority INTEGER DEFAULT 0,
            retry_after TEXT DEFAULT ''
        )
    """)
    # 迁移：为旧数据库添加新字段（如果不存在）
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # 字段已存在
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN retry_after TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # 字段已存在
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN download_dir TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # 字段已存在
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN source_id INTEGER DEFAULT NULL")
    except sqlite3.OperationalError:
        pass  # 字段已存在
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN source_name TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # 字段已存在
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN video_url TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # 字段已存在
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN temp_dir TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # 字段已存在
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscription_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            feed_type TEXT DEFAULT 'webpage',
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            page_url_pattern TEXT DEFAULT '',
            title_selector TEXT DEFAULT '',
            m3u8_selector TEXT DEFAULT '',
            video_id_pattern TEXT DEFAULT '',
            referer TEXT DEFAULT '',
            headers TEXT DEFAULT '',
            key_selector TEXT DEFAULT '',
            iv_selector TEXT DEFAULT '',
            refresh_url_pattern TEXT DEFAULT '',
            poll_cron TEXT DEFAULT '0 4 * * *'
        )
    """)
    # 迁移：为旧数据库添加订阅源扩展字段
    _source_extra_cols = [
        "page_url_pattern", "title_selector", "m3u8_selector", "video_id_pattern",
        "referer", "headers", "key_selector", "iv_selector", "refresh_url_pattern",
    ]
    for col in _source_extra_cols:
        try:
            conn.execute(f"ALTER TABLE subscription_sources ADD COLUMN {col} TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    # 迁移：poll_cron 字段
    try:
        conn.execute("ALTER TABLE subscription_sources ADD COLUMN poll_cron TEXT DEFAULT '0 4 * * *'")
    except sqlite3.OperationalError:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduler_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS download_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS proxy_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # 默认日志配置表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS log_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # 默认调度配置
    now = datetime.utcnow().isoformat() + "Z"
    conn.execute(
        "INSERT OR IGNORE INTO scheduler_config (key, value, updated_at) VALUES (?, ?, ?)",
        ("rss_cron", "0 4 * * *", now)
    )
    conn.execute(
        "INSERT OR IGNORE INTO scheduler_config (key, value, updated_at) VALUES (?, ?, ?)",
        ("rss_enabled", "true", now)
    )
    # 默认下载配置
    conn.execute(
        "INSERT OR IGNORE INTO download_config (key, value, updated_at) VALUES (?, ?, ?)",
        ("download_dir", str(Path.home() / ".dl-manager" / "tasks"), now)
    )
    conn.execute(
        "INSERT OR IGNORE INTO download_config (key, value, updated_at) VALUES (?, ?, ?)",
        ("max_concurrent", "2", now)
    )
    conn.execute(
        "INSERT OR IGNORE INTO download_config (key, value, updated_at) VALUES (?, ?, ?)",
        ("thread_count", "8", now)
    )
    conn.execute(
        "INSERT OR IGNORE INTO download_config (key, value, updated_at) VALUES (?, ?, ?)",
        ("temp_dir", str(Path.home() / ".dl-manager" / "temp"), now)
    )
    conn.execute(
        "INSERT OR IGNORE INTO download_config (key, value, updated_at) VALUES (?, ?, ?)",
        ("move_to_nas", "true", now)
    )
    # 默认代理配置
    conn.execute(
        "INSERT OR IGNORE INTO proxy_config (key, value, updated_at) VALUES (?, ?, ?)",
        ("enabled", "false", now)
    )
    conn.execute(
        "INSERT OR IGNORE INTO proxy_config (key, value, updated_at) VALUES (?, ?, ?)",
        ("type", "http", now)
    )
    conn.execute(
        "INSERT OR IGNORE INTO proxy_config (key, value, updated_at) VALUES (?, ?, ?)",
        ("host", "", now)
    )
    conn.execute(
        "INSERT OR IGNORE INTO proxy_config (key, value, updated_at) VALUES (?, ?, ?)",
        ("port", "7890", now)
    )
    conn.commit()
    conn.close()

def sanitize_dirname(name: str, max_len: int = 80) -> str:
    """将订阅源名称转为安全的目录名
    规则：替换非法路径字符，截断长度，空白名回退为 'unnamed'
    """
    safe = re.sub(r'[\\/:*?"<>|]', '_', name).strip('. ')
    if not safe:
        return 'unnamed'
    return safe[:max_len]


def get_source_download_dir(source_id: int = None) -> str:
    """获取指定订阅源的下载目录路径
    若有 source_id 且源存在，返回 {download_dir}/{sanitized_source_name}
    否则返回 {download_dir}/_no_source
    """
    cfg = get_download_config()
    base = cfg.get("download_dir", str(Path.home() / ".dl-manager" / "tasks"))
    if source_id:
        source = get_source(source_id)
        if source:
            dirname = sanitize_dirname(source["name"])
            return str(Path(base) / dirname)
    return str(Path(base) / "_no_source")


def get_task_temp_dir() -> str:
    """获取全局临时目录路径（位于下载根目录下的 _temp 子目录）
    若 download_config 中显式配置了 temp_dir 则优先使用（兼容旧配置）
    """
    cfg = get_download_config()
    temp_dir = cfg.get("temp_dir", "")
    # 若用户显式配置了非默认 temp_dir，尊重用户配置
    default_temp = str(Path.home() / ".dl-manager" / "temp")
    if temp_dir and temp_dir != default_temp:
        return temp_dir
    # 否则使用 {download_dir}/_temp
    base = cfg.get("download_dir", str(Path.home() / ".dl-manager" / "tasks"))
    return str(Path(base) / "_temp")


def create_task(task_id: str, name: str, m3u8_url: str, headers: str = "", key: str = "", iv: str = "", priority: int = 0, download_dir: str = "", source_id: int = None, video_url: str = "") -> dict:
    now = datetime.utcnow().isoformat() + "Z"
    # 自动计算下载目录：优先使用传入的 download_dir，否则按订阅源计算
    if not download_dir:
        download_dir = get_source_download_dir(source_id)
    # 获取源名称（冗余存储，防止源被删后丢失路径信息）
    source_name = ""
    if source_id:
        source = get_source(source_id)
        if source:
            source_name = source["name"]
    # 计算临时目录
    temp_dir = get_task_temp_dir()
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO tasks (id, name, m3u8_url, headers, key, iv, status, stage, priority, download_dir, source_id, source_name, video_url, temp_dir, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'waiting', 'waiting', ?, ?, ?, ?, ?, ?, ?, ?)
        """, (task_id, name, m3u8_url, headers, key, iv, priority, download_dir, source_id, source_name, video_url, temp_dir, now, now))
        conn.commit()
    except sqlite3.IntegrityError:
        if key or iv:
            conn.execute("UPDATE tasks SET key=COALESCE(NULLIF(?,''),key), iv=COALESCE(NULLIF(?,''),iv), updated_at=? WHERE id=?",
                      (key, iv, now, task_id))
            conn.commit()
    conn.close()
    from app.events import mark_dirty
    mark_dirty()
    return get_task(task_id)

def get_task(task_id: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

# 允许的排序列白名单
_ORDER_BY_WHITELIST = {
    "id", "name", "m3u8_url", "headers", "key", "iv",
    "status", "stage", "progress", "speed", "segments",
    "chunks", "move_speed", "move_elapsed", "error",
    "retry_count", "created_at", "updated_at", "completed_at",
    "file", "final_path", "priority", "retry_after", "download_dir",
    "source_id", "source_name", "temp_dir",
}


def _validate_order_by(order_by: str) -> str:
    """校验 order_by 参数，防止 SQL 注入"""
    if not order_by:
        return "created_at DESC"
    parts = order_by.split(",")
    validated = []
    for part in parts:
        part = part.strip()
        # 格式: column DESC 或 column ASC
        tokens = part.split()
        if len(tokens) not in (1, 2):
            continue
        col = tokens[0]
        direction = tokens[1].upper() if len(tokens) == 2 else ""
        if col not in _ORDER_BY_WHITELIST:
            continue
        if direction and direction not in ("ASC", "DESC"):
            continue
        validated.append(part if direction else col)
    if not validated:
        return "created_at DESC"
    return ", ".join(validated)


def list_tasks(status: str = None, limit: int = 500, order_by: str = None) -> list:
    """
    列出任务，支持自定义排序。

    order_by: 排序字段，默认为 'created_at DESC'
              队列调度时使用 'priority DESC, created_at ASC'（高优先级优先，同优先级按创建时间）
    """
    order_by = _validate_order_by(order_by)
    conn = get_db()

    if status:
        rows = conn.execute(f"SELECT * FROM tasks WHERE status = ? ORDER BY {order_by} LIMIT ?",
                          (status, limit)).fetchall()
    else:
        rows = conn.execute(f"SELECT * FROM tasks ORDER BY {order_by} LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_task(task_id: str, reset_retry: bool = False, **fields):
    fields["updated_at"] = datetime.utcnow().isoformat() + "Z"
    # 任务完成时自动记录完成时间
    if fields.get("status") == "completed" and "completed_at" not in fields:
        fields["completed_at"] = datetime.utcnow().isoformat() + "Z"
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [task_id]
    for attempt in range(3):
        try:
            conn = get_db()
            conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
            if reset_retry:
                conn.execute("UPDATE tasks SET retry_count = 0 WHERE id = ?", (task_id,))
            conn.commit()
            conn.close()
            # 通知 SSE 事件总线：任务已变更
            from app.events import mark_dirty
            mark_dirty()
            return
        except sqlite3.OperationalError as e:
            conn.close()
            if attempt < 2 and "locked" in str(e):
                time.sleep(0.5 * (attempt + 1))
                continue
            raise

def delete_task(task_id: str):
    conn = get_db()
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    from app.events import mark_dirty
    mark_dirty()

def get_task_retry(task_id: str) -> int:
    conn = get_db()
    row = conn.execute("SELECT retry_count FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return row["retry_count"] if row else 0

def set_task_retry(task_id: str, count: int):
    conn = get_db()
    conn.execute("UPDATE tasks SET retry_count = ?, updated_at = ? WHERE id = ?",
                 (count, datetime.utcnow().isoformat() + "Z", task_id))
    conn.commit()
    conn.close()

def reset_task_for_auto_retry(task_id: str) -> bool:
    """自动重试：增加 retry_count，上限 3 次，超过则放弃"""
    count = get_task_retry(task_id) + 1
    if count > 3:
        return False
    conn = get_db()
    conn.execute("UPDATE tasks SET status='waiting', stage='waiting', progress=0, speed='', segments='', error='', retry_count=?, updated_at=? WHERE id=?",
                 (count, datetime.utcnow().isoformat() + "Z", task_id))
    conn.commit()
    conn.close()
    return True

def reset_task_for_manual_retry(task_id: str):
    """手动重试：重置 retry_count=0（不受次数限制），返回错误原因供用户参考"""
    t = get_task(task_id)
    error_msg = t.get("error", "未知错误") if t else "未知错误"
    conn = get_db()
    conn.execute("UPDATE tasks SET status='waiting', stage='waiting', progress=0, speed='', segments='', error='', retry_count=0, updated_at=? WHERE id=?",
                 (datetime.utcnow().isoformat() + "Z", task_id))
    conn.commit()
    conn.close()
    return error_msg

def get_task_log_path(task_id: str) -> Path:
    log_dir = Path.home() / ".dl-manager" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{task_id}.log"

def get_task_dir(task_id: str) -> Path:
    """获取任务的工作目录（用于 legacy TS 分片场景）
    优先使用任务记录中的 download_dir，fallback 到全局配置
    """
    task = get_task(task_id)
    if task and task.get("download_dir"):
        base_dir = task["download_dir"]
    else:
        cfg = get_download_config()
        base_dir = cfg.get("download_dir", str(Path.home() / ".dl-manager" / "tasks"))
    task_dir = Path(base_dir) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir

# ── 订阅源 ──
_SOURCE_EXTRA_COLS = [
    "page_url_pattern", "title_selector", "m3u8_selector", "video_id_pattern",
    "referer", "headers", "key_selector", "iv_selector", "refresh_url_pattern",
    "poll_cron",
]

def add_source(name: str, url: str, feed_type: str = "webpage", **extra) -> dict:
    now = datetime.utcnow().isoformat() + "Z"
    conn = get_db()
    cols = "name, url, feed_type, enabled, created_at, updated_at"
    vals = [name, url, feed_type, 1, now, now]
    for k in _SOURCE_EXTRA_COLS:
        if k in extra:
            cols += f", {k}"
            vals.append(extra[k])
    placeholders = ", ".join(["?"] * len(vals))
    cur = conn.execute(
        f"INSERT INTO subscription_sources ({cols}) VALUES ({placeholders})", vals
    )
    conn.commit()
    row = conn.execute("SELECT * FROM subscription_sources WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row)

def list_sources(enabled_only: bool = False) -> list:
    conn = get_db()
    if enabled_only:
        rows = conn.execute("SELECT * FROM subscription_sources WHERE enabled = 1 ORDER BY id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM subscription_sources ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_source(source_id: int) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM subscription_sources WHERE id = ?", (source_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def update_source(source_id: int, **fields) -> dict:
    conn = get_db()
    fields["updated_at"] = datetime.utcnow().isoformat() + "Z"
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [source_id]
    conn.execute(f"UPDATE subscription_sources SET {set_clause} WHERE id = ?", values)
    conn.commit()
    row = conn.execute("SELECT * FROM subscription_sources WHERE id = ?", (source_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def delete_source(source_id: int):
    conn = get_db()
    conn.execute("DELETE FROM subscription_sources WHERE id = ?", (source_id,))
    conn.commit()
    conn.close()

def get_scheduler_config() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT * FROM scheduler_config").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}

def set_scheduler_config(key: str, value: str):
    now = datetime.utcnow().isoformat() + "Z"
    conn = get_db()
    conn.execute(
        "INSERT INTO scheduler_config (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?",
        (key, value, now, value, now)
    )
    conn.commit()
    conn.close()

def get_download_config() -> dict:
    """获取下载配置（下载目录、最大并发数、线程数、NAS转移开关）"""
    defaults = {
        "download_dir": str(Path.home() / ".dl-manager" / "tasks"),
        "temp_dir": str(Path.home() / ".dl-manager" / "temp"),
        "max_concurrent": "2",
        "thread_count": "8",
        "move_to_nas": "true",
        "nas_dest_dir": os.getenv("NAS_MEDIA_DIR", "/mnt/fn-nas-imovie"),
    }
    conn = get_db()
    rows = conn.execute("SELECT * FROM download_config").fetchall()
    conn.close()
    result = defaults.copy()
    for r in rows:
        result[r["key"]] = r["value"]
    return result

def set_download_config(key: str, value: str):
    now = datetime.utcnow().isoformat() + "Z"
    conn = get_db()
    conn.execute(
        "INSERT INTO download_config (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?",
        (key, value, now, value, now)
    )
    conn.commit()
    conn.close()

def get_log_config() -> dict:
    """获取日志配置（日志级别、日志保存路径）"""
    defaults = {
        "log_level": "INFO",
        "log_path": str(Path.home() / ".dl-manager" / "logs" / "dl-manager.log"),
    }
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM log_config").fetchall()
        conn.close()
        result = defaults.copy()
        for r in rows:
            result[r["key"]] = r["value"]
        return result
    except Exception:
        conn.close()
        return defaults

def set_log_config(key: str, value: str):
    now = datetime.utcnow().isoformat() + "Z"
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO log_config (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?",
            (key, value, now, value, now)
        )
        conn.commit()
    except Exception:
        conn.close()
        return
    conn.close()

def get_proxy_config() -> dict:
    """获取代理配置（启用状态、类型、主机、端口、用户名、密码）"""
    defaults = {
        "enabled": "false",
        "type": "http",
        "host": "",
        "port": "7890",
        "username": "",
        "password": "",
    }
    conn = get_db()
    rows = conn.execute("SELECT * FROM proxy_config").fetchall()
    conn.close()
    result = defaults.copy()
    for r in rows:
        result[r["key"]] = r["value"]
    return result

def set_proxy_config(key: str, value: str):
    now = datetime.utcnow().isoformat() + "Z"
    conn = get_db()
    conn.execute(
        "INSERT INTO proxy_config (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?",
        (key, value, now, value, now)
    )
    conn.commit()
    conn.close()