"""上板演示脚本：把「闪灯」实例真正烧进小梅哥开发板。

流程：探测下载器 → 生成 RTL 与引脚约束 → 云源综合/布局布线/比特流 → 下载。

用法
----
    # 1) 只检测下载器与器件（不烧录，安全）
    python scripts/hw_demo.py detect

    # 2) 已知引脚（推荐：从板卡原理图/用户手册查 clk 与 led 的引脚号）
    python scripts/hw_demo.py run --clk-pin F1 --led-pin G1 --program sram

    # 3) 不知道引脚时的"盲探测"：把 1Hz 方波同时送到所有安全 IO 上
    #    只做 SRAM 下载（断电即恢复），看到任意一个灯闪就说明链路通了
    python scripts/hw_demo.py run --probe-all --program sram

说明
----
* SRAM 下载（--program sram）是易失的，断电即消失，最安全；
* 外置 Flash 下载（--program flash）会写配置存储器，断电不丢，需自行确认；
* 引脚号必须与板卡原理图一致，否则灯不会亮（不会损坏板卡）。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yunfan.config import BoardProfile, DeviceProfile, Settings  # noqa: E402
from yunfan.eda import Project, build_flow  # noqa: E402
from yunfan.eda.constraints import load_package_pins  # noqa: E402
from yunfan.eda.gowin import GowinBackend  # noqa: E402
from yunfan.rtl.lint import lint  # noqa: E402

RUN_ROOT = ROOT / "hw_runs"
BLINK_REF = ROOT / "examples" / "blink" / "ref.v"

PROBE_WRAPPER = """`timescale 1ns/1ps
// 盲探测顶层：把同一个 1Hz 方波送到所有安全 IO，用于未知板卡的"链路自检"
module blink_probe #(
    parameter integer HALF = 25_000_000
) (
    input  wire       clk,
    output wire [%d:0] probe
);
    wire w_led;
    blink #(.HALF(HALF)) u_blink (
        .clk (clk),
        .led (w_led)
    );
    assign probe = {%d{w_led}};
endmodule
"""


def run_cmd(cmd: List[str], *, cwd: Optional[Path] = None, timeout: float = 600) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", errors="replace") + (p.stderr or b"").decode("utf-8", errors="replace")
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return -1, "命令超时"


def detect(settings: Settings) -> Dict[str, object]:
    """探测下载器与器件（不改变板卡状态）。"""
    prog = settings.tools.programmer_cli
    info: Dict[str, object] = {"programmer": prog, "cable": False, "device_text": "", "raw": ""}
    if not prog or not Path(prog).exists():
        info["error"] = "未找到 programmer_cli.exe（请检查云源安装）"
        return info

    rc1, out1 = run_cmd([prog, "--scan-cables"], timeout=60)
    info["raw"] += out1
    rc2, out2 = run_cmd([prog, "--scan", "-d", settings.device.programmer_device], timeout=90)
    info["raw"] += out2
    blob = (out1 + "\n" + out2)
    # 下载器与 FPGA 是两件事：下载器被识别 ≠ JTAG 链上有器件
    info["cable"] = ("Gowin USB Cable" in blob) and ("No USB Cable" not in blob)
    info["device"] = info["cable"] and ("No Gowin devices found" not in blob) and ("Scanning!" in blob)
    m = re.search(r"(GW[0-9A-Z]+-[0-9]+[A-Z]?|GW5A|GW2A|GW1N[A-Z]*)", blob)
    info["device_text"] = m.group(1) if m else ""
    info["rc_scan_cables"] = rc1
    info["rc_scan"] = rc2
    return info


def print_detect(info: Dict[str, object]) -> None:
    print("=" * 68)
    print("下载器与器件探测")
    print("=" * 68)
    if info.get("error"):
        print(f"  ✗ {info['error']}")
        return
    if info["cable"] and info.get("device"):
        print("  ✓ 已检测到 Gowin USB 下载器")
        print(f"  ✓ JTAG 扫描到器件：{info['device_text'] or settings.device.programmer_device}")
    elif info["cable"]:
        print("  ✓ 已检测到 Gowin USB 下载器（FT2CH）")
        print("  ✗ 但 JTAG 链上没有扫到 FPGA（No Gowin devices found）")
        print("\n  常见原因（按概率排序）：")
        print("   1. 板卡没上电 —— 下载器由 USB 供电，但 FPGA 需要板卡电源开关打开")
        print("   2. 板载下载器与 FPGA 之间的 JTAG 跳线帽/拨码没接上（部分板有 JTAG 选择跳线）")
        print("   3. USB 插在了只供电的口，而不是 JTAG/UART 调试口")
        print("   4. 板卡处于特殊启动模式（BOOT 拨码），FPGA 未进入 JTAG 可访问状态")
    else:
        print("  ✗ 未检测到下载器（No USB Cable Connection）")
        print("\n  请依次确认：")
        print("   1. USB 线插在板卡的 **JTAG/UART 调试口**（不是仅供电口）")
        print("   2. 板卡已上电，电源指示灯亮")
        print("   3. 设备管理器里出现 “Gowin USB Cable” 或 “USB Composite Device”")
        print("      若出现黄色感叹号：安装驱动")
        print("      C:\\Users\\Administrator\\Downloads\\programmer1.9.9b4_edu(build30911).Win32.x86\\Programmer\\driver\\GowinUSBCableDriverV5_for_win7+.exe")
        print("   4. 换一根能传数据的 USB 线（很多线只能充电）")
    print("\n--- 工具原始输出 ---")
    print(str(info["raw"]).strip()[:1200])


def build_cst_explicit(clk_pin: str, led_pin: str) -> str:
    return (
        "// YunFan 上板自检：显式引脚（请与板卡原理图核对）\n"
        f'IO_LOC "clk" {clk_pin};\n'
        'IO_PORT "clk" IO_TYPE=LVCMOS33 PULL_MODE=NONE BANK_VCCIO=3.3;\n'
        f'IO_LOC "led" {led_pin};\n'
        'IO_PORT "led" IO_TYPE=LVCMOS33 DRIVE=8 PULL_MODE=NONE BANK_VCCIO=3.3;\n'
    )


def probe_pins(settings: Settings) -> Tuple[str, str, List[str]]:
    """挑出安全的普通 IO（排除 JTAG/配置/复用脚），用于盲探测。"""
    pins = load_package_pins(settings.device, settings.tools.gowin_root) or []
    clk_candidates = [p["index"] for p in pins if p["is_gclk"]]
    plain = [p["index"] for p in pins if not p["is_gclk"]]
    if not clk_candidates:
        raise SystemExit("封装库里没有可用的全局时钟引脚")
    clk = settings.board.clock_pin if settings.board.clock_pin in clk_candidates else clk_candidates[0]
    return clk, plain[0] if plain else clk, plain


def build_project(settings: Settings, *, clk_pin: str, led_pin: str, probe_all: bool) -> Tuple[Project, str, str]:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    name = "blink_probe" if probe_all else "blink"
    run_dir = (RUN_ROOT / f"{name}_{stamp}").resolve()
    (run_dir / "src").mkdir(parents=True, exist_ok=True)

    ref = BLINK_REF.read_text(encoding="utf-8")
    probe_pins_list: List[str] = []
    if probe_all:
        _, _, plain = probe_pins(settings)
        probe_pins_list = plain
        n = len(plain)
        top_code = PROBE_WRAPPER % (n - 1, n) + "\n" + ref
        lines = [f"// 盲探测：1Hz 方波驱动 {n} 个安全 IO（不含 JTAG/配置复用脚）"]
        for i, pin in enumerate(plain):
            lines.append(f'IO_LOC "probe[{i}]" {pin};')
            lines.append(f'IO_PORT "probe[{i}]" IO_TYPE=LVCMOS33 DRIVE=8 PULL_MODE=NONE BANK_VCCIO=3.3;')
        cst = "\n".join(lines) + "\n"
        top = "blink_probe"
    else:
        top_code = ref
        cst = build_cst_explicit(clk_pin, led_pin)
        top = "blink"

    project = Project(
        name=name,
        top=top,
        root=run_dir,
        rtl=run_dir / "src" / f"{top}.v",
        cst=run_dir / "src" / f"{top}.cst",
        sdc=run_dir / "src" / f"{top}.sdc",
        device=settings.device,
        board=settings.board,
    )
    # 时钟统一放到 clk 端口（盲探测时也是全局时钟脚）
    if probe_all:
        cst = cst.replace('// 盲探测', f'IO_LOC "clk" {clk_pin};\nIO_PORT "clk" IO_TYPE=LVCMOS33 PULL_MODE=NONE BANK_VCCIO=3.3;\n// 盲探测')
    period = 1000.0 / settings.board.clock_mhz
    sdc = f"create_clock -name clk -period {period:.3f} -waveform {{0 {period/2:.3f}}} [get_ports {{clk}}]\n"

    project.rtl.write_text(top_code, encoding="utf-8")
    project.cst.write_text(cst, encoding="utf-8")
    project.sdc.write_text(sdc, encoding="utf-8")
    return project, cst, top_code


def program(settings: Settings, project: Project, mode: str) -> int:
    prog = settings.tools.programmer_cli
    bit = project.find_bitstream()
    if not bit:
        print("  ✗ 找不到比特流文件")
        return 1
    run_index = {"sram": 2, "flash": 9}[mode]
    cmd = [
        prog,
        "--device", settings.device.programmer_device,
        "--run", str(run_index),
        "--fsFile", str(bit),
    ]
    print(f"\n下载命令：{' '.join(cmd)}")
    rc, out = run_cmd(cmd, timeout=300)
    print(out.strip()[:2000])
    ok = rc == 0 and "Fail" not in out
    print(f"\n{'✓ 下载成功' if ok else '✗ 下载失败（退出码 %d）' % rc}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="hw_demo", description="云帆上板自检：闪灯实例")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_detect = sub.add_parser("detect", help="只探测下载器与器件")
    p_detect.add_argument("--device", default=None)

    p_run = sub.add_parser("run", help="综合+布局布线+（可选）下载")
    p_run.add_argument("--clk-pin", default=None, help="时钟引脚，如 F1")
    p_run.add_argument("--led-pin", default=None, help="LED 引脚，如 G1")
    p_run.add_argument("--probe-all", action="store_true", help="盲探测：1Hz 方波送到所有安全 IO")
    p_run.add_argument("--device", default=None, help="器件型号，如 GW5AT-138 / GW5A-25A / GW2A-18C")
    p_run.add_argument("--board", default=None, help="板卡档案，如 acg720 / xm_gw5a25")
    p_run.add_argument("--program", choices=["none", "sram", "flash"], default="none")
    p_run.add_argument("--force", action="store_true", help="跳过下载器检测直接构建")

    args = ap.parse_args()
    settings = Settings.build()
    if getattr(args, "device", None):
        settings.device = DeviceProfile.load(args.device)
    if getattr(args, "board", None):
        settings.board = BoardProfile.load(args.board)

    if args.cmd == "detect":
        print_detect(detect(settings))
        return 0

    # run
    if not args.force:
        info = detect(settings)
        print_detect(info)
        if not info["cable"] and args.program != "none":
            print("\n[中止] 没有检测到下载器，无法烧录。可先用 --program none 只生成比特流。")
            return 2

    clk_pin = args.clk_pin or settings.board.clock_pin
    led_pin = args.led_pin or settings.board.pins.get("led[0]", "G1")
    if not args.probe_all and (not args.clk_pin or not args.led_pin):
        print(f"\n[提示] 未指定引脚，使用 boards.json 默认值：clk={clk_pin} led={led_pin}")
        print("       这两个值来自器件封装库的合法焊盘，**不一定**等于板卡实际连线；")
        print("       若灯不亮，请按板卡原理图用 --clk-pin/--led-pin 指定，或改用 --probe-all。")

    project, cst, code = build_project(settings, clk_pin=clk_pin, led_pin=led_pin, probe_all=args.probe_all)
    print("\n" + "=" * 68)
    print(f"工程目录：{project.root}")
    print(f"顶层模块：{project.top}　｜　引脚约束 {len([l for l in cst.splitlines() if l.startswith('IO_LOC')])} 条")
    print("=" * 68)

    if args.probe_all:
        print("静态检查：盲探测为多模块顶层，跳过（综合阶段会给出真实判定）")
    else:
        rep = lint(code, spec_ports=None)
        print("静态检查：", "通过" if rep.ok else rep.summary(3))

    flow = build_flow(settings)
    print("\n[1/2] 综合 + 布局布线 + 生成比特流 ...")
    res = flow.implement(project)
    print(f"  结果：{'成功' if res.ok else '失败'}（{res.duration_s:.1f}s）")
    for d in res.diagnostics[:5]:
        print("   ", d.as_text())
    if not res.ok:
        log_path = project.log_dir / "implement.log"
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        if "can't find device" in text or "has not version" in text:
            print("\n  ✗ 当前云源不认这个器件型号。")
            print("    教育版只支持 GW1N-9C / GW1NR-9C / GW1NSR-4C / GW1NZ-1 / GW2A-18C / GW2AR-18C / GW5A-25A；")
            print("    GW5AT-138 需要「完整版云源 + license」，见 docs/上板指南.md 第零节。")
            print("    若已装完整版，请核对 yunfan/data/devices.json 里的 part_number 与实物封装是否一致。")
        print("\n日志：", log_path)
        return 1
    bit = project.find_bitstream()
    print(f"  比特流：{bit}（{bit.stat().st_size/1048576:.2f} MB）" if bit else "  比特流：未找到")
    if res.metrics.get("fmax_mhz"):
        print(f"  Fmax：{res.metrics['fmax_mhz']:.1f} MHz（约束 {settings.board.clock_mhz:g} MHz）")

    if args.program == "none":
        print("\n未指定 --program，已停在比特流阶段。要烧录请加：--program sram（易失，推荐首次）")
        return 0

    print(f"\n[2/2] 下载到板卡（{'SRAM，断电即消失' if args.program == 'sram' else '外置 Flash，掉电不丢'}）...")
    return program(settings, project, args.program)


if __name__ == "__main__":
    raise SystemExit(main())
