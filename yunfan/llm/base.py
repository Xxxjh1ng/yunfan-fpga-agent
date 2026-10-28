"""大模型接入层：统一的 ChatModel 抽象 + Token 计量。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence


class LLMError(RuntimeError):
    """大模型调用失败（网络、鉴权、格式）。"""


@dataclass
class Message:
    role: str
    content: str

    def as_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class LLMResponse:
    text: str
    model: str = ""
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    tag: str = ""
    finish_reason: str = ""
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class TokenLedger:
    """线程安全的 token 计量器（用于预算控制与成本核算）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    failures: int = 0
    latency_s: float = 0.0
    by_tag: Dict[str, Dict[str, float]] = field(default_factory=dict)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def record(self, resp: LLMResponse) -> None:
        with self._lock:  # type: ignore[union-attr]
            self.calls += 1
            self.prompt_tokens += resp.prompt_tokens
            self.completion_tokens += resp.completion_tokens
            self.latency_s += resp.latency_s
            slot = self.by_tag.setdefault(resp.tag or "unknown", {"calls": 0, "prompt": 0, "completion": 0, "latency": 0.0})
            slot["calls"] += 1
            slot["prompt"] += resp.prompt_tokens
            slot["completion"] += resp.completion_tokens
            slot["latency"] += resp.latency_s

    def record_failure(self) -> None:
        with self._lock:  # type: ignore[union-attr]
            self.failures += 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> Dict[str, object]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "latency_s": round(self.latency_s, 3),
            "by_tag": {k: {kk: (round(vv, 3) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in self.by_tag.items()},
        }


class ChatModel(Protocol):
    """所有 provider 需要实现的最小接口。"""

    name: str

    def complete(
        self,
        system: str,
        user: str,
        *,
        tag: str = "",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:  # pragma: no cover - protocol
        ...


def build_messages(system: str, user: str) -> List[Dict[str, str]]:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": user})
    return msgs


def join_sections(sections: Sequence[tuple]) -> str:
    """把 (标题, 正文) 拼成结构化 prompt 段落。"""
    out = []
    for title, body in sections:
        if body is None or (isinstance(body, str) and not body.strip()):
            continue
        out.append(f"【{title}】\n{body}")
    return "\n\n".join(out)
