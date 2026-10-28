"""EDA 适配层：把「综合/仿真/布局布线/烧录」抽象成可替换的后端。

设计动机：原方案把 Vivado 路径与 Tcl 脚本硬编码在业务代码里，
换国产工具链需要重写主流程。本层把差异收敛到后端实现，业务侧
只依赖 ``EdaFlow`` 协议，因此同一套智能体可以跑云源、也可以跑
Vivado/Mock（论文中的工具链可移植性即由此保证）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..config import Settings
from .base import EdaFlow, Project, StepResult, decode_output, write_log
from .gowin import GowinBackend, default_sdc
from .iverilog import IcarusEngine
from .mock import MockFlow

__all__ = [
    "EdaFlow",
    "Project",
    "StepResult",
    "GowinBackend",
    "IcarusEngine",
    "MockFlow",
    "CompositeFlow",
    "build_flow",
    "default_sdc",
]


@dataclass
class CompositeFlow:
    """综合后端 + 仿真后端 + 烧录后端的组合。"""

    synth: Optional[object] = None
    sim: Optional[object] = None
    programmer: Optional[object] = None
    name: str = "composite"
    notes: List[str] = field(default_factory=list)

    @property
    def has_synth(self) -> bool:
        return self.synth is not None

    @property
    def has_sim(self) -> bool:
        return self.sim is not None

    def compile(self, project: Project) -> StepResult:
        if self.synth is not None:
            return self.synth.compile(project)  # type: ignore[union-attr]
        if self.sim is not None:
            return self.sim.compile(project)  # type: ignore[union-attr]
        return StepResult(stage="compile", ok=False, log="无可用综合/编译后端", verdict="skipped")

    def simulate(self, project: Project) -> StepResult:
        if self.sim is not None:
            return self.sim.simulate(project)  # type: ignore[union-attr]
        return StepResult(stage="simulate", ok=False, log="无可用仿真后端", verdict="skipped")

    def implement(self, project: Project) -> StepResult:
        if self.synth is not None and hasattr(self.synth, "implement"):
            return self.synth.implement(project)  # type: ignore[union-attr]
        return StepResult(stage="implement", ok=False, log="无可用布局布线后端", verdict="skipped")

    def program(self, project: Project, *, dry_run: bool = True) -> StepResult:
        if self.synth is not None and hasattr(self.synth, "program"):
            return self.synth.program(project, dry_run=dry_run)  # type: ignore[union-attr]
        return StepResult(stage="program", ok=False, log="无可用烧录后端", verdict="skipped")

    def describe(self) -> Dict[str, object]:
        return {
            "synth": getattr(self.synth, "name", None),
            "sim": getattr(self.sim, "name", None),
            "has_synth": self.synth is not None,
            "has_sim": self.sim is not None,
            "notes": self.notes,
        }


def build_flow(settings: Settings, *, prefer: str = "auto") -> CompositeFlow:
    """按环境自动装配后端。prefer ∈ {auto, gowin, mock, sim-only}。"""
    tools = settings.tools
    notes: List[str] = []

    synth = None
    if prefer in ("auto", "gowin") and tools.gw_sh:
        synth = GowinBackend(
            tools.gw_sh,
            programmer_cli=tools.programmer_cli,
            gowin_root=tools.gowin_root,
            synth_timeout_s=settings.synth_timeout_s,
        )
        notes.append("综合/布局布线后端：高云云源 EDA (gw_sh)")
    elif prefer not in ("mock", "sim-only"):
        notes.append("未检测到云源 EDA，综合与布局布线降级为不可用")

    sim = None
    if prefer in ("auto", "gowin", "sim-only") and tools.iverilog and tools.vvp:
        sim = IcarusEngine(tools.iverilog, tools.vvp, sim_timeout_s=settings.sim_timeout_s)
        notes.append("仿真后端：Icarus Verilog")
    else:
        notes.append("未检测到 Icarus Verilog，仿真不可用")

    if prefer == "mock":
        return CompositeFlow(synth=None, sim=None, name="mock", notes=["强制使用 Mock 后端"])
    return CompositeFlow(synth=synth, sim=sim, name="composite", notes=notes)
