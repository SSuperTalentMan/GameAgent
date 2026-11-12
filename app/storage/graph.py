"""图库：Neo4j 存机制依赖图 (Mechanism)-[:DEPENDS_ON]->。

不可达时降级为本地 JSON 文件图，接口一致。
用途：断言失败时查询受影响的机制范围（反向 1~N 跳）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app import config

logger = logging.getLogger(__name__)


class _FileGraph:
    """本地 JSON 文件降级实现。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._nodes: dict[str, dict] = {}
        self._edges: set[tuple[str, str]] = set()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._nodes = data.get("nodes", {})
            self._edges = {(e[0], e[1]) for e in data.get("edges", [])}
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(
                {"nodes": self._nodes, "edges": [list(e) for e in self._edges]},
                f,
                ensure_ascii=False,
                indent=2,
            )

    async def merge_mechanism(self, mech_id: str, name: str, spec_id: int) -> None:
        key = f"{spec_id}:{mech_id}"
        self._nodes[key] = {"name": name, "spec_id": spec_id, "mechanism_id": mech_id}
        self._save()

    async def merge_dependency(
        self, from_id: str, to_id: str, spec_id: int | None = None
    ) -> None:
        prefix = f"{spec_id}:" if spec_id is not None else ""
        self._edges.add((f"{prefix}{from_id}", f"{prefix}{to_id}"))
        self._save()

    async def impacted_by(self, mech_id: str, max_depth: int = 2) -> list[str]:
        """所有（直接或传递）依赖 mech_id 的机制：谁会受它失败影响。"""
        dependents: dict[str, list[str]] = {}
        for a, b in self._edges:
            dependents.setdefault(b, []).append(a)
        seen: set[str] = set()
        frontier = [mech_id]
        for _ in range(max_depth):
            nxt = []
            for node in frontier:
                for dep in dependents.get(node, []):
                    if dep not in seen:
                        seen.add(dep)
                        nxt.append(dep)
            frontier = nxt
            if not frontier:
                break
        return sorted(seen)


class MechanismGraph:
    """机制依赖图：Neo4j 优先，不可达降级本地文件。"""

    def __init__(self) -> None:
        self._driver: Any = None
        self._file: _FileGraph | None = None

    async def connect(self) -> None:
        if config.FORCE_FILE_GRAPH or not config.NEO4J_URI:
            self._use_file("NEO4J_URI 未配置")
            return
        try:
            from neo4j import AsyncGraphDatabase

            driver = AsyncGraphDatabase.driver(
                config.NEO4J_URI,
                auth=(config.NEO4J_USER, config.NEO4J_PASSWORD or None),
                connection_timeout=2.0,
            )
            await driver.verify_connectivity()
            self._driver = driver
            logger.info("[Graph] Neo4j 已连接: %s", config.NEO4J_URI)
        except Exception as exc:  # noqa: BLE001
            self._use_file(f"Neo4j 不可达 ({exc})")

    def _use_file(self, reason: str) -> None:
        self._file = _FileGraph(str(config.GRAPH_FALLBACK_PATH))
        logger.warning("[Graph] %s，降级本地文件图: %s", reason, config.GRAPH_FALLBACK_PATH)

    @property
    def backend(self) -> str:
        return "neo4j" if self._driver is not None else "file"

    async def _run_write(self, cypher: str, **params) -> None:
        async def _tx(tx):
            result = await tx.run(cypher, params)  # neo4j 6.x: run 为协程
            await result.consume()

        async with self._driver.session() as session:
            await session.execute_write(_tx)

    async def write_spec_dependencies(
        self, spec_id: int, mechanisms: list[dict]
    ) -> None:
        """mechanisms: [{"id": "M1", "name": "伤害计算", "depends_on": ["M2"]}]"""
        for m in mechanisms:
            await self._merge_mechanism(m["id"], m.get("name", m["id"]), spec_id)
        for m in mechanisms:
            for dep in m.get("depends_on", []):
                await self._merge_dependency(m["id"], dep, spec_id)

    async def _merge_mechanism(self, mech_id: str, name: str, spec_id: int) -> None:
        key = f"{spec_id}:{mech_id}"
        if self._driver is not None:
            await self._run_write(
                "MERGE (m:Mechanism {id: $id}) "
                "SET m.name=$name, m.spec_id=$spec_id, m.mechanism_id=$mech_id",
                id=key, name=name, spec_id=spec_id, mech_id=mech_id,
            )
        else:
            assert self._file is not None
            await self._file.merge_mechanism(mech_id, name, spec_id)

    async def _merge_dependency(
        self, from_id: str, to_id: str, spec_id: int | None = None
    ) -> None:
        if self._driver is not None:
            prefix = f"{spec_id}:" if spec_id is not None else ""
            await self._run_write(
                # 用两个 MERGE 定位节点，避免 (a),(b) 笛卡尔积告警
                "MERGE (a:Mechanism {id: $from_id}) "
                "MERGE (b:Mechanism {id: $to_id}) "
                "MERGE (a)-[:DEPENDS_ON]->(b)",
                from_id=f"{prefix}{from_id}", to_id=f"{prefix}{to_id}",
            )
        else:
            assert self._file is not None
            await self._file.merge_dependency(from_id, to_id, spec_id)

    async def impacted_by(
        self, spec_id: int, mech_id: str, max_depth: int = 2
    ) -> list[str]:
        """仅查询当前 spec 内受 mechanism 失败影响的机制范围。"""
        key = f"{spec_id}:{mech_id}"
        if self._driver is not None:
            query = (
                "MATCH (m:Mechanism)-[:DEPENDS_ON*1..%d]->(root:Mechanism {id: $id}) "
                "RETURN DISTINCT m.id AS id" % max_depth
            )
            async with self._driver.session() as session:
                result = await session.run(query, id=key)
                rows = await result.values()
            prefix = f"{spec_id}:"
            return sorted({str(r[0]).removeprefix(prefix) for r in rows})
        assert self._file is not None
        found = await self._file.impacted_by(key, max_depth)
        prefix = f"{spec_id}:"
        return sorted({item.removeprefix(prefix) for item in found})

    async def close(self) -> None:
        if self._driver is not None:
            await self._driver.close()
            self._driver = None
