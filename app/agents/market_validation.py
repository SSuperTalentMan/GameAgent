"""Market Validation Agent（文档 9.2 ⑦）：

- 输入：player_events（MySQL）+ Spec 设计意图
- 计算：模拟留存曲线、完成率、付费转化漏斗、流失点分布（确定性代码）
- 报告：qwen3.8-flash 通过**工具调用循环**（R6）自主选择分析工具后撰写，
        LLM 不可用时退回确定性中文摘要
- 输出：validation_reports（MySQL）+ 前端可视化 metrics
- 预留真实运营数据 adapter（ops_adapter hook）
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict

from app.agents.base import SkillAgent
from app.config import MARKET_PORT
from app.llm import LLMError

logger = logging.getLogger(__name__)


class MarketValidationAgent(SkillAgent):
    name = "market_agent"
    port = MARKET_PORT
    description = "基于玩家行为事件计算留存/漏斗/流失指标并产出验证报告"
    skills_spec = [
        {
            "id": "validate_market",
            "name": "市场价值验证",
            "description": "输入任务 ID 与规格，输出含留存曲线、付费漏斗、流失点的"
            "验证报告（写入 validation_reports）",
        }
    ]

    def __init__(self, runtime) -> None:
        super().__init__()
        self._runtime = runtime

    async def handle(self, payload: dict) -> dict:
        task_id: str = payload["task_id"]
        spec: dict = payload.get("spec") or {}
        spec_id = payload.get("spec_id")

        events = await self._runtime.db.get_player_events(task_id)
        if not events:
            raise ValueError(f"任务 {task_id} 没有玩家行为事件，请先运行 Player Simulation")

        metrics = self._compute_metrics(events)
        summary = await self._write_report(metrics, spec)

        report_id = f"report-{task_id}"
        await self._runtime.db.save_validation_report(
            report_id, task_id, spec_id, metrics, summary
        )
        logger.info("[market] 合成实验报告 %s 已落库 (重复会话率=%.0f%%, 模拟付费事件率=%.1f%%)",
                    report_id, metrics["retention"]["d1"] * 100,
                    metrics["funnel"]["pay_conversion"] * 100)
        return {
            "report_id": report_id,
            "metrics": metrics,
            "summary": summary,
            "summary_short": (
                f"模拟完成率 {metrics['overview']['win_rate']:.0%}，"
                f"重复会话率 {metrics['retention']['d1']:.0%}（非真实留存），"
                f"模拟付费事件率 {metrics['funnel']['pay_conversion']:.1%}，"
                f"主要退出原因: {metrics['churn']['top_reason']}"
            ),
        }

    # ================= 指标计算（确定性） =================
    def _compute_metrics(self, events: list[dict]) -> dict:
        by_session: dict[str, list[dict]] = defaultdict(list)
        for e in events:
            by_session[e["session_id"]].append(e)
        by_player: dict[str, list[str]] = defaultdict(list)
        for sid, evs in by_session.items():
            pid = evs[0]["payload"].get("player_id", sid)
            by_player[pid].append(sid)

        durations, scores, win_cnt, lose_cnt, timeout_cnt, err_cnt = [], [], 0, 0, 0, 0
        quit_cnt = 0
        actions_total = 0
        pay_clicks, pay_success = 0, 0
        churn_at: list[int] = []
        churn_reasons: Counter = Counter()

        for sid, evs in by_session.items():
            types = [e["event_type"] for e in evs]
            end = next((e["payload"] for e in evs if e["event_type"] == "session_end"), None)
            if end:
                durations.append(end.get("duration_ms", 0) / 1000)
                scores.append(end.get("score", 0))
                actions_total += end.get("actions", 0)
                st = end.get("status")
                if st == "win":
                    win_cnt += 1
                elif st == "lose":
                    lose_cnt += 1
                elif st == "timeout":
                    timeout_cnt += 1
                    churn_reasons["模拟超时离开"] += 1
                elif st == "quit":
                    quit_cnt += 1
                    churn_reasons["中途放弃"] += 1
                elif st == "error":
                    err_cnt += 1
            if "pay_click" in types:
                pay_clicks += 1
            if "pay_success" in types:
                pay_success += 1
            for e in evs:
                if e["event_type"] == "churn":
                    churn_at.append(e["payload"].get("at_score", 0))
                    churn_reasons[e["payload"].get("reason", "unknown")] += 1

        sessions = len(by_session)
        players = len(by_player)
        sessions_per_player = [len(sids) for sids in by_player.values()]
        returning_players = sum(1 for n in sessions_per_player if n >= 2)

        # 这里只验证指标管线，不是真实留存。所有天数均为合成/外推。
        repeat_rate = (returning_players / players) if players else 0.0
        retention = {
            "d1": round(repeat_rate, 3),
            "d3": round(repeat_rate * 0.62, 3),
            "d7": round(repeat_rate * 0.38, 3),
            "label": "回访玩家占比（合成数据）",
            "method": "d1=产生第 2 个会话的玩家占比（是否回访由玩家画像概率决定，"
                      "不是模拟器写死跑满）；d3/d7=经验系数外推，不可作为真实留存结论",
            "curve": [
                {"day": d, "rate": round(v, 3)}
                for d, v in [
                    (1, repeat_rate), (3, repeat_rate * 0.62), (7, repeat_rate * 0.38),
                    (14, repeat_rate * 0.21), (30, repeat_rate * 0.09),
                ]
            ],
        }

        funnel = {
            "sessions": sessions,
            "pay_clicks": pay_clicks,
            "pay_success": pay_success,
            "click_rate": round(pay_clicks / sessions, 3) if sessions else 0.0,
            "pay_conversion": round(pay_success / sessions, 3) if sessions else 0.0,
            "label": "模拟付费事件漏斗（合成数据，非真实支付）",
        }

        status_cnt = {
            "win": win_cnt, "lose": lose_cnt, "quit": quit_cnt,
            "timeout": timeout_cnt, "error": err_cnt,
        }
        top_reason = (
            churn_reasons.most_common(1)[0][0]
            if churn_reasons
            else ("失败退出" if lose_cnt >= win_cnt else "通关后自然离开")
        )
        churn = {
            "by_status": status_cnt,
            "reasons": dict(churn_reasons),
            "top_reason": top_reason,
            "churn_at_score_avg": round(sum(churn_at) / len(churn_at), 1) if churn_at else None,
        }

        overview = {
            "players": players,
            "sessions": sessions,
            "win_rate": round(win_cnt / sessions, 3) if sessions else 0.0,
            "lose_rate": round(lose_cnt / sessions, 3) if sessions else 0.0,
            "avg_duration_s": round(sum(durations) / len(durations), 1) if durations else 0.0,
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
            "avg_actions": round(actions_total / sessions, 1) if sessions else 0.0,
        }
        return {
            "data_source": "synthetic_agent_simulation",
            "disclaimer": "合成玩家实验，仅验证事件管线与机制压测，不代表真人留存或付费意愿",
            "overview": overview,
            "retention": retention,
            "funnel": funnel,
            "churn": churn,
        }

    # ================= 报告撰写：R6 工具调用循环 =================
    async def _write_report(self, metrics: dict, spec: dict) -> str:
        """LLM 自主调用分析工具后写报告；不可用时确定性摘要。"""
        if not self._runtime.llm.available:
            return self._fallback_summary(metrics, spec)

        # metrics 已在本地算好，工具给 LLM 提供"下钻"能力
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_design_intent",
                    "description": "获取游戏规格中的设计意图（核心循环/胜负条件）",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_funnel_detail",
                    "description": "获取付费转化漏斗各级明细数据",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_churn_detail",
                    "description": "获取流失点分布与流失时玩家进度明细",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
        ]

        async def executor(name: str, args: dict) -> dict:
            if name == "get_design_intent":
                return {
                    "core_loop": spec.get("core_loop", ""),
                    "win_condition": spec.get("win_condition", ""),
                    "lose_condition": spec.get("lose_condition", ""),
                    "rules": spec.get("rules", []),
                }
            if name == "get_funnel_detail":
                return metrics["funnel"]
            if name == "get_churn_detail":
                return metrics["churn"]
            return {"error": f"未知工具 {name}"}

        messages = [
            {
                "role": "system",
                "content": "你是游戏数据分析师。基于合成玩家模拟指标撰写 200 字以内的中文实验报告，"
                "必须明确数据来自合成玩家、不是真人留存或真实付费；包含重复会话率、"
                "模拟付费事件率、退出原因和一条可验证的改进假设。"
                "需要下钻数据时调用提供的工具。直接输出报告正文。",
            },
            {
                "role": "user",
                "content": f"游戏「{spec.get('name', '未命名')}」模拟验证指标总览："
                f"{_compact_json(metrics)}",
            },
        ]
        try:
            return await self._runtime.llm.chat_with_tools(
                messages, tools, executor, temperature=0.2, max_rounds=3
            )
        except Exception as exc:  # noqa: BLE001
            # 报告撰写是分析链的"锦上添花"，任何异常（LLMError / 网络 / 代理 502）
            # 都必须退回确定性摘要，不能让指标已经算好的报告整体失败
            logger.warning("[market] LLM 工具调用失败，用确定性摘要: %s", exc)
            return self._fallback_summary(metrics, spec)

    @staticmethod
    def _fallback_summary(metrics: dict, spec: dict) -> str:
        o, r, f, c = metrics["overview"], metrics["retention"], metrics["funnel"], metrics["churn"]
        return (
            f"《{spec.get('name', '未命名')}》合成玩家实验：{o['sessions']} 个会话中通关率 "
            f"{o['win_rate']:.0%}，平均时长 {o['avg_duration_s']}s；重复会话率 "
            f"{r['d1']:.0%}（非真实留存）。模拟付费事件：点击率 {f['click_rate']:.1%}，"
            f"事件率 {f['pay_conversion']:.1%}（非真实支付）。"
            f"主要退出原因为「{c['top_reason']}」。"
            f"建议：{'降低失败惩罚以改善留存' if o['lose_rate'] > 0.4 else '增加中期目标点以延长会话'}。"
        )


def _compact_json(data: dict) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
