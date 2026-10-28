"""自检脚本：验证「参考模型 ↔ 自验证 Testbench ↔ 参考 RTL ↔ 真实 EDA」整条链路。

用法：
    python scripts/selfcheck.py            # 5 个基准设计全流程
    python scripts/selfcheck.py counter    # 单个设计

通过标准：
1. 参考 RTL 能通过由 oracle 生成的自验证 Testbench（说明模型与激励自洽）；
2. 参考 RTL 能被云源综合、布局布线并生成比特流（说明设计可上板）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yunfan.config import Settings  # noqa: E402
from yunfan.eda import Project, build_flow  # noqa: E402
from yunfan.eda.constraints import build_constraints  # noqa: E402
from yunfan.orchestration.spec import DesignSpec  # noqa: E402
from yunfan.rtl.lint import lint  # noqa: E402
from yunfan.rtl.oracles import build_plan  # noqa: E402
from yunfan.rtl.testbench import render_tb  # noqa: E402


def check_one(name: str, settings: Settings, flow) -> dict:
    d = ROOT / "examples" / name
    spec = DesignSpec.load(d / "spec.json")
    ref = (d / "ref.v").read_text(encoding="utf-8")
    out = Path(tempfile.mkdtemp(prefix=f"yunfan_{name}_"))
    (out / "src").mkdir(parents=True, exist_ok=True)

    project = Project(
        name=name,
        top=spec.top_module,
        root=out,
        rtl=out / "src" / f"{spec.top_module}.v",
        tb=out / "src" / f"tb_{spec.top_module}.v",
        cst=out / "src" / f"{spec.top_module}.cst",
        sdc=out / "src" / f"{spec.top_module}.sdc",
        device=settings.device,
        board=settings.board,
    )
    project.rtl.write_text(ref, encoding="utf-8")

    # 1) 静态检查
    rep = lint(ref, spec_ports=spec.ports)
    # 2) 自验证 TB
    plan = build_plan(spec.to_dict())
    if plan is None:
        raise SystemExit(f"{name}: 没有登记 oracle")
    project.tb.write_text(render_tb(spec.to_dict(), plan), encoding="utf-8")
    # 3) 约束
    cons = build_constraints(spec.to_dict(), settings.board, settings.device, settings.tools.gowin_root)
    project.cst.write_text(cons.cst, encoding="utf-8")
    project.sdc.write_text(cons.sdc, encoding="utf-8")

    t0 = time.time()
    sim = flow.simulate(project)
    sim_s = time.time() - t0

    t1 = time.time()
    impl = flow.implement(project)
    impl_s = time.time() - t1

    return {
        "design": name,
        "lint_ok": rep.ok,
        "lint_issues": [v.as_text() for v in rep.violations][:5],
        "checkpoints": len(plan.checks),
        "sim_ok": sim.ok,
        "sim_verdict": sim.verdict,
        "sim_s": round(sim_s, 2),
        "impl_ok": impl.ok,
        "impl_s": round(impl_s, 2),
        "bitstream": impl.artifacts.get("bitstream", ""),
        "fmax_mhz": impl.metrics.get("fmax_mhz", 0),
        "logic_used": impl.metrics.get("logic_used", 0),
        "register_used": impl.metrics.get("register_used", 0),
        "run_dir": str(out),
        "tail": (sim.log or "")[-400:] if not sim.ok else "",
        "impl_tail": (impl.log or "")[-500:] if not impl.ok else "",
    }


def main() -> int:
    settings = Settings.build()
    flow = build_flow(settings)
    names = sys.argv[1:] or ["counter", "pwm", "crc32", "uart_tx", "vga_timing"]
    rows = []
    bad = 0
    for name in names:
        print(f"\n===== {name} =====")
        row = check_one(name, settings, flow)
        rows.append(row)
        status = "OK" if (row["sim_ok"] and row["impl_ok"]) else "FAIL"
        if status == "FAIL":
            bad += 1
        print(
            f"lint={row['lint_ok']} checkpoints={row['checkpoints']} sim={row['sim_verdict']} "
            f"impl={row['impl_ok']} fmax={row['fmax_mhz']}MHz logic={row['logic_used']} "
            f"[{status}] sim_s={row['sim_s']} impl_s={row['impl_s']}"
        )
        if row["lint_issues"]:
            print("  lint:", "; ".join(row["lint_issues"]))
        if not row["sim_ok"]:
            print("  sim tail:", row["tail"].replace("\n", " | ")[-300:])
        if not row["impl_ok"]:
            print("  impl tail:", row["impl_tail"].replace("\n", " | ")[-400:])
    print("\n===== 汇总 =====")
    print(json.dumps(rows, ensure_ascii=False, indent=2)[:4000])
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "selfcheck.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
