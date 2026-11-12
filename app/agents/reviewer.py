"""Reviewer Agent（P1）：静态审查，与 QA 平行由编排器调用，报告写入 MySQL。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app.agents.engineer import validate_game_code
from app.agents.base import SkillAgent
from app.config import GENERATED_DIR, REVIEWER_PORT

logger = logging.getLogger(__name__)


class ReviewerAgent(SkillAgent):
    name = "reviewer_agent"
    port = REVIEWER_PORT
    description = "对构建产物做静态审查（契约完整性/离线性/资产引用/风险点）"
    skills_spec = [
        {
            "id": "static_review",
            "name": "静态审查",
            "description": "输入构建入口，输出 verdict 与 findings 列表，报告落库",
        }
    ]

    def __init__(self, runtime) -> None:
        super().__init__()
        self._runtime = runtime

    async def handle(self, payload: dict) -> dict:
        task_id: str = payload["task_id"]
        entry: str = payload["entry_path"]
        code = Path(entry).read_text(encoding="utf-8")
        assets_expected: list[str] = payload.get("assets_expected", [])

        findings: list[dict] = []

        # 1. 自动化契约静态检查（与 Engineer 共用规则）
        for p in validate_game_code(code, assets_expected):
            findings.append({"level": "error", "rule": "contract", "detail": p})

        # 2. 离线性：禁止外部网络引用
        for m in re.finditer(r"(src|href)\s*=\s*[\"']([^\"']+)[\"']", code, re.I):
            if m.group(2).startswith(("http://", "https://", "//")):
                findings.append(
                    {"level": "error", "rule": "offline", "detail": f"外部资源引用: {m.group(2)}"}
                )

        # 3. 风险点：动态执行与内联事件
        if re.search(r"\beval\s*\(", code):
            findings.append({"level": "error", "rule": "no_eval", "detail": "使用了 eval()"})
        if re.search(r"\binnerHTML\s*=", code):
            findings.append({"level": "warn", "rule": "no_innerhtml", "detail": "直接写 innerHTML，注意 XSS 风险"})
        for pattern, rule, detail in (
            (r"\bfetch\s*\(", "no_fetch", "使用了 fetch() 外部请求"),
            (r"\bXMLHttpRequest\b", "no_xhr", "使用了 XMLHttpRequest"),
            (r"\bWebSocket\b", "no_websocket", "使用了 WebSocket"),
            (r"createElement\s*\(\s*['\"]script", "no_dynamic_script", "动态插入 script"),
            (r"\bon\w+\s*=\s*['\"]", "inline_handler", "使用了内联事件处理器"),
        ):
            if re.search(pattern, code, re.IGNORECASE):
                findings.append({"level": "error", "rule": rule, "detail": detail})

        # 4. 资产引用存在性
        task_dir = Path(entry).parent
        for m in re.finditer(r"[\"'](assets/[^\"']+)[\"']", code):
            ref = m.group(1)
            if not (task_dir / ref).exists():
                findings.append(
                    {"level": "error", "rule": "asset_exists", "detail": f"引用的资产不存在: {ref}"}
                )

        errors = [f for f in findings if f["level"] == "error"]
        warns = [f for f in findings if f["level"] == "warn"]
        verdict = "fail" if errors else ("warn" if warns else "pass")

        await self._runtime.db.save_review_report(task_id, self.name, verdict, findings)
        logger.info("[reviewer] 任务 %s 静态审查: %s (%d findings)", task_id, verdict, len(findings))
        return {
            "verdict": verdict,
            "findings": findings,
            "summary": f"静态审查 {verdict}：{len(errors)} 错误 / {len(warns)} 警告",
        }
