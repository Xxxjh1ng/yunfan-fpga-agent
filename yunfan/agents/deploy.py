"""智能体 5：硬件部署。

职责边界刻意划清：
* **生成约束**（CST/SDC）——由规格 + 板卡资源表确定性推导，不由模型臆造引脚；
* **驱动实现**（综合/布局布线/比特流）——调用云源 gw_sh；
* **烧录**（Programmer CLI）——默认 dry-run，避免误烧；只有显式 --program 才真正下载。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from ..config import BoardProfile, DeviceProfile
from ..eda.constraints import ConstraintResult, build_constraints
from ..orchestration.spec import DesignSpec
from .base import BaseAgent


class DeployAgent(BaseAgent):
    name = "硬件部署"
    role = "生成物理/时序约束并驱动云源完成板级部署"

    def __init__(self, ctx, device: DeviceProfile, board: BoardProfile, gowin_root: Optional[str] = None) -> None:
        super().__init__(ctx)
        self.device = device
        self.board = board
        self.gowin_root = gowin_root

    # ------------------------------------------------------------------
    def prepare_constraints(self, spec: DesignSpec) -> ConstraintResult:
        res = build_constraints(spec.to_dict(), self.board, self.device, self.gowin_root)
        self.log(
            "约束生成",
            f"引脚 {len(res.assignments)} 个（覆盖率 {res.coverage:.0%}）｜SDC {spec.clock_freq_mhz:g}MHz",
        )
        if res.unmapped:
            self.warn("存在未映射端口", ", ".join(res.unmapped[:6]))
        return res

    # ------------------------------------------------------------------
    def describe_bitstream(self, artifacts: Dict[str, str], metrics: Dict[str, float]) -> str:
        bit = artifacts.get("bitstream", "")
        parts = []
        if bit:
            parts.append(f"比特流 {bit}")
        if "fmax_mhz" in metrics:
            parts.append(f"Fmax {metrics['fmax_mhz']:.1f}MHz")
        if "logic_used" in metrics:
            parts.append(f"LUT {metrics['logic_used']:.0f}")
        if "register_used" in metrics:
            parts.append(f"FF {metrics['register_used']:.0f}")
        return "｜".join(parts) if parts else "未产出实现结果"
