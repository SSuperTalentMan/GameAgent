"""Build & Deploy Agent（文档 9.2 ⑧）：

- H5 构建：构建产物打包 zip；静态部署服务（stdlib http.server）输出部署 URL
- Android：Capacitor → Gradle assembleDebug → 可安装的 debug APK
  * 工具链（Node + JDK17 + Android SDK）齐全时走真实打包（国内镜像加速）
  * 工具链缺失时降级：产出 Capacitor 工程 bundle + 一键打包脚本（不做假 APK）
- iOS：接口预留（需 macOS + Xcode，见 ios_build_instructions）
- 产物登记 build_artifacts 表
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
import zipfile
from functools import lru_cache
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.agents.base import SkillAgent
from app import config
from app.artifacts import contained_file, register_artifact, task_directory
from app.config import BUILD_PORT, DEPLOY_PORT, GENERATED_DIR

logger = logging.getLogger(__name__)

NPM_REGISTRY = os.getenv("NPM_REGISTRY", "https://registry.npmmirror.com")
GRADLE_DIST_MIRROR = os.getenv(
    "GRADLE_DIST_MIRROR",
    "https://mirrors.cloud.tencent.com/gradle/gradle-8.11.1-bin.zip",
)


# ================= H5 静态部署服务 =================
@lru_cache(maxsize=1)
def _deploy_server() -> ThreadingHTTPServer:
    handler = lambda *args, **kw: SimpleHTTPRequestHandler(*args, directory=str(GENERATED_DIR), **kw)  # noqa: E731
    server = ThreadingHTTPServer(("127.0.0.1", DEPLOY_PORT), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("[build] H5 静态部署服务启动: http://localhost:%d/", DEPLOY_PORT)
    return server


def deploy_url_for(task_id: str) -> str:
    _deploy_server()
    return f"http://localhost:{DEPLOY_PORT}/{task_id}/game.html"


class BuildDeployAgent(SkillAgent):
    name = "build_agent"
    port = BUILD_PORT
    description = "H5 构建/部署 + Capacitor 打包 Android APK（iOS 接口预留）"
    skills_spec = [
        {
            "id": "build_and_deploy",
            "name": "构建打包部署",
            "description": "输入构建目录，产出部署 URL、H5 zip 与 Android APK（或可构建 bundle）",
        }
    ]

    def __init__(self, runtime) -> None:
        super().__init__()
        self._runtime = runtime

    async def handle(self, payload: dict) -> dict:
        task_id: str = payload["task_id"]
        task_dir = task_directory(task_id)
        entry = contained_file(task_dir, task_dir / "game.html")
        build_dir = task_dir / "build"
        build_dir.mkdir(parents=True, exist_ok=True)
        log: list[str] = []

        # ---------- 1. H5 zip ----------
        h5_zip = build_dir / f"{task_id}-h5.zip"
        _zip_h5(task_dir, h5_zip, entry)
        register_artifact(
            task_id, h5_zip, "h5_bundle", self.name,
            inputs=["game_html"], metadata={"entry": entry.name},
        )
        log.append(f"H5 打包: {h5_zip.name} ({h5_zip.stat().st_size // 1024} KB)")

        # ---------- 2. 部署 URL ----------
        url = deploy_url_for(task_id)
        log.append(f"部署 URL: {url}")

        # ---------- 3. Android APK ----------
        apk_path, apk_status = None, "skipped"
        if _toolchain_ready(log):
            try:
                apk_path = await self._capacitor_build(task_id, task_dir, build_dir, log)
                apk_status = "built" if apk_path else "failed"
            except Exception as exc:  # noqa: BLE001
                logger.exception("[build] Capacitor 打包失败，降级 bundle")
                log.append(f"Capacitor 打包失败: {exc}")
                apk_status = "failed"
        else:
            apk_status = "bundle_ready"
            log.append("工具链不完整，产出可构建 bundle（详见 build_log 与一键脚本）")
        if apk_path is None and apk_status in ("bundle_ready", "failed"):
            _write_android_bundle(task_id, task_dir, build_dir, log)
        if apk_path is not None:
            register_artifact(
                task_id, apk_path, "android_apk", self.name,
                inputs=["h5_bundle"], metadata={"build_type": "debug", "verified": False},
            )

        # ---------- 4. 落库：H5 与安装包分开登记 ----------
        build_id = f"build-{task_id}"
        await self._runtime.db.save_build_artifact(
            build_id, task_id, "h5+android",
            str(apk_path or h5_zip),
            url, "\n".join(log),
        )
        logger.info("[build] %s 完成: apk=%s(%s) url=%s", build_id, apk_path, apk_status, url)
        return {
            "build_id": build_id,
            "deploy_url": url,
            "h5_zip": str(h5_zip),
            "apk_path": str(apk_path) if apk_path else None,
            "apk_status": apk_status,
            "apk_verified": False,
            "preview_scope": "localhost",
            "build_log": log,
            "ios": "接口预留：需 macOS + Xcode 环境（capacitor add ios 后 xcodebuild）",
            "summary": (
                f"本地 H5 预览已就绪，APK 状态: {apk_status}"
                + ("" if apk_status == "built" else "；安装包未生成，未做安装启动验证")
            ),
        }

    # ---------- Capacitor 真实打包 ----------
    async def _capacitor_build(
        self, task_id: str, task_dir: Path, build_dir: Path, log: list[str]
    ) -> Path | None:
        proj = build_dir / "capacitor"
        if proj.exists():
            shutil.rmtree(proj, ignore_errors=True)
        proj.mkdir(parents=True, exist_ok=True)
        env = {
            key: value for key, value in os.environ.items()
            if key.upper() not in {
                "API_KEY", "DASHSCOPE_API_KEY", "MYSQL_PASSWORD",
                "REDIS_URL", "NEO4J_PASSWORD",
            }
        }
        env.update(
            npm_config_registry=NPM_REGISTRY,
            ANDROID_SDK_ROOT=config.ANDROID_SDK_ROOT,
            ANDROID_HOME=config.ANDROID_SDK_ROOT,
            JAVA_HOME=_find_java_home(),
            CAPACITOR_ANDROID_STUDIO="false",
        )

        async def run(args: list[str], timeout: int = 900, cwd: Path = proj) -> None:
            logger.info("[build] $ %s", " ".join(args))
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(cwd), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(f"命令超时: {' '.join(args)}")
            text = (out or b"").decode("utf-8", errors="replace")
            log.append(f"$ {' '.join(args)}\n{text[-1500:]}")
            if proc.returncode != 0:
                raise RuntimeError(
                    f"命令失败({proc.returncode}): {' '.join(args)}\n{text[-800:]}"
                )

        (proj / "package.json").write_text(
            json.dumps({"name": f"gameagent-{task_id}", "version": "1.0.0", "private": True}),
            encoding="utf-8",
        )
        (proj / "capacitor.config.json").write_text(
            json.dumps(
                {
                    "appId": f"com.gameagent.{task_id.replace('-', '')}",
                    "appName": task_id,
                    "webDir": "www",
                    "server": {"androidScheme": "https"},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        www = proj / "www"
        www.mkdir(exist_ok=True)
        shutil.copy2(task_dir / "game.html", www / "index.html")
        if (task_dir / "assets").exists():
            shutil.copytree(task_dir / "assets", www / "assets", dirs_exist_ok=True)

        npm = shutil.which("npm") or "npm"
        npx = shutil.which("npx") or "npx"
        await run([
            npm, "install", "@capacitor/core@6", "@capacitor/cli@6",
            "@capacitor/android@6", "--registry", NPM_REGISTRY,
            "--no-audit", "--no-fund",
        ], timeout=600)
        try:
            await run([npx, "cap", "add", "android"], timeout=600)
        except RuntimeError as exc:
            if "already exists" not in str(exc):
                raise
            logger.warning("[build] android 平台目录残留，删除后重试 cap add")
            shutil.rmtree(proj / "android", ignore_errors=True)
            await run([npx, "cap", "add", "android"], timeout=600)
        _patch_gradle_mirrors(proj / "android")
        await run([npx, "cap", "sync", "android"], timeout=600)
        gradlew = proj / "android" / "gradlew.bat"
        await run([str(gradlew), "assembleDebug", "--no-daemon", "-q"],
                  timeout=1800, cwd=proj / "android")
        out_apk = proj / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
        if not out_apk.exists():
            raise RuntimeError("assembleDebug 完成但未找到 app-debug.apk")
        final_apk = build_dir / f"{task_id}-debug.apk"
        shutil.copy2(out_apk, final_apk)
        log.append(f"APK 产出: {final_apk} ({final_apk.stat().st_size // 1024 // 1024} MB)")
        return final_apk


# ================= 工具链与辅助 =================
def _find_java_home() -> str:
    if os.getenv("JAVA_HOME"):
        return os.environ["JAVA_HOME"]
    import glob

    for pattern in (
        "C:/Program Files/Microsoft/jdk-17*",
        "C:/Program Files/Eclipse Adoptium/jdk-17*",
        "C:/Program Files/Java/jdk-17*",
    ):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return ""


def _toolchain_ready(log: list[str]) -> bool:
    import glob as _g

    node = shutil.which("node")
    java = _find_java_home()
    sdk = config.ANDROID_SDK_ROOT
    sdk_ok = bool(sdk) and Path(sdk, "platform-tools").exists()
    if node:
        log.append(f"工具链: node={node}")
    if java:
        log.append(f"工具链: JAVA_HOME={java}")
    if sdk_ok:
        log.append(f"工具链: ANDROID_SDK_ROOT={sdk}")
    ready = bool(node) and bool(java) and sdk_ok
    if not ready:
        missing = [n for n, ok in (("node", node), ("jdk17", java), ("android-sdk", sdk_ok)) if not ok]
        log.append("缺少工具链: " + ", ".join(missing))
    return ready


def _patch_gradle_mirrors(android_dir: Path) -> None:
    """国内镜像加速：Gradle 发行版 + Maven 仓库。

    Capacitor 的 settings.gradle 自带 pluginManagement 块，不能重复声明，
    因此采用"已存在则向块内插入 repositories，不存在才新增块"的方式。
    失败不影响打包（回退官方源）。
    """
    try:
        wrapper_props = android_dir / "gradle" / "wrapper" / "gradle-wrapper.properties"
        if wrapper_props.exists():
            text = wrapper_props.read_text(encoding="utf-8")
            import re

            text = re.sub(r"distributionUrl=.*", f"distributionUrl={GRADLE_DIST_MIRROR}", text)
            wrapper_props.write_text(text, encoding="utf-8")

        settings = android_dir / "settings.gradle"
        if settings.exists():
            text = settings.read_text(encoding="utf-8")
            # 注意必须多行：单行多个闭包调用会被 Groovy 解析为方法链导致 maven() 找不到
            plugin_repos = (
                "pluginManagement {\n"
                "    repositories {\n"
                '        maven { url "https://maven.aliyun.com/repository/gradle-plugin" }\n'
                '        maven { url "https://maven.aliyun.com/repository/google" }\n'
                "        mavenCentral()\n"
                "        gradlePluginPortal()\n"
                "    }\n"
                "}"
            )
            dep_repos = (
                "dependencyResolutionManagement {\n"
                "    repositories {\n"
                '        maven { url "https://maven.aliyun.com/repository/google" }\n'
                '        maven { url "https://maven.aliyun.com/repository/public" }\n'
                "        google()\n"
                "        mavenCentral()\n"
                "    }\n"
                "}"
            )
            if "dependencyResolutionManagement" not in text:
                text = dep_repos + "\n" + text
            if "pluginManagement" not in text:
                text = plugin_repos + "\n" + text
            settings.write_text(text, encoding="utf-8")
        logger.info("[build] Gradle 国内镜像已配置")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[build] Gradle 镜像配置失败(不影响): %s", exc)


def _zip_h5(task_dir: Path, dst: Path, entry: Path) -> None:
    """只打包可运行页面与资产，避免把规格、截图和 APK 塞进 H5 包。"""
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(entry, "game.html")
        assets = task_dir / "assets"
        if assets.exists():
            for path in sorted(assets.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(task_dir))


def _write_android_bundle(task_id: str, task_dir: Path, build_dir: Path, log: list[str]) -> None:
    """工具链缺失时的降级产物：Capacitor 工程 + 一键打包脚本 + 说明。"""
    proj = build_dir / "android-bundle"
    www = proj / "www"
    www.mkdir(parents=True, exist_ok=True)
    shutil.copy2(task_dir / "game.html", www / "index.html")
    if (task_dir / "assets").exists():
        shutil.copytree(task_dir / "assets", www / "assets", dirs_exist_ok=True)
    (proj / "package.json").write_text(
        json.dumps(
            {
                "name": f"gameagent-{task_id}", "version": "1.0.0", "private": True,
                "scripts": {"apk": "cap sync android && cd android && gradlew.bat assembleDebug"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (proj / "capacitor.config.json").write_text(
        json.dumps(
            {"appId": f"com.gameagent.{task_id.replace('-', '')}", "appName": task_id, "webDir": "www"},
            indent=2,
        ),
        encoding="utf-8",
    )
    (proj / "build_apk.cmd").write_text(
        "@echo off\r\n"
        "REM 需先安装: Node.js + JDK17 + Android SDK (ANDROID_SDK_ROOT)\r\n"
        "npm install @capacitor/core@6 @capacitor/cli@6 @capacitor/android@6\r\n"
        "npx cap add android\r\n"
        "npx cap sync android\r\n"
        "cd android && gradlew.bat assembleDebug\r\n"
        "echo APK: android\\app\\build\\outputs\\apk\\debug\\app-debug.apk\r\n",
        encoding="gbk",
    )
    (proj / "README.md").write_text(
        f"# {task_id} Android 打包说明\n\n"
        "1. 安装 Node.js 18+、JDK 17、Android SDK（platform-tools + build-tools 34 + platform android-34）\n"
        "2. 设置环境变量 ANDROID_SDK_ROOT 与 JAVA_HOME\n"
        "3. 双击 build_apk.cmd，产物在 android/app/build/outputs/apk/debug/app-debug.apk\n"
        f"\nH5 已可直接访问部署 URL（见 build_artifacts.deploy_url）。\n",
        encoding="utf-8",
    )
    log.append(f"可构建 bundle: {proj}")
