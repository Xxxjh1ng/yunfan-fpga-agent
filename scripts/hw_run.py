"""在真机上跑更多演示：流水灯、8 开关 PWM 调光、按键计数器。

用法：
    python scripts/hw_run.py list                # 看可用演示
    python scripts/hw_run.py build  led_water    # 只综合布线，生成比特流
    python scripts/hw_run.py run    led_water    # 综合 + 下载到 SRAM
    python scripts/hw_run.py run    pwm_sw --flash   # 写入外部 Flash（掉电不丢）

引脚来自 yunfan/data/boards.json（acg720 档案），已按厂商例程核对：
    clk=Y18   led[0..7]=M22,N22,L21,K21,K22,J22,H22,M21
    sw[0..7]=G22,D22,E22,G21,E21,D21,C22,B22
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yunfan.config import BoardProfile, DeviceProfile, Settings  # noqa: E402
from yunfan.eda import Project, build_flow  # noqa: E402

RUN_ROOT = ROOT / "hw_runs"

# ---------------------------------------------------------------- 演示定义
LED_WATER = """`timescale 1ns/1ps
// 流水灯：8 颗 LED 依次点亮，每 DIV 拍右移一位
module led_water #(
    parameter integer DIV = 12_500_000
) (
    input  wire       clk,
    output reg  [7:0] led
);
    reg [24:0] cnt;
    reg [2:0]  idx;

    always @(posedge clk) begin
        if (cnt >= DIV - 1) begin
            cnt <= 25'd0;
            idx <= idx + 3'd1;
        end else begin
            cnt <= cnt + 25'd1;
        end
    end

    always @(*) begin
        case (idx)
            3'd0: led = 8'b0000_0001;
            3'd1: led = 8'b0000_0010;
            3'd2: led = 8'b0000_0100;
            3'd3: led = 8'b0000_1000;
            3'd4: led = 8'b0001_0000;
            3'd5: led = 8'b0010_0000;
            3'd6: led = 8'b0100_0000;
            default: led = 8'b1000_0000;
        endcase
    end
endmodule
"""

PWM_SW = """`timescale 1ns/1ps
// 8 位拨码开关调光：sw[7:0] 作为占空比，LED0 亮度随开关变化
module pwm_sw (
    input  wire       clk,
    input  wire [7:0] duty,
    output wire       pwm_out
);
    reg [7:0] cnt;

    always @(posedge clk) begin
        cnt <= cnt + 8'd1;
    end

    assign pwm_out = (cnt < duty) ? 1'b1 : 1'b0;
endmodule
"""

KEY_COUNT = """`timescale 1ns/1ps
// 按键计数器：SW0 作使能，计数值显示在 LED0..3 上（二进制）
module key_count (
    input  wire       clk,
    input  wire       en,
    output reg  [3:0] led
);
    reg [23:0] div;
    reg        tick;

    always @(posedge clk) begin
        if (div >= 24'd4_999_999) begin
            div  <= 24'd0;
            tick <= 1'b1;
        end else begin
            div  <= div + 24'd1;
            tick <= 1'b0;
        end
    end

    always @(posedge clk) begin
        if (tick && en) begin
            led <= led + 4'd1;
        end
    end
endmodule
"""

DEMOS: Dict[str, Dict[str, object]] = {
    "led_water": {
        "rtl": LED_WATER,
        "top": "led_water",
        "ports": [("clk", 1), ("led", 8)],
        "hint": "8 颗 LED 依次点亮（每 0.25 秒移动一位）",
    },
    "pwm_sw": {
        "rtl": PWM_SW,
        "top": "pwm_sw",
        "ports": [("clk", 1), ("duty", 8), ("pwm_out", 1)],
        "hint": "拨动 SW0..SW7 改占空比，LED0 亮度随之变化（0=全灭，255=最亮）",
    },
    "key_count": {
        "rtl": KEY_COUNT,
        "top": "key_count",
        "ports": [("clk", 1), ("en", 1), ("led", 4)],
        "hint": "SW0 拨上后 LED0..3 以二进制每 0.1 秒加一（0→15 循环）",
    },
}


def pin_map(demo: str, board: BoardProfile) -> Dict[str, str]:
    """把端口映射到板卡引脚（按端口名遍历，向量展开成 led[i]）。"""
    out: Dict[str, str] = {}
    pins = board.pins
    for name, width in DEMOS[demo]["ports"]:  # type: ignore[index]
        if width == 1:
            if name == "clk":
                out["clk"] = board.clock_pin
            elif name == "en":
                out["en"] = pins.get("sw[0]", "")
            elif name == "pwm_out":
                out["pwm_out"] = pins.get("led[0]", "")
            else:
                out[name] = pins.get(name, "")
        else:
            prefix = "duty" if name == "duty" else name
            src = "sw" if name == "duty" else "led"
            for i in range(width):
                out[f"{prefix}[{i}]"] = pins.get(f"{src}[{i}]", "")
    return {k: v for k, v in out.items() if v}


def build_cst(mapping: Dict[str, str]) -> str:
    lines = ["// 由 hw_run.py 按 boards.json 生成"]
    for port, pin in mapping.items():
        drive = ' DRIVE=8' if port.startswith(("led", "pwm_out")) else ""
        pull = " PULL_MODE=UP" if port.startswith(("duty", "en", "sw")) else " PULL_MODE=NONE"
        lines.append(f'IO_LOC "{port}" {pin};')
        lines.append(f'IO_PORT "{port}" IO_TYPE=LVCMOS33{drive}{pull} BANK_VCCIO=3.3;')
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="真机演示运行器")
    ap.add_argument("action", choices=["list", "build", "run"])
    ap.add_argument("demo", nargs="?", default=None)
    ap.add_argument("--device", default="GW5AT-138")
    ap.add_argument("--board", default="acg720")
    ap.add_argument("--flash", action="store_true", help="写入外部 Flash（掉电不丢）")
    args = ap.parse_args()

    if args.action == "list":
        for k, v in DEMOS.items():
            print(f"  {k:<12} {v['hint']}")
        return 0
    if not args.demo or args.demo not in DEMOS:
        print("请指定演示名：", ", ".join(DEMOS))
        return 2

    settings = Settings.build()
    settings.device = DeviceProfile.load(args.device)
    settings.board = BoardProfile.load(args.board)
    demo = DEMOS[args.demo]

    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = (RUN_ROOT / f"{demo['top']}_{stamp}").resolve()
    (run_dir / "src").mkdir(parents=True, exist_ok=True)
    project = Project(
        name=str(demo["top"]),
        top=str(demo["top"]),
        root=run_dir,
        rtl=run_dir / "src" / f"{demo['top']}.v",
        cst=run_dir / "src" / f"{demo['top']}.cst",
        sdc=run_dir / "src" / f"{demo['top']}.sdc",
        device=settings.device,
        board=settings.board,
    )
    mapping = pin_map(args.demo, settings.board)
    project.rtl.write_text(str(demo["rtl"]), encoding="utf-8")
    project.cst.write_text(build_cst(mapping), encoding="utf-8")
    period = 1000.0 / settings.board.clock_mhz
    project.sdc.write_text(f"create_clock -name clk -period {period:.3f} -waveform {{0 {period/2:.3f}}} [get_ports {{clk}}]\n", encoding="utf-8")

    print("=" * 62)
    print(f"演示：{args.demo} — {demo['hint']}")
    print(f"工程：{run_dir}")
    print(f"引脚：{len(mapping)} 个 -> " + ", ".join(f"{k}={v}" for k, v in list(mapping.items())[:6]) + (" ..." if len(mapping) > 6 else ""))
    print("=" * 62)

    flow = build_flow(settings)
    res = flow.implement(project)
    print(f"[综合+布线] {'成功' if res.ok else '失败'}（{res.duration_s:.1f}s）")
    if not res.ok:
        for d in res.diagnostics[:5]:
            print("   ", d.as_text())
        print("   日志：", project.log_dir / "implement.log")
        return 1
    bit = project.find_bitstream()
    print(f"比特流：{bit.name}（{bit.stat().st_size/1048576:.2f} MB）" if bit else "未生成比特流")
    if res.metrics.get("fmax_mhz"):
        print(f"Fmax：{res.metrics['fmax_mhz']:.1f} MHz")

    if args.action == "build":
        print("\n只生成比特流，未下载。")
        return 0

    prog = settings.tools.programmer_cli
    run_mode = "9" if args.flash else "2"
    log = project.log_dir / ("program_flash.log" if args.flash else "program_sram.log")
    cmd = [prog, "--device", settings.device.programmer_device, "--run", run_mode, "--fsFile", str(bit)]
    print(f"\n[下载] {'外部 Flash' if args.flash else 'SRAM'} ...")
    with open(log, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
        proc.wait()
    text = log.read_text(encoding="utf-8", errors="replace")
    done = "Finished" in text and "100%" in text
    print(f"结果：{'成功' if done else '失败'}（日志 {log.name}）")
    if done:
        print(f"\n看板子：{demo['hint']}")
    else:
        print(text[-600:])
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
