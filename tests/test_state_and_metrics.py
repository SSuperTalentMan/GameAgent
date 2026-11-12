from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from app.agents.market_validation import MarketValidationAgent
from app.orchestrator import OrchestratorAgent


class FakeDB:
    def __init__(self) -> None:
        self.task = {
            "task_id": "task-abcdef12", "status": "working",
            "meta": json.dumps({"spec_id": 7, "keep": "yes"}),
        }

    async def get_task(self, task_id):
        return self.task

    async def upsert_task(self, task_id, **kwargs):
        if kwargs.get("status"):
            self.task["status"] = kwargs["status"]
        if kwargs.get("meta") is not None:
            self.task["meta"] = json.dumps(kwargs["meta"])


class FakeKV:
    def __init__(self) -> None:
        self.values = {}

    async def set(self, key, value, ttl=None):
        self.values[key] = value


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_write_preserves_meta_and_syncs_kv(self) -> None:
        db, kv = FakeDB(), FakeKV()
        orch = OrchestratorAgent(SimpleNamespace(db=db, kv=kv))
        await orch._set_task_state(
            "task-abcdef12", "failed", round_no=2,
            meta_update={"reason": "quality_gate_failed"},
        )
        meta = json.loads(db.task["meta"])
        self.assertEqual(meta["spec_id"], 7)
        self.assertEqual(meta["keep"], "yes")
        self.assertEqual(db.task["status"], "failed")
        self.assertEqual(kv.values["task:task-abcdef12:status"], "failed")


class MetricTests(unittest.TestCase):
    def test_error_session_is_not_lost(self) -> None:
        agent = MarketValidationAgent(SimpleNamespace(llm=None))
        events = [
            {"session_id": "p1-s1", "event_type": "session_start",
             "payload": {"player_id": "p1"}},
            {"session_id": "p1-s1", "event_type": "session_end",
             "payload": {"status": "error", "duration_ms": 120, "score": 0, "actions": 0}},
            {"session_id": "p1-s2", "event_type": "session_end",
             "payload": {"status": "win", "duration_ms": 900, "score": 10, "actions": 20}},
        ]
        metrics = agent._compute_metrics(events)
        self.assertEqual(metrics["churn"]["by_status"]["error"], 1)
        self.assertEqual(metrics["overview"]["sessions"], 2)
        self.assertEqual(metrics["data_source"], "synthetic_agent_simulation")
        self.assertIn("不代表", metrics["disclaimer"])

    def test_quit_session_counts_as_churn(self) -> None:
        # 中途放弃必须是独立状态，否则流失原因全被"超时"吞掉
        agent = MarketValidationAgent(SimpleNamespace(llm=None))
        events = [
            {"session_id": "p1-s1", "event_type": "session_end",
             "payload": {"status": "quit", "duration_ms": 5000, "score": 2, "actions": 8}},
            {"session_id": "p1-s1", "event_type": "churn",
             "payload": {"at_score": 2, "reason": "中途放弃（进度落后）"}},
        ]
        metrics = agent._compute_metrics(events)
        self.assertEqual(metrics["churn"]["by_status"]["quit"], 1)
        self.assertEqual(metrics["churn"]["reasons"]["中途放弃"], 1)
        self.assertEqual(metrics["retention"]["d1"], 0.0)

    def test_returning_player_drives_d1(self) -> None:
        # d1 只能来自"真的产生了第 2 个会话的玩家"，不能因为配置写死就恒等于 1
        agent = MarketValidationAgent(SimpleNamespace(llm=None))
        events = [
            {"session_id": "p1-s1", "event_type": "session_end",
             "payload": {"player_id": "p1", "status": "win", "duration_ms": 900,
                         "score": 10, "actions": 20}},
            {"session_id": "p1-s2", "event_type": "session_end",
             "payload": {"player_id": "p1", "status": "win", "duration_ms": 800,
                         "score": 10, "actions": 18}},
            {"session_id": "p2-s1", "event_type": "session_end",
             "payload": {"player_id": "p2", "status": "lose", "duration_ms": 700,
                         "score": 3, "actions": 10}},
        ]
        metrics = agent._compute_metrics(events)
        self.assertEqual(metrics["overview"]["players"], 2)
        self.assertEqual(metrics["retention"]["d1"], 0.5)


if __name__ == "__main__":
    unittest.main()
