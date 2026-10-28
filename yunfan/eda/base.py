"""EDA 适配层的公共抽象：Project、StepResult、EdaFlow 接口。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Sequence

from ..config import BoardProfile, DeviceProfile
from ..diagnostics.parser import Diagnostic, parse_log


@dataclass
class Project:
    """一次设计尝试在磁盘上的工程视图。"""

    name: str
    top: str
    root: Path
    rtl: Path
    tb: Optional[Path] = None
    cst: Optional[Path] = None
    sdc: Optional[Path] = None
    device: Optional[DeviceProfile] = None
    board: Optional[BoardProfile] = None

    @property
    def impl_dir(self) -> Path:
        return self.root / "impl"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"

    def ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(path).replace("\\", "/")

    def find_bitstream(self) -> Optional[Path]:
        for pat in ("impl/pnr/*.fs", "impl/pnr/*.bin", "*.fs"):
            hits = sorted(self.root.glob(pat))
            if hits:
                return hits[0]
        return None

    def find_rpt_txt(self) -> Optional[Path]:
        hits = sorted((self.root / "impl" / "pnr").glob("*.rpt.txt"))
        return hits[0] if hits else None

    def find_timing_report(self) -> Optional[Path]:
        hits = sorted((self.root / "impl" / "pnr").glob("*_tr_content.html"))
        return hits[0] if hits else None

    def find_pin_report(self) -> Optional[Path]:
        hits = sorted((self.root / "impl" / "pnr").glob("*.pin.html"))
        return hits[0] if hits else None

    def find_syn_report(self) -> Optional[Path]:
        hits = sorted((self.root / "impl" / "gwsynthesis").glob("*_syn.rpt.html"))
        return hits[0] if hits else None


@dataclass
class StepResult:
    stage: str
    ok: bool
    tool: str = ""
    cmd: str = ""
    returncode: int = 0
    duration_s: float = 0.0
    log: str = ""
    log_path: Optional[Path] = None
    diagnostics: List[Diagnostic] = field(default_factory=list)
    artifacts: Dict[str, str] = field(default_factory=dict)
    verdict: str = ""  # pass / fail / timeout / error / skipped
    metrics: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "stage": self.stage,
            "ok": self.ok,
            "tool": self.tool,
            "cmd": self.cmd,
            "returncode": self.returncode,
            "duration_s": round(self.duration_s, 3),
            "verdict": self.verdict,
            "log_path": str(self.log_path) if self.log_path else "",
            "artifacts": self.artifacts,
            "metrics": self.metrics,
            "diagnostics": [d.as_dict() for d in self.diagnostics],
        }

    def brief(self) -> str:
        tail = ""
        if self.diagnostics:
            tail = " | " + "; ".join(d.as_text() for d in self.diagnostics[:3])
        return f"[{self.stage}] {'OK' if self.ok else 'FAIL'} ({self.duration_s:.2f}s){tail}"


class EdaFlow(Protocol):
    name: str

    def compile(self, project: Project) -> StepResult: ...
    def simulate(self, project: Project) -> StepResult: ...
    def implement(self, project: Project) -> StepResult: ...
    def program(self, project: Project, *, dry_run: bool = True) -> StepResult: ...


def write_log(project: Project, stage: str, text: str, suffix: str = "log") -> Path:
    project.ensure_dirs()
    path = project.log_dir / f"{stage}.{suffix}"
    path.write_text(text or "", encoding="utf-8", errors="replace")
    return path


def decode_output(data: bytes) -> str:
    """Windows 下工具输出编码不确定：优先 UTF-8，回退 GBK/CP936。"""
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def make_result(
    stage: str,
    *,
    ok: bool,
    tool: str,
    cmd: Sequence[str],
    returncode: int,
    started: float,
    log: str,
    project: Optional[Project] = None,
    verdict: str = "",
    parse_tool: str = "auto",
    artifacts: Optional[Dict[str, str]] = None,
) -> StepResult:
    diags = parse_log(log, tool=parse_tool)
    log_path = write_log(project, stage, log) if project else None
    return StepResult(
        stage=stage,
        ok=ok,
        tool=tool,
        cmd=" ".join(cmd),
        returncode=returncode,
        duration_s=time.time() - started,
        log=log,
        log_path=log_path,
        diagnostics=diags,
        artifacts=artifacts or {},
        verdict=verdict,
    )
