"""时间工具：统一使用带偏移量的 ISO 8601，禁止用到达时间覆盖发生时间。"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CANAL_TZ = ZoneInfo("Asia/Shanghai")


def now() -> datetime:
    """当前时间（带 +08:00 偏移）。"""
    return datetime.now(CANAL_TZ)


def parse(value: str) -> datetime:
    """解析 ISO 8601；无时区的旧数据按东八区处理，避免朴素时间参与比较。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CANAL_TZ)
    return dt


def format_value(dt: datetime) -> str:
    """序列化为带偏移量的 ISO 8601 字符串。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CANAL_TZ)
    return dt.isoformat()


def utc_stamp(dt: datetime) -> str:
    """以 UTC 表达的稳定排序键。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CANAL_TZ)
    return dt.astimezone(timezone.utc).isoformat()
