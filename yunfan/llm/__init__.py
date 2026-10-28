"""大模型接入层。"""

from __future__ import annotations

from typing import Optional

from ..config import LLMConfig
from .base import LLMError, LLMResponse, Message, ChatModel, TokenLedger, build_messages, join_sections
from .offline import CallableModel, FixtureModel, ReplayModel
from .openai_compat import OpenAICompatModel

__all__ = [
    "LLMError",
    "LLMResponse",
    "Message",
    "ChatModel",
    "TokenLedger",
    "build_messages",
    "join_sections",
    "OpenAICompatModel",
    "FixtureModel",
    "ReplayModel",
    "CallableModel",
    "build_model",
]


def build_model(cfg: LLMConfig, ledger: Optional[TokenLedger] = None, fixtures: Optional[dict] = None) -> ChatModel:
    """按配置构造模型实例。"""
    provider = (cfg.provider or "deepseek").lower()
    if provider in ("offline", "offline-fixture", "fixture"):
        return FixtureModel(fixtures or {}, ledger=ledger)
    if provider in ("replay", "offline-replay"):
        raise LLMError("ReplayModel 需要显式传入记录文件路径")
    if provider in ("deepseek", "openai", "openai-compat"):
        return OpenAICompatModel(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            model=cfg.model,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout_s=cfg.timeout_s,
            retries=cfg.retries,
            ledger=ledger,
        )
    raise LLMError(f"未知 provider: {cfg.provider}")
