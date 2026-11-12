"""向量库：Milvus Lite (MilvusClient 本地文件，零部署)。

milvus-lite 3.x 起存储引擎为纯 Python 实现（检索基于 faiss-cpu），
Windows 原生可用；numpy 余弦检索仅作为 milvus-lite 缺失时的兜底。
检索链路（召回 top-20 → 重排 top-5）两种后端完全一致。
"""

from __future__ import annotations

import json
import logging

import numpy as np

from app import config

logger = logging.getLogger(__name__)


class _NumpyVectorStore:
    """numpy 余弦检索降级实现（BGE-M3 向量已归一化，点积即余弦）。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._vectors = np.zeros((0, config.EMBED_DIM), dtype=np.float32)
        self._rows: list[dict] = []
        try:
            data = np.load(path, allow_pickle=False)
            vectors = data["vectors"]
            with open(path + ".meta.json", encoding="utf-8") as f:
                rows = json.load(f)
            # 向量与元数据不一致（如残留半写状态）时自愈为空库，重新索引即可
            if len(rows) == vectors.shape[0]:
                self._vectors = vectors
                self._rows = rows
            else:
                import os

                os.remove(path)
        except (OSError, KeyError, json.JSONDecodeError, FileNotFoundError):
            pass

    def insert(self, rows: list[dict], vectors: np.ndarray) -> None:
        if not rows:
            return
        if self._vectors.shape[0] == 0:
            self._vectors = vectors.astype(np.float32)
        else:
            self._vectors = np.vstack([self._vectors, vectors.astype(np.float32)])
        self._rows.extend(rows)
        np.savez(self._path, vectors=self._vectors)
        with open(self._path + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(self._rows, f, ensure_ascii=False)

    def search(self, vector: np.ndarray, top_k: int) -> list[dict]:
        if self._vectors.shape[0] == 0:
            return []
        v = np.asarray(vector, dtype=np.float32).reshape(1, -1)
        norm = np.linalg.norm(self._vectors, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        scores = (self._vectors / norm) @ (v / max(np.linalg.norm(v), 1e-9)).T
        order = np.argsort(-scores.reshape(-1))[:top_k]
        return [
            {**self._rows[i], "score": float(scores.reshape(-1)[i])} for i in order
        ]

    @property
    def count(self) -> int:
        return self._vectors.shape[0]


class VectorStore:
    """RAG 向量存储：Milvus Lite 优先，Windows 降级 numpy。

    支持多集合：rag_chunks（引擎文档/设计模式/修复经验）、
    player_strategy_templates（玩家策略模板，文档 9.4）。
    numpy 降级模式下集合以前缀区分存储文件。
    """

    def __init__(self) -> None:
        self._client = None
        self._numpy: dict[str, _NumpyVectorStore] = {}
        self._default_collection = config.MILVUS_COLLECTION
        self._loaded: set[str] = set()

    def _numpy_store(self, collection: str) -> _NumpyVectorStore:
        if collection not in self._numpy:
            path = str(config.VECTOR_FALLBACK_PATH).replace(
                ".npz", f"_{collection}.npz"
            )
            self._numpy[collection] = _NumpyVectorStore(path)
        return self._numpy[collection]

    async def connect(self) -> None:
        try:
            from pymilvus import MilvusClient

            client = MilvusClient(uri=config.MILVUS_DB_PATH)
            self._client = client
            logger.info("[Vector] Milvus Lite 就绪: %s", config.MILVUS_DB_PATH)
        except Exception as exc:  # noqa: BLE001
            self._client = None
            self._numpy_store(self._default_collection)
            logger.warning(
                "[Vector] Milvus Lite 在当前环境不可用 (%s)，"
                "降级为本地 numpy 检索（RAG 链路不变）",
                exc,
            )

    def _ensure_loaded(self, collection: str) -> None:
        """Milvus Lite 重启后集合处于 released 状态，检索前必须 load。"""
        if self._client is None or collection in self._loaded:
            return
        try:
            if self._client.has_collection(collection):
                self._client.load_collection(collection)
            self._loaded.add(collection)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Vector] 集合 %s load 失败: %s", collection, exc)

    @property
    def backend(self) -> str:
        return "milvus-lite" if self._client is not None else "numpy-fallback"

    def count(self, collection: str | None = None) -> int:
        collection = collection or self._default_collection
        if self._client is not None:
            try:
                if not self._client.has_collection(collection):
                    return 0
                stats = self._client.get_collection_stats(collection)
                return int(stats.get("row_count", 0))
            except Exception:  # noqa: BLE001
                return 0
        return self._numpy_store(collection).count

    def _ensure_collection(self, collection: str) -> None:
        from pymilvus import DataType

        if self._client.has_collection(collection):
            return
        schema = self._client.create_schema(auto_id=True, enable_dynamic_field=True)
        schema.add_field("id", DataType.INT64, is_primary=True)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=config.EMBED_DIM)
        schema.add_field("text", DataType.VARCHAR, max_length=65535)
        schema.add_field("source", DataType.VARCHAR, max_length=512)
        schema.add_field("title", DataType.VARCHAR, max_length=512)
        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="vector", index_type="AUTOINDEX", metric_type="COSINE"
        )
        self._client.create_collection(
            collection_name=collection, schema=schema, index_params=index_params
        )

    def insert(
        self, rows: list[dict], vectors: np.ndarray, collection: str | None = None
    ) -> None:
        """rows: [{"text", "source", "title"}]，与 vectors 一一对应。"""
        collection = collection or self._default_collection
        if not rows:
            return
        if self._client is not None:
            self._ensure_collection(collection)
            self._loaded.discard(collection)  # 新建/重建后需重新 load
            data = [
                {
                    "vector": vectors[i].tolist(),
                    "text": rows[i]["text"],
                    "source": rows[i].get("source", ""),
                    "title": rows[i].get("title", ""),
                }
                for i in range(len(rows))
            ]
            self._client.insert(collection_name=collection, data=data)
        else:
            self._numpy_store(collection).insert(rows, vectors)

    def search(
        self, vector: np.ndarray, top_k: int, collection: str | None = None
    ) -> list[dict]:
        collection = collection or self._default_collection
        if self._client is not None:
            if not self._client.has_collection(collection):
                return []
            self._ensure_loaded(collection)
            results = self._client.search(
                collection_name=collection,
                data=[vector.tolist()],
                limit=top_k,
                output_fields=["text", "source", "title"],
                search_params={"metric_type": "COSINE"},
            )
            hits = results[0] if results else []
            return [
                {
                    "text": h["entity"].get("text", ""),
                    "source": h["entity"].get("source", ""),
                    "title": h["entity"].get("title", ""),
                    "score": float(h.get("distance", 0.0)),
                }
                for h in hits
            ]
        return self._numpy_store(collection).search(vector, top_k)

    def reset(self, collection: str | None = None) -> None:
        """清空并重建（供语料索引脚本 --rebuild 使用）。"""
        collection = collection or self._default_collection
        if self._client is not None:
            if self._client.has_collection(collection):
                self._client.drop_collection(collection)
            self._ensure_collection(collection)
            self._loaded.discard(collection)
        else:
            self._numpy.pop(collection, None)
