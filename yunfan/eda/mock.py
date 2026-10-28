"""MockFlow：不依赖任何 EDA 工具的确定性流程，用于单元测试与离线 CI。

它既可直接返回预设结果（测试编排逻辑），也可调用真实静态检查，
从而在「没有云源许可证」的机器上仍然验证流水线正确性。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..diagnostics.parser import Diagnostic, parse_log
from ..rtl.lint import lint
from .base import Project, StepResult


@dataclass
class MockFlow:
    name: str = "mock"
    # 依次消费的仿真脚本；耗尽后默认返回通过
    sim_script: List[Dict[str, object]] = field(default_factory=list)
    # 依次消费的编译脚本
    compile_script: List[Dict[str, object]] = field(default_factory=list)
    implement_ok: bool = True
    call_log: List[str] = field(default_factory=list)

    def _pop(self, script: List[Dict[str, object]], default: Dict[str, object]) -> Dict[str, object]:
        return script.pop(0) if script else default

    def compile(self, project: Project) -> StepResult:
        self.call_log.append("compile")
        spec = self._pop(self.compile_script, {"ok": True, "log": "mock compile ok"})
        code = project.rtl.read_text(encoding="utf-8") if project.rtl.exists() else ""
        rep = lint(code)
        log = str(spec.get("log", "mock compile ok"))
        ok = bool(spec.get("ok", True)) and (rep.ok or not spec.get("use_lint", True))
        if not rep.ok and spec.get("use_lint", True):
            log += "\n" + rep.summary()
        return StepResult(
            stage="compile", ok=ok, tool="mock", log=log, verdict="compiled" if ok else "error",
            diagnostics=parse_log(log, tool="mock") or [Diagnostic(v.taxon, v.severity, "lint", v.message) for v in rep.errors],
            duration_s=0.001,
        )

    def simulate(self, project: Project) -> StepResult:
        self.call_log.append("simulate")
        spec = self._pop(self.sim_script, {"ok": True, "verdict": "pass", "log": "FINAL RESULT: PASSED"})
        log = str(spec.get("log", ""))
        ok = bool(spec.get("ok", True))
        verdict = str(spec.get("verdict", "pass" if ok else "fail"))
        diags = parse_log(log, tool="sentinel")
        if not diags and not ok:
            diags = [Diagnostic("E11", "error", "sentinel", "mock 失败")]
        return StepResult(
            stage="simulate", ok=ok, tool="mock", log=log, verdict=verdict,
            diagnostics=diags, duration_s=0.001,
        )

    def implement(self, project: Project) -> StepResult:
        self.call_log.append("implement")
        ok = self.implement_ok
        return StepResult(
            stage="implement", ok=ok, tool="mock", log="mock pnr ok" if ok else "mock pnr fail",
            verdict="ok" if ok else "error",
            metrics={"fmax_mhz": 120.0, "logic_used": 100.0} if ok else {},
            artifacts={"bitstream": "mock.fs"} if ok else {},
            duration_s=0.002,
        )

    def program(self, project: Project, *, dry_run: bool = True) -> StepResult:
        self.call_log.append("program")
        return StepResult(stage="program", ok=True, tool="mock", log="mock program dry-run", verdict="dry-run")
