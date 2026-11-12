"""A2A 客户端调用助手：Orchestrator 通过 Agent Card 发现并调用下游 Agent。"""

from __future__ import annotations

import json
import logging

import httpx
from a2a.client import ClientConfig, ClientFactory, create_text_message_object
from a2a.types import AgentCard, DataPart, Message, Part, Task
from a2a.utils.parts import get_text_parts

logger = logging.getLogger(__name__)


async def resolve_card(base_url: str, httpx_client: httpx.AsyncClient) -> AgentCard:
    """通过 /.well-known/agent-card.json 发现 Agent。"""
    from a2a.client import A2ACardResolver

    resolver = A2ACardResolver(httpx_client=httpx_client, base_url=base_url)
    return await resolver.get_agent_card()


async def call_agent(base_url: str, payload: dict, timeout: float = 600.0) -> dict:
    """向下游 Agent 发送 A2A 消息，等待完成并提取结构化结果。

    结果来源优先级：最终 Task 的 DataPart artifact > 最终 agent message JSON > 文本。
    """
    # trust_env=False：A2A 全部是 127.0.0.1 内部调用，不能被系统代理
    # （Clash/VPN 常设置 HTTP_PROXY）劫持——否则请求会以 absolute-form 发出，
    # 服务端拿到 path="http://host:port/..." 直接 404。
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0), trust_env=False
    ) as http:
        config = ClientConfig(httpx_client=http, streaming=False)
        client = await ClientFactory.connect(base_url, client_config=config)
        message = create_text_message_object(
            content=json.dumps(payload, ensure_ascii=False)
        )
        final_task: Task | None = None
        final_message: Message | None = None
        async for event in client.send_message(message):
            # Client.send_message 产出 ClientEvent=(Task, UpdateEvent) 元组或 Message
            if isinstance(event, Message):
                final_message = event
            elif isinstance(event, Task):
                final_task = event
            elif isinstance(event, tuple) and event:
                task = event[0]
                if isinstance(task, Task):
                    final_task = task

    result: dict = {}
    if final_task is not None:
        for artifact in final_task.artifacts or []:
            for part in artifact.parts:
                root = part.root
                if isinstance(root, DataPart):
                    data = root.data
                    if isinstance(data, dict):
                        result.update(data)
        if final_task.status and final_task.status.message:
            texts = get_text_parts(final_task.status.message.parts)
            if texts and "summary" not in result:
                result["summary"] = texts[0]
        if final_task.status and final_task.status.state:
            result["a2a_state"] = str(final_task.status.state.value)
    elif final_message is not None:
        texts = get_text_parts(final_message.parts)
        raw = texts[0] if texts else ""
        try:
            parsed = json.loads(raw)
            result = parsed if isinstance(parsed, dict) else {"text": raw}
        except json.JSONDecodeError:
            result = {"text": raw}
    if not result:
        raise RuntimeError(f"Agent {base_url} 未返回结果")
    if result.get("a2a_state") == "failed":
        raise RuntimeError(f"Agent {base_url} 任务失败: {result.get('summary', '未知错误')}")
    return result


async def wait_agent_ready(base_url: str, timeout: float = 30.0) -> bool:
    """轮询 Agent Card 直至就绪。"""
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(timeout=2.0, trust_env=False) as http:
        while asyncio.get_event_loop().time() < deadline:
            try:
                resp = await http.get(
                    base_url.rstrip("/") + "/.well-known/agent-card.json"
                )
                if resp.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.3)
    return False
