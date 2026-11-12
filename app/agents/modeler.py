"""Modeler Agent（多模态升级版，文档 9.2 ③'）：

- 文生图：百炼万相 wanx 系列（DashScope 原生异步 HTTP 接口）
- 文生 3D：接口预留（text_to_3d stub），未接入时走占位降级
- 降级路径：procedural 程序化生成占位 PNG / GLB（纯 Python，无第三方依赖）

资产清单约定：assets/player.png / assets/coin.png / assets/player.glb
2D 贴图由 Canvas 游戏加载；GLB 当前只是占位文件，未进入 3D 渲染链路。
资产存在性由 Reviewer 静态检查，不作为“已渲染 3D”的证据。
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import zlib
from pathlib import Path

import httpx

from app.agents.base import SkillAgent
from app import config
from app.artifacts import register_artifact, task_directory
from app.config import GENERATED_DIR, MODELER_PORT

logger = logging.getLogger(__name__)

ASSET_MANIFEST = [
    {"name": "assets/player.png", "kind": "2d", "desc": "玩家角色贴图"},
    {"name": "assets/coin.png", "kind": "2d", "desc": "金币贴图"},
    {"name": "assets/player.glb", "kind": "3d", "desc": "玩家 3D 模型占位"},
]


class ModelerAgent(SkillAgent):
    name = "modeler_agent"
    port = MODELER_PORT
    description = "多模态资产生产：万相文生图 + 文生3D预留，降级为程序化占位资产"
    skills_spec = [
        {
            "id": "generate_assets",
            "name": "生成占位/多模态资产",
            "description": "输入规格，输出 2D/3D 资产文件与元数据清单，供构建集成",
        }
    ]

    def __init__(self, runtime) -> None:
        super().__init__()
        self._runtime = runtime

    async def handle(self, payload: dict) -> dict:
        task_id: str = payload["task_id"]
        spec: dict = payload.get("spec") or {}
        task_dir = task_directory(task_id)
        assets_dir = task_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        prompt_base = (
            f"{spec.get('name', '游戏')} 风格游戏素材，主题：{spec.get('core_loop', '休闲玩法')}，"
            "扁平卡通风，纯色背景，单主体居中"
        )
        results, backend_used = [], {"player.png": None, "coin.png": None, "player.glb": None}

        # --- 2D: 万相文生图 → 失败降级 procedural ---
        wanx_ok = (
            config.WANX_ENABLED
            and bool(config.API_KEY)
            and self._runtime.llm.available
        )
        jobs = [
            ("player.png", f"{prompt_base}，玩家角色，16-bit 像素风小怪物"),
            ("coin.png", f"{prompt_base}，一枚金色金币"),
        ]
        if wanx_ok:
            async with httpx.AsyncClient(timeout=60.0) as http:
                for filename, prompt in jobs:
                    path = assets_dir / filename
                    ok = await self._wanx_text_to_image(http, prompt, path)
                    if ok:
                        backend_used[filename] = f"wanx({config.WANX_MODEL})"
                    await asyncio.sleep(0.2)

        # 降级：任何没拿到的 2D 资产用程序化生成
        if backend_used["player.png"] is None:
            write_player_png(assets_dir / "player.png")
            backend_used["player.png"] = "procedural"
        if backend_used["coin.png"] is None:
            write_coin_png(assets_dir / "coin.png")
            backend_used["coin.png"] = "procedural"

        # --- 3D: 文生 3D 接口预留 → 占位 GLB ---
        glb_backend = await self._text_to_3d(spec, assets_dir / "player.glb")
        backend_used["player.glb"] = glb_backend

        manifest = {
            "task_id": task_id,
            "assets": [
                {
                    "path": a["name"],
                    "kind": a["kind"],
                    "desc": a["desc"],
                    "backend": backend_used[Path(a["name"]).name],
                }
                for a in ASSET_MANIFEST
            ],
        }
        manifest_path = assets_dir / "manifest.json"
        manifest_path.write_text(
            __import__("json").dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for item in ASSET_MANIFEST:
            path = task_dir / item["name"]
            if path.exists():
                register_artifact(
                    task_id, path, item["kind"] + "_asset", self.name,
                    metadata={"backend": backend_used[path.name], "desc": item["desc"]},
                )
        register_artifact(
            task_id, manifest_path, "asset_manifest", self.name,
            metadata={"backends": backend_used},
        )
        asset_paths = [a["name"] for a in ASSET_MANIFEST]
        logger.info("[modeler] 资产就绪 %s (backends=%s)", asset_paths, backend_used)
        return {
            "assets": asset_paths,
            "assets_dir": str(assets_dir),
            "manifest": manifest,
            "modeler_backend": backend_used,
            "summary": "资产生产完成并已进入构建集成清单",
        }

    # ---------- 万相文生图 ----------
    async def _wanx_text_to_image(
        self, http: httpx.AsyncClient, prompt: str, out_path: Path
    ) -> bool:
        """DashScope 异步文生图：提交任务 → 轮询 → 下载图片。带 prompt 级缓存。"""
        import hashlib

        cache_dir = GENERATED_DIR / "_wanx_cache"
        cache_file = cache_dir / (hashlib.sha256(
            f"{config.WANX_MODEL}|{prompt}".encode("utf-8")
        ).hexdigest() + ".png")
        if cache_file.exists():
            out_path.write_bytes(cache_file.read_bytes())
            logger.info("[modeler] 万相缓存命中: %s", out_path.name)
            return True
        try:
            resp = await http.post(
                "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis",
                headers={
                    "Authorization": f"Bearer {config.API_KEY}",
                    "Content-Type": "application/json",
                    "X-DashScope-Async": "enable",
                },
                json={
                    "model": config.WANX_MODEL,
                    "input": {"prompt": prompt},
                    "parameters": {"size": "512*512", "n": 1},
                },
            )
            resp.raise_for_status()
            task_id = resp.json()["output"]["task_id"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[modeler] 万相任务提交失败 (%s): %s", out_path.name, exc)
            return False
        for _ in range(config.WANX_POLL_TIMES):
            await asyncio.sleep(2)
            try:
                poll = await http.get(
                    f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {config.API_KEY}"},
                )
                data = poll.json()
                status = data.get("output", {}).get("task_status")
                if status == "SUCCEEDED":
                    url = data["output"]["results"][0]["url"]
                    img = await http.get(url)
                    img.raise_for_status()
                    out_path.write_bytes(img.content)
                    try:
                        cache_dir.mkdir(parents=True, exist_ok=True)
                        cache_file.write_bytes(img.content)
                    except OSError:
                        pass
                    logger.info("[modeler] 万相文生图成功: %s", out_path.name)
                    return True
                if status in ("FAILED", "CANCELED", "UNKNOWN"):
                    logger.warning("[modeler] 万相任务 %s: %s", status, data.get("output", {}).get("message"))
                    return False
            except Exception as exc:  # noqa: BLE001
                logger.warning("[modeler] 万相轮询异常: %s", exc)
                return False
        logger.warning("[modeler] 万相任务超时: %s", out_path.name)
        return False

    # ---------- 文生 3D（接口预留） ----------
    async def _text_to_3d(self, spec: dict, out_path: Path) -> str:
        """TODO: 接入真实文生 3D 服务（如 Meshy/Tripo API）。
        Demo 阶段接口预留，直接走占位 GLB 降级路径。"""
        write_box_glb(out_path)
        return "procedural(文生3D接口预留)"


# ================= procedural 占位资产生成（纯 Python） =================

def write_png(path: Path, width: int, height: int, pixel_fn) -> None:
    """pixel_fn(x, y) -> (r, g, b, a)。"""
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            row.extend(pixel_fn(x, y))
        rows.append(bytes(row))
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def write_player_png(path: Path, size: int = 32) -> None:
    def px(x: int, y: int):
        border = x < 2 or y < 2 or x >= size - 2 or y >= size - 2
        if border:
            return (30, 90, 180, 255)
        if 10 <= y <= 12 and 8 <= x <= 13 or 10 <= y <= 12 and 19 <= x <= 24:
            return (255, 255, 255, 255)  # 眼睛
        return (77, 163, 255, 255)

    write_png(path, size, size, px)


def write_coin_png(path: Path, size: int = 32) -> None:
    c = size / 2

    def px(x: int, y: int):
        d = math.hypot(x + 0.5 - c, y + 0.5 - c)
        if d > c - 1:
            return (0, 0, 0, 0)
        if d > c - 3:
            return (200, 150, 20, 255)
        return (255, 215, 94, 255)

    write_png(path, size, size, px)


def write_box_glb(path: Path) -> None:
    """最小合法 GLB：一个立方体网格（glTF 2.0，POSITION + indices）。"""
    positions = [
        -1, -1, 1, 1, -1, 1, 1, 1, 1, -1, 1, 1,
        -1, -1, -1, 1, -1, -1, 1, 1, -1, -1, 1, -1,
    ]
    indices = [
        0, 1, 2, 0, 2, 3,
        5, 4, 7, 5, 7, 6,
        1, 5, 6, 1, 6, 2,
        0, 3, 7, 0, 7, 4,
        3, 2, 6, 3, 6, 7,
        4, 5, 1, 4, 1, 0,
    ]
    pos_bin = struct.pack("<24f", *positions)
    idx_bin = struct.pack("<36H", *indices)
    bin_chunk = pos_bin + idx_bin

    gltf = {
        "asset": {"version": "2.0", "generator": "GameAgent procedural placeholder"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "player_placeholder"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
        "buffers": [{"byteLength": len(bin_chunk)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bin), "target": 34962},
            {"buffer": 0, "byteOffset": len(pos_bin), "byteLength": len(idx_bin), "target": 34963},
        ],
        "accessors": [
            {
                "bufferView": 0, "componentType": 5126, "count": 8, "type": "VEC3",
                "min": [-1, -1, -1], "max": [1, 1, 1],
            },
            {"bufferView": 1, "componentType": 5123, "count": 36, "type": "SCALAR"},
        ],
    }
    import json

    json_data = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_pad = (4 - len(json_data) % 4) % 4
    json_data += b" " * json_pad
    bin_pad = (4 - len(bin_chunk) % 4) % 4
    bin_chunk += b"\x00" * bin_pad
    total = 12 + 8 + len(json_data) + 8 + len(bin_chunk)

    path.write_bytes(
        struct.pack("<III", 0x46546C67, 2, total)
        + struct.pack("<II", len(json_data), 0x4E4F534A) + json_data
        + struct.pack("<II", len(bin_chunk), 0x004E4942) + bin_chunk
    )
