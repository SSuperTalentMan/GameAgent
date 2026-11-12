"""LLM Gateway：所有 Agent 共用，走百炼 OpenAI 兼容接口。

- API_KEY 从环境变量读取，禁止硬编码
- 按 prompt hash 缓存到 Redis（不可达时内存），降低修复迭代成本
- 模型不可用时按回退列表切换
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from openai import AsyncOpenAI

from app import config
from app.storage.kv import KVStore, llm_cache_key

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 24 * 3600


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> dict | list:
    """从 LLM 输出中稳健地提取 JSON（容忍 ```json 围栏与前后缀文本）。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 回退：截取第一个 { 或 [ 到与之配对的结束符
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == open_ch:
                depth += 1
            elif text[i] == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise LLMError(f"LLM 输出无法解析为 JSON: {text[:200]}")


class LLMGateway:
    def __init__(self, kv: KVStore) -> None:
        self._kv = kv
        self._client: AsyncOpenAI | None = None
        self._working_model: str | None = None
        if config.API_KEY:
            self._client = AsyncOpenAI(
                api_key=config.API_KEY,
                base_url=config.LLM_BASE_URL,
                timeout=config.LLM_TIMEOUT_SECONDS,
                max_retries=1,
            )

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def model_name(self) -> str:
        return self._working_model or config.LLM_MODEL

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        json_mode: bool = False,
        use_cache: bool = True,
    ) -> str:
        """返回文本。json_mode=True 时要求 JSON 输出。"""
        if self._client is None:
            raise LLMError("API_KEY 未配置，LLM 网关不可用")

        cache_key = llm_cache_key(
            hashlib.sha256(
                json.dumps(
                    {
                        "m": self.model_name,
                        "t": temperature,
                        "j": json_mode,
                        "msgs": messages,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
        )
        if use_cache:
            cached = await self._kv.get(cache_key)
            if cached is not None:
                logger.info("[LLM] 命中缓存 %s", cache_key[:16])
                return cached

        errors: list[str] = []
        candidates = [self._working_model or config.LLM_MODEL] + [
            m for m in config.LLM_FALLBACK_MODELS if m != self._working_model
        ]
        for model in candidates:
            try:
                content = await self._call_model(model, messages, temperature, json_mode)
                self._working_model = model
                if use_cache:
                    await self._kv.set(cache_key, content, ttl=CACHE_TTL_SECONDS)
                return content
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{model}: {exc}")
                logger.warning("[LLM] 模型 %s 调用失败，尝试下一个: %s", model, exc)
        raise LLMError("所有候选模型均调用失败: " + " | ".join(errors))

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        executor,
        *,
        temperature: float = 0.0,
        max_rounds: int = 4,
    ) -> str:
        """工具调用循环（文档 9.1 R6）：模型选择工具 → 本地执行 → 回填结果 → 直到最终答复。

        executor: async (tool_name: str, args: dict) -> dict
        """
        if self._client is None:
            raise LLMError("API_KEY 未配置，LLM 网关不可用")
        working = list(messages)
        for _ in range(max_rounds):
            # 网络/代理/鉴权异常统一收敛成 LLMError：调用方只需要处理一种异常，
            # 否则底层 openai.APIConnectionError 会穿透到 Agent 层把整条流水线打挂
            try:
                resp = await self._client.chat.completions.create(
                    model=self.model_name, messages=working, tools=tools,
                    temperature=temperature,
                )
            except Exception as exc:  # noqa: BLE001
                raise LLMError(f"工具调用请求失败: {type(exc).__name__}: {exc}") from exc
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return msg.content or ""
            working.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                }
            )
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    result = await executor(tc.function.name, args)
                except Exception as exc:  # noqa: BLE001
                    result = {"error": str(exc)}
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        raise LLMError("工具调用轮次超限，未得到最终答复")

    async def _call_model(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
        json_mode: bool,
    ) -> str:
        assert self._client is not None
        kwargs: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        # 流式：长生成不会被中间网关的空闲超时掐断
        kwargs["stream"] = True
        try:
            stream = await self._client.chat.completions.create(**kwargs)
            parts: list[str] = []
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    parts.append(delta.content)
            return "".join(parts)
        except Exception as exc:  # noqa: BLE001
            if json_mode and _is_response_format_error(exc):
                kwargs.pop("response_format", None)
                stream = await self._client.chat.completions.create(**kwargs)
                parts = []
                async for chunk in stream:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta and delta.content:
                        parts.append(delta.content)
                return "".join(parts)
            raise


def _is_response_format_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "response_format" in text or "json_object" in text
