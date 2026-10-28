"""RTL 处理层：抽取、净化、接口解析、静态检查、自验证激励、参考模型。"""

from __future__ import annotations

from .extract import extract_verilog, inspect_structure, mask_noncode, module_name_of, strip_fences, strip_thinking
from .interface import ModuleInterface, Port, PortMismatch, compare_ports, parse_interface
from .lint import LintReport, Violation, lint
from .oracles import ORACLES, CycleOracle, build_plan, get_oracle
from .sanitize import Fix, SanitizeReport, sanitize
from .testbench import FAIL_TOKEN, PASS_TOKEN, TIMEOUT_TOKEN, Check, Drive, TestPlan, judge_sim_output, render_tb

__all__ = [
    "extract_verilog",
    "inspect_structure",
    "mask_noncode",
    "module_name_of",
    "strip_fences",
    "strip_thinking",
    "ModuleInterface",
    "Port",
    "PortMismatch",
    "compare_ports",
    "parse_interface",
    "LintReport",
    "Violation",
    "lint",
    "ORACLES",
    "CycleOracle",
    "build_plan",
    "get_oracle",
    "Fix",
    "SanitizeReport",
    "sanitize",
    "PASS_TOKEN",
    "FAIL_TOKEN",
    "TIMEOUT_TOKEN",
    "Check",
    "Drive",
    "TestPlan",
    "judge_sim_output",
    "render_tb",
]
