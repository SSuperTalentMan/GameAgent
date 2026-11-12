# GameAgent — 游戏研发自动化多智能体流水线

一句话需求 → 规格契约 → 编码 ∥ 资产 → 运行时验证 ∥ 静态审查 → 定向修复 → 合成玩家实验 → 指标报告 → H5/APK 交付。
全流程由 A2A（Agent-to-Agent，智能体间通信协议）编排，9 个独立 Agent 服务，每步留证据、可复现。

> 项目定位、需求拆解、能力边界见 `docs/`：
> - `docs/01-需求理解与方案定位.md` — 这套系统解决什么问题、9 个角色怎么分工、六个关键设计判断
> - `docs/02-A2A架构与角色契约.md` — 协议分层、状态机、失败路由、扩展点
> - `docs/03-实测验证与能力边界.md` — 真实跑数、踩过的坑、明确做不到的部分
> - `docs/04-面试讲法与高频追问.md` — 30 秒/3 分钟讲法与追问应答（内部材料）

---

## 一、快速开始

```bash
# 1. 依赖（uv 管理）
uv pip install -r requirements.txt -p .venv
.venv/Scripts/python.exe -m playwright install chromium

# 2. 密钥只走环境变量（.env 里只放占位符，不落库、不进代码）
#    export API_KEY=<阿里云百炼 key>

# 3. 一键跑全链路（跳过 APK 构建）
ANDROID_SDK_ROOT= .venv/Scripts/python.exe -m app.runner --no-wait

# 4. 前端控制台（另开终端）
.venv/Scripts/python.exe -m streamlit run app/frontend.py
```

- 游戏预览：`http://localhost:8080/{task_id}/game.html`
- 前端控制台：`http://localhost:8501`（时间线 / 断言明细 / 留存漏斗 / APK 下载）
- 编排器 API：`http://localhost:9000/api/health | /api/tasks | /api/tasks/{id}`

也可以 `python run_demo.py "做一个接金币的网页小游戏"`（PyCharm 里直接右键运行 `run_demo.py`）。

---

## 二、流水线

```
用户文本
  ↓ BERT 意图识别（本地 bert-base-chinese，规则兜底，降级可见）
  ├─ new_requirement → 全链路
  ├─ query           → 任务状态 + 断言汇总
  └─ fix             → 回到产物所有者 + 上一版代码
                    ↓
① spec_agent      需求 → 规格契约（RAG 召回 + 重排序注入设计模式/引擎文档）
                    ↓
② engineer_agent ∥ modeler_agent      编码生成 ∥ 素材生成（占位/文生图）
                    ↓
③ qa_agent ∥ reviewer_agent          真实浏览器断言 ∥ 静态审查
                    ↓
④ 失败 → Neo4j 查受影响机制 → 只把失败断言回退给 engineer_agent（带上一版代码）
        修复轮次上限 MAX_FIX_ROUNDS，到顶仍失败 → 判 failed
                    ↓
⑤ build_agent        H5 打包 + 部署 URL +（可选）APK 构建
⑥ player_sim_agent   多画像合成玩家并发实验（非真人）
⑦ market_agent       回访/付费事件/流失点指标 + 报告
```

**失败语义分三层**（这是踩坑后定的）：

| 链路 | 角色 | 失败后果 |
|---|---|---|
| 质量门禁 | `qa_agent` + `reviewer_agent` | 判 `failed`，不交付 |
| 分析链 | `player_sim_agent` + `market_agent` | 降级跳过，不影响交付，检查点留痕 |
| 交付链 | `build_agent` | 降级为告警，状态 `completed_with_warnings` |

---

## 三、代码结构

```
app/
├── runner.py            # 一键启动：9 个 A2A 服务 + 四个存储 + demo 驱动 + /api/* 路由
├── frontend.py          # Streamlit 控制台
├── orchestrator.py      # 编排：Card 发现、意图路由、所有权表、修复轮次、状态机
├── a2a.py               # A2A 客户端（Card 发现 / 消息收发 / 结果提取，内部调用 trust_env=False）
├── contracts.py         # 规格契约与 6 类可执行断言的分类逻辑
├── artifacts.py         # 产物版本、sha256、manifest、路径穿越校验
├── llm.py               # LLM 网关：百炼 OpenAI 兼容、流式、缓存、候选模型回退、工具调用循环
├── intent.py            # BERT 意图识别（本地模型 + 规则兜底）
├── rag.py               # RAG：bge-m3 召回 → bge-reranker-v2-m3 精排
├── runtime.py           # 共享运行时（存储 / LLM / RAG / 意图）
├── config.py            # 全部环境变量化
├── agents/
│   ├── spec.py              # ① 需求 → 规格 + 可执行断言 + 机制依赖图
│   ├── engineer.py          # ② 规格 → 自包含 Canvas 游戏（写盘前 JS 语法门禁）
│   ├── modeler.py           # ③ 多模态资产（文生图可关 / 文生 3D 预留 / 程序化降级）
│   ├── qa.py                # ④ Playwright 断言驱动，真实键盘输入 + 截图证据
│   ├── reviewer.py          # ⑤ 静态审查（error 级阻塞交付）
│   ├── player_sim.py        # ⑥ 多画像并发玩家模拟（回访/放弃由行为概率决定）
│   ├── market_validation.py # ⑦ 指标计算 + 报告撰写（LLM 工具调用，失败退确定性摘要）
│   ├── build_deploy.py      # ⑧ H5 部署 + Capacitor/Gradle APK
│   ├── game_template.py     # 确定性降级模板（满足 window.__game 契约）
│   └── base.py              # SkillAgent → AgentExecutor 适配 + AgentCard + uvicorn
└── storage/
    ├── database.py      # MySQL(asyncmy)，断线重连；SQLite 降级
    ├── kv.py            # Redis：任务状态/心跳/LLM 缓存；内存降级
    ├── graph.py         # Neo4j 机制依赖 + 影响面查询；JSON 文件降级
    └── vector.py        # Milvus Lite 3.x；numpy 余弦降级
tests/                   # 19 条单测，重点锁"曾经骗过自己"的地方
```

---

## 四、规格契约：为什么必须是"可执行断言"

`spec_agent` 的输出要过 `GameSpecContract` 校验，断言类型限定在 6 种：

| check_type | QA 怎么验 |
|---|---|
| `start` | 页面加载后 `window.__game` 可用、进入 `playing`、canvas 有尺寸 |
| `controls` | 发真实 `ArrowLeft/ArrowRight` 键事件，比对 `playerX` 变化 |
| `score` | 触发核心动作，比对 `score` 增量 |
| `win` | 驱动玩法到 `target`，校验状态变 `win` |
| `win_text` | 状态 win **且** DOM 可见 **且** 文案与规格一致 |
| `lose` | 驱动到漏接上限，校验状态变 `lose` |

写不出这 6 类的规格直接判失败——"游戏应该好玩"这种断言不可执行，不能进流水线。

---

## 五、存储与降级

| 服务 | 用途 | 不可用时 |
|---|---|---|
| MySQL | 规格/断言/审查/任务状态/产物所有权/玩家事件/验证报告/构建记录 | SQLite |
| Redis | 任务状态缓存/心跳/LLM 缓存/模拟会话状态 | 进程内存（仅缓存，丢了不影响结果） |
| Neo4j | `(Mechanism)-[:DEPENDS_ON]->` 影响面查询 | JSON 文件 |
| Milvus Lite | RAG 语料、玩家策略模板 | numpy 余弦 |

降级会写进 `/api/health` 的 `backends` 字段，**不隐藏**。

本地模型目录：`./bert-base-chinese`、`./bge-m3`、`./bge-reranker-v2-m3`（CPU 推理，16 GB 内存可跑）。
Android 工具链：`./tools/android-sdk`；不装 SDK 时 `ANDROID_SDK_ROOT=` 直接跳过 APK 构建。

---

## 六、已验证到什么程度

实测结果、逐条断言证据、踩过并修掉的问题、以及**明确没做到的部分**，都写在
[`docs/03-实测验证与能力边界.md`](docs/03-实测验证与能力边界.md)，这里只给结论：

- 全链路（需求→规格→编码→QA→修复→模拟→报告→H5）在本机（16 GB / 集显）真实跑通；
- 出现过的两类真实失败都被正确处理：LLM 生成的 JS 语法错误被 QA 抓出并回退修复；分析链网络故障降级不阻塞交付；
- **合成玩家的留存/付费数字不是市场结论**，只用于先验筛选；APK 只验证"可构建"；3D 与 iOS 是接口预留。

---

## 七、安全与约定

- 密钥只走环境变量（`API_KEY`），`.env` 只放占位符，代码零硬编码；
- 生成代码在子进程隔离执行，禁止外部网络资源，HTML 大小上限，产物路径穿越校验；
- 内部 A2A 调用统一 `trust_env=False`（否则本地代理会把 `127.0.0.1` 请求劫持成 absolute-form 导致 404）；
- QA 的通过判定不接受"状态里写了就算"的证据，必须 DOM 可见 + 文本一致。
