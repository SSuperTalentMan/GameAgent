"""意图识别：本地 bert-base-chinese 语义匹配分类（new_requirement / fix / query）。

做法：对意图锚点例句与用户输入分别取 BERT 平均池化句向量，按余弦相似度
投票；模型不可用或置信度过低时用规则关键词兜底（链路上始终保留 BERT 调用）。
"""

from __future__ import annotations

import logging
import re

import numpy as np

from app import config

logger = logging.getLogger(__name__)

INTENTS = ("new_requirement", "fix", "query")

ANCHORS: dict[str, list[str]] = {
    "new_requirement": [
        "帮我做一个全新的小游戏",
        "我想做一个接金币的网页游戏",
        "设计一个新的塔防游戏",
        "开发一个贪吃蛇游戏项目",
        "帮我生成一个全新的游戏需求",
        "做一个新的闯关游戏",
    ],
    "fix": [
        "修复上次任务失败的断言",
        "游戏有一个bug，请修复",
        "胜利判定没有生效，帮忙修正",
        "上次的代码运行报错了，请修复",
        "金币不显示，修复这个问题",
        "根据QA的失败证据修复代码",
    ],
    "query": [
        "查一下任务状态",
        "现在的规格内容是什么",
        "任务进行到哪一步了",
        "看看QA的验证结果",
        "查询一下修复进度",
        "当前游戏项目进展如何",
    ],
}

RULES: list[tuple[str, re.Pattern[str]]] = [
    ("fix", re.compile(r"修复|修正|修好|fix|bug|报错|错误|失败|异常|不改不行|有问题")),
    ("query", re.compile(
        r"查询|查一下|查看|看看|状态|进度|结果|怎么样|是什么|什么时候"
        r"|有哪些|有没有|列出|列一下|最近.{0,4}任务"
    )),
    ("new_requirement", re.compile(r"做一个|做一个新|帮我做|开发|实现|新建|设计|生成|来一[个局]")),
]

CONFIDENCE_THRESHOLD = 0.55


class BERTIntentClassifier:
    def __init__(self) -> None:
        self._tokenizer = None
        self._model = None
        self._anchor_vectors: np.ndarray | None = None
        self._anchor_labels: list[str] = []
        self._load_failed = False

    def _ensure_loaded(self) -> bool:
        if self._model is not None or self._load_failed:
            return self._model is not None
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(config.BERT_MODEL_DIR)
            self._model = AutoModel.from_pretrained(config.BERT_MODEL_DIR)
            self._model.eval()
            texts = [t for intent in INTENTS for t in ANCHORS[intent]]
            self._anchor_labels = [
                intent for intent in INTENTS for _ in ANCHORS[intent]
            ]
            self._anchor_vectors = self._embed(texts)
            logger.info(
                "[Intent] bert-base-chinese 加载完成 (%s)", config.BERT_MODEL_DIR
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self._load_failed = True
            logger.warning("[Intent] BERT 加载失败，将使用规则兜底: %s", exc)
            return False

    def _embed(self, texts: list[str]) -> np.ndarray:
        import torch

        assert self._tokenizer is not None and self._model is not None
        encoded = self._tokenizer(
            texts, padding=True, truncation=True, max_length=64, return_tensors="pt"
        )
        with torch.no_grad():
            output = self._model(**encoded)
        hidden = output.last_hidden_state  # (batch, seq, dim)
        mask = encoded["attention_mask"].unsqueeze(-1).float()
        pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)
        return pooled.numpy().astype(np.float32)

    def classify(self, text: str) -> dict:
        """返回 {"intent", "confidence", "method", "scores"}。

        决策策略是"高精度规则优先，BERT 处理歧义"：
        实测中 BERT 锚点法会把"查询任务 task-xxx 的状态"判成 fix（0.757），
        只因句子里带"任务/状态"这类在两批锚点里都常见的词。
        而规则命中是词级精确的：只命中 query 关键词、完全不命中 fix 关键词时，
        几乎不可能是 fix。所以单意图规则命中直接采信，多意图命中才交给 BERT。
        """
        scores: dict[str, float] = {}
        method = "bert"
        rule_hits = self._rule_intents(text)
        if self._ensure_loaded() and self._anchor_vectors is not None:
            q = self._embed([text])[0]
            q = q / max(float(np.linalg.norm(q)), 1e-9)
            anchors = self._anchor_vectors / np.linalg.norm(
                self._anchor_vectors, axis=1, keepdims=True
            ).clip(min=1e-9)
            sims = anchors @ q
            # 锚点投票 → 每个意图取最高相似度并略加平均
            for intent in INTENTS:
                idx = [i for i, l in enumerate(self._anchor_labels) if l == intent]
                intent_sims = sims[idx]
                scores[intent] = float(
                    0.7 * intent_sims.max() + 0.3 * intent_sims.mean()
                )
            best = max(scores, key=scores.get)  # type: ignore[arg-type]
            confidence = scores[best]
            if len(rule_hits) == 1 and rule_hits[0][0] != best:
                logger.info(
                    "[Intent] 规则与 BERT 冲突，采信高精度规则: rule=%s(%.2f) bert=%s(%.2f) text=%s",
                    rule_hits[0][0], rule_hits[0][1], best, confidence, text[:40],
                )
                return {
                    "intent": rule_hits[0][0],
                    "confidence": confidence,
                    "method": "rule(高精度优先)",
                    "scores": scores,
                }
            if confidence < CONFIDENCE_THRESHOLD and rule_hits:
                return {
                    "intent": rule_hits[0][0],
                    "confidence": confidence,
                    "method": "rule(低置信度兜底)",
                    "scores": scores,
                }
            return {"intent": best, "confidence": confidence, "method": method, "scores": scores}
        rule_intent, rule_score = self._rule_classify(text)
        return {
            "intent": rule_intent or "new_requirement",
            "confidence": rule_score,
            "method": "rule(BERT不可用兜底)",
            "scores": {},
        }

    @staticmethod
    def _rule_intents(text: str) -> list[tuple[str, float]]:
        """返回所有命中的意图（按分数降序），用于判断规则是否"单意图精确命中"。"""
        hits: list[tuple[str, float]] = []
        for intent, pattern in RULES:
            matches = pattern.findall(text)
            if matches:
                hits.append((intent, min(0.9, 0.6 + 0.05 * len(matches))))
        hits.sort(key=lambda x: -x[1])
        return hits

    @staticmethod
    def _rule_classify(text: str) -> tuple[str | None, float]:
        hits = BERTIntentClassifier._rule_intents(text)
        if not hits:
            return None, 0.0
        return hits[0]
