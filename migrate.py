"""
数据迁移脚本：.jable-dl-server → .dl-manager
运行一次即可：python migrate.py
"""
import shutil
from pathlib import Path

OLD_DIR = Path.home() / ".jable-dl-server"
NEW_DIR = Path.home() / ".dl-manager"


def migrate():
    if OLD_DIR.exists() and not NEW_DIR.exists():
        print(f"迁移数据目录: {OLD_DIR} → {NEW_DIR}")
        shutil.copytree(str(OLD_DIR), str(NEW_DIR))
        print("迁移完成！旧目录保留，确认无误后可手动删除。")
    elif NEW_DIR.exists():
        print(f"新目录已存在: {NEW_DIR}，跳过迁移。")
    else:
        print(f"旧目录不存在: {OLD_DIR}，无需迁移。")


if __name__ == "__main__":
    migrate()
