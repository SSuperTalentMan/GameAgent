"""全局配置：全部通过环境变量读取，禁止硬编码密钥。

未配置外部服务 (MySQL/Redis/Neo4j) 时，存储层自动降级，
保证 demo 在任意机器上可端到端运行。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# 本机服务（A2A / MySQL / Redis / Neo4j / Milvus Lite）不走系统代理。
# 有些客户端（httpx、grpc）默认读取 HTTP_PROXY，被 Clash/VPN 之类劫持后
# 表现为：A2A 调用返回 404（请求行变成绝对 URL）、gRPC 报 too_many_pings。
_LOCAL_NO_PROXY = "localhost,127.0.0.1,0.0.0.0,::1"
for _proxy_env in ("NO_PROXY", "no_proxy"):
    _existing = os.getenv(_proxy_env, "")
    if _LOCAL_NO_PROXY not in _existing:
        os.environ[_proxy_env] = ",".join(p for p in (_existing, _LOCAL_NO_PROXY) if p)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# --- LLM (阿里云百炼 OpenAI 兼容接口) ---
API_KEY = os.getenv("API_KEY", "")
LLM_BASE_URL = os.getenv(
    "LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
# 文档指定 qwen3.8-flash；若该模型在账号侧不可用，按序尝试回退列表
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.8-flash")
LLM_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("LLM_FALLBACK_MODELS", "qwen3.8-flash").split(",")
    if m.strip()
]
LLM_TIMEOUT_SECONDS = _int("LLM_TIMEOUT_SECONDS", 240)

# --- 本地模型目录 ---
def _model_dir(env_name: str, default: Path) -> str:
    """解析本地模型目录，兼容「解压后多套了一层同名目录」的情况。

    例如 bert-base-chinese/bert-base-chinese/config.json 这种结构，
    直接用外层目录会导致 tokenizer 加载失败、意图识别静默降级为规则。
    """
    raw = os.getenv(env_name) or str(default)
    path = Path(raw)
    if (path / "config.json").exists():
        return str(path)
    if path.is_dir():
        nested = [
            child for child in path.iterdir()
            if child.is_dir() and (child / "config.json").exists()
        ]
        if len(nested) == 1:
            return str(nested[0])
    return str(path)


BERT_MODEL_DIR = _model_dir("BERT_MODEL_DIR", PROJECT_ROOT / "bert-base-chinese")
BGE_M3_DIR = _model_dir("BGE_M3_DIR", PROJECT_ROOT / "bge-m3")
RERANKER_DIR = _model_dir("RERANKER_DIR", PROJECT_ROOT / "bge-reranker-v2-m3")

# --- Agent 端口 ---
ORCHESTRATOR_PORT = _int("ORCHESTRATOR_PORT", 9000)
SPEC_PORT = _int("SPEC_PORT", 9001)
ENGINEER_PORT = _int("ENGINEER_PORT", 9002)
MODELER_PORT = _int("MODELER_PORT", 9003)
QA_PORT = _int("QA_PORT", 9004)
REVIEWER_PORT = _int("REVIEWER_PORT", 9005)
PLAYER_SIM_PORT = _int("PLAYER_SIM_PORT", 9006)
MARKET_PORT = _int("MARKET_PORT", 9007)
BUILD_PORT = _int("BUILD_PORT", 9008)

# --- 玩家模拟 / 市场验证 (文档 9.5) ---
SIM_CONCURRENCY = _int("SIM_CONCURRENCY", 10)  # Demo 并发上限 10，可扩到 50
SIM_SESSIONS_PER_PLAYER = _int("SIM_SESSIONS_PER_PLAYER", 3)
SIM_PAY_PROBABILITY = float(os.getenv("SIM_PAY_PROBABILITY", "0.15"))
SIM_LLM_DECISION_PROBABILITY = float(os.getenv("SIM_LLM_DECISION_PROBABILITY", "0.1"))

# --- 多模态资产 (百炼万相文生图；接口走 DashScope 原生 HTTP) ---
WANX_MODEL = os.getenv("WANX_MODEL", "wanx2.1-t2i-turbo")
WANX_ENABLED = os.getenv("WANX_ENABLED", "1") == "1"
WANX_POLL_TIMES = _int("WANX_POLL_TIMES", 20)

# --- 构建打包 ---
DEPLOY_PORT = _int("DEPLOY_PORT", 8080)  # H5 静态部署端口（部署 URL）
ANDROID_SDK_ROOT = os.getenv("ANDROID_SDK_ROOT", os.getenv("ANDROID_HOME", ""))

# --- 外部服务 (未配置则降级) ---
MYSQL_HOST = os.getenv("MYSQL_HOST", "")
MYSQL_PORT = _int("MYSQL_PORT", 3306)
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "gameagent")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_ENABLED = os.getenv("REDIS_ENABLED", "1") == "1"

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

# Milvus Lite 本地数据库文件（Windows 无 milvus-lite 时自动降级为本地向量检索）
MILVUS_DB_PATH = os.getenv("MILVUS_DB_PATH", str(PROJECT_ROOT / "milvus_demo.db"))
MILVUS_COLLECTION = os.getenv("MILVUS_COLLECTION", "rag_chunks")
SQLITE_PATH = os.getenv("SQLITE_PATH", str(PROJECT_ROOT / "gameagent_demo.db"))

# --- 编排参数 ---
MAX_FIX_ROUNDS = _int("MAX_FIX_ROUNDS", 2)  # 修复轮次上限
QA_TIMEOUT_SECONDS = _int("QA_TIMEOUT_SECONDS", 30)
MAX_REQUIREMENT_CHARS = _int("MAX_REQUIREMENT_CHARS", 4000)
MAX_GAME_HTML_BYTES = _int("MAX_GAME_HTML_BYTES", 524288)
SIM_SEED = _int("SIM_SEED", 20261027)

# --- 目录 ---
GENERATED_DIR = Path(os.getenv("GENERATED_DIR", str(PROJECT_ROOT / "generated")))
RAG_CORPUS_DIR = Path(os.getenv("RAG_CORPUS_DIR", str(PROJECT_ROOT / "rag_corpus")))
GRAPH_FALLBACK_PATH = PROJECT_ROOT / "graph_fallback.json"
VECTOR_FALLBACK_PATH = PROJECT_ROOT / "vector_fallback.npz"

GENERATED_DIR.mkdir(parents=True, exist_ok=True)

# --- RAG ---
RAG_RECALL_TOP_K = _int("RAG_RECALL_TOP_K", 20)  # Milvus 召回
RAG_RERANK_TOP_K = _int("RAG_RERANK_TOP_K", 5)  # 重排精排
EMBED_DIM = _int("EMBED_DIM", 1024)  # BGE-M3 dense 维度

# 强制走降级实现的开关（调试用）
FORCE_SQLITE = os.getenv("FORCE_SQLITE", "0") == "1"
FORCE_MEMORY_KV = os.getenv("FORCE_MEMORY_KV", "0") == "1"
FORCE_FILE_GRAPH = os.getenv("FORCE_FILE_GRAPH", "0") == "1"
