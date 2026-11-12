"""Agent 基类：SkillAgent 业务逻辑 + A2A AgentExecutor 适配 + 服务构建。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.apps import A2AStarletteApplication
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    DataPart,
    Part,
    Task,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.utils import new_agent_text_message, new_task

logger = logging.getLogger(__name__)


class SkillAgent(ABC):
    """业务 Agent：实现 handle()，由 A2AExecutor 适配为协议层。"""

    name: str = "agent"
    port: int = 9000
    description: str = ""
    skills_spec: list[dict] = []  # [{"id","name","description"}]

    def __init__(self) -> None:
        self._heartbeat_kv = None

    def bind_kv(self, kv) -> None:
        self._heartbeat_kv = kv

    @abstractmethod
    async def handle(self, payload: dict) -> dict:
        """处理一条消息负载，返回结构化结果（写入 DataPart artifact）。"""

    def build_card(self, host: str = "localhost") -> AgentCard:
        return AgentCard(
            name=self.name,
            description=self.description,
            url=f"http://{host}:{self.port}/",
            version="0.1.0",
            capabilities=AgentCapabilities(streaming=True),
            default_input_modes=["text/plain", "application/json"],
            default_output_modes=["application/json", "text/plain"],
            preferred_transport="JSONRPC",
            skills=[
                AgentSkill(
                    id=s["id"], name=s["name"], description=s["description"],
                    tags=[self.name, s["id"]],
                    examples=s.get("examples"),
                )
                for s in self.skills_spec
            ],
        )

    async def heartbeat(self) -> None:
        if self._heartbeat_kv is not None:
            await self._heartbeat_kv.set(
                f"agent:{self.name}:heartbeat", str(int(time.time())), ttl=120
            )


class A2AExecutorWrapper(AgentExecutor):
    """把 SkillAgent.handle 适配为 A2A 事件流：
    submitted → working → artifact(DataPart) + completed / failed。
    """

    def __init__(self, agent: SkillAgent) -> None:
        self._agent = agent

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task: Task | None = context.current_task
        if task is None:
            task = new_task(context.message)  # type: ignore[arg-type]
            await event_queue.enqueue_event(task)
        task_id, context_id = task.id, task.context_id

        raw = context.get_user_input()
        try:
            payload = json.loads(raw) if raw.strip().startswith("{") else {"text": raw}
        except json.JSONDecodeError:
            payload = {"text": raw}
        payload.setdefault("task_id", task_id)

        await self._agent.heartbeat()
        logger.info("[%s] 收到任务 %s", self._agent.name, task_id)
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.working),
                final=False,
            )
        )
        try:
            result = await self._agent.handle(payload)
            from a2a.types import TaskArtifactUpdateEvent, Artifact
            from a2a.utils import new_data_artifact

            artifact = new_data_artifact(
                name=f"{self._agent.name}-result",
                data=result,
                description=f"{self._agent.name} 产物",
            )
            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    artifact=artifact,
                    append=False,
                    last_chunk=True,
                )
            )
            summary = str(result.get("summary", f"{self._agent.name} 完成"))
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    status=TaskStatus(
                        state=TaskState.completed,
                        message=new_agent_text_message(summary, context_id, task_id),
                    ),
                    final=True,
                )
            )
            logger.info("[%s] 任务 %s 完成", self._agent.name, task_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] 任务 %s 失败", self._agent.name, task_id)
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    status=TaskStatus(
                        state=TaskState.failed,
                        message=new_agent_text_message(
                            f"{type(exc).__name__}: {exc}", context_id, task_id
                        ),
                    ),
                    final=True,
                )
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id or str(uuid.uuid4()),
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.canceled),
                final=True,
            )
        )


def build_app(agent: SkillAgent, task_store: TaskStore | None = None) -> A2AStarletteApplication:
    handler = DefaultRequestHandler(
        agent_executor=A2AExecutorWrapper(agent),
        task_store=task_store or InMemoryTaskStore(),
    )
    return A2AStarletteApplication(agent_card=agent.build_card(), http_handler=handler)


def build_uvicorn_server(agent: SkillAgent) -> uvicorn.Server:
    app = build_app(agent).build()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=agent.port, log_level="warning", access_log=False
    )
    return uvicorn.Server(config)


async def serve_agents(agents: list[SkillAgent]) -> list[uvicorn.Server]:
    """在同一事件循环中并行启动多个 Agent 的 A2A server。"""
    servers = [build_uvicorn_server(a) for a in agents]
    await asyncio.gather(*(s.serve() for s in servers))
    return servers
