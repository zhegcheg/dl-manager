"""
SQLite 任务状态管理
"""
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Optional

DB_PATH = Path.home() / ".jable-dl-server" / "state.db"

def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
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
            final_path TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscription_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            feed_type TEXT DEFAULT 'jable',
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
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
        ("download_dir", str(Path.home() / ".jable-dl-server" / "tasks"), now)
    )
    conn.execute(
        "INSERT OR IGNORE INTO download_config (key, value, updated_at) VALUES (?, ?, ?)",
        ("max_concurrent", "2", now)
    )
    conn.execute(
        "INSERT OR IGNORE INTO download_config (key, value, updated_at) VALUES (?, ?, ?)",
        ("thread_count", "8", now)
    )
    conn.commit()
    conn.close()

def create_task(task_id: str, name: str, m3u8_url: str, headers: str = "", key: str = "", iv: str = "") -> dict:
    now = datetime.utcnow().isoformat() + "Z"
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO tasks (id, name, m3u8_url, headers, key, iv, status, stage, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'waiting', 'waiting', ?, ?)
        """, (task_id, name, m3u8_url, headers, key, iv, now, now))
        conn.commit()
    except sqlite3.IntegrityError:
        if key or iv:
            conn.execute("UPDATE tasks SET key=COALESCE(NULLIF(?,''),key), iv=COALESCE(NULLIF(?,''),iv), updated_at=? WHERE id=?",
                      (key, iv, now, task_id))
            conn.commit()
    conn.close()
    return get_task(task_id)

def get_task(task_id: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def list_tasks(status: str = None, limit: int = 500) -> list:
    conn = get_db()
    if status:
        rows = conn.execute("SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                          (status, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_task(task_id: str, reset_retry: bool = False, **fields):
    fields["updated_at"] = datetime.utcnow().isoformat() + "Z"
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
            return
        except sqlite3.OperationalError as e:
            conn.close()
            if attempt < 2 and "locked" in str(e):
                import time; time.sleep(0.5 * (attempt + 1))
                continue
            raise

def delete_task(task_id: str):
    conn = get_db()
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

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

def reset_task_for_retry(task_id: str) -> bool:
    """Reset task to waiting status and increment retry_count. Returns False if max retries exceeded."""
    count = get_task_retry(task_id) + 1
    if count > 3:
        return False
    conn = get_db()
    conn.execute("UPDATE tasks SET status='waiting', stage='waiting', progress=0, speed='', segments='', error='', retry_count=?, updated_at=? WHERE id=?",
                 (count, datetime.utcnow().isoformat() + "Z", task_id))
    conn.commit()
    conn.close()
    return True

def get_task_log_path(task_id: str) -> Path:
    log_dir = Path.home() / ".jable-dl-server" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{task_id}.log"

def get_task_dir(task_id: str) -> Path:
    cfg = get_download_config()
    base_dir = cfg.get("download_dir", str(Path.home() / ".jable-dl-server" / "tasks"))
    task_dir = Path(base_dir) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir

# ── 订阅源 ──
def add_source(name: str, url: str, feed_type: str = "jable") -> dict:
    now = datetime.utcnow().isoformat() + "Z"
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO subscription_sources (name, url, feed_type, enabled, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
        (name, url, feed_type, now, now)
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
    """获取下载配置（下载目录、最大并发数、线程数）"""
    defaults = {
        "download_dir": str(Path.home() / ".jable-dl-server" / "tasks"),
        "max_concurrent": "2",
        "thread_count": "8",
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

init()