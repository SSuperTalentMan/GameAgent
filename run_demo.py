"""一键启动（PyCharm 中直接右键 Run 此文件即可）：

流程：
  1. 启动 Streamlit 前端（http://localhost:8501，独立子进程）
  2. 拉起 8 个下游 Agent + Orchestrator 的 A2A 服务并完成 Card 发现
  3. 跑一遍全链路：规格 → 编码∥资产 → 审查∥QA → 修复 → 10并发模拟 → 市场验证 → APK 打包
  4. 打开浏览器（游戏预览 + 前端面板）
  5. 在 PyCharm 控制台按回车退出（退出前服务保持可用）

也可以命令行自定义需求：
  python run_demo.py "做一个贪吃蛇游戏"
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import config  # noqa: E402

STREAMLIT_PORT = 8501
PY = sys.executable


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_streamlit() -> subprocess.Popen | None:
    """启动 Streamlit 前端（若端口已被占用说明已在运行，直接复用）。"""
    if port_in_use(STREAMLIT_PORT):
        print(f"[one-click] Streamlit 已在运行: http://localhost:{STREAMLIT_PORT}")
        return None
    log = open(PROJECT_ROOT / "streamlit.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            PY, "-X", "utf8", "-m", "streamlit", "run", "app/frontend.py",
            "--server.headless", "true",
            "--server.port", str(STREAMLIT_PORT),
        ],
        cwd=str(PROJECT_ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    print(f"[one-click] Streamlit 前端启动中: http://localhost:{STREAMLIT_PORT} (pid={proc.pid})")
    return proc


def open_browser_later(urls: list[str], delay: float = 50.0) -> None:
    """延迟打开浏览器：等模型加载 + 服务就绪后再展示。"""

    def _open():
        for url in urls:
            webbrowser.open(url)

    threading.Timer(delay, _open).start()


async def run_pipeline(requirement: str | None) -> dict | None:
    from app.runner import main

    # runner.main 内部完成：服务拉起 → Card 发现 → 全链路 → 等待回车
    await main(requirement=requirement, wait=True)
    return None


def main() -> None:
    print("=" * 66)
    print(" GameAgent 一键启动")
    print(" 流水线: 意图识别 → 规格 → 编码∥资产 → 审查∥QA → 修复闭环")
    print("          → 10 并发玩家模拟 → 市场验证 → APK 打包 → H5 部署")
    print("=" * 66)

    requirement = (
        sys.argv[1]
        if len(sys.argv) > 1 and not sys.argv[1].startswith("--")
        else None
    )

    streamlit_proc = start_streamlit()
    open_browser_later(
        [f"http://localhost:{STREAMLIT_PORT}", f"http://localhost:{config.DEPLOY_PORT}"],
        delay=45.0,
    )

    exit_code = 0
    try:
        asyncio.run(run_pipeline(requirement))
    except KeyboardInterrupt:
        print("\n[one-click] 用户中断")
    except Exception as exc:  # noqa: BLE001
        exit_code = 1
        print(f"\n[one-click] 运行失败: {type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
    finally:
        if streamlit_proc is not None:
            print("[one-click] 关闭 Streamlit 前端")
            streamlit_proc.terminate()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
