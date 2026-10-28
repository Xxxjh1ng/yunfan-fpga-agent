"""智能体 2：RTL 代码生成。

与「一次生成即交付」的做法不同，本智能体内部自带**静态门控闭环**：
生成 -> 净化 -> 契约校验（端口/位宽）-> Lint -> 若不合格，把结构化的
违规清单回灌模型重写。只有通过门控的代码才会被送去消耗 EDA 时间。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..llm import prompts
from ..orchestration.spec import DesignSpec
from ..rtl.lint import LintReport, lint
from ..rtl.sanitize import SanitizeReport, sanitize
from .base import BaseAgent


@dataclass
class GenerationResult:
    code: str = ""
    attempts: int = 0
    ok: bool = False
    lint: Optional[LintReport] = None
    sanitize: Optional[SanitizeReport] = None
    feedback_history: List[str] = field(default_factory=list)
    tokens: int = 0

    def summary(self) -> str:
        fixes = self.sanitize.summary() if self.sanitize else {}
        return f"尝试 {self.attempts} 次｜净化修复 {fixes}｜{'通过门控' if self.ok else '未通过门控'}"


class RtlAgent(BaseAgent):
    name = "代码生成"
    role = "由规格生成可综合的 Verilog RTL"

    def generate(
        self,
        spec: DesignSpec,
        *,
        max_attempts: int = 3,
        extra_rules: Optional[List[str]] = None,
        temperature: Optional[float] = None,
    ) -> GenerationResult:
        system = prompts.RTL_SYSTEM
        feedback: Optional[str] = None
        result = GenerationResult()
        for attempt in range(1, max_attempts + 1):
            user = prompts.rtl_user(spec.to_json(), extra_rules=extra_rules, feedback=feedback)
            resp = self.ctx.llm(self.name, system, user, tag=f"rtl-{attempt}", temperature=temperature)
            result.tokens += resp.total_tokens
            result.attempts = attempt

            san = sanitize(resp.text, target_name=spec.top_module)
            rep = lint(san.code, spec_ports=spec.ports)
            result.code = san.code
            result.sanitize = san
            result.lint = rep

            if san.changed:
                self.log("确定性净化", "，".join(f"{k}×{v}" for k, v in san.summary().items()))
            if rep.ok and san.code:
                self.log("RTL 通过静态门控", f"{len(san.code.splitlines())} 行｜{san.code.count(chr(10))} 换行", tokens=resp.total_tokens)
                result.ok = True
                return result

            feedback = "静态检查发现以下问题，必须逐条修复：\n" + rep.summary(max_items=10)
            result.feedback_history.append(feedback)
            self.warn(f"RTL 未通过静态门控（第 {attempt} 次）", "；".join(v.message for v in rep.errors[:3]))

        return result
