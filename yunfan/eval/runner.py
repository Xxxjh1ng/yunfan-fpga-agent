"""批量评测运行器：把「设计 × 变体 × 随机种子」跑成论文可用的数据。

特性
----
* 断点续跑：已存在同 (design, variant, seed) 记录时跳过；
* 逐步落盘：每个 trial 一份 JSON + 汇总 JSONL，长跑不丢数据；
* 失败隔离：单个 trial 抛异常不影响其余 trial。
"""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from ..config import Settings
from ..orchestration.metrics import RunRecord, aggregate, compare_variants, to_markdown
from ..orchestration.pipeline import Pipeline, PipelineConfig
from ..orchestration.spec import DesignSpec


@dataclass
class EvalConfig:
    designs: List[str] = field(default_factory=list)
    variants: List[str] = field(default_factory=lambda: ["yunfan"])
    seeds: List[int] = field(default_factory=lambda: [0])
    out_dir: Path = field(default_factory=lambda: Path("runs/eval"))
    examples_dir: Path = field(default_factory=lambda: Path("examples"))
    max_repair: int = 5
    mode: str = "full"
    resume: bool = True
    temperature: float = 0.7


def run_suite(
    settings: Settings,
    cfg: EvalConfig,
    *,
    model_factory: Callable,
    verbose: bool = True,
) -> List[RunRecord]:
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    records: List[RunRecord] = []
    index_path = cfg.out_dir / "records.jsonl"
    existing: Dict[str, dict] = {}
    if cfg.resume and index_path.exists():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            try:
                data = json.loads(line)
                existing[f"{data['design']}|{data['variant']}|{data['seed']}"] = data
            except Exception:
                continue

    total = len(cfg.designs) * len(cfg.variants) * len(cfg.seeds)
    done = 0
    for design in cfg.designs:
        spec_path = cfg.examples_dir / design / "spec.json"
        if not spec_path.exists():
            print(f"[跳过] 找不到规格：{spec_path}")
            continue
        spec = DesignSpec.load(spec_path)
        for variant in cfg.variants:
            for seed in cfg.seeds:
                done += 1
                key = f"{design}|{variant}|{seed}"
                if key in existing:
                    records.append(RunRecord(**{k: v for k, v in existing[key].items() if k in RunRecord.__dataclass_fields__}))
                    if verbose:
                        print(f"[{done}/{total}] 复用已有记录 {key}")
                    continue

                if verbose:
                    print(f"\n[{done}/{total}] ===== {design} / {variant} / seed={seed} =====")
                model = model_factory()
                cfgp = PipelineConfig.baseline() if variant == "baseline" else PipelineConfig()
                cfgp.mode = cfg.mode
                cfgp.max_repair_rounds = cfg.max_repair
                cfgp.temperature = cfg.temperature
                pipeline = Pipeline(settings, model, config=cfgp, verbose=verbose)
                try:
                    rec = pipeline.run(spec=spec, seed=seed)
                except Exception as exc:  # noqa: BLE001
                    traceback.print_exc()
                    rec = RunRecord(design=design, variant=variant, seed=seed, run_id=f"{design}_{variant}_s{seed}_error")
                    rec.error = f"{type(exc).__name__}: {exc}"
                rec.variant = variant
                records.append(rec)
                with index_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec.as_dict(), ensure_ascii=False) + "\n")

    # 汇总
    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "aggregate": aggregate(records),
        "variants": compare_variants(records),
        "records": [r.as_dict() for r in records],
    }
    (cfg.out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (cfg.out_dir / "summary.md").write_text(to_markdown(records, title="云帆评测汇总"), encoding="utf-8")
    return records
