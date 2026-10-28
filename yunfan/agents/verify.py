"""智能体 3：仿真验证。

两条 Testbench 生成路径，正好用于论文的消融对照：
* **契约驱动（默认）**：由参考模型合成自验证激励，接口 100% 对齐；
* **LLM 直写（对照）**：复刻原始方案，让模型自由发挥，接口/判据都靠运气。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..llm import prompts
from ..orchestration.spec import DesignSpec
from ..rtl.oracles import build_plan, get_oracle
from ..rtl.testbench import TestPlan, judge_sim_output, render_tb
from .base import BaseAgent


@dataclass
class TbResult:
    code: str = ""
    plan: Optional[TestPlan] = None
    method: str = "template"  # template / llm
    oracle: Optional[str] = None
    tokens: int = 0

    @property
    def checkpoints(self) -> int:
        return len(self.plan.checks) if self.plan else 0


class VerifyAgent(BaseAgent):
    name = "仿真验证"
    role = "构造自验证激励并判定仿真结果"

    # ------------------------------------------------------------------
    def build_tb(self, spec: DesignSpec, *, period_ns: int = 20) -> TbResult:
        plan = build_plan(spec.to_dict(), period_ns=period_ns)
        if plan is None:
            self.warn("无参考模型", f"设计 {spec.name} 未登记参考模型，回退到 LLM 直写 Testbench")
            return TbResult(method="llm")

        code = render_tb(spec.to_dict(), plan)
        oracle = get_oracle(spec.to_dict())
        self.log(
            "自验证激励生成",
            f"{len(plan.drives)} 组驱动｜{len(plan.checks)} 个检查点｜覆盖率来源={oracle.description if oracle else '-'}",
        )
        return TbResult(code=code, plan=plan, method="template", oracle=oracle.name if oracle else "")

    # ------------------------------------------------------------------
    def build_llm_tb(self, spec: DesignSpec, rtl_code: str) -> TbResult:
        """对照路径：完全由大模型手写 Testbench。"""
        resp = self.ctx.llm(
            self.name,
            prompts.TB_SYSTEM,
            prompts.tb_user(spec.to_json(), rtl_code),
            tag="tb-llm",
        )
        from ..rtl.extract import extract_verilog

        code = extract_verilog(resp.text, prefer_name=f"tb_{spec.top_module}")
        self.log("LLM 直写 Testbench", f"{len(code.splitlines())} 行", tokens=resp.total_tokens)
        return TbResult(code=code, method="llm", tokens=resp.total_tokens)

    # ------------------------------------------------------------------
    def judge(self, stdout: str, stderr: str = "") -> Tuple[bool, str]:
        verdict, detail = judge_sim_output(stdout, stderr)
        return verdict == "pass", f"{verdict}: {detail}"
