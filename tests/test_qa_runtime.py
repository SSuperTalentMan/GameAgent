from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.game_template import render_template
from app.agents.qa import QAAgent
from app.artifacts import task_directory


class FakeDatabase:
    async def update_assertion(self, *args) -> None:
        return None


def qa_payload(task_id: str, entry: Path) -> dict:
    return {
        "task_id": task_id,
        "entry_path": str(entry),
        "spec_id": 1,
        "spec": {
            "target": 2,
            "lose_limit": 1,
            "win_text": "你赢了！",
            "lose_text": "游戏结束",
        },
        "assertions": [
            {"id": "A1", "text": "启动", "mechanism": "启动", "check_type": "start"},
            {"id": "A2", "text": "键盘移动", "mechanism": "移动", "check_type": "controls"},
            {"id": "A3", "text": "计分", "mechanism": "计分", "check_type": "score"},
            {"id": "A4", "text": "胜利", "mechanism": "胜利", "check_type": "win"},
            {"id": "A5", "text": "胜利文案", "mechanism": "文案", "check_type": "win_text"},
            {"id": "A6", "text": "失败", "mechanism": "失败", "check_type": "lose"},
        ],
    }


class QARuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch("app.artifacts.GENERATED_DIR", Path(self.temp.name))
        self.patch.start()
        self.task_id = "task-abcdef12"
        self.task_dir = task_directory(self.task_id)
        (self.task_dir / "assets").mkdir()
        from app.agents.modeler import write_player_png, write_coin_png
        write_player_png(self.task_dir / "assets" / "player.png")
        write_coin_png(self.task_dir / "assets" / "coin.png")
        self.agent = QAAgent(SimpleNamespace(db=FakeDatabase(), kv=None))

    def tearDown(self) -> None:
        self.patch.stop()
        self.temp.cleanup()

    def write_game(self, html: str) -> Path:
        entry = self.task_dir / "game_v1.html"
        entry.write_text(html, encoding="utf-8")
        return entry

    async def test_playable_game_passes_all_evidence(self) -> None:
        entry = self.write_game(render_template("测试", 2, 1, "你赢了！", "游戏结束"))
        result = await self.agent.handle(qa_payload(self.task_id, entry))
        self.assertTrue(result["passed"], result)
        self.assertEqual(len(result["results"]), 6)
        self.assertTrue(any(name.startswith("qa_win_") for name in result["screenshots"]))

    async def test_hidden_win_text_must_fail(self) -> None:
        html = render_template("测试", 2, 1, "你赢了！", "游戏结束")
        html = html.replace(
            "if (next === 'win') { winEl.style.display = 'flex';",
            "if (next === 'win') { winEl.style.display = 'none';",
        )
        entry = self.write_game(html)
        result = await self.agent.handle(qa_payload(self.task_id, entry))
        self.assertFalse(result["passed"])
        failed = {item["check_type"] for item in result["failed_assertions"]}
        self.assertIn("win_text", failed)
        self.assertNotIn("win", failed)


if __name__ == "__main__":
    unittest.main()
