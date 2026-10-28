"""故障注入修复基准：度量闭环对各类错误的恢复能力。

用法：
    python scripts/repair_bench.py                    # 全部设计 × 全部故障
    python scripts/repair_bench.py counter crc32      # 指定设计
    python scripts/repair_bench.py --faults F01,F05   # 指定故障

输出：reports/repair_bench.json + 控制台表格
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yunfan.config import Settings  # noqa: E402
from yunfan.eda.constraints import build_constraints  # noqa: E402
from yunfan.eval.faults import FAULTS, get_fault  # noqa: E402
from yunfan.llm import TokenLedger, build_model  # noqa: E402
from yunfan.orchestration.pipeline import Pipeline, PipelineConfig  # noqa: E402
from yunfan.orchestration.spec import DesignSpec  # noqa: E402

STAGE_MARKERS = {
    "lint": "静态门控拦截",
    "synth": "综合失败",
    "sim": "仿真未通过",
    "pnr": "布局布线失败",
}


def first_stage(rec) -> str:
    for e in rec.events:
        for key, marker in STAGE_MARKERS.items():
            if marker in str(e.get("action", "")):
                return key
    return ""


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    fault_filter = None
    for a in sys.argv[1:]:
        if a.startswith("--faults="):
            fault_filter = set(a.split("=", 1)[1].split(","))
    designs = args or ["counter", "pwm", "crc32", "uart_tx", "vga_timing"]

    settings = Settings.build()
    settings.max_repair_rounds = 4
    rows = []
    for design in designs:
        spec = DesignSpec.load(ROOT / "examples" / design / "spec.json")
        ref = (ROOT / "examples" / design / "ref.v").read_text(encoding="utf-8")
        cons = build_constraints(spec.to_dict(), settings.board, settings.device, settings.tools.gowin_root)
        for fault in FAULTS:
            if fault_filter and fault.fid not in fault_filter:
                continue
            text = ref if fault.target == "code" else cons.cst
            mutated = fault.run(text, spec.to_dict())
            if mutated is None or mutated == text:
                continue
            code, cst = (mutated, cons.cst) if fault.target == "code" else (ref, mutated)
            print(f"\n===== {design} / {fault.fid} {fault.name} (期望 {fault.taxon}) =====")
            ledger = TokenLedger()
            model = build_model(settings.llm, ledger=ledger)
            cfg = PipelineConfig(mode="full", variant=f"fault_{fault.fid}", max_repair_rounds=settings.max_repair_rounds)
            pipeline = Pipeline(settings, model, config=cfg, verbose=True)
            rec = pipeline.run(spec=spec, preset_code=code, preset_cst=cst, seed=0, variant_label=f"fault_{fault.fid}")
            row = {
                "design": design,
                "fault": fault.fid,
                "fault_name": fault.name,
                "expected_taxon": fault.taxon,
                "first_stage": first_stage(rec),
                "hit_taxa": rec.taxonomy_hits,
                "success": rec.success,
                "repair_rounds": rec.repair_rounds,
                "methods": rec.repair_methods,
                "tokens": rec.total_tokens,
                "wall_s": rec.wall_s,
                "run_id": rec.run_id,
                "eda_calls": sum(1 for k in rec.durations if k.startswith("synth_")),
                "sanitize_fixes": rec.sanitize_fixes,
                "pre_eda": first_stage(rec) == "lint",
            }
            rows.append(row)
            print(f"  -> success={row['success']} stage={row['first_stage']} rounds={row['repair_rounds']} "
                  f"methods={row['methods']} eda_calls={row['eda_calls']} tokens={row['tokens']}")

    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    bench_path = out / "repair_bench.json"
    merged = {}
    if bench_path.exists() and len(designs) < 5:
        try:
            for r in json.loads(bench_path.read_text(encoding="utf-8")):
                merged[(r["design"], r["fault"])] = r
        except Exception:
            merged = {}
    for r in rows:
        merged[(r["design"], r["fault"])] = r
    rows = [merged[k] for k in sorted(merged)]
    bench_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    total = len(rows)
    ok = sum(1 for r in rows if r["success"])
    det = sum(1 for r in rows if r["methods"] and r["methods"][0] == "deterministic")
    no_eda = sum(1 for r in rows if r["pre_eda"])
    sanitized = sum(1 for r in rows if r["sanitize_fixes"] and r["repair_rounds"] == 0)
    print("\n===== 汇总 =====")
    print(f"cases={total} recovered={ok} rate={ok / max(1, total):.1%} deterministic_first={det} pre_eda_caught={no_eda} sanitized={sanitized}")
    for r in rows:
        print(f"  {r['design']:<11} {r['fault']} {r['expected_taxon']} stage={r['first_stage'] or '-':<6} "
              f"ok={r['success']} rounds={r['repair_rounds']} {r['methods'][:2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
