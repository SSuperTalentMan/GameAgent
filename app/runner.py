"""Demo 启动器：

    python -m app.runner "做一个接金币的小游戏"
    python -m app.runner --no-wait   # 不在结束时等待回车

启动 8 个下游 Agent + Orchestrator 的 A2A 服务，完成 Card 发现后，
向 Orchestrator 发送 A2A 消息驱动全链路，并演示 query 意图。
同时为 Streamlit 前端暴露 /api/* 查询路由。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

import uvicorn
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app import config
from app.a2a import call_agent, wait_agent_ready
from app.agents import (
    BuildDeployAgent, EngineerAgent, MarketValidationAgent, ModelerAgent,
    PlayerSimulationAgent, QAAgent, ReviewerAgent, SpecAgent,
    build_app, build_uvicorn_server,
)
from app.orchestrator import OrchestratorAgent
from app.runtime import Runtime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("runner")


def _json_response(data, status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(data, ensure_ascii=False, default=str),
        media_type="application/json",
        status_code=status_code,
    )


def build_api_routes(orch: OrchestratorAgent, runtime: Runtime) -> list[Route]:
    """前端查询 API（挂载在 Orchestrator 的 Starlette 应用上）。"""

    async def health(request):
        return _json_response({
            "status": "ok",
            "backends": runtime.backends,
            "agents": {name: card.url for name, card in orch._cards.items()},
        })

    async def tasks(request):
        rows = await runtime.db.list_tasks(20)
        return _json_response(rows)

    async def task_detail(request):
        task_id = request.path_params["task_id"]
        task = await runtime.db.get_task(task_id)
        if not task:
            return _json_response({"error": "task not found"}, status_code=404)
        meta = task.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        spec_id = meta.get("spec_id")
        spec = await runtime.db.get_spec(spec_id) if spec_id else None
        assertions = await runtime.db.get_assertions(spec_id) if spec_id else []
        cps_raw = await runtime.kv.get(f"task:{task_id}:checkpoints")
        try:
            checkpoints = json.loads(cps_raw) if cps_raw else []
        except json.JSONDecodeError:
            checkpoints = []
        status = await runtime.kv.get(f"task:{task_id}:status")
        validation = await runtime.db.get_validation_report(task_id)
        build = await runtime.db.get_build_artifact(task_id)
        return _json_response({
            "task": {**task, "meta": meta},
            "kv_status": status,
            "checkpoints": checkpoints,
            "spec": spec,
            "assertions": assertions,
            "validation": validation,
            "build": build,
        })

    return [
        Route("/api/health", health),
        Route("/api/tasks", tasks),
        Route("/api/tasks/{task_id}", task_detail),
    ]


def build_orchestrator_server(orch: OrchestratorAgent, runtime: Runtime) -> uvicorn.Server:
    a2a_app = build_app(orch)
    starlette_app = a2a_app.build()
    starlette_app.router.routes.extend(build_api_routes(orch, runtime))
    cfg = uvicorn.Config(
        starlette_app, host="127.0.0.1", port=orch.port,
        log_level="warning", access_log=False,
    )
    return uvicorn.Server(cfg)


DEFAULT_REQUIREMENT = (
    "帮我做一个接金币的网页小游戏：玩家用左右方向键移动小怪物，接住下落的金币得分，"
    "漏接太多会失败；分数达到目标就胜利并显示恭喜文案。"
)


async def main(requirement: str | None = None, wait: bool | None = None) -> None:
    """一键入口：requirement/wait 为 None 时回落到命令行参数。"""
    argv_req = (
        sys.argv[1]
        if len(sys.argv) > 1 and not sys.argv[1].startswith("--")
        else None
    )
    text = requirement or argv_req or DEFAULT_REQUIREMENT
    do_wait = ("--no-wait" not in sys.argv) if wait is None else wait

    runtime = await Runtime.create()
    logger.info("后端栈: %s", json.dumps(runtime.backends, ensure_ascii=False))

    agents = [
        SpecAgent(runtime), EngineerAgent(runtime), ModelerAgent(runtime),
        QAAgent(runtime), ReviewerAgent(runtime),
        PlayerSimulationAgent(runtime), MarketValidationAgent(runtime),
        BuildDeployAgent(runtime),
    ]
    for a in agents:
        a.bind_kv(runtime.kv)
    orch = OrchestratorAgent(runtime)
    orch.bind_kv(runtime.kv)

    servers = [build_uvicorn_server(a) for a in agents]
    servers.append(build_orchestrator_server(orch, runtime))
    server_tasks = [asyncio.create_task(s.serve()) for s in servers]

    try:
        # 等待全部就绪
        urls = [f"http://localhost:{a.port}" for a in agents] + [f"http://localhost:{orch.port}"]
        for url in urls:
            if not await wait_agent_ready(url, timeout=20):
                logger.error("Agent %s 未就绪", url)
                raise RuntimeError(f"Agent {url} 未就绪")
        logger.info("全部 %d 个 A2A 服务就绪", len(urls))

        # A2A Card 发现
        discovered = await orch.discover(urls[:-1])
        logger.info("Card 发现完成: %s", json.dumps(discovered, indent=2, ensure_ascii=False))

        # ---- demo: new_requirement 全链路 ----
        logger.info(">>> 发送需求: %s", text)
        result = await call_agent(f"http://localhost:{orch.port}", {"text": text}, timeout=3600)
        print("\n" + "=" * 70)
        print("全链路结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str)[:4000])
        print("=" * 70)

        # ---- demo: query 意图 ----
        task_id = result.get("task_id", "")
        if result.get("final_status") in ("completed", "completed_with_warnings"):
            q = await call_agent(
                f"http://localhost:{orch.port}", {"text": f"查询任务 {task_id} 的状态"}
            )
            print("\n[query 意图演示]", json.dumps(q, ensure_ascii=False, indent=2, default=str)[:1500])

        deploy_hint = f"http://localhost:{config.DEPLOY_PORT}/{task_id}/game.html"
        print(f"\n游戏预览: {deploy_hint}")
        print("前端面板: uv run streamlit run app/frontend.py")

        if do_wait:
            print("\n按回车退出（保持服务运行以便浏览器预览/前端访问）...")
            await asyncio.to_thread(input)
    finally:
        for s in servers:
            s.should_exit = True
        await asyncio.gather(*server_tasks, return_exceptions=True)
        await runtime.close()
        logger.info("已退出")


if __name__ == "__main__":
    asyncio.run(main())
