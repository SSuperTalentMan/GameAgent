from __future__ import annotations

import unittest

from app.agents.spec import SpecAgent
from app.contracts import classify_assertion, validate_game_spec


def base_spec() -> dict:
    return {
        "name": "测试游戏",
        "game_type": "catch_collect",
        "core_loop": "接金币",
        "entities": ["player"],
        "rules": ["接到金币加分"],
        "win_condition": "分数达到 10 后显示'你赢了！'",
        "lose_condition": "漏接 3 次后显示'游戏结束'",
        "acceptance_assertions": [
            {"id": "A1", "text": "页面加载后进入 playing 状态", "mechanism": "启动",
             "check_type": "start"},
            {"id": "A2", "text": "分数变化", "mechanism": "计分", "check_type": "score"},
            {"id": "A3", "text": "分数达到阈值进入 win", "mechanism": "胜利",
             "check_type": "win"},
        ],
        "mechanisms": [
            {"id": "M1", "name": "核心玩法", "depends_on": []},
            {"id": "M2", "name": "胜利判定", "depends_on": ["M1"]},
        ],
    }


class ContractTests(unittest.TestCase):
    def test_assertion_classification(self) -> None:
        self.assertEqual(classify_assertion("页面显示胜利文案"), "win_text")
        self.assertEqual(classify_assertion("按下方向键可以左右移动"), "controls")
        self.assertEqual(classify_assertion("得分增加"), "score")
        self.assertEqual(classify_assertion("这是一个主观好玩断言"), "unsupported")

    def test_window_is_not_a_win_assertion(self) -> None:
        # 回归用例：window.game.start() 里的 "win" 曾被误判为胜利断言
        text = (
            "页面加载后调用 window.game.start()，#game 的 data-state 属性变为 playing，"
            "并且页面中至少出现一个带有 class coin 的下落金币元素。"
        )
        self.assertEqual(classify_assertion(text, "游戏状态管理"), "start")

    def test_scoring_assertion_is_not_downgraded_to_start(self) -> None:
        # 回归用例：断言里出现 "在 playing 状态下" 时曾被 start 分支抢先命中，
        # 导致"接住金币后分数从 0 变为 1"只验证了"画布存在"，门禁名存实亡。
        text = (
            "在 playing 状态下，将 #player 的水平坐标设置为与某个 .coin 元素相同"
            "并触发碰撞检测后，#score 的文本从 0 变为 1。"
        )
        self.assertEqual(classify_assertion(text, "碰撞接住检测"), "score")

    def test_coin_word_alone_is_not_scoring(self) -> None:
        # "金币"是场景元素词，不能单独作为计分信号
        text = "页面加载后出现下落金币，状态变为 playing"
        self.assertEqual(classify_assertion(text, "金币生成与下落"), "start")

    def test_duplicate_assertion_id_is_rejected(self) -> None:
        spec = base_spec()
        spec["acceptance_assertions"][1]["id"] = "A1"
        with self.assertRaises(ValueError):
            validate_game_spec(spec)

    def test_unknown_dependency_is_rejected(self) -> None:
        spec = base_spec()
        spec["mechanisms"][1]["depends_on"] = ["M99"]
        with self.assertRaises(ValueError):
            validate_game_spec(spec)

    def test_normalize_creates_executable_full_coverage(self) -> None:
        spec = SpecAgent.__new__(SpecAgent)._normalize(
            {"acceptance_assertions": [{"id": "A1", "text": "页面加载后进入 playing"}]},
            "做一个接金币游戏，10 分胜利，漏接 3 次失败",
        )
        kinds = {a["check_type"] for a in spec["acceptance_assertions"]}
        self.assertTrue({"start", "controls", "score", "win", "win_text", "lose"} <= kinds)
        self.assertEqual(spec["target"], 10)
        self.assertEqual(spec["lose_limit"], 3)
        self.assertEqual(spec["win_text"], "你赢了！")


if __name__ == "__main__":
    unittest.main()
