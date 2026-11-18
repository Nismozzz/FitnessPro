"""只读加载应用自带技能；不接受文件路径或外部技能内容。"""

from app.agent.skills import load_training_plan_skill
from app.agent.tools.base import BaseTool, ToolContext


LOAD_TRAINING_PLAN_SKILL = "load_training_plan_skill"
PLAN_DESIGN_TOOLS = frozenset({"draft_weekly_plan", "update_plan_exercise", "suggest_next_week"})


class LoadTrainingPlanSkillTool(BaseTool):
    name = LOAD_TRAINING_PLAN_SKILL
    description = (
        "当用户希望制定、评估或调整训练计划时，加载训练规划技能全文。"
        "结合对话判断继续规划、补充条件、确认或修改安排等隐含意图；"
        "单纯知识问答、动作讲解、查看计划或记录训练时不调用。"
        "每个需要规划的对话轮次先加载一次，再拟定计划或调用计划编排工具。"
    )

    async def run(self, params, ctx: ToolContext):
        return {"name": "training-plan-design", "body": load_training_plan_skill()}
