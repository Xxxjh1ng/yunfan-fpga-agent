"""运行记录与指标：让每次设计都可追溯、可聚合、可复现。"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class RunRecord:
    run_id: str = ""
    design: str = ""
    seed: int = 0
    mode: str = "full"  # full / sim-only / lint-only
    variant: str = "yunfan"  # yunfan / baseline
    provider: str = ""
    model: str = ""
    prompt_version: str = ""

    spec_source: str = ""  # llm / file
    spec_ok: bool = False
    spec_issues: List[str] = field(default_factory=list)

    rtl_attempts: int = 0
    rtl_lines: int = 0
    lint_errors: int = 0
    lint_warnings: int = 0
    sanitize_fixes: Dict[str, int] = field(default_factory=dict)

    tb_method: str = ""
    tb_checkpoints: int = 0

    compile_ok: bool = False
    sim_ok: bool = False
    sim_verdict: str = ""
    repair_rounds: int = 0
    repair_methods: List[str] = field(default_factory=list)
    taxonomy_hits: Dict[str, int] = field(default_factory=dict)

    synth_ok: bool = False
    pnr_ok: bool = False
    bitstream: str = ""
    programmed: bool = False
    resources: Dict[str, float] = field(default_factory=dict)
    fmax_mhz: float = 0.0

    tokens: Dict[str, Any] = field(default_factory=dict)
    durations: Dict[str, float] = field(default_factory=dict)
    success: bool = False
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    artifacts: Dict[str, str] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.run_id or 'run'}.json"
        path.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @property
    def total_tokens(self) -> int:
        return int(self.tokens.get("total_tokens") or 0)

    @property
    def wall_s(self) -> float:
        return float(self.durations.get("total_s") or 0.0)

    def brief(self) -> str:
        flag = "✅" if self.success else "❌"
        return (
            f"{flag} {self.design}#{self.seed} [{self.variant}] "
            f"rtl×{self.rtl_attempts} 修复×{self.repair_rounds} "
            f"tokens={self.total_tokens} {self.wall_s:.1f}s"
        )


def pass_at_k(n: int, c: int, k: int) -> float:
    """Chen et al. 的无偏 Pass@k 估计：1 - C(n-c,k)/C(n,k)。"""
    if n <= 0 or k <= 0:
        return 0.0
    if c <= 0:
        return 0.0
    if n - c < k:
        return 1.0
    try:
        return 1.0 - math.comb(n - c, k) / math.comb(n, k)
    except Exception:
        return 1.0 if c >= k else 0.0


def aggregate(records: Sequence[RunRecord]) -> Dict[str, Any]:
    """把一组运行聚合成论文表格所需的统计量。"""
    if not records:
        return {"runs": 0}

    def rate(pred) -> float:
        return round(sum(1 for r in records if pred(r)) / len(records), 4)

    by_design: Dict[str, List[RunRecord]] = {}
    for r in records:
        by_design.setdefault(r.design, []).append(r)

    by_variant_design: Dict[str, Dict[str, Any]] = {}
    for variant in sorted({r.variant for r in records}):
        sub = [r for r in records if r.variant == variant]
        by_variant_design[variant] = {}
        for design in sorted({r.design for r in sub}):
            rs = [r for r in sub if r.design == design]
            n = len(rs)
            c = sum(1 for r in rs if r.success)
            by_variant_design[variant][design] = {
                "n": n,
                "sim_pass_rate": round(sum(1 for r in rs if r.sim_ok) / n, 4),
                "success_rate": round(c / n, 4),
                "avg_repair_rounds": round(sum(r.repair_rounds for r in rs) / n, 2),
                "avg_tokens": round(sum(r.total_tokens for r in rs) / n, 1),
                "avg_wall_s": round(sum(r.wall_s for r in rs) / n, 2),
                "fmax_mhz": round(sum(r.fmax_mhz for r in rs if r.fmax_mhz) / max(1, sum(1 for r in rs if r.fmax_mhz)), 1),
                "pass@1": round(pass_at_k(n, c, 1), 4),
                "pass@3": round(pass_at_k(n, c, min(3, n)), 4),
            }

    design_stats = {}
    for design, rs in by_design.items():
        n = len(rs)
        c = sum(1 for r in rs if r.success)
        sim_c = sum(1 for r in rs if r.sim_ok)
        design_stats[design] = {
            "n": n,
            "success": c,
            "success_rate": round(c / n, 4),
            "sim_pass_rate": round(sim_c / n, 4),
            "pass@1": round(pass_at_k(n, c, 1), 4),
            "pass@k": round(pass_at_k(n, c, min(3, n)), 4),
            "avg_repair_rounds": round(sum(r.repair_rounds for r in rs) / n, 2),
            "avg_rtl_attempts": round(sum(r.rtl_attempts for r in rs) / n, 2),
            "avg_tokens": round(sum(r.total_tokens for r in rs) / n, 1),
            "avg_wall_s": round(sum(r.wall_s for r in rs) / n, 2),
            "fmax_mhz": round(sum(r.fmax_mhz for r in rs if r.fmax_mhz) / max(1, sum(1 for r in rs if r.fmax_mhz)), 1),
        }

    tax: Dict[str, int] = {}
    for r in records:
        for k, v in r.taxonomy_hits.items():
            tax[k] = tax.get(k, 0) + v

    methods: Dict[str, int] = {}
    for r in records:
        for m in r.repair_methods:
            methods[m] = methods.get(m, 0) + 1

    first_sim = sum(1 for r in records if r.sim_ok and r.repair_rounds == 0)
    return {
        "runs": len(records),
        "variants": sorted({r.variant for r in records}),
        "models": sorted({r.model for r in records if r.model}),
        "success_rate": rate(lambda r: r.success),
        "sim_pass_rate": rate(lambda r: r.sim_ok),
        "first_attempt_sim_pass_rate": rate(lambda r: r.sim_ok and r.repair_rounds == 0),
        "first_attempt_compile_rate": rate(lambda r: r.compile_ok and r.rtl_attempts <= 1),
        "bitstream_rate": rate(lambda r: bool(r.bitstream)),
        "avg_repair_rounds": round(sum(r.repair_rounds for r in records) / len(records), 2),
        "avg_repair_rounds_on_failure": round(
            sum(r.repair_rounds for r in records if not r.success) / max(1, sum(1 for r in records if not r.success)), 2
        ),
        "avg_tokens": round(sum(r.total_tokens for r in records) / len(records), 1),
        "avg_wall_s": round(sum(r.wall_s for r in records) / len(records), 2),
        "total_tokens": sum(r.total_tokens for r in records),
        "first_sim_pass_count": first_sim,
        "taxonomy_hits": dict(sorted(tax.items())),
        "repair_methods": dict(sorted(methods.items())),
        "by_design": dict(sorted(design_stats.items())),
        "by_variant_design": by_variant_design,
    }


def compare_variants(records: Sequence[RunRecord]) -> List[Dict[str, Any]]:
    """按 variant 分组对比（论文中的消融表）。"""
    groups: Dict[str, List[RunRecord]] = {}
    for r in records:
        groups.setdefault(r.variant, []).append(r)
    rows = []
    for variant, rs in sorted(groups.items()):
        agg = aggregate(rs)
        rows.append(
            {
                "variant": variant,
                "runs": agg["runs"],
                "sim_pass_rate": agg["sim_pass_rate"],
                "first_attempt_sim_pass_rate": agg["first_attempt_sim_pass_rate"],
                "success_rate": agg["success_rate"],
                "avg_repair_rounds": agg["avg_repair_rounds"],
                "avg_tokens": agg["avg_tokens"],
                "avg_wall_s": agg["avg_wall_s"],
                "bitstream_rate": agg["bitstream_rate"],
            }
        )
    return rows


def to_markdown(records: Sequence[RunRecord], title: str = "运行汇总") -> str:
    agg = aggregate(records)
    lines = [f"# {title}", ""]
    lines.append(f"- 运行数：{agg['runs']}")
    lines.append(f"- 变体：{', '.join(agg['variants'])}")
    lines.append(f"- 仿真通过率：{agg['sim_pass_rate']:.1%}")
    lines.append(f"- 首次仿真通过率：{agg['first_attempt_sim_pass_rate']:.1%}")
    lines.append(f"- 端到端成功率：{agg['success_rate']:.1%}")
    lines.append(f"- 平均修复轮次：{agg['avg_repair_rounds']}")
    lines.append(f"- 平均 token：{agg['avg_tokens']}")
    lines.append("")
    lines.append("| 设计 | n | 仿真通过 | 成功率 | Pass@1 | 平均修复 | 平均 token | 平均耗时(s) | Fmax(MHz) |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for name, st in agg["by_design"].items():
        lines.append(
            f"| {name} | {st['n']} | {st['sim_pass_rate']:.0%} | {st['success_rate']:.0%} | "
            f"{st['pass@1']:.2f} | {st['avg_repair_rounds']} | {st['avg_tokens']:.0f} | {st['avg_wall_s']} | {st['fmax_mhz']} |"
        )
    if agg["taxonomy_hits"]:
        lines += ["", "## 诊断类别分布", "", "| 类别 | 次数 |", "| --- | --- |"]
        for k, v in agg["taxonomy_hits"].items():
            lines.append(f"| {k} | {v} |")
    return "\n".join(lines) + "\n"
