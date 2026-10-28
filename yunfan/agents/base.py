"""智能体基类：统一的上下文、日志与 JSON 解析。"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..config import Settings
from ..llm.base import ChatModel, LLMResponse, TokenLedger


@dataclass
class AgentEvent:
    agent: str
    action: str
    detail: str = ""
    level: str = "info"
    duration_s: float = 0.0
    tokens: int = 0
    ts: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts or time.strftime("%H:%M:%S"),
            "agent": self.agent,
            "action": self.action,
            "detail": self.detail,
            "level": self.level,
            "duration_s": round(self.duration_s, 3),
            "tokens": self.tokens,
        }


class AgentContext:
    """一次设计运行共享的上下文（模型、配置、事件流）。"""

    def __init__(self, settings: Settings, model: ChatModel, *, verbose: bool = True) -> None:
        self.settings = settings
        self.model = model
        self.ledger: TokenLedger = getattr(model, "ledger", TokenLedger())
        self.verbose = verbose
        self.events: List[AgentEvent] = []
        self._sink = None

    def set_sink(self, fn) -> None:
        self._sink = fn

    def emit(self, event: AgentEvent) -> None:
        if not event.ts:
            event.ts = time.strftime("%H:%M:%S")
        self.events.append(event)
        if self._sink:
            try:
                self._sink(event)
            except Exception:
                pass
        if self.verbose:
            print(f"[{event.ts}] [{event.agent}] {event.action}" + (f" — {event.detail}" if event.detail else ""))

    def llm(
        self,
        agent: str,
        system: str,
        user: str,
        *,
        tag: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        started = time.time()
        resp = self.model.complete(
            system, user, tag=tag, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
        )
        resp.tag = tag
        self.emit(
            AgentEvent(
                agent=agent,
                action=f"LLM 调用 ({tag})",
                detail=f"{resp.total_tokens} tokens / {time.time() - started:.1f}s",
                duration_s=time.time() - started,
                tokens=resp.total_tokens,
            )
        )
        return resp


class BaseAgent:
    """五类智能体的公共外壳。"""

    name = "agent"
    role = "智能体"

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    def log(self, action: str, detail: str = "", level: str = "info", **kw: Any) -> None:
        self.ctx.emit(AgentEvent(agent=self.name, action=action, detail=detail, level=level, **kw))

    def warn(self, action: str, detail: str = "") -> None:
        self.log(action, detail, level="warning")

    def error(self, action: str, detail: str = "") -> None:
        self.log(action, detail, level="error")


# --------------------------------------------------------------------------
# JSON 解析工具
# --------------------------------------------------------------------------
def extract_json(text: str) -> Dict[str, Any]:
    """从模型输出中稳健抽取第一个 JSON 对象。"""
    if not text:
        raise ValueError("空响应，无法解析 JSON")
    s = text.strip()
    # 去掉 Markdown 围栏
    s = re.sub(r"^\s*```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # 逐个尝试大括号匹配
    start = s.find("{")
    while start != -1:
        depth, in_str, escape = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = s[start : i + 1]
                    for candidate in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk)):
                        try:
                            obj = json.loads(candidate)
                            if isinstance(obj, dict):
                                return obj
                        except Exception:
                            continue
                    break
        start = s.find("{", start + 1)
    raise ValueError(f"无法从响应中解析 JSON：{text[:200]!r}")


def render_ports_table(spec_ports: Sequence[Dict[str, Any]]) -> str:
    rows = ["| 端口 | 方向 | 位宽 | 说明 |", "| --- | --- | --- | --- |"]
    for p in spec_ports:
        rows.append(f"| {p.get('name')} | {p.get('dir')} | {p.get('width', 1)} | {p.get('desc', '')} |")
    return "\n".join(rows)
