"""Orchestrator：A2A Client + Server 编排核心。

- 启动时通过 Agent Card 发现全部下游 Agent
- 用户输入先过本地 BERT 意图分类：new_requirement / fix / query
- 按意图与产物所有权表路由；修复任务永远回到产物生产者
- Redis 维护任务状态 submitted → working → completed / failed
- 检查点写入 Redis 供前端实时展示
- 修复轮次上限 MAX_FIX_ROUNDS
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid

import httpx
from a2a.types import AgentCard

from app.agents.base import SkillAgent
from app.artifacts import register_artifact, task_directory
from app.a2a import call_agent, resolve_card
from app.config import (
    MAX_FIX_ROUNDS, MAX_REQUIREMENT_CHARS, ORCHESTRATOR_PORT,
    QA_TIMEOUT_SECONDS, SIM_CONCURRENCY, SIM_SESSIONS_PER_PLAYER,
)
from app.storage.kv import task_status_key

logger = logging.getLogger(__name__)

ASSETS_EXPECTED = ["assets/player.png", "assets/coin.png", "assets/player.glb"]


class OrchestratorAgent(SkillAgent):
    name = "orchestrator"
    port = ORCHESTRATOR_PORT
    description = "意图识别与全链路编排：规格→编码∥资产→审查∥验证→修复→模拟→验证→打包"
    skills_spec = [
        {
            "id": "orchestrate",
            "name": "编排流水线",
            "description": "接收用户任意输入，BERT 识别意图后路由：新需求走全链路，"
            "修复回产物所有者，查询返回任务状态",
        }
    ]

    def __init__(self, runtime) -> None:
        super().__init__()
        self._runtime = runtime
        self._cards: dict[str, AgentCard] = {}
        self._urls: dict[str, str] = {}
        self._last_task_id: str | None = None

    # ================= Agent 发现 =================
    async def discover(self, base_urls: list[str]) -> dict[str, str]:
        """按 A2A 协议拉取每个 Agent 的 Card 完成发现。"""
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as http:
            for url in base_urls:
                try:
                    card = await resolve_card(url.rstrip("/") + "/", http)
                    self._cards[card.name] = card
                    self._urls[card.name] = card.url
                    logger.info("[orchestrator] 发现 Agent: %s @ %s (skills=%s)",
                                card.name, card.url, [s.id for s in card.skills])
                except Exception as exc:  # noqa: BLE001
                    logger.error("[orchestrator] 发现失败 %s: %s", url, exc)
        return {name: c.url for name, c in self._cards.items()}

    def _url(self, name: str) -> str:
        if name not in self._urls:
            raise RuntimeError(f"Agent {name} 未被发现，无法路由")
        return self._urls[name]

    # ================= 入口：意图识别与路由 =================
    async def handle(self, payload: dict) -> dict:
        text: str = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("缺少输入文本 payload.text")
        if len(text) > MAX_REQUIREMENT_CHARS:
            raise ValueError(f"需求超过 {MAX_REQUIREMENT_CHARS} 字上限")

        intent = self._runtime.intent.classify(text)
        logger.info("[orchestrator] 意图=%s (%s, 置信度=%.2f)",
                    intent["intent"], intent["method"], intent["confidence"])

        if intent["intent"] == "new_requirement":
            return await self.run_pipeline(text)
        if intent["intent"] == "fix":
            return await self.handle_fix(text)
        return await self.handle_query(text)

    # ================= 新需求全链路 =================
    async def run_pipeline(self, text: str) -> dict:
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        self._last_task_id = task_id
        db = self._runtime.db
        task_directory(task_id)

        await db.upsert_task(
            task_id, intent="new_requirement", status="submitted",
            meta={"origin_requirement": text[:500], "schema_version": "1.0"},
        )
        await self._set_task_state(task_id, "submitted")
        await self._checkpoint(task_id, "intent", "BERT 意图识别: new_requirement，开始全链路")
        await self._set_task_state(task_id, "working")

        result: dict = {"task_id": task_id, "stages": {}}
        try:
            # ---------- 1. Spec ----------
            spec_res = await call_agent(
                self._url("spec_agent"), {"text": text, "task_id": task_id}
            )
            await db.set_artifact_owner(task_id, "spec", "spec_agent")
            result["stages"]["spec"] = {
                "spec_id": spec_res["spec_id"],
                "backend": spec_res.get("spec_backend"),
                "rag_titles": spec_res.get("rag_titles", []),
            }
            spec = spec_res["spec"]
            spec_id = spec_res["spec_id"]
            spec_path = task_directory(task_id) / "spec.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
            register_artifact(
                task_id, spec_path, "spec", "spec_agent", metadata={"spec_id": spec_id}
            )
            await self._checkpoint(
                task_id, "spec",
                f"规格「{spec['name']}」{len(spec['acceptance_assertions'])} 条断言已落库"
                f"（RAG 参考: {', '.join(spec_res.get('rag_titles', [])[:3]) or '无'}）",
            )
            await self._set_task_state(task_id, "working", meta_update={"spec_id": spec_id})

            # ---------- 2. Engineer ∥ Modeler（并行） ----------
            eng_res, mod_res = await asyncio.gather(
                call_agent(
                    self._url("engineer_agent"),
                    {"task_id": task_id, "spec": spec, "assets_expected": ASSETS_EXPECTED},
                    timeout=1200,
                ),
                call_agent(
                    self._url("modeler_agent"),
                    {"task_id": task_id, "spec": spec},
                ),
            )
            await db.set_artifact_owner(task_id, "game.html", "engineer_agent")
            await db.set_artifact_owner(task_id, "assets", "modeler_agent")
            entry = eng_res["artifact"]
            result["stages"]["build"] = {
                "engineer": {"backend": eng_res.get("engineer_backend"),
                             "artifact_id": eng_res.get("artifact_id"),
                             "version": eng_res.get("version"),
                             "contract_claims": eng_res.get("contract_claims", [])},
                "modeler": mod_res.get("modeler_backend"),
            }
            await self._checkpoint(
                task_id, "build",
                f"编码与资产并行完成：产出 {eng_res.get('artifact_id')}，"
                f"资产后端 {mod_res.get('modeler_backend')}；"
                f"可玩性由后续 QA 门禁判定",
            )

            # ---------- 3. Reviewer ∥ QA（并行） + 修复闭环 ----------
            qa_res, rev_res = await self._qa_round(task_id, entry, spec, spec_id)
            result["stages"]["review"] = {"verdict": rev_res.get("verdict"),
                                          "findings": rev_res.get("findings", [])}
            rounds = 0
            while self._quality_blocked(qa_res, rev_res) and rounds < MAX_FIX_ROUNDS:
                rounds += 1
                revision_failures = self._revision_failures(qa_res, rev_res)
                failed_ids = [item["id"] for item in revision_failures]
                impacted = await self._impacted_mechanisms(spec, qa_res, spec_id)
                if any(
                    f.get("rule") == "asset_exists"
                    for f in rev_res.get("findings", []) if f.get("level") == "error"
                ):
                    mod_res = await call_agent(
                        self._url("modeler_agent"),
                        {"task_id": task_id, "spec": spec, "repair_missing_assets": True},
                    )
                    await self._checkpoint(
                        task_id, f"fix_round_{rounds}",
                        f"资产缺失，先路由 Modeler 重建（backend={mod_res.get('modeler_backend')}）",
                    )
                await self._checkpoint(
                    task_id, f"fix_round_{rounds}",
                    f"质量门禁失败 {failed_ids}，按所有权表路由回 engineer_agent "
                    f"(影响机制: {impacted or '同层'})",
                )
                eng_res = await call_agent(
                    self._url("engineer_agent"),
                    {
                        "task_id": task_id, "spec": spec,
                        "assets_expected": ASSETS_EXPECTED,
                        "fix": {
                            "round": rounds,
                            "failed_assertions": revision_failures,
                            "failed_stage": qa_res.get("failed_stage") or "static_review",
                            "impacted_mechanisms": impacted,
                            "review_findings": [
                                f for f in rev_res.get("findings", [])
                                if f.get("level") == "error"
                            ],
                        },
                    },
                )
                entry = eng_res["artifact"]
                await self._checkpoint(
                    task_id, f"fix_round_{rounds}",
                    f"Engineer 产出 v{eng_res.get('version')}，重新执行 QA 与静态审查",
                )
                qa_res, rev_res = await self._qa_round(task_id, entry, spec, spec_id)
            await self._register_qa_artifacts(task_id, eng_res, qa_res, rev_res)
            result["stages"]["qa"] = {
                "passed": qa_res["passed"],
                "results": qa_res["results"],
                "screenshots": qa_res.get("screenshots", []),
                "fix_rounds": rounds,
            }
            result["stages"]["review"] = {"verdict": rev_res.get("verdict"),
                                          "findings": rev_res.get("findings", [])}
            if self._quality_blocked(qa_res, rev_res):
                reasons = [f"{f['id']}:{f['text']}" for f in qa_res["failed_assertions"]]
                reasons += [
                    f"静态审查:{f.get('detail')}" for f in rev_res.get("findings", [])
                    if f.get("level") == "error"
                ]
                await self._set_task_state(
                    task_id, "failed", round_no=rounds,
                    meta_update={"reason": "quality_gate_failed", "blocking": reasons[:10]},
                )
                result["final_status"] = "failed"
                result["summary"] = (
                    f"修复 {rounds} 轮后质量门禁仍未通过：" + "；".join(reasons[:4])
                )
                return result
            await self._checkpoint(
                task_id, "qa",
                f"质量门禁通过（含 {rounds} 轮修复），静态审查: {rev_res.get('verdict')}",
            )

            # ---------- 4. 分析链：Player Simulation（失败不阻塞交付） ----------
            # 三条链的失败语义必须分开：分析链（玩家实验/指标）只影响"决策参考"，
            # 交付链（打包）只影响"安装包"，都不能把已经通过质量门禁的可玩 MVP 判成失败。
            sim_res: dict = {}
            try:
                sim_res = await call_agent(
                    self._url("player_sim_agent"),
                    {
                        "task_id": task_id, "entry_path": entry,
                        "strategy": "casual",
                        # 并发与会话数走配置，避免"改了环境变量但不生效"
                        "num_players": SIM_CONCURRENCY,
                        "sessions_per_player": SIM_SESSIONS_PER_PLAYER,
                    },
                    timeout=900,
                )
                await db.set_artifact_owner(task_id, "player_events", "player_sim_agent")
                result["stages"]["player_sim"] = {
                    "players": sim_res.get("players"),
                    "by_status": sim_res.get("by_status"),
                    "events": sim_res.get("total_events"),
                    "llm_decisions": sim_res.get("llm_decisions_used"),
                }
                await self._checkpoint(
                    task_id, "player_sim",
                    f"合成玩家模拟完成，{sim_res.get('total_events')} 条行为事件落库"
                    f"（会话分布: {sim_res.get('by_status')}；非真人实验）",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[orchestrator] 玩家模拟失败（不阻塞交付）: %s", exc)
                result["stages"]["player_sim"] = {"error": f"{type(exc).__name__}: {exc}"}
                await self._checkpoint(
                    task_id, "player_sim",
                    f"合成玩家模拟失败，已跳过（不影响 MVP 交付）: {type(exc).__name__}",
                )

            # ---------- 5. 分析链：Market Validation（失败不阻塞交付） ----------
            val_res: dict = {}
            try:
                val_res = await call_agent(
                    self._url("market_agent"),
                    {"task_id": task_id, "spec_id": spec_id, "spec": spec},
                )
                await db.set_artifact_owner(task_id, "validation_report", "market_agent")
                result["stages"]["market"] = {
                    "report_id": val_res.get("report_id"),
                    "metrics": val_res.get("metrics"),
                }
                await self._checkpoint(
                    task_id, "market",
                    "合成行为实验报告已生成：" + val_res.get("summary_short", ""),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[orchestrator] 市场验证失败（不阻塞交付）: %s", exc)
                result["stages"]["market"] = {"error": f"{type(exc).__name__}: {exc}"}
                await self._checkpoint(
                    task_id, "market",
                    f"市场验证失败，已跳过（不影响 MVP 交付）: {type(exc).__name__}",
                )

            # ---------- 6. 交付链：Build & Deploy（失败降级为告警） ----------
            build_res: dict = {}
            try:
                build_res = await call_agent(
                    self._url("build_agent"),
                    {"task_id": task_id, "task_dir": eng_res["task_dir"]},
                    timeout=2400,
                )
                await db.set_artifact_owner(task_id, "apk", "build_agent")
                result["stages"]["build_deploy"] = {
                    "deploy_url": build_res.get("deploy_url"),
                    "apk_status": build_res.get("apk_status"),
                    "apk_path": build_res.get("apk_path"),
                    "h5_zip": build_res.get("h5_zip"),
                    "build_log": build_res.get("build_log", [])[-6:],
                }
                await self._checkpoint(
                    task_id, "build_deploy",
                    f"部署 URL 就绪: {build_res.get('deploy_url')}；APK: {build_res.get('apk_status')}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[orchestrator] 打包部署失败（降级为告警）: %s", exc)
                result["stages"]["build_deploy"] = {
                    "error": f"{type(exc).__name__}: {exc}"
                }
                await self._checkpoint(
                    task_id, "build_deploy",
                    f"打包部署失败，游戏本体仍可本地运行: {type(exc).__name__}",
                )

            # ---------- 收尾：MVP 就绪与安装包就绪分开表达 ----------
            apk_built = build_res.get("apk_status") == "built"
            final_status = "completed" if apk_built else "completed_with_warnings"
            await self._set_task_state(
                task_id, final_status, round_no=rounds,
                meta_update={
                    "spec_id": spec_id,
                    "mvp_ready": True,
                    "apk_status": build_res.get("apk_status") or "not_built",
                    "player_sim_ok": bool(sim_res),
                    "market_report_ok": bool(val_res),
                    "build_ok": bool(build_res),
                },
            )
            result["final_status"] = final_status
            result["summary"] = (
                f"MVP 质量门禁通过：{len(qa_res['results'])} 条断言全通过；"
                f"合成玩家实验 {sim_res.get('total_events', 0)} 条事件；"
                f"H5 可预览；APK {build_res.get('apk_status') or '未构建'}"
                + ("" if apk_built else "（未完成安装包构建，不代表安装包已验证）")
            )
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("[orchestrator] 任务 %s 流水线异常", task_id)
            await self._set_task_state(
                task_id, "failed", meta_update={"error": str(exc)[:300]},
            )
            result["final_status"] = "failed"
            result["summary"] = f"流水线异常: {type(exc).__name__}: {exc}"
            return result

    async def _qa_round(self, task_id: str, entry: str, spec: dict, spec_id: int):
        qa_res, rev_res = await asyncio.gather(
            call_agent(
                self._url("qa_agent"),
                {
                    "task_id": task_id, "entry_path": entry,
                    "assertions": spec["acceptance_assertions"],
                    "spec_id": spec_id, "spec": spec,
                },
                timeout=QA_TIMEOUT_SECONDS * 6,
            ),
            call_agent(
                self._url("reviewer_agent"),
                {"task_id": task_id, "entry_path": entry, "assets_expected": ASSETS_EXPECTED},
            ),
        )
        return qa_res, rev_res

    @staticmethod
    def _quality_blocked(qa_res: dict, rev_res: dict) -> bool:
        return (not qa_res.get("passed")) or rev_res.get("verdict") == "fail"

    @staticmethod
    def _revision_failures(qa_res: dict, rev_res: dict) -> list[dict]:
        failures = list(qa_res.get("failed_assertions") or [])
        for index, finding in enumerate(rev_res.get("findings") or [], 1):
            if finding.get("level") == "error":
                failures.append({
                    "id": f"R{index}",
                    "text": f"静态审查未通过：{finding.get('detail', '')}",
                    "mechanism": "构建契约",
                    "check_type": "review",
                    "evidence": finding.get("rule", ""),
                })
        return failures

    async def _impacted_mechanisms(
        self, spec: dict, qa_res: dict, spec_id: int
    ) -> list[str]:
        impacted: list[str] = []
        try:
            for mechanism in spec.get("mechanisms", []):
                if any(
                    mechanism["name"] in failure.get("mechanism", "")
                    for failure in qa_res.get("failed_assertions", [])
                ):
                    impacted = await self._runtime.graph.impacted_by(spec_id, mechanism["id"])
                    if impacted:
                        break
        except Exception:  # noqa: BLE001
            logger.warning("[orchestrator] 机制影响面查询失败，按同层处理")
        return impacted

    async def _register_qa_artifacts(
        self, task_id: str, eng_res: dict, qa_res: dict, rev_res: dict
    ) -> None:
        task_dir = task_directory(task_id)
        qa_dir = task_dir / "qa"
        qa_dir.mkdir(exist_ok=True)
        version = int(eng_res.get("version") or 1)
        report_path = qa_dir / f"qa_v{version}.json"
        report_path.write_text(
            json.dumps(qa_res, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        register_artifact(
            task_id, report_path, "qa_report", "qa_agent", version=version,
            inputs=[eng_res.get("artifact_id", "")],
        )
        review_path = qa_dir / f"review_v{version}.json"
        review_path.write_text(
            json.dumps(rev_res, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        register_artifact(
            task_id, review_path, "review_report", "reviewer_agent", version=version,
            inputs=[eng_res.get("artifact_id", "")],
        )
        for screenshot in qa_res.get("screenshots", []):
            path = task_dir / screenshot
            if path.exists():
                register_artifact(
                    task_id, path, "qa_screenshot", "qa_agent", version=version,
                    inputs=[eng_res.get("artifact_id", "")],
                )

    async def _set_task_state(
        self, task_id: str, status: str, round_no: int | None = None,
        meta_update: dict | None = None,
    ) -> None:
        """数据库是权威状态；KV 仅作为前端快速缓存，二者始终同步写入。"""
        db = self._runtime.db
        task = await db.get_task(task_id) or {}
        meta = task.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        meta = {**meta, **(meta_update or {})}
        await db.upsert_task(
            task_id, status=status, round_no=round_no, meta=meta
        )
        await self._runtime.kv.set(task_status_key(task_id), status)

    # ================= fix 意图：回到产物生产者 =================
    async def handle_fix(self, text: str) -> dict:
        db = self._runtime.db
        task_id = self._extract_task_id(text) or self._last_task_id
        if not task_id:
            return {"summary": "没有可修复的任务，请先提交一个新需求"}
        task = await db.get_task(task_id)
        if not task:
            return {"summary": f"任务 {task_id} 不存在"}
        meta = json.loads(task.get("meta") or "{}") if isinstance(task.get("meta"), str) else (task.get("meta") or {})
        spec_id = meta.get("spec_id")
        spec_row = await db.get_spec(spec_id) if spec_id else None
        if not spec_row:
            return {"summary": f"任务 {task_id} 没有关联规格，无法路由修复"}
        spec = spec_row["content"]
        assertions = await db.get_assertions(spec_id)
        failed = [a for a in assertions if a.get("status") == "fail"]
        if not failed:
            return {"summary": f"任务 {task_id} 当前没有失败断言，无需修复", "task_id": task_id}

        rounds = int(task.get("round") or 0) + 1
        if rounds > MAX_FIX_ROUNDS:
            return {"summary": f"任务 {task_id} 已达修复轮次上限 {MAX_FIX_ROUNDS}", "task_id": task_id}
        await self._checkpoint(
            task_id, f"manual_fix_{rounds}",
            f"用户触发修复，按所有权表路由回 engineer_agent"
            f"（断言 {[a['assertion_id'] for a in failed]}）",
        )
        await self._set_task_state(task_id, "working", round_no=rounds)
        eng_res = await call_agent(
            self._url("engineer_agent"),
            {
                "task_id": task_id, "spec": spec, "assets_expected": ASSETS_EXPECTED,
                "fix": {
                    "round": rounds,
                    "failed_assertions": [
                        {"id": a["assertion_id"], "text": a["text"], "mechanism": a.get("mechanism", "")}
                        for a in failed
                    ],
                    "failed_stage": "manual",
                },
            },
        )
        qa_res, rev_res = await self._qa_round(
            task_id, eng_res["artifact"], spec, spec_id
        )
        await self._register_qa_artifacts(task_id, eng_res, qa_res, rev_res)
        passed = not self._quality_blocked(qa_res, rev_res)
        status = "completed" if passed else "failed"
        await self._set_task_state(task_id, status, round_no=rounds)
        return {
            "task_id": task_id,
            "fix_round": rounds,
            "artifact_id": eng_res.get("artifact_id"),
            "contract_claims": eng_res.get("contract_claims", []),
            "qa_passed": qa_res["passed"],
            "review_verdict": rev_res.get("verdict"),
            "summary": (
                f"任务 {task_id} 第 {rounds} 轮修复完成，质量门禁"
                f"{'通过' if passed else '仍未通过'}"
            ),
        }

    # ================= query 意图 =================
    async def handle_query(self, text: str) -> dict:
        db, kv = self._runtime.db, self._runtime.kv
        task_id = self._extract_task_id(text) or self._last_task_id
        if not task_id:
            tasks = await db.list_tasks(5)
            return {"summary": "最近任务: " + (", ".join(
                f"{t['task_id']}({t['status']})" for t in tasks) or "无")}
        task = await db.get_task(task_id)
        if not task:
            return {"summary": f"任务 {task_id} 不存在", "task_id": task_id}
        status = task.get("status")
        cached_status = await kv.get(task_status_key(task_id))
        meta = task.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        spec_id = meta.get("spec_id")
        assertion_summary = ""
        if spec_id:
            rows = await db.get_assertions(spec_id)
            cnt = {"pass": 0, "fail": 0, "pending": 0}
            for a in rows:
                cnt[a.get("status", "pending")] = cnt.get(a.get("status", "pending"), 0) + 1
            assertion_summary = (
                f"；断言 {len(rows)} 条: 通过 {cnt['pass']} / 失败 {cnt['fail']} / 待验 {cnt['pending']}"
            )
        checkpoints = await self._get_checkpoints(task_id)
        return {
            "task_id": task_id,
            "status": status,
            "cached_status": cached_status,
            "round": task.get("round"),
            "assertions": assertion_summary,
            "checkpoints": checkpoints,
            "summary": f"任务 {task_id} 状态: {status}（第 {task.get('round')} 轮）{assertion_summary}",
        }

    # ================= 辅助 =================
    @staticmethod
    def _extract_task_id(text: str) -> str | None:
        m = re.search(r"task-[0-9a-f]{6,12}", text)
        return m.group(0) if m else None

    async def _checkpoint(self, task_id: str, stage: str, detail: str) -> None:
        import time

        cps = await self._get_checkpoints(task_id)
        cps.append({"stage": stage, "detail": detail, "ts": int(time.time())})
        await self._runtime.kv.set(
            f"task:{task_id}:checkpoints", json.dumps(cps, ensure_ascii=False), ttl=86400
        )
        logger.info("[orchestrator][%s] %s: %s", task_id, stage, detail)

    async def _get_checkpoints(self, task_id: str) -> list[dict]:
        raw = await self._runtime.kv.get(f"task:{task_id}:checkpoints")
        try:
            return json.loads(raw) if raw else []
        except json.JSONDecodeError:
            return []
