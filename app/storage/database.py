"""关系库：MySQL（asyncmy 异步驱动），不可达时降级 SQLite。

表：specs / assertions / review_reports / task_state / artifact_owners
规格与断言以 JSON 字符串存储（Demo 阶段不过度范式化）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from app import config

logger = logging.getLogger(__name__)

MYSQL_DDL = [
    """CREATE TABLE IF NOT EXISTS specs (
        id INT AUTO_INCREMENT PRIMARY KEY,
        task_id VARCHAR(64) NOT NULL,
        name VARCHAR(256),
        content JSON,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_specs_task (task_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS assertions (
        id INT AUTO_INCREMENT PRIMARY KEY,
        spec_id INT NOT NULL,
        assertion_id VARCHAR(32) NOT NULL,
        text TEXT,
        mechanism VARCHAR(128),
        status VARCHAR(16) DEFAULT 'pending',
        detail TEXT,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_assertion (spec_id, assertion_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS review_reports (
        id INT AUTO_INCREMENT PRIMARY KEY,
        task_id VARCHAR(64) NOT NULL,
        agent VARCHAR(64),
        verdict VARCHAR(16),
        findings JSON,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS task_state (
        task_id VARCHAR(64) PRIMARY KEY,
        intent VARCHAR(32),
        status VARCHAR(32),
        round INT DEFAULT 0,
        meta JSON,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS artifact_owners (
        task_id VARCHAR(64) NOT NULL,
        artifact VARCHAR(128) NOT NULL,
        owner_agent VARCHAR(64) NOT NULL,
        PRIMARY KEY (task_id, artifact)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS player_events (
        id INT AUTO_INCREMENT PRIMARY KEY,
        task_id VARCHAR(64) NOT NULL,
        session_id VARCHAR(64) NOT NULL,
        event_type VARCHAR(32) NOT NULL,
        ts BIGINT,
        payload JSON,
        INDEX idx_pe_task (task_id),
        INDEX idx_pe_session (session_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS validation_reports (
        id INT AUTO_INCREMENT PRIMARY KEY,
        report_id VARCHAR(64) NOT NULL,
        task_id VARCHAR(64),
        spec_id INT,
        metrics JSON,
        summary TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_report (report_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS build_artifacts (
        id INT AUTO_INCREMENT PRIMARY KEY,
        build_id VARCHAR(64) NOT NULL,
        task_id VARCHAR(64),
        platform VARCHAR(32),
        file_path VARCHAR(512),
        deploy_url VARCHAR(512),
        build_log TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_build (build_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

SQLITE_DDL = [
    """CREATE TABLE IF NOT EXISTS specs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        name TEXT,
        content TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS assertions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        spec_id INTEGER NOT NULL,
        assertion_id TEXT NOT NULL,
        text TEXT,
        mechanism TEXT,
        status TEXT DEFAULT 'pending',
        detail TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (spec_id, assertion_id)
    )""",
    """CREATE TABLE IF NOT EXISTS review_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        agent TEXT,
        verdict TEXT,
        findings TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS task_state (
        task_id TEXT PRIMARY KEY,
        intent TEXT,
        status TEXT,
        round INTEGER DEFAULT 0,
        meta TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS artifact_owners (
        task_id TEXT NOT NULL,
        artifact TEXT NOT NULL,
        owner_agent TEXT NOT NULL,
        PRIMARY KEY (task_id, artifact)
    )""",
    """CREATE TABLE IF NOT EXISTS player_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        ts INTEGER,
        payload TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS idx_pe_task ON player_events (task_id)""",
    """CREATE TABLE IF NOT EXISTS validation_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id TEXT NOT NULL,
        task_id TEXT,
        spec_id INTEGER,
        metrics TEXT,
        summary TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (report_id)
    )""",
    """CREATE TABLE IF NOT EXISTS build_artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        build_id TEXT NOT NULL,
        task_id TEXT,
        platform TEXT,
        file_path TEXT,
        deploy_url TEXT,
        build_log TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (build_id)
    )""",
]


class Database:
    """统一异步关系库接口。MySQL 优先，连接失败自动降级 SQLite。"""

    def __init__(self) -> None:
        self._mysql_conn = None  # asyncmy connection
        self._sqlite_conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._ph = "%s"  # 占位符：MySQL %s / SQLite ?

    @property
    def backend(self) -> str:
        return "mysql" if self._mysql_conn is not None else "sqlite"

    # ---------- 连接 ----------
    async def connect(self) -> None:
        if not config.FORCE_SQLITE and config.MYSQL_HOST:
            try:
                await self._connect_mysql()
                logger.info(
                    "[DB] MySQL 已连接: %s:%s/%s",
                    config.MYSQL_HOST,
                    config.MYSQL_PORT,
                    config.MYSQL_DATABASE,
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("[DB] MySQL 不可达 (%s)，降级 SQLite: %s", exc, config.SQLITE_PATH)
        self._connect_sqlite()
        logger.info("[DB] SQLite 就绪: %s", config.SQLITE_PATH)

    async def _connect_mysql(self) -> None:
        import asyncmy  # 延迟导入：仅配置了 MySQL 时才需要

        self._ph = "%s"
        try:
            conn = await asyncmy.connect(
                host=config.MYSQL_HOST,
                port=config.MYSQL_PORT,
                user=config.MYSQL_USER,
                password=config.MYSQL_PASSWORD,
                autocommit=True,
                connect_timeout=3,
            )
            async with conn.cursor() as cur:
                await cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS {config.MYSQL_DATABASE} "
                    "DEFAULT CHARACTER SET utf8mb4"
                )
                await cur.execute(f"USE {config.MYSQL_DATABASE}")
            conn.close()
        except Exception:
            raise
        self._mysql_conn = await asyncmy.connect(
            host=config.MYSQL_HOST,
            port=config.MYSQL_PORT,
            user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD,
            db=config.MYSQL_DATABASE,
            autocommit=True,
            connect_timeout=3,
        )
        for ddl in MYSQL_DDL:
            async with self._mysql_conn.cursor() as cur:
                await cur.execute(ddl)

    def _connect_sqlite(self) -> None:
        self._ph = "?"
        Path(config.SQLITE_PATH).parent.mkdir(parents=True, exist_ok=True)
        self._sqlite_conn = sqlite3.connect(
            config.SQLITE_PATH, check_same_thread=False
        )
        self._sqlite_conn.row_factory = sqlite3.Row
        for ddl in SQLITE_DDL:
            self._sqlite_conn.execute(ddl)
        self._sqlite_conn.commit()

    # ---------- 底层执行 ----------
    async def execute(self, sql: str, args: tuple = ()) -> None:
        await self._with_reconnect(self._execute_once, sql, args)

    async def _execute_once(self, sql: str, args: tuple) -> None:
        if self._mysql_conn is not None:
            async with self._mysql_conn.cursor() as cur:
                await cur.execute(sql, args)
        else:
            async with self._lock:
                await asyncio.to_thread(self._sqlite_exec, sql, args)

    def _sqlite_exec(self, sql: str, args: tuple) -> None:
        assert self._sqlite_conn is not None
        self._sqlite_conn.execute(sql, args)
        self._sqlite_conn.commit()

    async def query_all(self, sql: str, args: tuple = ()) -> list[dict]:
        return await self._with_reconnect(self._query_all_once, sql, args)

    async def _query_all_once(self, sql: str, args: tuple) -> list[dict]:
        if self._mysql_conn is not None:
            async with self._mysql_conn.cursor() as cur:
                await cur.execute(sql, args)
                rows = await cur.fetchall()
                cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
        async with self._lock:
            assert self._sqlite_conn is not None
            cur = self._sqlite_conn.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
        return rows

    async def query_one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = await self.query_all(sql, args)
        return rows[0] if rows else None

    async def insert_returning_id(self, sql: str, args: tuple) -> int:
        """插入并返回自增 id（MySQL: LAST_INSERT_ID；SQLite: lastrowid）。"""
        return await self._with_reconnect(self._insert_once, sql, args)

    async def _insert_once(self, sql: str, args: tuple) -> int:
        if self._mysql_conn is not None:
            async with self._mysql_conn.cursor() as cur:
                await cur.execute(sql, args)
                await cur.execute("SELECT LAST_INSERT_ID()")
                row = await cur.fetchone()
            return int(row[0])
        async with self._lock:
            assert self._sqlite_conn is not None
            cur = self._sqlite_conn.execute(sql, args)
            rowid = cur.lastrowid
            self._sqlite_conn.commit()
            cur.close()
        return int(rowid)

    async def _with_reconnect(self, fn, *args):
        """MySQL 长流水线中连接可能被 wait_timeout 掐断：断线自动重连并重试一次。"""
        try:
            return await fn(*args)
        except Exception as exc:  # noqa: BLE001
            if self._mysql_conn is None or not _is_connection_error(exc):
                raise
            logger.warning("[DB] 连接中断 (%s)，重连后重试", exc)
            await self._reconnect_mysql()
            return await fn(*args)

    async def _reconnect_mysql(self) -> None:
        try:
            self._mysql_conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._mysql_conn = None
        await self._connect_mysql()

    # ---------- 业务方法 ----------
    async def save_spec(self, task_id: str, name: str, content: dict) -> int:
        return await self.insert_returning_id(
            f"INSERT INTO specs (task_id, name, content) VALUES ({self._ph}, {self._ph}, {self._ph})",
            (task_id, name, json.dumps(content, ensure_ascii=False)),
        )

    async def save_assertions(self, spec_id: int, assertions: list[dict]) -> None:
        for a in assertions:
            await self.execute(
                f"INSERT INTO assertions (spec_id, assertion_id, text, mechanism, status) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, 'pending')",
                (spec_id, a["id"], a.get("text", ""), a.get("mechanism", "")),
            )

    async def update_assertion(
        self, spec_id: int, assertion_id: str, status: str, detail: str = ""
    ) -> None:
        await self.execute(
            f"UPDATE assertions SET status={self._ph}, detail={self._ph} "
            f"WHERE spec_id={self._ph} AND assertion_id={self._ph}",
            (status, detail, spec_id, assertion_id),
        )

    async def get_assertions(self, spec_id: int) -> list[dict]:
        return await self.query_all(
            f"SELECT assertion_id, text, mechanism, status, detail FROM assertions "
            f"WHERE spec_id={self._ph} ORDER BY id",
            (spec_id,),
        )

    async def save_review_report(
        self, task_id: str, agent: str, verdict: str, findings: list | dict
    ) -> None:
        await self.execute(
            f"INSERT INTO review_reports (task_id, agent, verdict, findings) "
            f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph})",
            (task_id, agent, verdict, json.dumps(findings, ensure_ascii=False)),
        )

    async def upsert_task(
        self,
        task_id: str,
        intent: str | None = None,
        status: str | None = None,
        round_no: int | None = None,
        meta: dict | None = None,
    ) -> None:
        row = await self.query_one(
            f"SELECT task_id FROM task_state WHERE task_id={self._ph}", (task_id,)
        )
        if row is None:
            await self.execute(
                f"INSERT INTO task_state (task_id, intent, status, round, meta) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph})",
                (task_id, intent, status, round_no or 0,
                 json.dumps(meta or {}, ensure_ascii=False)),
            )
            return
        sets, args = [], []
        ph = self._ph
        if intent is not None:
            sets.append(f"intent={ph}"); args.append(intent)
        if status is not None:
            sets.append(f"status={ph}"); args.append(status)
        if round_no is not None:
            sets.append(f"round={ph}"); args.append(round_no)
        if meta is not None:
            sets.append(f"meta={ph}"); args.append(json.dumps(meta, ensure_ascii=False))
        if sets:
            args.append(task_id)
            await self.execute(
                f"UPDATE task_state SET {', '.join(sets)} WHERE task_id={ph}", tuple(args)
            )

    async def get_task(self, task_id: str) -> dict | None:
        return await self.query_one(
            f"SELECT * FROM task_state WHERE task_id={self._ph}", (task_id,)
        )

    async def list_tasks(self, limit: int = 20) -> list[dict]:
        return await self.query_all(
            f"SELECT * FROM task_state ORDER BY updated_at DESC LIMIT {int(limit)}"
        )

    async def get_spec(self, spec_id: int) -> dict | None:
        row = await self.query_one(
            f"SELECT id, task_id, name, content FROM specs WHERE id={self._ph}", (spec_id,)
        )
        if row and row.get("content"):
            try:
                row["content"] = json.loads(row["content"])
            except json.JSONDecodeError:
                pass
        return row

    async def set_artifact_owner(self, task_id: str, artifact: str, owner_agent: str) -> None:
        if self._mysql_conn is not None:
            await self.execute(
                f"INSERT INTO artifact_owners (task_id, artifact, owner_agent) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph}) "
                f"ON DUPLICATE KEY UPDATE owner_agent=VALUES(owner_agent)",
                (task_id, artifact, owner_agent),
            )
        else:
            await self.execute(
                f"INSERT OR REPLACE INTO artifact_owners (task_id, artifact, owner_agent) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph})",
                (task_id, artifact, owner_agent),
            )

    async def get_artifact_owner(self, task_id: str, artifact: str) -> str | None:
        row = await self.query_one(
            f"SELECT owner_agent FROM artifact_owners "
            f"WHERE task_id={self._ph} AND artifact={self._ph}",
            (task_id, artifact),
        )
        return row["owner_agent"] if row else None

    # ---------- 玩家模拟 / 市场验证 / 构建产物 (文档 9.4) ----------
    async def insert_player_events(self, events: list[dict]) -> int:
        for e in events:
            await self.execute(
                f"INSERT INTO player_events (task_id, session_id, event_type, ts, payload) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph})",
                (
                    e["task_id"],
                    e["session_id"],
                    e["event_type"],
                    int(e.get("ts", 0)),
                    json.dumps(e.get("payload", {}), ensure_ascii=False),
                ),
            )
        return len(events)

    async def get_player_events(self, task_id: str) -> list[dict]:
        rows = await self.query_all(
            f"SELECT session_id, event_type, ts, payload FROM player_events "
            f"WHERE task_id={self._ph} ORDER BY id",
            (task_id,),
        )
        for r in rows:
            try:
                r["payload"] = json.loads(r.get("payload") or "{}")
            except json.JSONDecodeError:
                r["payload"] = {}
        return rows

    async def save_validation_report(
        self, report_id: str, task_id: str, spec_id: int, metrics: dict, summary: str
    ) -> None:
        if self._mysql_conn is not None:
            await self.execute(
                f"INSERT INTO validation_reports (report_id, task_id, spec_id, metrics, summary) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph}) "
                f"ON DUPLICATE KEY UPDATE metrics=VALUES(metrics), summary=VALUES(summary)",
                (report_id, task_id, spec_id,
                 json.dumps(metrics, ensure_ascii=False), summary),
            )
        else:
            await self.execute(
                f"INSERT OR REPLACE INTO validation_reports "
                f"(report_id, task_id, spec_id, metrics, summary) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph})",
                (report_id, task_id, spec_id,
                 json.dumps(metrics, ensure_ascii=False), summary),
            )

    async def get_validation_report(self, task_id: str) -> dict | None:
        row = await self.query_one(
            f"SELECT report_id, task_id, spec_id, metrics, summary FROM validation_reports "
            f"WHERE task_id={self._ph} ORDER BY id DESC",
            (task_id,),
        )
        if row:
            try:
                row["metrics"] = json.loads(row.get("metrics") or "{}")
            except json.JSONDecodeError:
                row["metrics"] = {}
        return row

    async def save_build_artifact(
        self, build_id: str, task_id: str, platform: str,
        file_path: str, deploy_url: str, build_log: str,
    ) -> None:
        if self._mysql_conn is not None:
            await self.execute(
                f"INSERT INTO build_artifacts (build_id, task_id, platform, file_path, deploy_url, build_log) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph}) "
                f"ON DUPLICATE KEY UPDATE file_path=VALUES(file_path), deploy_url=VALUES(deploy_url)",
                (build_id, task_id, platform, file_path, deploy_url, build_log),
            )
        else:
            await self.execute(
                f"INSERT OR REPLACE INTO build_artifacts "
                f"(build_id, task_id, platform, file_path, deploy_url, build_log) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph})",
                (build_id, task_id, platform, file_path, deploy_url, build_log),
            )

    async def get_build_artifact(self, task_id: str) -> dict | None:
        return await self.query_one(
            f"SELECT build_id, platform, file_path, deploy_url, build_log FROM build_artifacts "
            f"WHERE task_id={self._ph} ORDER BY id DESC",
            (task_id,),
        )

    async def close(self) -> None:
        if self._mysql_conn is not None:
            self._mysql_conn.close()
            self._mysql_conn = None
        if self._sqlite_conn is not None:
            self._sqlite_conn.close()
            self._sqlite_conn = None


def _is_connection_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        key in text
        for key in (
            "gone away", "lost connection", "closed", "broken pipe",
            "2006", "2013", "connection reset", "not connected", "timeout",
            "operationalerror", "interfaceerror",
        )
    )
