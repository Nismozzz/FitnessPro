from datetime import date

from pydantic import Field

from app.agent.tools.base import BaseTool, EmptyParams, ToolError
from app.training import service
from app.training.schema import LogCreate, PlanDraft, Replacement, StrictModel
from app.training.stats import weekly_stats


def compact_plan(plan):
    if plan is None:
        return None
    if "id" not in plan:
        return plan
    return {"plan_id": plan["id"], "name": plan["name"], "week_start": plan["week_start"],
            "status": plan["status"], "version": plan["version"], "validation": plan["validation"],
            "days": [{"weekday": d["weekday"], "plan_day_id": d["id"], "focus": d["focus"],
                      "exercises": [{"exercise_id": e["exercise_id"], "name": e["exercise"].get("name_zh") or e["exercise"]["name"],
                                     **{k: e[k] for k in ("position", "sets", "reps_min", "reps_max", "duration_seconds", "rest_seconds", "target_weight_kg")}}
                                    for e in d["exercises"]]} for d in plan["days"]]}


async def business(awaitable):
    try:
        return await awaitable
    except service.TrainingError as exc:
        if exc.validation:
            return {"saved": False, **exc.validation}
        raise ToolError(str(exc)) from exc


class DraftWeeklyPlanTool(BaseTool):
    name = "draft_weekly_plan"
    description = "校验完整周计划，不保存。动作必须先检索。根据 errors 修改重试，warnings 展示给用户，不应伪造画像来通过校验。"
    Params = PlanDraft

    async def run(self, params, ctx):
        return await service.check_plan(ctx.user_id, params)


class SavePlanTool(BaseTool):
    name = "save_plan"
    description = "重新校验并保存计划草稿。不会激活，用户在计划页明确确认后激活。同样内容重试不会重复创建。"
    Params = PlanDraft
    side_effect = True

    async def run(self, params, ctx):
        return compact_plan(await business(service.save_plan(ctx.user_id, params)))


class GetActivePlanTool(BaseTool):
    name = "get_active_plan"
    description = "读取当前激活的训练计划，回答今天练什么前使用；注意 week_start，过期周计划不能当作本周计划。"
    Params = EmptyParams

    async def run(self, params, ctx):
        return compact_plan(await service.active_plan(ctx.user_id))


class ReplaceParams(Replacement):
    plan_id: str = Field(description="要修改的计划 id，来自 get_active_plan 或 save_plan")


class UpdatePlanExerciseTool(BaseTool):
    name = "update_plan_exercise"
    description = "为用户替换某个动作，重新校验全周并保存新版本草稿，原计划与记录不变，用户需在计划页确认激活。替换不自动修改档案的器械类别。"
    Params = ReplaceParams
    side_effect = True

    async def run(self, params, ctx):
        replacement = Replacement.model_validate(params.model_dump(exclude={"plan_id"}))
        return compact_plan(await business(service.replace_exercise(ctx.user_id, params.plan_id, replacement)))


class LogWorkoutTool(BaseTool):
    name = "log_workout"
    description = "将用户明确提供的训练结果记录为逐组数据。未知次数、重量或时间必须询问，不能编造。performed_at 需时区。同一次记录重试必须复用 idempotency_key，不同训练用新键。"
    Params = LogCreate
    side_effect = True

    async def run(self, params, ctx):
        return await business(service.log_workout(ctx.user_id, params))


class StatsParams(StrictModel):
    week_start: date = Field(description="查询周的周一")
    plan_id: str | None = Field(None, description="可选，指定同一周某个计划版本")


class GetTrainingStatsTool(BaseTool):
    name = "get_training_stats"
    description = "读取本人的周训练完成率、容量、肌群组数与估算1RM及PR，按返回口径解释，未记录不能声称已完成。"
    Params = StatsParams

    async def run(self, params, ctx):
        return await business(weekly_stats(ctx.user_id, params.week_start, params.plan_id))
