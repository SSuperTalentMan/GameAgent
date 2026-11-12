from __future__ import annotations

import unittest

from app.intent import BERTIntentClassifier


class RuleSignalTests(unittest.TestCase):
    """规则信号只做"单意图精确命中"判断，不依赖本地模型，可在 CI 跑。"""

    def test_query_sentence_hits_only_query(self) -> None:
        hits = BERTIntentClassifier._rule_intents("查询任务 task-6a961fd5 的状态")
        self.assertEqual([i for i, _ in hits], ["query"])

    def test_fix_sentence_hits_only_fix(self) -> None:
        # "失败"是 fix 关键词；只要不出现 query 关键词就应该保持单意图
        hits = BERTIntentClassifier._rule_intents("修复任务 task-6a961fd5 的失败断言")
        self.assertEqual([i for i, _ in hits], ["fix"])

    def test_mixed_requirement_is_ambiguous(self) -> None:
        # "做一个…会失败" 同时命中 new_requirement 与 fix，必须交给 BERT 判
        hits = BERTIntentClassifier._rule_intents("帮我做一个接金币游戏，漏接太多会失败")
        self.assertGreater(len(hits), 1)


if __name__ == "__main__":
    unittest.main()
