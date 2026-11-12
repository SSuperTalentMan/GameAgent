"""共享运行时：单例持有存储层 / LLM / RAG / 意图识别，全 Agent 复用。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.intent import BERTIntentClassifier
from app.llm import LLMGateway
from app.rag import Embedder, RAGRetriever, Reranker
from app.storage import Database, KVStore, MechanismGraph, VectorStore

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    db: Database
    kv: KVStore
    graph: MechanismGraph
    vector: VectorStore
    llm: LLMGateway
    retriever: RAGRetriever
    intent: BERTIntentClassifier
    embedder: Embedder
    reranker: Reranker
    backends: dict = field(default_factory=dict)

    @classmethod
    async def create(cls, load_models: bool = True) -> "Runtime":
        db = Database()
        kv = KVStore()
        graph = MechanismGraph()
        vector = VectorStore()
        await kv.connect()
        await db.connect()
        await graph.connect()
        await vector.connect()

        embedder = Embedder()
        reranker = Reranker()
        llm = LLMGateway(kv)
        retriever = RAGRetriever(vector, embedder, reranker)
        intent = BERTIntentClassifier()

        if load_models:
            try:
                if vector.count() == 0:
                    retriever.index_corpus_dir()
                else:
                    logger.info("[RAG] 已有索引 %d chunks，跳过重建", vector.count())
                if vector.count("player_strategy_templates") == 0:
                    retriever.index_strategy_templates()
            except Exception:  # noqa: BLE001
                logger.exception("[RAG] 语料索引失败")

        return cls(
            db=db, kv=kv, graph=graph, vector=vector, llm=llm, retriever=retriever,
            intent=intent, embedder=embedder, reranker=reranker,
            backends={
                "db": db.backend,
                "kv": "redis" if kv.is_redis else "memory",
                "graph": graph.backend,
                "vector": vector.backend,
                "reranker": reranker.backend,
                "llm": llm.model_name if llm.available else "unavailable(模板降级)",
            },
        )

    async def close(self) -> None:
        await self.db.close()
        await self.kv.close()
        await self.graph.close()
