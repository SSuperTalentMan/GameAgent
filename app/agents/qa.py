"""QA Agent：在真实 Chromium 中执行可复现的运行时验收。

证据边界：
- 契约检查通过 `window.__game` 驱动，能证明状态机与计分契约；
- 键盘检查使用真实 ArrowLeft/ArrowRight 输入，不直接改状态；
- 胜利文案必须同时满足状态为 win、DOM 可见、文本与规格期望一致；
- 页面脚本错误、资源加载失败或外联请求会使本轮验证失败。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.agents.base import SkillAgent
from app.artifacts import contained_file, task_directory
from app.config import QA_PORT, QA_TIMEOUT_SECONDS
from app.contracts import classify_assertion

logger = logging.getLogger(__name__)

KNOWN_CHECKS = ("start", "controls", "score", "win", "win_text", "lose")


class QAAgent(SkillAgent):
    name = "qa_agent"
    port = QA_PORT
    description = "用真实浏览器与键盘输入验证游戏契约，产出可定位、可复现的证据"
    skills_spec = [
        {
            "id": "run_playthrough",
            "name": "运行时验证",
            "description": "输入构建入口、规格与断言列表，给出逐条通过/失败、截图和运行错误",
        }
    ]

    def __init__(self, runtime) -> None:
        super().__init__()
        self._runtime = runtime

    async def handle(self, payload: dict) -> dict:
        task_id: str = payload["task_id"]
        entry: str = payload["entry_path"]
        assertions: list[dict] = payload.get("assertions") or []
        spec: dict = payload.get("spec") or {}
        spec_id = payload.get("spec_id")
        if not assertions:
            raise ValueError("QA 缺少断言列表")

        try:
            async with asyncio.timeout(QA_TIMEOUT_SECONDS):
                run = await self._run_playwright(entry, task_id, spec)
        except TimeoutError:
            run = self._empty_run(f"QA 总耗时超过 {QA_TIMEOUT_SECONDS}s，已强制终止")
        checks = run["checks"]
        run_errors = run["errors"]

        results, failed = [], []
        for assertion in assertions:
            kind = assertion.get("check_type") or classify_assertion(
                assertion.get("text", ""), assertion.get("mechanism", "")
            )
            check = checks.get(kind)
            passed = bool(check and check["passed"]) and not run_errors
            evidence = check["evidence"] if check else f"不支持的断言类型: {kind}"
            if run_errors:
                evidence += "；运行错误: " + " | ".join(run_errors[:3])
            result = {
                "id": assertion["id"],
                "text": assertion.get("text", ""),
                "mechanism": assertion.get("mechanism", ""),
                "check_type": kind,
                "status": "pass" if passed else "fail",
                "evidence": evidence,
            }
            results.append(result)
            if not passed:
                failed.append(result)
            if spec_id is not None:
                await self._runtime.db.update_assertion(
                    spec_id, assertion["id"], result["status"], evidence
                )

        passed_all = not failed
        logger.info(
            "[qa] 任务 %s: %d/%d 断言通过 %s",
            task_id, len(results) - len(failed), len(results),
            "✓" if passed_all else f"失败: {[f['id'] for f in failed]}",
        )
        return {
            "passed": passed_all,
            "gate": "pass" if passed_all else "block",
            "results": results,
            "failed_assertions": failed,
            "failed_stage": "; ".join(sorted({f["check_type"] for f in failed})) or None,
            "console_errors": (run_errors + run["console_errors"])[:10],
            "blocked_requests": run["blocked_requests"][:10],
            "screenshots": run["screenshots"],
            "summary": (
                f"QA 通过 {len(results)}/{len(results)} 条断言"
                if passed_all
                else f"QA 失败 {len(failed)} 条断言: "
                + ", ".join(f"{f['id']}({f['check_type']})" for f in failed)
            ),
        }

    @staticmethod
    def _empty_run(reason: str) -> dict:
        return {
            "checks": {
                key: {"passed": False, "evidence": reason} for key in KNOWN_CHECKS
            },
            "errors": [reason], "console_errors": [], "blocked_requests": [], "screenshots": [],
        }

    async def _run_playwright(self, entry: str, task_id: str, spec: dict) -> dict:
        from playwright.async_api import async_playwright

        task_dir = task_directory(task_id)
        entry_path = contained_file(task_dir, entry)
        if entry_path.suffix.lower() not in {".html", ".htm"}:
            raise ValueError("QA 仅接受 HTML 构建产物")
        url = entry_path.as_uri()
        version = entry_path.stem.replace("game_", "")
        checks: dict = {}
        console_errors: list[str] = []
        runtime_errors: list[str] = []
        blocked_requests: list[str] = []
        screenshots: list[str] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 520, "height": 720})
                await self._instrument(page, console_errors, runtime_errors, blocked_requests)
                try:
                    await page.goto(url, timeout=15000, wait_until="load")
                    await page.wait_for_function("() => !!window.__game", timeout=8000)
                    checks["start"] = await self._check_start(page)
                    checks["controls"] = await self._check_controls(page)
                    checks["score"] = await self._check_score(page)
                    checks["win"] = await self._check_win(page, spec)
                    checks["win_text"] = await self._check_win_text(page, spec)
                    screenshot = task_dir / f"qa_win_{version}.png"
                    await page.screenshot(path=str(screenshot), full_page=True)
                    screenshots.append(screenshot.name)
                except Exception as exc:  # noqa: BLE001
                    for key in ("start", "controls", "score", "win", "win_text"):
                        checks.setdefault(key, {"passed": False, "evidence": f"页面加载/契约失败: {exc}"})
                finally:
                    await page.close()

                page2 = await browser.new_page(viewport={"width": 520, "height": 720})
                await self._instrument(page2, console_errors, runtime_errors, blocked_requests)
                try:
                    await page2.goto(url, timeout=15000, wait_until="load")
                    await page2.wait_for_function("() => !!window.__game", timeout=8000)
                    await self._start(page2)
                    lose_limit = max(1, min(20, int(spec.get("lose_limit") or 3)))
                    state = await page2.evaluate("() => window.__game.state()")
                    rounds = 0
                    while state.get("status") not in ("lose", "win") and rounds < lose_limit + 5:
                        await page2.evaluate("() => window.__game.action('miss')")
                        await page2.wait_for_timeout(60)
                        state = await page2.evaluate("() => window.__game.state()")
                        rounds += 1
                    lose_visible, lose_text = await self._overlay(page2, "#lose-text")
                    expected = str(spec.get("lose_text") or "").strip()
                    ok = (
                        state.get("status") == "lose"
                        and lose_visible
                        and bool(lose_text)
                        and (not expected or lose_text == expected)
                    )
                    checks["lose"] = {
                        "passed": ok,
                        "evidence": f"{rounds} 次 core fail 后 status={state.get('status')}, "
                        f"DOM可见={lose_visible}, 文案='{lose_text}', 期望='{expected}'",
                    }
                    screenshot = task_dir / f"qa_lose_{version}.png"
                    await page2.screenshot(path=str(screenshot), full_page=True)
                    screenshots.append(screenshot.name)
                except Exception as exc:  # noqa: BLE001
                    checks["lose"] = {"passed": False, "evidence": f"失败路径异常: {exc}"}
                finally:
                    await page2.close()
            finally:
                await browser.close()

        for key in KNOWN_CHECKS:
            checks.setdefault(key, {"passed": False, "evidence": "检查未执行"})
        if blocked_requests:
            runtime_errors.append("页面尝试访问外部网络，已阻断")
        return {
            "checks": checks,
            "errors": runtime_errors,
            "console_errors": console_errors,
            "blocked_requests": blocked_requests,
            "screenshots": screenshots,
        }

    @staticmethod
    async def _instrument(page, console_errors, runtime_errors, blocked_requests) -> None:
        def on_console(message):
            if message.type == "error":
                console_errors.append(message.text[:300])

        page.on("console", on_console)
        page.on("pageerror", lambda error: runtime_errors.append(str(error)[:300]))
        page.on(
            "requestfailed",
            lambda request: runtime_errors.append(
                f"资源加载失败: {request.url[:180]}"
            ) if request.url.startswith("file:") else None,
        )

        async def block_network(route):
            if route.request.url.startswith(("http://", "https://")):
                blocked_requests.append(route.request.url[:200])
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", block_network)

    @staticmethod
    async def _start(page) -> dict:
        button = page.locator("#start-button")
        if await button.count() and await button.first.is_visible():
            await button.first.click()
        else:
            await page.evaluate("() => window.__game.start()")
        await page.wait_for_timeout(50)
        return await page.evaluate("() => window.__game.state()")

    async def _check_start(self, page) -> dict:
        try:
            has_canvas = await page.evaluate("() => !!document.getElementById('game')")
            state = await self._start(page)
            passed = has_canvas and state.get("status") == "playing"
            return {
                "passed": passed,
                "evidence": f"canvas存在={has_canvas}, 开始后 status={state.get('status')}",
            }
        except Exception as exc:  # noqa: BLE001
            return {"passed": False, "evidence": f"启动检查异常: {exc}"}

    async def _check_controls(self, page) -> dict:
        try:
            before = await page.evaluate("() => window.__game.state()")
            if "playerX" not in before:
                return {"passed": False, "evidence": "state() 未暴露 playerX，无法验证真实键盘移动"}
            await page.keyboard.press("ArrowLeft")
            await page.wait_for_timeout(80)
            left = await page.evaluate("() => window.__game.state()")
            await page.keyboard.press("ArrowRight")
            await page.keyboard.press("ArrowRight")
            await page.wait_for_timeout(80)
            right = await page.evaluate("() => window.__game.state()")
            passed = (
                float(left.get("playerX", before["playerX"])) < float(before["playerX"])
                and float(right.get("playerX", 0)) > float(left.get("playerX", 0))
            )
            return {
                "passed": passed,
                "evidence": f"playerX: {before.get('playerX')} → 左键 {left.get('playerX')} "
                f"→ 右键×2 {right.get('playerX')}",
            }
        except Exception as exc:  # noqa: BLE001
            return {"passed": False, "evidence": f"键盘检查异常: {exc}"}

    async def _check_score(self, page) -> dict:
        try:
            before = await page.evaluate("() => window.__game.state()")
            await page.evaluate("() => window.__game.action('collect')")
            await page.wait_for_timeout(80)
            after = await page.evaluate("() => window.__game.state()")
            return {
                "passed": int(after.get("score", 0)) > int(before.get("score", 0)),
                "evidence": f"核心动作一次后分数 {before.get('score')} → {after.get('score')}",
            }
        except Exception as exc:  # noqa: BLE001
            return {"passed": False, "evidence": f"计分检查异常: {exc}"}

    async def _check_win(self, page, spec: dict) -> dict:
        try:
            expected_target = max(1, min(100, int(spec.get("target") or 10)))
            state = await page.evaluate("() => window.__game.state()")
            rounds = 0
            while state.get("status") not in ("win", "lose") and rounds < expected_target + 8:
                await page.evaluate("() => window.__game.action('collect')")
                await page.wait_for_timeout(50)
                state = await page.evaluate("() => window.__game.state()")
                rounds += 1
            passed = (
                state.get("status") == "win"
                and int(state.get("score", 0)) >= expected_target
            )
            return {
                "passed": passed,
                "evidence": f"{rounds} 次动作后 status={state.get('status')}, "
                f"score={state.get('score')}, 规格阈值={expected_target}",
            }
        except Exception as exc:  # noqa: BLE001
            return {"passed": False, "evidence": f"胜利触发异常: {exc}"}

    async def _check_win_text(self, page, spec: dict) -> dict:
        try:
            state = await page.evaluate("() => window.__game.state()")
            visible, text = await self._overlay(page, "#win-text")
            expected = str(spec.get("win_text") or "").strip()
            passed = (
                state.get("status") == "win"
                and visible
                and bool(text)
                and (not expected or text == expected)
            )
            return {
                "passed": passed,
                "evidence": f"status={state.get('status')}, DOM可见={visible}, "
                f"文案='{text}', 期望='{expected}'",
            }
        except Exception as exc:  # noqa: BLE001
            return {"passed": False, "evidence": f"胜利文案检查异常: {exc}"}

    @staticmethod
    async def _overlay(page, selector: str) -> tuple[bool, str]:
        locator = page.locator(selector)
        if not await locator.count():
            return False, ""
        first = locator.first
        visible = await first.is_visible()
        text = (await first.inner_text()).strip() if visible else ""
        return visible, text
