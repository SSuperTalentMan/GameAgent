"""Player Simulation Agent（文档 9.2 ⑥）：

- 输入：构建产物 + 玩家策略模板（Milvus Lite player_strategy_templates 集合检索）
- 执行：Playwright 多 context 并发，asyncio.Semaphore 控并发上限（默认 10）
- 决策：规则策略为主；LLM 仅按概率在关键决策点介入（预算护栏，成本控制）
- 输出：player_events 行为日志写入 MySQL；Redis 维护会话状态与活跃池
- 只产生数据，不做"好不好"的判断（判断归 Market Validation Agent）
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import uuid
from pathlib import Path

from app.agents.base import SkillAgent
from app import config
from app.config import GENERATED_DIR, PLAYER_SIM_PORT
from app.storage.kv import KVStore

logger = logging.getLogger(__name__)

STRATEGY_COLLECTION = "player_strategy_templates"

DEFAULT_STRATEGY = {
    "name": "casual",
    "description": "默认休闲玩家：缓慢移动，中高频核心动作，偶尔放弃",
    "move_interval": 0.8,
    "collect_prob": 0.45,
    "session_timeout": 25,
    "llm_decision_prob": config.SIM_LLM_DECISION_PROBABILITY,
}

# 玩家画像：只用一个策略会让实验结果退化——12 个会话全胜、留存 100%，
# 那不是"玩家喜欢"，而是"模拟器参数写死了"。按画像轮转分配，
# 让失败、中途放弃、回访与否都由行为概率产生，指标才有区分度。
PERSONAS: dict[str, dict] = {
    "casual": {
        "description": "休闲玩家：节奏中等，能通关，愿不愿意再来一半一半",
        "move_interval": 0.8, "collect_prob": 0.45, "session_timeout": 25,
        "return_prob": 0.55, "quit_prob": 0.10,
    },
    "impatient": {
        "description": "急性子：节奏慢、耐心差，进度落后就退出，很少回访",
        "move_interval": 1.3, "collect_prob": 0.30, "session_timeout": 12,
        "return_prob": 0.20, "quit_prob": 0.45,
    },
    "skilled": {
        "description": "熟练玩家：操作快、命中高，基本能通关，回访意愿强",
        "move_interval": 0.45, "collect_prob": 0.75, "session_timeout": 30,
        "return_prob": 0.75, "quit_prob": 0.02,
    },
    "explorer": {
        "description": "探索型：到处移动、很少认真收集，分数低，容易中途离开",
        "move_interval": 0.6, "collect_prob": 0.15, "session_timeout": 20,
        "return_prob": 0.40, "quit_prob": 0.30,
    },
}


class PlayerSimulationAgent(SkillAgent):
    name = "player_sim_agent"
    port = PLAYER_SIM_PORT
    description = "多 context 并发模拟真实玩家行为，产出 player_events 行为日志"
    skills_spec = [
        {
            "id": "simulate_players",
            "name": "并发玩家模拟",
            "description": "输入构建入口与策略模板名，按并发上限模拟 N 玩家 × M 会话，"
            "行为事件落库并回写 Redis 会话状态",
        }
    ]

    def __init__(self, runtime) -> None:
        super().__init__()
        self._runtime = runtime
        self._llm_budget = 10  # 关键决策点 LLM 调用预算（成本护栏）

    async def handle(self, payload: dict) -> dict:
        task_id: str = payload["task_id"]
        entry: str = payload["entry_path"]
        strategy_name: str = payload.get("strategy", "casual")
        num_players = int(payload.get("num_players", config.SIM_CONCURRENCY))
        num_players = max(1, min(num_players, config.SIM_CONCURRENCY))
        sessions_per = int(
            payload.get("sessions_per_player", config.SIM_SESSIONS_PER_PLAYER)
        )

        strategy = await self._load_strategy_template(strategy_name)
        url = Path(entry).resolve().as_uri()

        from playwright.async_api import async_playwright

        events: list[dict] = []
        status_counts: dict[str, int] = {}
        self._llm_budget = 10
        sem = asyncio.Semaphore(config.SIM_CONCURRENCY)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                players = [
                    self._play_player(
                        p, browser, sem, url, task_id, f"player_{i + 1:02d}",
                        strategy, sessions_per, events, status_counts, i,
                    )
                    for i in range(num_players)
                ]
                await asyncio.gather(*players)
            finally:
                await browser.close()

        written = await self._runtime.db.insert_player_events(events)
        logger.info(
            "[player_sim] %d 玩家 × %d 会话 → %d 事件落库 (策略=%s)",
            num_players, sessions_per, written, strategy["name"],
        )
        return {
            "players": num_players,
            "sessions_expected": num_players * sessions_per,
            "strategy": strategy["name"],
            "strategy_params": {k: v for k, v in strategy.items() if k != "description"},
            "total_events": written,
            "by_status": status_counts,
            "llm_decisions_used": 10 - self._llm_budget,
            "data_source": "synthetic",
            "seed": config.SIM_SEED,
            "summary": f"合成模拟 {num_players} 玩家 × {sessions_per} 会话，产出 {written} 条行为事件",
        }

    # ---------- 策略模板：Milvus 检索 + 规则参数解析 ----------
    async def _load_strategy_template(self, strategy_name: str) -> dict:
        template = dict(DEFAULT_STRATEGY)
        template["name"] = strategy_name
        try:
            qvec = self._runtime.embedder.encode_one(f"玩家策略模板 {strategy_name} 休闲 行为节奏")
            hits = self._runtime.vector.search(qvec, 1, collection=STRATEGY_COLLECTION)
            if hits:
                text = hits[0]["text"]
                template["description"] = text.splitlines()[0][:120]
                for m in re.finditer(r"(\w+)=([\d.]+)", text):
                    key, val = m.group(1), float(m.group(2))
                    if key in ("move_interval", "collect_prob", "session_timeout"):
                        template[key] = val
                logger.info(
                    "[player_sim] 命中策略模板 '%s' (backend=%s): %s",
                    hits[0].get("title", ""), self._runtime.vector.backend, template["description"],
                )
                return template
        except Exception as exc:  # noqa: BLE001
            logger.warning("[player_sim] 策略模板检索失败，用默认策略: %s", exc)
        logger.info("[player_sim] 未命中模板，使用内置默认策略 casual")
        return template

    # ---------- 单玩家：连续多个会话 ----------
    @staticmethod
    def _persona_for(index: int) -> tuple[str, dict]:
        """按玩家序号轮转分配画像，保证小样本下分布也均衡且可复现。"""
        names = list(PERSONAS)
        name = names[index % len(names)]
        return name, PERSONAS[name]

    async def _play_player(
        self, playwright, browser, sem, url, task_id, player_id,
        strategy, sessions_per, events, status_counts, index: int = 0,
    ):
        rng = random.Random(f"{config.SIM_SEED}:{player_id}")
        persona_name, persona = self._persona_for(index)
        # 画像参数覆盖会话级策略；name/description 仍沿用检索到的模板，便于追溯
        session_strategy = {
            **strategy,
            **{k: v for k, v in persona.items() if k != "description"},
            "persona": persona_name,
        }
        for s in range(1, sessions_per + 1):
            # 回访是"玩家行为"，不是"模拟器配置"：第 2 个会话按画像的 return_prob 决定
            # 是否真的回来。写死跑满 2 个会话会让"重复会话率"恒等于 100%，指标失去意义。
            if s > 1 and rng.random() >= float(session_strategy["return_prob"]):
                break
            async with sem:
                session_id = f"{player_id}-s{s}-{uuid.uuid4().hex[:6]}"
                if s > 1:
                    await asyncio.sleep(rng.uniform(0.3, 1.2))
                await self._play_session(
                    browser, url, task_id, player_id, session_id,
                    session_strategy, events, status_counts,
                )

    async def _play_session(
        self, browser, url, task_id, player_id, session_id, strategy, events, status_counts
    ):
        kv: KVStore = self._runtime.kv
        start_ts = int(time.time() * 1000)
        await self._set_sim_state(kv, session_id, "playing", task_id)

        session_events: list[dict] = []
        last_score = 0
        rng = random.Random(f"{config.SIM_SEED}:{session_id}")
        status = "error"

        def emit(event_type: str, payload: dict) -> None:
            session_events.append(
                {
                    "task_id": task_id,
                    "session_id": session_id,
                    "event_type": event_type,
                    "ts": int(time.time() * 1000),
                    "payload": {"player_id": player_id, **payload},
                }
            )

        context = await browser.new_context(viewport={"width": 520, "height": 720})
        try:
            page = await context.new_page()
            await page.goto(url, timeout=15000)
            await page.wait_for_function("() => !!window.__game", timeout=8000)
            await page.evaluate("() => window.__game.start()")
            emit("session_start", {"strategy": strategy["name"],
                                   "persona": strategy.get("persona")})

            t0 = time.time()
            last_move = 0.0
            action_count = 0
            status = "timeout"
            state: dict = {}
            target = 10
            while time.time() - t0 < float(strategy["session_timeout"]):
                state = await page.evaluate("() => window.__game.state()")
                status_now = state.get("status")
                if status_now in ("win", "lose"):
                    status = status_now
                    break
                target = int(state.get("target") or target or 10)
                score_now = int(state.get("score") or 0)
                # 中途放弃：进度明显落后且画像"耐心"耗尽。这是流失信号的真实来源，
                # 而不是靠超时兜底——超时只是模拟器给的边界，不代表玩家意愿。
                if (
                    score_now < target * 0.5
                    and (time.time() - t0) > float(strategy["session_timeout"]) * 0.4
                    and rng.random() < float(strategy.get("quit_prob", 0.1)) * 0.005
                ):
                    status = "quit"
                    emit("churn", {"at_score": score_now, "reason": "中途放弃（进度落后）"})
                    break
                now = time.time()
                if now - last_move >= float(strategy["move_interval"]):
                    action = await self._decide_action(state, strategy, rng)
                    await page.evaluate(f"() => window.__game.action('{action}')")
                    action_count += 1
                    if action_count % 5 == 0:  # 采样记录，避免事件表膨胀
                        emit("action", {"action": action, "seq": action_count})
                    last_move = now + rng.uniform(0, 0.3)
                if rng.random() < float(strategy["collect_prob"]):
                    await page.evaluate("() => window.__game.action('collect')")
                score = state.get("score", 0)
                if score != last_score:
                    emit("score_change", {"from": last_score, "to": score})
                    last_score = score
                await asyncio.sleep(0.15)
            else:
                state = await page.evaluate("() => window.__game.state()")
                status = state.get("status", "timeout")

            status_counts[status] = status_counts.get(status, 0) + 1
            emit(
                "session_end",
                {
                    "status": status,
                    "duration_ms": int(time.time() * 1000) - start_ts,
                    "score": state.get("score", 0),
                    "misses": state.get("misses", 0),
                    "target": state.get("target"),
                    "actions": action_count,
                },
            )
            # 付费漏斗埋点（模拟）
            if status == "win" and rng.random() < config.SIM_PAY_PROBABILITY:
                emit("pay_click", {"product": "remove_ads", "synthetic": True})
                if rng.random() < 0.6:
                    emit("pay_success", {"product": "remove_ads", "amount": 6.0, "synthetic": True})
            # 流失埋点：中途超时离开视为流失
            if status == "timeout":
                emit("churn", {"at_score": state.get("score", 0), "reason": "abandoned"})
        except Exception as exc:  # noqa: BLE001
            status = "error"
            status_counts["error"] = status_counts.get("error", 0) + 1
            emit("session_error", {"error": str(exc)[:200]})
            emit(
                "session_end",
                {
                    "status": "error",
                    "duration_ms": int(time.time() * 1000) - start_ts,
                    "score": last_score,
                    "actions": 0,
                },
            )
        finally:
            await context.close()

        events.extend(session_events)
        await self._set_sim_state(kv, session_id, status, task_id, len(session_events))

    async def _set_sim_state(
        self, kv: KVStore, session_id: str, state: str, task_id: str, events: int = 0
    ) -> None:
        await kv.set(
            f"sim:{session_id}:state",
            json.dumps({"state": state, "task_id": task_id, "events": events,
                        "ts": int(time.time())}, ensure_ascii=False),
            ttl=3600,
        )
        # 活跃池（非原子，demo 可接受）
        active = await kv.get("sim:pool:active")
        await kv.set(
            "sim:pool:active",
            str(max(0, int(active or 0) + (1 if state == "playing" else -1))),
            ttl=3600,
        )

    # ---------- 决策：规则为主，LLM 关键点介入 ----------
    async def _decide_action(
        self, state: dict, strategy: dict, rng: random.Random
    ) -> str:
        """规则策略：随机游走；关键决策点按概率交给 LLM（有预算护栏）。"""
        if (
            self._runtime.llm.available
            and self._llm_budget > 0
            and rng.random() < float(strategy.get("llm_decision_prob", 0.1))
        ):
            self._llm_budget -= 1
            try:
                text = await self._runtime.llm.chat(
                    [
                        {
                            "role": "system",
                            "content": "你是模拟玩家。只回复一个动作词：move_left / move_right / collect。不要解释。",
                        },
                        {
                            "role": "user",
                            "content": f"当前游戏状态: {json.dumps(state, ensure_ascii=False)}，"
                            "分数落后时更激进地收集，漏接多时先移动。",
                        },
                    ],
                    temperature=0,
                )
                text = text.strip().strip(".,。 '\"")
                if text in ("move_left", "move_right", "collect"):
                    return text
            except Exception:  # noqa: BLE001
                pass  # LLM 决策失败静默回退规则
        return rng.choice(["move_left", "move_right", "collect", "collect"])
