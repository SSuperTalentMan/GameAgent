"""Engineer Agent：根据规格生成可运行的 Canvas 游戏（含修复闭环）。

每次生成/修复必须附带"本次修改声称解决的契约条目 ID"（contract_claims），
供编排器判断修复是否闭环。
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from app.agents.base import SkillAgent
from app.agents.game_template import render_template
from app.artifacts import register_artifact, task_directory
from app.config import ENGINEER_PORT, MAX_GAME_HTML_BYTES
from app.llm import LLMError

logger = logging.getLogger(__name__)

ENGINEER_SYSTEM_PROMPT = """你是资深 HTML5 Canvas 游戏工程师。根据规格输出一个可直接在浏览器打开的单文件游戏。
只输出完整 HTML 代码，不要 markdown 围栏，不要解释。

硬性自动化契约（违反即验收失败）：
1. 必须有 <canvas id="game" width="480" height="640">。
2. 必须暴露 window.__game = { start(), action(name), state() }：
   - start(): 把状态置为 'playing'；
   - action('collect'): 核心玩法成功一次（如接到金币），分数 +1；
   - action('miss'): 失败一次（如漏接），misses +1；
   - action('move_left') / action('move_right'): 移动玩家；
   - state(): 返回 {status:'ready'|'playing'|'win'|'lose', score, target, misses,
     loseLimit, playerX, winText, loseText}。
3. score >= target 时 status 变 'win'，且页面显示胜利文案（DOM 覆盖层 div#win-text）；
   misses >= loseLimit 时 status 变 'lose'，显示 div#lose-text。winText/loseText 必须非空。
4. 禁止引用任何外部网络资源（不允许 http:// 或 https:// 的 src/href）。
5. 若给出 assets 列表，用 assets/player.png 作玩家贴图、assets/coin.png 作金币贴图，
   加载失败时回退为纯色绘制。
6. 游戏循环用 requestAnimationFrame，胜利/失败后停止玩法推进但保留画面。
7. 提供 <button id="start-button">，点击后调用 start()；支持键盘 ArrowLeft/ArrowRight
   真实移动玩家，移动后 state().playerX 必须变化。测试钩子只能观测，不能替代真实输入。
8. 整个 HTML 控制在 250 行以内、无注释废话，功能完整优先于花哨。"""


class EngineerAgent(SkillAgent):
    name = "engineer_agent"
    port = ENGINEER_PORT
    description = "根据结构化规格生成自包含 Canvas 游戏页面，或按失败断言修复"
    skills_spec = [
        {
            "id": "generate_game",
            "name": "生成游戏构建",
            "description": "输入规格 JSON 与资产清单，输出可直接打开的 game.html 构建产物"
            "及本次声称解决的契约条目 ID",
        },
        {
            "id": "fix_game",
            "name": "修复游戏构建",
            "description": "输入失败断言与上一轮构建信息，仅修复声称解决的契约条目并输出新构建",
        },
    ]

    def __init__(self, runtime) -> None:
        super().__init__()
        self._runtime = runtime

    async def handle(self, payload: dict) -> dict:
        task_id: str = payload["task_id"]
        spec: dict = payload.get("spec") or {}
        assets: list[str] = payload.get("assets_expected", [])
        fix_info: dict | None = payload.get("fix")

        task_dir = task_directory(task_id)
        (task_dir / "assets").mkdir(parents=True, exist_ok=True)
        # 版本文件必须和 assets/ 同层，否则浏览器按相对路径加载贴图会 404。
        version = len(list(task_dir.glob("game_v*.html"))) + 1
        previous_path = task_dir / "game.html"
        previous_code = (
            previous_path.read_text(encoding="utf-8")
            if fix_info and previous_path.exists()
            else ""
        )

        rag_context = ""
        try:
            rag_context = self._runtime.retriever.context_for(
                f"Canvas 游戏实现 游戏循环 碰撞 分数 {spec.get('name', '')} {spec.get('core_loop', '')}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[engineer] RAG 检索失败: %s", exc)

        code, backend = None, "llm"
        if self._runtime.llm.available:
            code = await self._generate_with_llm(
                spec, assets, fix_info, rag_context, previous_code
            )
        if code is None:
            backend = "template(LLM不可用、输出不合格或修复无有效增量)"
            code = self._template_code(spec)
        if len(code.encode("utf-8")) > MAX_GAME_HTML_BYTES:
            raise ValueError(f"生成代码超过 {MAX_GAME_HTML_BYTES} 字节上限")

        entry_path = task_dir / f"game_v{version}.html"
        entry_path.write_text(code, encoding="utf-8")
        # 稳定入口供本地预览和打包使用；历史版本仍独立保留。
        (task_dir / "game.html").write_text(code, encoding="utf-8")
        artifact = register_artifact(
            task_id, entry_path, "game_html", self.name, version=version,
            inputs=[f"spec:{spec.get('name', '')}"] + ([f"game_html:v{version - 1}"] if fix_info else []) + assets,
            metadata={"backend": backend, "fix_round": (fix_info or {}).get("round", 0)},
        )

        # 本次修改声称解决的契约条目 ID
        if fix_info:
            claims = [a["id"] for a in fix_info.get("failed_assertions", [])]
        else:
            claims = [a["id"] for a in spec.get("acceptance_assertions", [])]

        logger.info("[engineer] 构建完成 %s (backend=%s, 契约条目=%s)", entry_path, backend, claims)
        return {
            "artifact": str(entry_path),
            "artifact_id": artifact["artifact_id"],
            "artifact_sha256": artifact["sha256"],
            "version": version,
            "entry_file": f"game_v{version}.html",
            "task_dir": str(task_dir),
            "contract_claims": claims,
            "engineer_backend": backend,
            "summary": f"构建 game.html 完成（声称解决契约条目 {', '.join(claims) or '无'}）",
        }

    async def _generate_with_llm(
        self, spec: dict, assets: list[str], fix_info: dict | None,
        rag_context: str, previous_code: str = "",
    ) -> str | None:
        import json as _json

        user_prompt = f"游戏规格 JSON：\n{_json.dumps(spec, ensure_ascii=False)}\n"
        if assets:
            user_prompt += f"需集成的资产（相对路径）：{assets}\n"
        if rag_context:
            user_prompt += f"\n参考知识（可选择性采纳）：\n{rag_context}\n"
        if fix_info:
            user_prompt += (
                f"\n这是第 {fix_info.get('round', 1)} 轮修复。QA 失败断言：\n"
                f"{_json.dumps(fix_info.get('failed_assertions', []), ensure_ascii=False)}\n"
                f"失败环节：{fix_info.get('failed_stage', '未知')}。只修复这些断言对应的机制，"
                "其余代码保持稳定，必须输出完整 HTML。\n"
            )
            if previous_code:
                user_prompt += f"\n上一版完整代码：\n{previous_code[:80000]}\n"
        messages = [
            {"role": "system", "content": ENGINEER_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        # 偏低温度，稳定生成；不合格时带错误提示重试一次
        for attempt in range(2):
            try:
                code = await self._runtime.llm.chat(messages, temperature=0.2)
                code = _strip_fence(code)
                problems = validate_game_code(code, assets)
                if not problems:
                    return code
                logger.warning("[engineer] 第 %d 次生成不合格: %s", attempt + 1, problems)
                messages.append({"role": "assistant", "content": code})
                messages.append(
                    {
                        "role": "user",
                        "content": "生成的代码未通过契约检查: " + "; ".join(problems)
                        + "。请重新输出完整 HTML，严格遵守全部硬性契约。",
                    }
                )
            except LLMError as exc:
                logger.warning("[engineer] LLM 生成失败: %s", exc)
                return None
        return None

    @staticmethod
    def _template_code(spec: dict) -> str:
        win_text = str(spec.get("win_text") or "你赢了！")
        lose_text = str(spec.get("lose_text") or "游戏结束")
        target = int(spec.get("target") or _extract_number(spec.get("win_condition"), 10))
        lose_limit = int(
            spec.get("lose_limit") or _extract_number(spec.get("lose_condition"), 3)
        )
        return render_template(
            title=str(spec.get("name", "Canvas 游戏")),
            target=target,
            lose_limit=lose_limit,
            win_text=win_text,
            lose_text=lose_text,
        )


def _strip_fence(code: str) -> str:
    code = code.strip()
    fence = re.search(r"```(?:html)?\s*(.+?)\s*```", code, re.DOTALL)
    if fence:
        code = fence.group(1).strip()
    return code


def _extract_number(text, default: int) -> int:
    if not text:
        return default
    m = re.search(r"(\d+)", str(text))
    return int(m.group(1)) if m else default


def validate_game_code(code: str, assets: list[str]) -> list[str]:
    """静态契约检查（Reviewer 也会复用）。资产集成是软要求（有 onerror 兜底），不做阻塞。"""
    problems = []
    if "<canvas" not in code:
        problems.append("缺少 <canvas>")
    if "__game" not in code:
        problems.append("缺少 window.__game 自动化契约")
    for hook in ("start", "action", "state"):
        if hook not in code:
            problems.append(f"__game 缺少 {hook}()")
    if re.search(r"(src|href)\s*=\s*[\"']https?://", code, re.IGNORECASE):
        problems.append("引用了外部网络资源")
    if len(code) < 800:
        problems.append("代码过短，疑似不完整")
    problems.extend(check_js_syntax(code))
    return problems


# 内联 <script> 提取：只取不带 src 的脚本块，拼起来做语法检查
_INLINE_SCRIPT = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)


def check_js_syntax(code: str) -> list[str]:
    """用 Node 的 --check 做纯语法校验。

    为什么需要：实测中 LLM 生成过 `Identifier 'miss' has already been declared`
    这类语法错误，页面加载即抛异常，8 条断言全失败，白白消耗一轮修复预算。
    静态文本检查（有没有 __game、有没有 canvas）看不出这种错，必须在写盘前拦。
    Node 不存在时静默跳过——不能让本地环境缺 Node 导致生成失败。
    """
    scripts = _INLINE_SCRIPT.findall(code)
    if not scripts:
        return []
    source = "\n;\n".join(scripts)
    node = _find_node()
    if node is None:
        return []
    tmp_path = None
    try:
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(source)
            tmp_path = fh.name
        proc = subprocess.run(
            [node, "--check", tmp_path],
            capture_output=True, timeout=20, check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            detail = _first_error_line(detail)
            return [f"JS 语法错误: {detail[:200]}"]
        return []
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("[engineer] JS 语法检查跳过: %s", exc)
        return []
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


def _first_error_line(stderr: str) -> str:
    """Node 的报错首行只有临时文件路径，真正原因在 "SyntaxError: ..." 这一行。"""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    for line in lines:
        if "Error" in line:
            return line
    return lines[0] if lines else "未知语法错误"


def _find_node() -> str | None:
    import os
    import shutil

    candidate = os.getenv("NODE_BIN")
    if candidate and Path(candidate).exists():
        return candidate
    return shutil.which("node")
