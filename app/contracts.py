"""规格与验收断言的共享契约，避免 Spec 和 QA 各自解释。"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

SUPPORTED_CHECKS = {"start", "controls", "score", "win", "win_text", "lose"}


def classify_assertion(text: str, mechanism: str = "") -> str:
    """把自然语言断言归到 6 种可执行检查之一。

    判定顺序是有讲究的，两类误判都是实测踩出来的：
    1. "win" 用 \\b 边界词，否则 window.game.start() 会被当成胜利断言；
    2. "start" 必须放在**最后**判定。规格里几乎每条断言都会写"在 playing 状态下…"，
       若 start 先判，一条"接住金币后 #score 从 0 变为 1"的断言会被降级成
       "只要画布存在、状态是 playing"就算通过——门禁形同虚设。
       同理 score 只认"分数/得分/计分/score"这类明确指向计分的词，
       不能把"金币"（常作为场景元素出现）当计分信号。
    """
    blob = f"{mechanism} {text}"
    if re.search(r"文案|显示|提示|文字", blob):
        return "win_text"
    if re.search(r"方向键|键盘|左右移动|移动玩家|controls?", blob, re.IGNORECASE):
        return "controls"
    if re.search(r"胜利|\bwin\b|通关", blob, re.IGNORECASE):
        return "win"
    if re.search(r"失败|漏接|\blose\b|结束", blob, re.IGNORECASE):
        return "lose"
    if re.search(r"分数|得分|计分|\bscore\b", blob, re.IGNORECASE):
        return "score"
    if re.search(r"启动|加载|playing|画布|canvas|开始|data-state", blob, re.IGNORECASE):
        return "start"
    return "unsupported"


class AssertionContract(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(pattern=r"^A\d{1,3}$")
    text: str = Field(min_length=4, max_length=500)
    mechanism: str = Field(default="", max_length=100)
    check_type: str

    @field_validator("check_type")
    @classmethod
    def executable_check(cls, value: str) -> str:
        if value not in SUPPORTED_CHECKS:
            raise ValueError(f"不支持的验收断言类型: {value}")
        return value


class GameSpecContract(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=80)
    game_type: str = "catch_collect"
    core_loop: str = Field(min_length=1, max_length=1000)
    entities: list[str] = Field(max_length=30)
    rules: list[str] = Field(max_length=50)
    win_condition: str = Field(min_length=1, max_length=500)
    lose_condition: str = Field(min_length=1, max_length=500)
    acceptance_assertions: list[AssertionContract] = Field(min_length=3, max_length=20)
    mechanisms: list[dict[str, Any]] = Field(default_factory=list, max_length=30)


def validate_game_spec(spec: dict) -> dict:
    validated = GameSpecContract.model_validate(spec)
    ids = [a.id for a in validated.acceptance_assertions]
    if len(ids) != len(set(ids)):
        raise ValueError("验收断言 ID 重复")
    mechanism_ids = [str(m.get("id", "")) for m in validated.mechanisms]
    if len(mechanism_ids) != len(set(mechanism_ids)) or not all(mechanism_ids):
        raise ValueError("机制 ID 为空或重复")
    known = set(mechanism_ids)
    for mechanism in validated.mechanisms:
        unknown = set(map(str, mechanism.get("depends_on") or [])) - known
        if unknown:
            raise ValueError(f"机制依赖引用未知 ID: {sorted(unknown)}")
    return validated.model_dump()
