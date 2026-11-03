"""控制台 ↔ EDA 的桥接脚本（在项目 venv 里运行）。

为什么要单独一个脚本：控制台 exe 是独立打包的，里面没有 yunfan 包；
所有真正碰 iverilog / gw_sh / programmer_cli 的动作都交给这个脚本，
用项目自己的 .venv 执行，保证和命令行行为完全一致。

协议：正常日志打到 stdout；最后一行打印 @@RESULT@@{json} 作为结构化结果。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from yunfan.config import BoardProfile, DeviceProfile, Settings  # noqa: E402
from yunfan.eda.base import Project  # noqa: E402
from yunfan.eda.gowin import GowinBackend  # noqa: E402
from yunfan.eda.iverilog import IcarusEngine  # noqa: E402

MARK = "@@RESULT@@"


def emit(obj: dict) -> None:
    print(MARK + json.dumps(obj, ensure_ascii=False), flush=True)


def _device(name: str | None):
    if not name:
        return None
    try:
        return DeviceProfile.load(name)
    except Exception:
        return None


def _ensure_dump(tb_path: Path) -> bool:
    """给没有 $dumpfile 的 Testbench 补上波形导出，返回是否做了修改。"""
    try:
        text = tb_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if "$dumpfile" in text or text.count("endmodule") != 1:
        return False
    mods = list(re.finditer(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)", text))
    if not mods:
        return False
    top = mods[-1].group(1)
    snippet = (
        "\n    initial begin\n"
        '        $dumpfile("wave.vcd");\n'
        f"        $dumpvars(0, {top});\n"
        "    end\n"
    )
    idx = text.rfind("endmodule")
    tb_path.write_text(text[:idx] + snippet + text[idx:], encoding="utf-8")
    return True


def cmd_simulate(args) -> int:
    settings = Settings.build()
    root = Path(args.dir).resolve()
    (root / "logs").mkdir(parents=True, exist_ok=True)

    rtl = Path(args.rtl).resolve()
    tb = Path(args.tb).resolve()
    if _ensure_dump(tb):
        print("[仿真] Testbench 缺少波形导出，已自动补 $dumpfile/$dumpvars", flush=True)

    project = Project(name=args.top, top=args.top, root=root, rtl=rtl, tb=tb)

    engine = IcarusEngine(settings.tools.iverilog, settings.tools.vvp)
    print(f"[仿真] iverilog 编译：{rtl.name} + {tb.name}", flush=True)
    res = engine.simulate(project)
    (root / "logs" / "simulate.log").write_text(res.log or "", encoding="utf-8")

    wave = None
    vcd = root / "wave.vcd"
    if vcd.is_file():
        try:
            from vcd import parse_vcd

            data = parse_vcd(vcd)
            (root / "wave.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            wave = {"signals": len(data["signals"]), "tmax": data["tmax"], "timescale": data["timescale"]}
            print(f"[仿真] 波形已解析：{len(data['signals'])} 个信号，{data['tmax']} {data['timescale']}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 波形解析失败：{exc}", flush=True)

    for line in (res.log or "").splitlines()[-40:]:
        print(line, flush=True)

    emit({
        "ok": res.ok,
        "verdict": res.verdict,
        "stage": "simulate",
        "run_dir": root.name,
        "waveform": wave,
        "duration_s": round(res.duration_s, 2),
        "log_tail": (res.log or "")[-4000:],
    })
    return 0 if res.ok else 1


def cmd_program(args) -> int:
    settings = Settings.build()
    root = Path(args.dir).resolve()
    device = _device(args.device)
    top = args.top or "top"
    bit = root / "impl" / "pnr"
    project = Project(name=root.name, top=top, root=root, rtl=root / "src" / f"{top}.v",
                      device=device)

    backend = GowinBackend(
        settings.tools.gw_sh,
        programmer_cli=settings.tools.programmer_cli,
        gowin_root=settings.tools.gowin_root,
    )
    found = project.find_bitstream()
    if not found:
        print("[错误] 该运行目录下没有比特流（.fs）", flush=True)
        emit({"ok": False, "verdict": "error", "message": "未找到 .fs 比特流"})
        return 1

    mode = "真实烧录" if args.real else "dry-run 校验"
    print(f"[烧录] 模式：{mode}｜器件：{args.device or '默认'}｜比特流：{found.name}", flush=True)
    print(f"[烧录] 命令：programmer_cli --device ... --run 2 --fsFile \"{found}\" --scan-cables", flush=True)
    res = backend.program(project, dry_run=not args.real)

    for line in (res.log or "").splitlines():
        print(line, flush=True)

    if not res.ok and "MAINCMD" in (res.log or ""):
        print("\n[提示] programmer_cli 报 MAINCMD module not found：这是它在「打包后的"
              "无控制台进程树」里的已知问题。请改用命令行烧录：", flush=True)
        print("       .\\.venv\\Scripts\\python.exe scripts\\hw_run.py run led_water", flush=True)
        print("       .\\.venv\\Scripts\\python.exe -m yunfan design --spec <spec> --program --program-real", flush=True)

    cable = bool(res.metrics.get("cable_detected"))
    emit({
        "ok": res.ok,
        "verdict": res.verdict,
        "stage": "program",
        "run_dir": root.name,
        "bitstream": found.name,
        "size_mb": round(found.stat().st_size / 1048576, 2),
        "cable_detected": cable,
        "log_tail": (res.log or "")[-4000:],
    })
    return 0 if res.ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("simulate")
    s.add_argument("--dir", required=True)
    s.add_argument("--top", required=True)
    s.add_argument("--rtl", required=True)
    s.add_argument("--tb", required=True)
    s.set_defaults(func=cmd_simulate)

    p = sub.add_parser("program")
    p.add_argument("--dir", required=True)
    p.add_argument("--top")
    p.add_argument("--device")
    p.add_argument("--real", action="store_true")
    p.set_defaults(func=cmd_program)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
