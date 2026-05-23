"""
订阅源 + 调度器配置管理
（注意：数据库初始化由 task_db.init() 统一处理，不要在这里重复 init）
"""
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path.home() / ".jable-dl-server" / "state.db"

def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ── 订阅源 CRUD ──
def add_source(name: str, url: str, feed_type: str = "jable") -> dict:
    conn = get_db()
    now = datetime.utcnow().isoformat() + "Z"
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

# ── 调度器配置 ──
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