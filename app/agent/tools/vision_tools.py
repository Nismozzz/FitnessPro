from pydantic import BaseModel, Field

from app.agent.tools.base import BaseTool, ToolContext, ToolError
from app.vision.service import identify_equipment


class IdentifyEquipmentParams(BaseModel):
    image_id: str = Field(pattern=r"^[a-f0-9]{32}$", description="用户消息中图片附件的 id")


class IdentifyEquipmentTool(BaseTool):
    name = "identify_equipment"
    description = "识别用户上传的健身器械照片；仅支持器械，不支持体态诊断。返回候选和可见特征，先让用户确认，再从动作库推荐。"
    Params = IdentifyEquipmentParams
    timeout = 150

    async def run(self, params: IdentifyEquipmentParams, ctx: ToolContext):
        try:
            return await identify_equipment(ctx.user_id, params.image_id)
        except FileNotFoundError as exc:
            raise ToolError("图片不存在或已删除，请重新上传") from exc
        except ValueError as exc:
            raise ToolError("图片识别不可用、未返回有效结果或尚未配置，请检查配置或补拍后重试") from exc
