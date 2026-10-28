"""云帆命令行入口。

常用：
    python -m yunfan doctor
    python -m yunfan design --spec examples/counter/spec.json
    python -m yunfan design --text "设计一个 4 位计数器…" --out runs
    python -m yunfan eval --seeds 3 --variants yunfan,baseline
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import Settings, discover_toolchain
from .eda.constraints import build_constraints
from .llm import TokenLedger, build_model
from .llm.base import LLMError
from .orchestration.metrics import aggregate, compare_variants, to_markdown
from .orchestration.pipeline import Pipeline, PipelineConfig
from .orchestration.spec import DesignSpec


def _fix_console() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _banner() -> None:
    print("=" * 72)
    print(f"  云帆 YunFan v{__version__} — 高云 FPGA 多智能体全流程自动化设计框架")
    print("  需求解析 → 代码生成 → 仿真验证 → 调试修复 → 硬件部署")
    print("=" * 72)


# --------------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> int:
    _fix_console()
    _banner()
    settings = Settings.build()
    print(settings.describe())
    print("\n环境自检：")
    ok = True
    checks = [
        ("云源 EDA (gw_sh)", settings.tools.has_synth),
        ("Icarus Verilog", settings.tools.has_sim),
        ("Gowin Programmer", settings.tools.has_programmer),
        ("LLM API Key", bool(settings.llm.api_key)),
    ]
    for name, passed in checks:
        print(f"  [{'✓' if passed else '×'}] {name}")
        ok = ok and passed
    return 0 if ok else 1


def _make_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.build()
    if getattr(args, "device", None):
        from .config import DeviceProfile

        settings.device = DeviceProfile.load(args.device)
    if getattr(args, "board", None):
        from .config import BoardProfile

        settings.board = BoardProfile.load(args.board)
    if getattr(args, "out", None):
        settings.out_dir = Path(args.out)
    settings.llm.provider = getattr(args, "provider", None) or settings.llm.provider
    if getattr(args, "model", None):
        settings.llm.model = args.model
    return settings


def _build_pipeline(args: argparse.Namespace, settings: Settings) -> Pipeline:
    ledger = TokenLedger()
    try:
        model = build_model(settings.llm, ledger=ledger)
    except LLMError as exc:
        print(f"[错误] 模型初始化失败：{exc}")
        raise SystemExit(2)

    cfg = PipelineConfig(
        mode=args.mode,
        variant=args.variant,
        tb_mode=args.tb,
        use_lint_gate=not args.no_lint_gate,
        use_structured_diag=not args.no_structured_diag,
        use_deterministic_repair=not args.no_deterministic,
        max_repair_rounds=args.max_repair,
        max_rtl_attempts=args.max_rtl,
        program=args.program,
        dry_run_program=not args.program_real,
    )
    if args.variant == "baseline":
        cfg = PipelineConfig.baseline()
        cfg.max_repair_rounds = args.max_repair
        cfg.program = args.program
    return Pipeline(settings, model, config=cfg, verbose=not args.quiet)


def cmd_spec(args: argparse.Namespace) -> int:
    _fix_console()
    settings = _make_settings(args)
    ledger = TokenLedger()
    model = build_model(settings.llm, ledger=ledger)
    from .agents.base import AgentContext
    from .agents.requirement import RequirementAgent

    ctx = AgentContext(settings, model)
    agent = RequirementAgent(ctx, settings.device, settings.board)
    result = agent.analyze(args.text)
    if not result.ok:
        print("[失败] 规格不完整：" + "; ".join(result.issues))
        return 1
    print(result.spec.to_json())
    if args.out_spec:
        Path(args.out_spec).write_text(result.spec.to_json(), encoding="utf-8")
        print(f"\n已写入 {args.out_spec}")
    return 0


def cmd_design(args: argparse.Namespace) -> int:
    _fix_console()
    _banner()
    settings = _make_settings(args)
    spec = DesignSpec.load(args.spec) if args.spec else None
    if spec is None and not args.text:
        print("[错误] 需要 --spec 或 --text")
        return 2
    pipeline = _build_pipeline(args, settings)
    t0 = time.time()
    rec = pipeline.run(user_text=args.text, spec=spec, seed=args.seed)
    print("\n" + "-" * 72)
    print(rec.brief())
    if rec.error:
        print(f"原因：{rec.error}")
    print(f"产物目录：{Path(settings.out_dir) / rec.run_id}")
    if args.json:
        print(json.dumps(rec.as_dict(), ensure_ascii=False, indent=2))
    return 0 if rec.success else 1


def cmd_eval(args: argparse.Namespace) -> int:
    _fix_console()
    _banner()
    settings = _make_settings(args)
    from .eval.runner import EvalConfig, run_suite

    designs = args.designs.split(",") if args.designs else ["counter", "pwm", "crc32", "uart_tx", "vga_timing"]
    variants = args.variants.split(",")
    seeds = list(range(args.seeds))
    cfg = EvalConfig(
        designs=designs,
        variants=variants,
        seeds=seeds,
        out_dir=Path(args.out) if args.out else settings.out_dir / "eval",
        max_repair=args.max_repair,
        mode=args.mode,
        resume=not args.no_resume,
    )
    records = run_suite(settings, cfg, model_factory=lambda: build_model(settings.llm, ledger=TokenLedger()), verbose=not args.quiet)
    print("\n" + "=" * 72)
    print(to_markdown(records, title="评测汇总"))
    for row in compare_variants(records):
        print(row)
    return 0


def cmd_table(args: argparse.Namespace) -> int:
    _fix_console()
    from .orchestration.metrics import RunRecord

    rows = []
    for p in sorted(Path(args.dir).glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            rows.append(RunRecord(**{k: v for k, v in data.items() if k in RunRecord.__dataclass_fields__}))
        except Exception:
            continue
    print(to_markdown(rows, title=f"汇总（{args.dir}）"))
    return 0


def cmd_constraints(args: argparse.Namespace) -> int:
    _fix_console()
    settings = _make_settings(args)
    spec = DesignSpec.load(args.spec)
    res = build_constraints(spec.to_dict(), settings.board, settings.device, settings.tools.gowin_root)
    if args.output:
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{spec.top_module}.cst").write_text(res.cst, encoding="utf-8")
        (out / f"{spec.top_module}.sdc").write_text(res.sdc, encoding="utf-8")
        print(f"已写入 {out}")
    print(res.cst)
    print(res.sdc)
    print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yunfan", description="云帆：高云 FPGA 多智能体全流程自动化设计框架")
    parser.add_argument("--version", action="version", version=f"yunfan {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--device", help="器件型号，如 GW5A-25A / GW2A-18C")
        p.add_argument("--board", help="开发板，如 xm_gw5a25")
        p.add_argument("--provider", help="LLM provider: deepseek / openai / offline")
        p.add_argument("--model", help="模型名，如 deepseek-chat")
        p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("doctor", help="环境自检")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("spec", help="由自然语言生成设计规格")
    p.add_argument("--text", required=True)
    p.add_argument("--out-spec")
    common(p)
    p.set_defaults(func=cmd_spec)

    p = sub.add_parser("design", help="执行一次端到端设计")
    p.add_argument("--text", help="自然语言需求")
    p.add_argument("--spec", help="规格 JSON 路径")
    p.add_argument("--out", help="输出目录")
    p.add_argument("--mode", default="full", choices=["full", "sim-only", "lint-only"])
    p.add_argument("--variant", default="yunfan", choices=["yunfan", "baseline"])
    p.add_argument("--tb", default="template", choices=["template", "llm"])
    p.add_argument("--max-repair", type=int, default=5)
    p.add_argument("--max-rtl", type=int, default=3)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--program", action="store_true", help="生成比特流后尝试烧录（默认 dry-run）")
    p.add_argument("--program-real", action="store_true", help="真正烧录到板卡")
    p.add_argument("--no-lint-gate", action="store_true")
    p.add_argument("--no-structured-diag", action="store_true")
    p.add_argument("--no-deterministic", action="store_true")
    p.add_argument("--json", action="store_true")
    common(p)
    p.set_defaults(func=cmd_design)

    p = sub.add_parser("eval", help="批量评测（论文实验）")
    p.add_argument("--designs", help="逗号分隔的设计名")
    p.add_argument("--variants", default="yunfan", help="逗号分隔：yunfan,baseline")
    p.add_argument("--seeds", type=int, default=3)
    p.add_argument("--out")
    p.add_argument("--mode", default="full", choices=["full", "sim-only", "lint-only"])
    p.add_argument("--max-repair", type=int, default=5)
    p.add_argument("--no-resume", action="store_true")
    common(p)
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("table", help="汇总某一目录下的运行记录")
    p.add_argument("--dir", required=True)
    p.set_defaults(func=cmd_table)

    p = sub.add_parser("constraints", help="生成 .cst/.sdc 约束")
    p.add_argument("--spec", required=True)
    p.add_argument("--output")
    common(p)
    p.set_defaults(func=cmd_constraints)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _fix_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
