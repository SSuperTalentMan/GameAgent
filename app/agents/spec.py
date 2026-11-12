"""Spec Agent：原始需求 → 结构化 JSON 规格 + 可执行断言 + 机制依赖图。"""

from __future__ import annotations

import logging
import re

from app.agents.base import SkillAgent
from app.config import SPEC_PORT
from app.contracts import classify_assertion, validate_game_spec
from app.llm import LLMError, extract_json

logger = logging.getLogger(__name__)

SPEC_SYSTEM_PROMPT = """你是游戏规格设计专家。把用户的原始需求转化为结构化 JSON 规格。
只输出一个 JSON 对象，不要输出其他任何文本，字段如下：
{
  "name": "游戏名称",
  "game_type": "catch_collect",
  "core_loop": "核心玩法循环的一句话描述",
  "entities": ["实体1", "实体2"],
  "rules": ["规则1", "规则2"],
  "win_condition": "胜利条件的可执行描述",
  "lose_condition": "失败条件的可执行描述",
  "acceptance_assertions": [
    {"id": "A1", "text": "可在浏览器自动化验证的自然语言断言", "mechanism": "对应机制名"}
  ],
  "mechanisms": [
    {"id": "M1", "name": "机制名", "depends_on": ["被依赖机制id"]}
  ]
}
要求：
- acceptance_assertions 至少 3 条，必须覆盖：游戏能启动并进入进行中状态、
  核心玩法动作产生可观测状态变化、触发胜利条件并显示胜利文案；如有失败条件也覆盖。
- 断言 text 必须具体到可以用 Playwright 在页面里验证（例如"调用 start() 后进入 playing 状态"、
  "分数达到 10 时页面显示胜利文案'你赢了'"）。
- mechanisms 用 depends_on 表达依赖，例如 胜利判定 DEPENDS_ON 计分，计分 DEPENDS_ON 碰撞判定。"""

DEFAULT_ASSERTION_TEMPLATES = [
    ("游戏页面加载后出现画布，调用 start() 后进入 playing 状态", "启动与状态机"),
    ("按下左右方向键后玩家位置发生相应变化", "移动控制"),
    ("执行一次核心玩法动作后分数发生变化", "计分"),
    ("分数达到胜利阈值时进入 win 状态", "胜利判定"),
    ("进入 win 状态后页面显示胜利文案", "胜利文案展示"),
    ("漏接次数达到上限时进入 lose 状态", "失败判定"),
]


class SpecAgent(SkillAgent):
    name = "spec_agent"
    port = SPEC_PORT
    description = "把原始需求转为结构化 JSON 规格、可执行验收断言和机制依赖图"
    skills_spec = [
        {
            "id": "generate_spec",
            "name": "生成结构化规格",
            "description": "输入原始需求文本，输出含 core_loop/entities/rules/win/lose/"
            "acceptance_assertions 的 JSON 规格，断言落库，机制依赖写入图数据库",
        }
    ]

    def __init__(self, runtime) -> None:
        super().__init__()
        self._runtime = runtime

    async def handle(self, payload: dict) -> dict:
        text: str = payload.get("text") or payload.get("requirement") or ""
        task_id: str = payload["task_id"]
        if not text:
            raise ValueError("缺少需求文本 payload.text")

        rag_context = ""
        rag_titles: list[str] = []
        try:
            rag_context = self._runtime.retriever.context_for(
                f"游戏设计规格 {text}", top_k=3
            )
            rag_titles = [
                d.get("title", "")
                for d in self._runtime.retriever.retrieve(f"游戏设计 {text}", top_k=3)
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[spec] RAG 检索失败，跳过: %s", exc)

        spec: dict | None = None
        backend = "llm"
        if self._runtime.llm.available:
            user_prompt = f"用户原始需求：\n{text}\n"
            if rag_context:
                user_prompt += f"\n参考检索到的设计知识（可选择性采纳）：\n{rag_context}\n"
            try:
                raw = await self._runtime.llm.chat(
                    [
                        {"role": "system", "content": SPEC_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.7,  # Spec 偏高温度，鼓励澄清与多样方案
                    json_mode=True,
                )
                parsed = extract_json(raw)
                if isinstance(parsed, dict):
                    spec = parsed
            except LLMError as exc:
                logger.warning("[spec] LLM 生成失败，使用模板规格: %s", exc)
        if spec is None:
            backend = "template(LLM不可用)"
            spec = self._mock_spec(text)

        spec = self._normalize(spec, text)

        spec_id = await self._runtime.db.save_spec(task_id, spec.get("name", "未命名"), spec)
        await self._runtime.db.save_assertions(spec_id, spec["acceptance_assertions"])
        await self._runtime.graph.write_spec_dependencies(
            spec_id, spec.get("mechanisms", [])
        )
        logger.info(
            "[spec] 规格 %s 已落库 (%d 条断言, 图后端=%s)",
            spec_id, len(spec["acceptance_assertions"]), self._runtime.graph.backend,
        )
        return {
            "spec_id": spec_id,
            "spec": spec,
            "spec_backend": backend,
            "rag_titles": rag_titles,
            "summary": f"规格「{spec.get('name')}」已生成，含 {len(spec['acceptance_assertions'])} 条可执行断言",
        }

    # ---------- 规格规整 ----------
    def _normalize(self, spec: dict, raw_text: str) -> dict:
        spec = dict(spec)
        spec["name"] = str(spec.get("name") or "未命名游戏")[:80]
        spec["game_type"] = str(spec.get("game_type") or "catch_collect")
        spec["core_loop"] = str(spec.get("core_loop") or raw_text[:120] or "未定义")
        spec["entities"] = [str(v) for v in (spec.get("entities") or [])]
        spec["rules"] = [str(v) for v in (spec.get("rules") or [])]
        spec["win_condition"] = str(spec.get("win_condition") or "分数达到 10 获胜")
        spec["lose_condition"] = str(spec.get("lose_condition") or "漏接 3 次失败")
        spec["target"] = _bounded_number(spec["win_condition"], 10, 1, 100)
        spec["lose_limit"] = _bounded_number(spec["lose_condition"], 3, 1, 20)
        spec["win_text"] = _quoted_text(spec["win_condition"], "你赢了！")
        spec["lose_text"] = _quoted_text(spec["lose_condition"], "游戏结束")
        spec["origin_requirement"] = raw_text[:4000]

        assertions = spec.get("acceptance_assertions") or []
        norm = []
        for i, assertion in enumerate(assertions, 1):
            if isinstance(assertion, str):
                assertion = {"text": assertion}
            if not isinstance(assertion, dict):
                raise ValueError("验收断言必须是对象或字符串")
            text = str(assertion.get("text") or "")
            mechanism = str(assertion.get("mechanism") or "")
            norm.append({
                "id": str(assertion.get("id") or f"A{i}"),
                "text": text,
                "mechanism": mechanism,
                "check_type": classify_assertion(text, mechanism),
            })
        existing_types = {a["check_type"] for a in norm}
        for text, mechanism in DEFAULT_ASSERTION_TEMPLATES:
            check_type = classify_assertion(text, mechanism)
            if check_type not in existing_types:
                norm.append({
                    "id": f"A{len(norm) + 1}", "text": text,
                    "mechanism": mechanism, "check_type": check_type,
                })
                existing_types.add(check_type)
        spec["acceptance_assertions"] = norm

        norm_mechs = []
        for i, mechanism in enumerate(spec.get("mechanisms") or [], 1):
            if isinstance(mechanism, str):
                mechanism = {"name": mechanism}
            norm_mechs.append({
                "id": str(mechanism.get("id") or f"M{i}"),
                "name": str(mechanism.get("name") or f"M{i}"),
                "depends_on": [str(d) for d in (mechanism.get("depends_on") or [])],
            })
        if not norm_mechs:
            norm_mechs = [
                {"id": "M1", "name": "核心玩法", "depends_on": []},
                {"id": "M2", "name": "计分", "depends_on": ["M1"]},
                {"id": "M3", "name": "胜利判定", "depends_on": ["M2"]},
            ]
        spec["mechanisms"] = norm_mechs
        return validate_game_spec(spec)

    @staticmethod
    def _mock_spec(raw_text: str) -> dict:
        return {
            "name": "接金币小游戏",
            "core_loop": "玩家左右移动控制角色，接住下落的金币得分，漏接计入失败次数",
            "entities": ["player", "coin", "score", "miss_counter"],
            "rules": [
                "玩家可以左右移动",
                "接到金币加 1 分",
                "漏接一个金币失败次数 +1",
                "分数达到 10 获胜",
                "漏接 3 个金币失败",
            ],
            "win_condition": "分数达到 10，页面显示胜利文案'你赢了！'",
            "lose_condition": "漏接 3 个金币，页面显示失败文案'游戏结束'",
            "acceptance_assertions": [
                {"id": "A1", "text": "页面出现画布，调用 start() 后进入 playing 状态", "mechanism": "启动与状态机"},
                {"id": "A2", "text": "接到一个金币后分数增加 1", "mechanism": "计分"},
                {"id": "A3", "text": "分数达到 10 时进入 win 状态", "mechanism": "胜利判定"},
                {"id": "A4", "text": "进入 win 状态后页面显示胜利文案'你赢了！'", "mechanism": "胜利文案展示"},
                {"id": "A5", "text": "漏接 3 个金币后进入 lose 状态", "mechanism": "失败判定"},
            ],
            "mechanisms": [
                {"id": "M1", "name": "移动控制", "depends_on": []},
                {"id": "M2", "name": "碰撞判定", "depends_on": ["M1"]},
                {"id": "M3", "name": "计分", "depends_on": ["M2"]},
                {"id": "M4", "name": "胜利判定", "depends_on": ["M3"]},
                {"id": "M5", "name": "失败判定", "depends_on": ["M2"]},
            ],
            "origin_requirement": raw_text[:200],
        }


def _bounded_number(text: str, default: int, minimum: int, maximum: int) -> int:
    match = re.search(r"\d+", text)
    return max(minimum, min(maximum, int(match.group()) if match else default))


def _quoted_text(text: str, default: str) -> str:
    match = re.search(r"['\"“‘]([^'\"”’]{1,40})['\"”’]", text)
    return match.group(1) if match else default
