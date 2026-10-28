"""智能体 1：需求解析（Design Requirement Analyst）。

把自然语言需求折叠为机器可读规格，并在解析失败时用「问题清单」回灌模型
自我修正。规格一旦通过校验即锁定，后续三个智能体只读它，不再读自然语言，
从流程上消除「同一需求被不同模型各自理解」的漂移。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import BoardProfile, DeviceProfile
from ..llm import prompts
from ..orchestration.spec import DesignSpec
from .base import BaseAgent, extract_json


@dataclass
class RequirementResult:
    spec: DesignSpec
    attempts: int = 0
    issues: List[str] = field(default_factory=list)
    ok: bool = True


class RequirementAgent(BaseAgent):
    name = "需求解析"
    role = "把自然语言需求转为机器可读设计规格"

    def __init__(self, ctx, device: DeviceProfile, board: BoardProfile) -> None:
        super().__init__(ctx)
        self.device = device
        self.board = board

    # ------------------------------------------------------------------
    def analyze(self, user_text: str, *, max_attempts: int = 3) -> RequirementResult:
        system = prompts.requirement_system(self.device.name, self.board.name)
        feedback = ""
        issues: List[str] = []
        for attempt in range(1, max_attempts + 1):
            user = prompts.requirement_user(user_text, self.device.name, self.board.name, self.board.clock_mhz)
            if feedback:
                user += f"\n\n【上一轮输出的问题（必须修正）】\n{feedback}"
            resp = self.ctx.llm(self.name, system, user, tag=f"requirement-{attempt}", json_mode=True)
            try:
                data = extract_json(resp.text)
            except Exception as exc:  # noqa: BLE001
                issues = [f"JSON 解析失败：{exc}"]
                feedback = "\n".join(f"- {i}" for i in issues)
                self.warn("规格解析失败，重试", str(exc)[:160])
                continue

            spec = DesignSpec.from_dict(data)
            # 平台默认值兜底：确保时钟/复位名与板卡一致
            if not spec.clock.get("name"):
                spec.clock["name"] = "clk"
            if not spec.reset.get("name"):
                spec.reset["name"] = "rst_n"
            issues = spec.validate()
            if not issues:
                self.log("规格锁定", f"{spec.top_module}｜{len(spec.ports)} 端口｜{spec.clock_freq_mhz:g}MHz", tokens=resp.total_tokens)
                return RequirementResult(spec=spec, attempts=attempt, issues=[], ok=True)

            feedback = "\n".join(f"- {i}" for i in issues)
            self.warn(f"规格校验未通过（第 {attempt} 轮）", "; ".join(issues[:3]))

        return RequirementResult(spec=DesignSpec(), attempts=max_attempts, issues=issues, ok=False)

    # ------------------------------------------------------------------
    def refine(self, spec: DesignSpec, feedback: str) -> RequirementResult:
        """用户对已锁定规格提出修改时使用。"""
        system = prompts.requirement_system(self.device.name, self.board.name)
        user = (
            prompts.requirement_user(spec.summary, self.device.name, self.board.name, self.board.clock_mhz)
            + f"\n\n【当前规格】\n{spec.to_json()}\n\n【用户修改意见】\n{feedback}\n\n请输出修改后的完整规格 JSON。"
        )
        resp = self.ctx.llm(self.name, system, user, tag="requirement-refine", json_mode=True)
        data = extract_json(resp.text)
        new_spec = DesignSpec.from_dict(data)
        issues = new_spec.validate()
        return RequirementResult(spec=new_spec, attempts=1, issues=issues, ok=not issues)
