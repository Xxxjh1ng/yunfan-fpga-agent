"""离线（可复现）模型：用于无网络环境下的单元测试与实验回放。

两种用法
--------
1. ``Fixtures``：按 tag 返回预置响应，供单元测试断言;
2. ``Replay``：从 JSONL 记录中按调用顺序回放真实模型的输出，
   使论文实验在无网络时仍可逐位复现（trace 即证据）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .base import LLMError, LLMResponse, TokenLedger


class FixtureModel:
    """按 tag 返回固定文本；未登记的 tag 抛错，避免静默通过。"""

    name = "offline-fixture"

    def __init__(self, fixtures: Dict[str, str], ledger: Optional[TokenLedger] = None) -> None:
        self.fixtures = fixtures
        self.ledger = ledger or TokenLedger()
        self.calls: List[str] = []

    def complete(
        self,
        system: str,
        user: str,
        *,
        tag: str = "",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        self.calls.append(tag)
        text = self.fixtures.get(tag)
        if text is None:
            raise LLMError(f"离线模式缺少 tag={tag!r} 的预置响应；已登记：{sorted(self.fixtures)}")
        resp = LLMResponse(
            text=text,
            model="offline-fixture",
            provider=self.name,
            prompt_tokens=max(1, len(system) // 4 + len(user) // 4),
            completion_tokens=max(1, len(text) // 4),
            tag=tag,
        )
        self.ledger.record(resp)
        return resp


class ReplayModel:
    """按调用顺序回放 LLM 调用记录（JSONL，每行一条 LLMResponse）。"""

    name = "offline-replay"

    def __init__(self, path: Path, ledger: Optional[TokenLedger] = None) -> None:
        self.path = Path(path)
        self.ledger = ledger or TokenLedger()
        self._responses: List[dict] = []
        self._cursor = 0
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._responses.append(json.loads(line))

    def complete(
        self,
        system: str,
        user: str,
        *,
        tag: str = "",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        if self._cursor >= len(self._responses):
            raise LLMError("回放记录已耗尽：请检查 trace 与调用序列是否一致")
        item = self._responses[self._cursor]
        self._cursor += 1
        resp = LLMResponse(**{k: item.get(k, v) for k, v in {
            "text": "",
            "model": "offline-replay",
            "provider": self.name,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "latency_s": 0.0,
            "tag": tag,
            "finish_reason": "",
            "cached_tokens": 0,
        }.items()})
        self.ledger.record(resp)
        return resp


class CallableModel:
    """把任意函数包装成 ChatModel，便于测试与自定义策略。"""

    name = "offline-callable"

    def __init__(self, fn: Callable[[str, str, str], str], ledger: Optional[TokenLedger] = None) -> None:
        self.fn = fn
        self.ledger = ledger or TokenLedger()

    def complete(
        self,
        system: str,
        user: str,
        *,
        tag: str = "",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        text = self.fn(system, user, tag)
        resp = LLMResponse(text=text, model="offline-callable", provider=self.name, tag=tag)
        self.ledger.record(resp)
        return resp
