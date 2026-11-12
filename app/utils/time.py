from datetime import datetime, timezone
from zoneinfo import ZoneInfo

WEEKDAYS_ZH = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def utcnow() -> datetime:
    """数据库统一存储无时区的 UTC 时间，兼容 SQLite 与 PostgreSQL。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_now(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def format_local(dt: datetime) -> str:
    return f"{dt:%Y-%m-%d %H:%M}（{WEEKDAYS_ZH[dt.weekday()]}）"
