"""LLM-as-judge：让另一个大模型按评分标准（rubric）给行程打分

规则评估器只能检查"能写成代码的东西"。"这条路线顺不顺""符不符合带婴儿出行的需求"
这种需要常识和理解力的判断，交给 LLM 评委

LLM 评委的几个已知偏差，以及这里的应对：
  - 自我偏好：模型倾向于给自己（同一家族的模型）写的内容打高分 → 评委尽量用另一家的模型
  - 分数漂移：没有锚点时，"4 分"在不同调用里含义不同 → rubric 给每个分数写清楚标准
  - 长度偏好：越长的回答越容易被打高分 → 只给评委看精简后的行程，不给它看 description 长文
  - 随机性 → temperature=0，并且让它先写理由再打分
"""

import json

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.models.schemas import TripPlan, TripRequest

JUDGE_SYSTEM_PROMPT = """你是严格的旅行行程评审专家。你会看到用户的需求和一份行程，请按下面的标准逐项打 1-5 分。

【route 路线合理性】
5 = 每天的景点在同一片区域，顺序顺路，没有来回折返
3 = 大体合理，有一天出现明显的跨城区折返
1 = 景点随意堆砌，完全不考虑距离

【preference_fit 偏好匹配】
5 = 偏好标签和额外要求（free_text_input）全部体现，必去点都安排了
3 = 体现了一部分偏好，有明显遗漏
1 = 基本无视用户偏好，或者违反了用户明确提出的要求（比如用户不吃辣却安排了火锅）

【party_fit 同行人员适配】
5 = 完全考虑了同行人员（婴儿、儿童、老人）的体力和需求，节奏合适
3 = 没有明显问题，但也没有专门考虑
1 = 明显不适合（比如带婴儿一天安排 3 个需要大量步行的景点）

【feasibility 可执行性】
5 = 时间安排现实，游览时长合理，三餐时间地点可行
3 = 有一两处不太现实的安排
1 = 根本走不完

先写理由，再打分。只输出一个 JSON 对象：
{"reason": "100 字以内的理由", "route": 1-5, "preference_fit": 1-5, "party_fit": 1-5, "feasibility": 1-5}"""


class JudgeScore(BaseModel):
    reason: str
    route: int = Field(ge=1, le=5)
    preference_fit: int = Field(ge=1, le=5)
    party_fit: int = Field(ge=1, le=5)
    feasibility: int = Field(ge=1, le=5)

    def normalized(self) -> dict[str, float]:
        """1~5 分 → 0~1，和规则评估器的分数放在同一个尺度上"""
        return {f"judge_{k}": (getattr(self, k) - 1) / 4 for k in ("route", "preference_fit", "party_fit", "feasibility")}


def plan_digest(plan: TripPlan) -> list[dict]:
    """给评委看的精简行程：只保留判断需要的信息（应对长度偏好，也省 token）"""
    return [
        {
            "date": day.date,
            "hotel": day.hotel.name if day.hotel else None,
            "attractions": [{"name": a.name, "address": a.address, "minutes": a.visit_duration} for a in day.attractions],
            "meals": [f"{m.type}:{m.name}" for m in day.meals],
        }
        for day in plan.days
    ]


async def judge_plan(llm: BaseChatModel, request: TripRequest, plan: TripPlan) -> JudgeScore:
    user = (
        f"【用户需求】\n{request.model_dump_json(exclude={'start_date', 'end_date'})}\n\n"
        f"【行程】\n{json.dumps(plan_digest(plan), ensure_ascii=False)}"
    )
    response = await llm.ainvoke([SystemMessage(JUDGE_SYSTEM_PROMPT), HumanMessage(user)], temperature=0)
    text = response.text
    # 评委的输出同样是"不可信输入"，复用第 7 课的思路：抠出 JSON 再用 Pydantic 校验
    start, end = text.find("{"), text.rfind("}")
    return JudgeScore.model_validate_json(text[start : end + 1])
