"""日志诊断评测（论文实验 E2）。

两条互相独立的证据链：
1. **真实日志归因**：从故障注入基准的实跑日志中，取首个失败阶段的原始输出，
   检查解析器给出的主诊断是否等于注入故障的期望类别（无偏）；
2. **语料回归**：用 32 条格式真实的日志片段覆盖 15 个类别 × 3 种工具方言，
   统计分类准确率与混淆情况（用于回归与覆盖率分析）。

输出：reports/diagnostics_eval.json + 控制台表格
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yunfan.diagnostics.parser import parse_log, primary_taxon  # noqa: E402

STAGE_FILES = ["synthesize.log", "simulate.log", "implement.log", "lint.log"]


def eval_corpus() -> dict:
    path = ROOT / "yunfan" / "eval" / "log_corpus.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        diags = parse_log(item["log"], tool=item["tool"])
        got = primary_taxon(diags) or ""
        rows.append({**item, "predicted": got, "ok": got == item["taxon"], "n_diag": len(diags)})
    acc = sum(1 for r in rows if r["ok"]) / max(1, len(rows))
    conf: dict = defaultdict(dict)
    for r in rows:
        if not r["ok"]:
            conf[r["taxon"]][r["predicted"]] = conf[r["taxon"]].get(r["predicted"], 0) + 1
    return {"total": len(rows), "accuracy": round(acc, 4), "rows": rows, "confusions": conf}


def eval_real_logs() -> dict:
    bench_path = ROOT / "reports" / "repair_bench.json"
    if not bench_path.exists():
        return {"total": 0, "accuracy": 0.0, "rows": []}
    cases = json.loads(bench_path.read_text(encoding="utf-8"))
    rows = []
    for case in cases:
        run_dir = ROOT / "runs" / case.get("run_id", "")
        if not run_dir.is_dir():
            continue
        stage = case.get("first_stage") or ""
        # 按阶段先后顺序扫描所有日志，取第一条错误级诊断
        order = {"lint": 0, "synthesize": 1, "compile": 1, "simulate": 2, "implement": 3}
        logs = sorted(
            [p for p in (run_dir / "logs").glob("*.log")],
            key=lambda p: (order.get(p.name.split("_r")[0].split(".")[0], 9), p.name),
        )
        got = ""
        for target in logs:
            diags = [d for d in parse_log(target.read_text(encoding="utf-8", errors="replace"), tool="auto") if d.severity == "error"]
            if diags:
                got = primary_taxon(diags)
                break
        rows.append(
            {
                "design": case["design"],
                "fault": case["fault"],
                "expected": case["expected_taxon"],
                "stage": stage,
                "predicted": got,
                "n_diag": len(diags),
                "ok": got == case["expected_taxon"],
            }
        )
    acc = sum(1 for r in rows if r["ok"]) / max(1, len(rows))
    return {"total": len(rows), "accuracy": round(acc, 4), "rows": rows}


def main() -> int:
    corpus = eval_corpus()
    real = eval_real_logs()
    print(f"[语料回归] n={corpus['total']} accuracy={corpus['accuracy']:.1%}")
    for r in corpus["rows"]:
        if not r["ok"]:
            print(f"   miss {r['id']} expect={r['taxon']} got={r['predicted']} tool={r['tool']}")
    print(f"\n[真实日志归因] n={real['total']} accuracy={real['accuracy']:.1%}")
    for r in real["rows"]:
        flag = "OK " if r["ok"] else "MISS"
        print(f"  {flag} {r['design']:<11} {r['fault']} stage={r['stage']:<6} expect={r['expected']} got={r['predicted']}")

    out = {"corpus": corpus, "real_logs": real}
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "diagnostics_eval.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
