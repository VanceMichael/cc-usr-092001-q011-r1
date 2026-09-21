"""服务入口：启动程序衔接引擎 HTTP 服务。"""

import os

from .api import DEMO_UNIT_TOKENS, build_server
from .domain import time_utils
from .storage.repo import connect, init_schema


def health_payload() -> dict[str, str]:
    """返回可供运行环境探测的服务状态。"""
    return {"status": "ok"}


def seed_demo_tokens(database_path: str) -> None:
    """写入演示用单位/监督令牌；生产环境应由统一身份设施签发。"""
    conn = connect(database_path)
    try:
        init_schema(conn)
        at = time_utils.format_value(time_utils.now())
        for token, (unit, role) in DEMO_UNIT_TOKENS.items():
            conn.execute(
                "INSERT INTO unit_tokens(token, unit, role) VALUES(?,?,?) "
                "ON CONFLICT(token) DO UPDATE SET unit=excluded.unit, role=excluded.role",
                (token, unit, role))
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    database_path = os.environ.get("DATABASE_PATH", "data/app.sqlite3")
    port = int(os.environ.get("PORT", "8080"))
    seed_demo_tokens(database_path)
    server = build_server(database_path, port)
    print(f"运河纠纷程序衔接引擎已启动：0.0.0.0:{port}（数据库 {database_path}）")
    server.serve_forever()


if __name__ == "__main__":
    main()
