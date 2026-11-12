from __future__ import annotations

from typing import Any

from app.agent.tools.base import BaseTool, ToolContext, ToolError
from app.users.profile import ProfileData, get_profile, update_profile


class GetUserProfileTool(BaseTool):
    name = "get_user_profile"
    description = "读取当前用户的训练画像（目标、训练水平、可训练天数、器械、伤病等）。系统提示词中已有画像，通常无需调用，除非刚刚更新过。"

    async def run(self, params: Any, ctx: ToolContext) -> Any:
        return await get_profile(ctx.user_id)


class UpdateUserProfileTool(BaseTool):
    name = "update_user_profile"
    description = (
        "更新当前用户的训练画像。只填写用户在对话中明确提供的字段；"
        "equipment 与 injuries 会整体替换原列表，需要追加时请带上原有内容。"
    )
    Params = ProfileData
    side_effect = True

    async def run(self, params: ProfileData, ctx: ToolContext) -> Any:
        if not params.model_dump(exclude_none=True):
            raise ToolError("没有需要更新的字段")
        profile = await update_profile(ctx.user_id, params)
        return {"updated_fields": sorted(params.model_dump(exclude_none=True)), "profile": profile}
