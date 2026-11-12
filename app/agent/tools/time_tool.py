from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.agent.tools.base import BaseTool, ToolContext
from app.config import settings
from app.utils.time import WEEKDAYS_ZH, local_now


class CurrentTimeTool(BaseTool):
    name = "get_current_time"
    description = "获取当前日期、星期以及本周（周一到周日）的日期范围，用于安排每周训练计划。"

    async def run(self, params: Any, ctx: ToolContext) -> Any:
        now = local_now(settings.timezone)
        monday = (now - timedelta(days=now.weekday())).date()
        return {
            "datetime": now.isoformat(timespec="minutes"),
            "date": now.date().isoformat(),
            "weekday": WEEKDAYS_ZH[now.weekday()],
            "timezone": settings.timezone,
            "this_week": [
                {"date": (monday + timedelta(days=i)).isoformat(), "weekday": WEEKDAYS_ZH[i]}
                for i in range(7)
            ],
        }
