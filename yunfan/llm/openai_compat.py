"""OpenAI 兼容 / DeepSeek 的 ChatModel 实现（零第三方依赖，仅用标准库 urllib）。

工程要点
--------
* 指数退避 + 抖动重试，覆盖 429/5xx/超时/连接重置；
* 支持 JSON 模式（deepseek-chat 的 response_format=json_object）；
* 记录 prompt/completion token、延迟，便于预算控制与论文成本核算；
* 不使用 keep-alive，规避部分企业代理下的 TLS 会话复用错误。
"""
from __future__ import annotations

import json
import random
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .base import LLMError, LLMResponse, TokenLedger, build_messages

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAICompatModel:
    """DeepSeek / OpenAI / 任何兼容 /v1/chat/completions 的服务。"""

    name = "openai_compat"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_s: float = 180.0,
        retries: int = 3,
        ledger: Optional[TokenLedger] = None,
    ) -> None:
        if not api_key:
            raise LLMError("缺少 API Key：请设置 YUNFAN_API_KEY 或 MY_API_KEY，或改用 --provider offline")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.retries = max(1, retries)
        self.ledger = ledger or TokenLedger()

    # ------------------------------------------------------------------
    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    def _payload(
        self,
        system: str,
        user: str,
        temperature: Optional[float],
        max_tokens: Optional[int],
        json_mode: bool,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": build_messages(system, user),
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

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
        payload = self._payload(system, user, temperature, max_tokens, json_mode)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Connection": "close",
        }

        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            started = time.time()
            try:
                req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                text = message.get("content") or ""
                usage = data.get("usage") or {}
                out = LLMResponse(
                    text=text,
                    model=data.get("model", self.model),
                    provider=self.name,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    latency_s=time.time() - started,
                    tag=tag,
                    finish_reason=str(choice.get("finish_reason") or ""),
                    cached_tokens=int(usage.get("prompt_cache_hit_tokens") or 0),
                )
                self.ledger.record(out)
                if not text.strip():
                    raise LLMError("模型返回空内容")
                return out

            except urllib.error.HTTPError as exc:
                last_err = exc
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:400]
                except Exception:
                    pass
                retryable = exc.code in _RETRYABLE_STATUS
                if not retryable:
                    self.ledger.record_failure()
                    raise LLMError(f"HTTP {exc.code}: {detail}") from exc
                self._sleep(attempt)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
                last_err = exc
                self._sleep(attempt)
            except json.JSONDecodeError as exc:
                last_err = exc
                self._sleep(attempt)

        self.ledger.record_failure()
        raise LLMError(f"重试 {self.retries} 次仍失败：{last_err}")

    def _sleep(self, attempt: int) -> None:
        delay = min(8.0, 0.8 * (2 ** (attempt - 1))) + random.uniform(0, 0.4)
        time.sleep(delay)
