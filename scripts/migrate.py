"""初始化项目的本地 SQLite 数据文件。"""

import os
from pathlib import Path

from src.schema import DDL, SCHEMA_VERSION
from src.store import Store


def main() -> None:
    database_path = Path(os.environ.get("DATABASE_PATH", "data/app.sqlite3"))
    database_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(str(database_path))
    store.close()
    print(f"数据库初始化完成：{database_path}（结构版本 {SCHEMA_VERSION}，共 {len(DDL)} 张表）")


if __name__ == "__main__":
    main()
