from __future__ import annotations

from app.agent.tools.base import BaseTool


def build_default_tools() -> list[BaseTool]:
    from app.agent.tools.exercise_tools import GetExerciseDetailTool, SearchExercisesTool
    from app.agent.tools.memory_tools import ConfirmMemoryTool, ForgetMemoryTool, SaveMemoryTool
    from app.agent.tools.profile_tools import GetUserProfileTool, UpdateUserProfileTool
    from app.agent.tools.time_tool import CurrentTimeTool

    return [
        SearchExercisesTool(),
        GetExerciseDetailTool(),
        GetUserProfileTool(),
        UpdateUserProfileTool(),
        SaveMemoryTool(),
        ConfirmMemoryTool(),
        ForgetMemoryTool(),
        CurrentTimeTool(),
    ]
