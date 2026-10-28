"""Icarus Verilog 仿真引擎。

选择开源仿真器的理由：
* 零许可成本，可在实验室 / 学生机上复现论文实验；
* 命令行语义干净（compile / run 两段），便于结构化诊断；
* 与云源的综合/PnR 解耦——云源本身把仿真交给外部仿真器，
  本适配层因此只替换「仿真后端」而不影响综合链路。
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..diagnostics.parser import parse_log
from ..rtl.testbench import judge_sim_output
from .base import Project, StepResult, decode_output, make_result, write_log


class IcarusEngine:
    name = "iverilog"

    def __init__(self, iverilog: str, vvp: str, *, compile_timeout_s: float = 120.0, sim_timeout_s: float = 180.0) -> None:
        self.iverilog = iverilog
        self.vvp = vvp
        self.compile_timeout_s = compile_timeout_s
        self.sim_timeout_s = sim_timeout_s

    # ------------------------------------------------------------------
    def available(self) -> bool:
        return bool(self.iverilog and self.vvp and Path(self.iverilog).exists() and Path(self.vvp).exists())

    def _bin_dir(self) -> Optional[str]:
        return str(Path(self.iverilog).parent) if self.iverilog else None

    def _env(self) -> Dict[str, str]:
        import os

        env = dict(os.environ)
        bindir = self._bin_dir()
        if bindir:
            env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        return env

    # ------------------------------------------------------------------
    def compile(self, project: Project) -> StepResult:
        """只做编译（不运行），用于快速语法门控。"""
        sources = [str(project.rtl)]
        if project.tb:
            sources.insert(0, str(project.tb))
        out = project.root / f"{project.top}.sim"
        cmd = [self.iverilog, "-g2012", "-o", str(out), *sources]
        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(project.root),
                capture_output=True,
                timeout=self.compile_timeout_s,
                env=self._env(),
            )
            log = decode_output(proc.stdout) + "\n" + decode_output(proc.stderr)
            ok = proc.returncode == 0 and "error" not in log.lower()
            return make_result(
                "compile", ok=ok, tool=self.name, cmd=cmd, returncode=proc.returncode,
                started=started, log=log, project=project, verdict="compiled" if ok else "error",
                parse_tool="icarus", artifacts={"sim_bin": str(out)},
            )
        except subprocess.TimeoutExpired:
            return make_result(
                "compile", ok=False, tool=self.name, cmd=cmd, returncode=-1,
                started=started, log="编译超时", project=project, verdict="timeout", parse_tool="icarus",
            )
        except FileNotFoundError as exc:
            return StepResult(stage="compile", ok=False, tool=self.name, cmd=" ".join(cmd), log=str(exc), verdict="error")

    # ------------------------------------------------------------------
    def simulate(self, project: Project) -> StepResult:
        """编译 + 运行，返回带 PASS/FAIL 判定的结果。"""
        comp = self.compile(project)
        if not comp.ok:
            comp.stage = "simulate"
            comp.verdict = "error"
            return comp

        sim_bin = project.root / f"{project.top}.sim"
        cmd = [self.vvp, "-n", str(sim_bin)]
        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(project.root),
                capture_output=True,
                timeout=self.sim_timeout_s,
                env=self._env(),
            )
            stdout = decode_output(proc.stdout)
            stderr = decode_output(proc.stderr)
            log = stdout + "\n" + stderr
            verdict, detail = judge_sim_output(stdout, stderr)
            ok = verdict == "pass"
            res = make_result(
                "simulate", ok=ok, tool=self.name, cmd=cmd, returncode=proc.returncode,
                started=started, log=log, project=project, verdict=verdict, parse_tool="icarus",
                artifacts={"log": str(project.log_dir / "simulate.log")},
            )
            if not ok and not res.diagnostics:
                from ..diagnostics.parser import Diagnostic

                taxon = "E12" if verdict == "timeout" else "E11"
                res.diagnostics = [Diagnostic(taxon=taxon, severity="error", tool="icarus", message=detail, raw=log[-800:])]
            res.duration_s += comp.duration_s
            res.metrics["compile_s"] = round(comp.duration_s, 3)
            wave = project.root / "wave.vcd"
            if wave.exists():
                res.artifacts["waveform"] = str(wave)
            return res
        except subprocess.TimeoutExpired:
            return make_result(
                "simulate", ok=False, tool=self.name, cmd=cmd, returncode=-1,
                started=started, log="仿真进程超时", project=project, verdict="timeout", parse_tool="icarus",
            )

    # ------------------------------------------------------------------
    def implement(self, project: Project) -> StepResult:
        return StepResult(stage="implement", ok=False, tool=self.name, log="Icarus 不提供综合/布局布线能力", verdict="skipped")

    def program(self, project: Project, *, dry_run: bool = True) -> StepResult:
        return StepResult(stage="program", ok=False, tool=self.name, log="Icarus 不提供烧录能力", verdict="skipped")
