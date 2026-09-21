"""初始化本地 SQLite 数据库并写入演示令牌与共建单位种子数据。"""

import os
from pathlib import Path

from src.api import DEMO_UNIT_TOKENS
from src.domain import time_utils
from src.storage.repo import connect, init_schema


def main() -> None:
    database_path = Path(os.environ.get("DATABASE_PATH", "data/app.sqlite3"))
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(database_path)
    try:
        version = init_schema(conn)
        at = time_utils.format_value(time_utils.now())
        for token, (unit, role) in DEMO_UNIT_TOKENS.items():
            conn.execute(
                "INSERT INTO unit_tokens(token, unit, role) VALUES(?,?,?) "
                "ON CONFLICT(token) DO UPDATE SET unit=excluded.unit, role=excluded.role",
                (token, unit, role))
        conn.commit()
    finally:
        conn.close()
    print(f"数据库初始化完成（schema v{version}）：{database_path}")
    print("演示单位令牌：")
    for token, (unit, role) in DEMO_UNIT_TOKENS.items():
        print(f"  {unit:<8} {role:<10} {token}")
    print("当事方令牌在案件登记成功后随响应返回。")


if __name__ == "__main__":
    main()
