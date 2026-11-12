"""Streamlit 前端（文档 9.3）：

    uv run streamlit run app/frontend.py

- 需求输入 → A2A 提交 Orchestrator → 全链路执行
- 实时 Agent 状态（检查点时间线）
- 游戏预览（iframe 嵌入 H5 部署 URL）
- 验证报告图表（plotly：留存曲线、付费漏斗、流失分布）
- 一键打包产物下载（APK / H5 zip）

前端只依赖轻量库（streamlit/plotly/httpx），不加载本地模型。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import streamlit as st
from streamlit.components.v1 import iframe as st_iframe

from app import config
from app.a2a import call_agent

ORCH_URL = f"http://localhost:{config.ORCHESTRATOR_PORT}"
API = ORCH_URL

st.set_page_config(page_title="GameAgent 控制台", page_icon="🎮", layout="wide")


@st.cache_data(ttl=5)
def fetch(path: str, timeout: float = 5.0):
    try:
        resp = httpx.get(API + path, timeout=timeout, trust_env=False)
        resp.raise_for_status()
        return resp.json(), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def submit_requirement(text: str) -> dict:
    return asyncio.run(call_agent(ORCH_URL, {"text": text}, timeout=3600))


# ================= 侧栏 =================
with st.sidebar:
    st.title("🎮 GameAgent")
    health, err = fetch("/api/health")
    if health:
        st.success("Orchestrator 在线")
        st.caption("**存储后端栈**")
        for k, v in health["backends"].items():
            st.caption(f"· {k}: `{v}`")
        st.caption(f"**已发现 Agent** ({len(health['agents'])})")
        for name in health["agents"]:
            st.caption(f"· {name}")
    else:
        st.error(f"Orchestrator 离线: {err}\n\n先运行 `python -m app.runner --no-wait`")
    st.divider()
    st.caption(
        f"LLM: {config.LLM_MODEL}  \n"
        f"修复上限: {config.MAX_FIX_ROUNDS} 轮  \n"
        f"模拟并发: {config.SIM_CONCURRENCY}"
    )

# ================= Tab 布局 =================
tab_submit, tab_task, tab_game = st.tabs(["📝 提交需求", "📊 任务详情与验证报告", "🕹️ 游戏预览"])

# ---------- 提交需求 ----------
with tab_submit:
    st.header("提交游戏需求")
    default_text = (
        "帮我做一个接金币的网页小游戏：玩家用左右方向键移动小怪物，接住下落的金币得分，"
        "漏接太多会失败；分数达到目标就胜利并显示恭喜文案。"
    )
    text = st.text_area("需求描述（会经 BERT 意图识别路由）", value=default_text, height=120)
    if st.button("🚀 启动全链路（规格→编码∥资产→审查∥验证→修复→模拟→市场验证→打包）", type="primary"):
        with st.status("流水线执行中…", expanded=True) as status:
            st.write("1/7 BERT 意图识别…")
            try:
                result = submit_requirement(text)
                status.update(label="流水线完成", state="complete", expanded=False)
            except Exception as exc:  # noqa: BLE001
                status.update(label="流水线失败", state="error")
                st.error(f"调用失败: {exc}")
                result = None
        if result:
            st.session_state["last_result"] = result
            st.session_state["last_task_id"] = result.get("task_id")
    if "last_result" in st.session_state:
        result = st.session_state["last_result"]
        st.subheader(f"任务 {result.get('task_id')} — {result.get('final_status', '?')}")
        if result.get("final_status") == "failed":
            st.error(result.get("summary", ""))
        elif result.get("final_status") == "completed_with_warnings":
            st.warning(result.get("summary", ""))
        else:
            st.success(result.get("summary", ""))
        stages = result.get("stages", {})
        col_a, col_b, col_c = st.columns(3)
        if "qa" in stages:
            col_a.metric("QA 断言通过", f"{sum(1 for r in stages['qa']['results'] if r['status']=='pass')}"
                         f"/{len(stages['qa']['results'])}", f"修复 {stages['qa'].get('fix_rounds',0)} 轮")
        if "player_sim" in stages:
            col_b.metric("模拟事件", stages["player_sim"].get("events"),
                         f"{stages['player_sim'].get('players')} 玩家")
        if "build_deploy" in stages:
            col_c.metric("APK 状态", stages["build_deploy"].get("apk_status", "-"))
        with st.expander("完整流水线 JSON"):
            st.json(result, expanded=False)

# ---------- 任务详情 ----------
with tab_task:
    st.header("任务详情与验证报告")
    tasks, err = fetch("/api/tasks")
    task_ids = [t["task_id"] for t in tasks] if tasks else []
    default_id = st.session_state.get("last_task_id")
    selected = st.selectbox(
        "选择任务", task_ids,
        index=(task_ids.index(default_id) if default_id in task_ids else 0) if task_ids else None,
    )
    custom = st.text_input("或输入任务 ID")
    task_id = custom.strip() or selected
    if task_id and st.button("🔍 查询", type="primary"):
        st.session_state["detail"] = task_id
    task_id = st.session_state.get("detail") or task_id

    if task_id:
        detail, err = fetch(f"/api/tasks/{task_id}")
        if err or not detail or "error" in (detail or {}):
            st.warning(f"任务 {task_id} 不可查: {err or detail}")
        else:
            col1, col2, col3 = st.columns([1, 2, 1])
            task_status = detail["task"].get("status")
            col1.metric("状态", task_status)
            col2.metric("修复轮次", detail["task"].get("round", 0))
            if detail.get("build"):
                col3.metric("交付", "APK" if str(
                    detail["build"].get("file_path", "")
                ).endswith(".apk") else "H5")

            st.subheader("📌 检查点时间线")
            for cp in detail.get("checkpoints", []):
                import datetime

                ts = datetime.datetime.fromtimestamp(cp["ts"]).strftime("%H:%M:%S")
                st.markdown(f"`{ts}` **{cp['stage']}** — {cp['detail']}")

            st.subheader("✅ 断言验证")
            if detail.get("assertions"):
                st.dataframe(
                    [{"ID": a["assertion_id"], "断言": a["text"],
                      "机制": a.get("mechanism", ""), "状态": a.get("status", "")}
                     for a in detail["assertions"]],
                    use_container_width=True,
                )

            st.subheader("📈 合成玩家实验（不代表真实市场结论）")
            val = detail.get("validation")
            if val:
                st.info(val.get("summary", ""))
                metrics = val.get("metrics", {})
                if metrics.get("disclaimer"):
                    st.caption(metrics["disclaimer"])
                left, right = st.columns(2)
                with left:
                    import plotly.graph_objects as go

                    curve = metrics.get("retention", {}).get("curve", [])
                    if curve:
                        fig = go.Figure()
                        fig.add_trace(go.Scatter(
                            x=[c["day"] for c in curve], y=[c["rate"] * 100 for c in curve],
                            mode="lines+markers", name="重复会话率(%)"))
                        fig.update_layout(
                            title="合成重复会话/外推曲线（非真实留存）",
                            xaxis_title="模拟天", yaxis_title="比例 %",
                        )
                        st.plotly_chart(fig, use_container_width=True)
                with right:
                    f = metrics.get("funnel", {})
                    if f:
                        fig = go.Figure(go.Funnel(
                            y=["会话", "付费点击", "付费成功"],
                            x=[f.get("sessions", 0), f.get("pay_clicks", 0), f.get("pay_success", 0)],
                            textinfo="value+percent initial"))
                        fig.update_layout(title="模拟付费事件漏斗（无真实支付）")
                        st.plotly_chart(fig, use_container_width=True)
                churn = metrics.get("churn", {})
                if churn.get("by_status"):
                    st.caption(f"流失/结束分布: {churn['by_status']} ｜ 主要流失点: {churn.get('top_reason')}")
            else:
                st.caption("该任务还没有验证报告")

            st.subheader("📦 构建产物")
            build = detail.get("build")
            if build:
                st.caption(f"本地 H5 预览: {build.get('deploy_url')}")
                st.caption(f"最近交付文件: {build.get('file_path')}")
                if str(build.get("file_path", "")).endswith(".zip"):
                    st.caption("当前只生成 H5 包，APK 未生成/未验证，不能视为 Android 交付。")
                with st.expander("构建日志"):
                    st.text(build.get("build_log", ""))
                file_path = build.get("file_path", "")
                if file_path and Path(file_path).exists():
                    p = Path(file_path)
                    if p.suffix == ".apk":
                        with open(p, "rb") as fh:
                            st.download_button("⬇️ 下载 APK", fh.read(),
                                               file_name=p.name, mime="application/vnd.android.package-archive")
                    elif p.suffix == ".zip":
                        with open(p, "rb") as fh:
                            st.download_button("⬇️ 下载 H5 包", fh.read(), file_name=p.name, mime="application/zip")

# ---------- 游戏预览 ----------
with tab_game:
    st.header("游戏预览")
    tasks, _ = fetch("/api/tasks")
    preview_id = st.session_state.get("last_task_id") or (
        tasks[0]["task_id"] if tasks else "")
    url = f"http://localhost:{config.DEPLOY_PORT}/{preview_id}/game.html"
    st.caption(f"预览地址: {url}（方向键移动，全部断言通过后可试玩）")
    if preview_id:
        st_iframe(url, height=760, scrolling=False)
    else:
        st.info("还没有任务，先到「提交需求」运行一次流水线")
