"""RAG 检索链路：query → BGE-M3 编码 → Milvus 召回 top-20 → BGE-reranker 精排 top-5 → 拼 LLM context。"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from app import config
from app.storage.vector import VectorStore

logger = logging.getLogger(__name__)


class Embedder:
    """BGE-M3 dense 向量化（sentence-transformers 加载本地模型）。"""

    def __init__(self) -> None:
        self._model = None

    def _ensure_loaded(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(str(config.BGE_M3_DIR), device="cpu")
            # Demo 语料均为短 chunk，截断到 512 加速 CPU 编码
            self._model.max_seq_length = 512
            logger.info("[RAG] BGE-M3 加载完成 (%s)", config.BGE_M3_DIR)
        return self._model

    def encode(self, texts: list[str]) -> np.ndarray:
        model = self._ensure_loaded()
        vecs = model.encode(
            texts, batch_size=8, normalize_embeddings=True, show_progress_bar=False
        )
        return np.asarray(vecs, dtype=np.float32)

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]


class Reranker:
    """BGE-reranker-v2-m3 精排：优先 pymilvus BGERerankFunction（与文档一致），
    失败则回退 sentence-transformers CrossEncoder，再失败则跳过精排。"""

    def __init__(self) -> None:
        self._fn = None
        self._backend = "none"
        self._init_error: str | None = None

    def _ensure_loaded(self) -> bool:
        if self._fn is not None or self._init_error is not None:
            return self._fn is not None
        try:
            from pymilvus.model.reranker import BGERerankFunction

            self._fn = BGERerankFunction(
                model_name=str(config.RERANKER_DIR), device="cpu"
            )
            self._backend = "pymilvus.BGERerankFunction"
            logger.info("[RAG] 重排序加载完成: %s", self._backend)
            return True
        except Exception as exc:  # noqa: BLE001
            self._init_error = str(exc)
            logger.info("[RAG] pymilvus BGERerankFunction 不可用 (%s)，尝试 CrossEncoder", exc)
        try:
            from sentence_transformers import CrossEncoder

            self._fn = CrossEncoder(str(config.RERANKER_DIR), max_length=512)
            self._backend = "sentence_transformers.CrossEncoder"
            logger.info("[RAG] 重排序加载完成: %s", self._backend)
            return True
        except Exception as exc:  # noqa: BLE001
            self._init_error = str(exc)
            logger.warning("[RAG] 重排序不可用，精排将被跳过: %s", exc)
            return False

    def rerank(self, query: str, docs: list[dict], top_k: int) -> list[dict]:
        if not docs:
            return []
        if not self._ensure_loaded():
            return docs[:top_k]
        texts = [d["text"] for d in docs]
        try:
            if self._backend == "pymilvus.BGERerankFunction":
                results = self._fn(query, texts)  # type: ignore[misc]
                order = sorted(range(len(results)), key=lambda i: -results[i].score)
                out = []
                for i in order[:top_k]:
                    out.append({**docs[i], "rerank_score": float(results[i].score)})
                return out
            scores = self._fn.predict([(query, t) for t in texts])  # type: ignore[union-attr]
            order = np.argsort(-np.asarray(scores))[:top_k]
            return [
                {**docs[i], "rerank_score": float(scores[i])} for i in order
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RAG] 精排失败，退回召回顺序: %s", exc)
            return docs[:top_k]

    @property
    def backend(self) -> str:
        return self._backend


class RAGRetriever:
    def __init__(self, store: VectorStore, embedder: Embedder, reranker: Reranker) -> None:
        self._store = store
        self._embedder = embedder
        self._reranker = reranker

    # ---------- 语料索引 ----------
    def index_corpus_dir(self, corpus_dir: Path | None = None, rebuild: bool = False) -> int:
        """将 rag_corpus 下 markdown 按 '## ' 标题切块、向量化并入库，返回 chunk 数。"""
        corpus_dir = corpus_dir or config.RAG_CORPUS_DIR
        chunks = load_corpus_chunks(corpus_dir)
        if not chunks:
            logger.warning("[RAG] 语料目录为空: %s", corpus_dir)
            return 0
        if rebuild and hasattr(self._store, "reset"):
            self._store.reset()
        vectors = self._embedder.encode([c["text"] for c in chunks])
        self._store.insert(chunks, vectors)
        logger.info("[RAG] 语料索引完成: %d chunks (backend=%s)", len(chunks), self._store.backend)
        return len(chunks)

    # ---------- 检索 ----------
    def retrieve(self, query: str, top_k: int | None = None) -> list[dict]:
        """召回 top-20 → 精排 top-5。"""
        top_k = top_k or config.RAG_RERANK_TOP_K
        qvec = self._embedder.encode_one(query)
        recalled = self._store.search(qvec, config.RAG_RECALL_TOP_K)
        return self._reranker.rerank(query, recalled, top_k)

    def context_for(self, query: str, top_k: int | None = None) -> str:
        docs = self.retrieve(query, top_k)
        if not docs:
            return ""
        blocks = []
        for i, d in enumerate(docs, 1):
            blocks.append(
                f"[{i}] {d.get('source', '')} / {d.get('title', '')}\n{d['text']}"
            )
        return "\n\n".join(blocks)

    # ---------- 玩家策略模板库（文档 9.4 独立集合） ----------
    def index_strategy_templates(
        self, corpus_dir: Path | None = None, collection: str = "player_strategy_templates"
    ) -> int:
        corpus_dir = corpus_dir or config.RAG_CORPUS_DIR
        chunks = [
            c for c in load_corpus_chunks(corpus_dir) if c["source"] == "player_strategies"
        ]
        if not chunks:
            return 0
        vectors = self._embedder.encode([c["text"] for c in chunks])
        self._store.insert(chunks, vectors, collection=collection)
        logger.info(
            "[RAG] 玩家策略模板索引完成: %d 条 (集合=%s)", len(chunks), collection
        )
        return len(chunks)


def load_corpus_chunks(corpus_dir: Path) -> list[dict]:
    """解析语料 markdown：一级文件标题作 source，'## ' 分节作 chunk。"""
    chunks: list[dict] = []
    for path in sorted(Path(corpus_dir).glob("*.md")):
        source = path.stem
        doc_title = ""
        current_title = source
        current_lines: list[str] = []

        def flush():
            text = "\n".join(current_lines).strip()
            if text:
                chunks.append({"text": text[:1200], "source": source, "title": current_title})

        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# ") and not doc_title:
                doc_title = line[2:].strip()
                continue
            if line.startswith("## "):
                flush()
                current_title = f"{doc_title} - {line[3:].strip()}" if doc_title else line[3:].strip()
                current_lines = []
            else:
                current_lines.append(line)
        flush()
    return chunks
