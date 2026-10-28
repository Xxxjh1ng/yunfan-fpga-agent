"""一次性生成 examples/ 下的 5 个基准设计（规格 + 参考 RTL）。

运行：python scripts/_make_examples.py
生成后本脚本可删除；examples 目录是正式产物。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "examples"


def spec(name, top, summary, ports, params, behavior, latency, checks):
    return {
        "name": name,
        "top_module": top,
        "summary": summary,
        "clock": {"name": "clk", "freq_mhz": 50.0, "edge": "posedge"},
        "reset": {"name": "rst_n", "polarity": "active_low", "style": "async"},
        "ports": ports,
        "parameters": params,
        "behavior": behavior,
        "timing": {"latency_cycles": latency, "notes": checks},
        "verification": {
            "stimulus": [
                {"name": "复位", "cycles": 4, "drive": {"rst_n": "1'b0"}},
                {"name": "释放复位", "cycles": 2, "drive": {"rst_n": "1'b1"}},
            ],
            "checkpoints": [{"cycle": 8, "signals": ["q"], "expect": "按行为逐拍推导"}],
        },
    }


def port(name, direction, width, desc):
    return {"name": name, "dir": direction, "width": width, "desc": desc}


SPECS = {
    "counter": spec(
        "counter", "counter",
        "4 位可加载使能计数器：load 优先于 en，异步低有效复位后输出 0；计满后自然回绕。",
        [
            port("clk", "input", 1, "系统时钟 50MHz"),
            port("rst_n", "input", 1, "异步复位，低有效"),
            port("en", "input", 1, "计数使能，高有效"),
            port("load", "input", 1, "同步加载使能，优先级高于 en"),
            port("din", "input", 4, "加载值"),
            port("q", "output", 4, "当前计数值"),
        ],
        [],
        [
            "复位期间 q 恒为 0",
            "load=1 时在时钟上升沿把 din 打入 q",
            "load=0 且 en=1 时 q 每拍加 1，4 位回绕",
            "load=0 且 en=0 时 q 保持不变",
        ],
        1,
        "输出在时钟上升沿后一拍更新，无额外延迟",
    ),
    "pwm": spec(
        "pwm", "pwm",
        "8 位 PWM 发生器：自由运行计数器与占空比比较，输出无毛刺的组合比较结果。",
        [
            port("clk", "input", 1, "系统时钟 50MHz"),
            port("rst_n", "input", 1, "异步复位，低有效"),
            port("duty", "input", 8, "占空比阈值 0~255"),
            port("pwm_out", "output", 1, "PWM 输出，cnt < duty 时为 1"),
        ],
        [],
        [
            "8 位计数器 cnt 每拍加 1，从 0 计到 255 后回绕",
            "pwm_out 为 cnt 与 duty 的组合比较结果（组合输出，无寄存）",
            "duty=0 时 pwm_out 恒为 0；duty=255 时除 cnt=255 外恒为 1",
        ],
        0,
        "pwm_out 为纯组合输出，与 cnt 同拍",
    ),
    "crc32": spec(
        "crc32", "crc32",
        "CRC-32 字节并行校验：多项式 0x04C11DB7，MSB 先行，输入输出均不反转，初值 0xFFFFFFFF，无最终异或。",
        [
            port("clk", "input", 1, "系统时钟 50MHz"),
            port("rst_n", "input", 1, "异步复位，低有效"),
            port("valid", "input", 1, "数据有效，高电平表示本拍送入一个字节"),
            port("data", "input", 8, "输入字节"),
            port("crc", "output", 32, "当前 CRC 寄存器值"),
        ],
        [],
        [
            "复位时 crc = 0xFFFFFFFF",
            "valid=1 时，crc_next = LFSR(crc ^ {data, 24'b0})，共 8 次移位",
            "每次移位：若最高位为 1 则 (c<<1)^0x04C11DB7，否则 c<<1",
            "valid=0 时 crc 保持",
        ],
        1,
        "crc 在时钟上升沿后一拍更新",
    ),
    "uart_tx": spec(
        "uart_tx", "uart_tx",
        "UART 8N1 发送器：1 起始位 + 8 数据位（LSB 先行）+ 1 停止位，位宽由 CLKS_PER_BIT 决定（115200bps@50MHz 为 434）。",
        [
            port("clk", "input", 1, "系统时钟 50MHz"),
            port("rst_n", "input", 1, "异步复位，低有效"),
            port("start", "input", 1, "发送启动脉冲，idle 时采样"),
            port("data", "input", 8, "待发送字节"),
            port("tx", "output", 1, "串行输出，空闲为高"),
            port("busy", "output", 1, "发送中标志"),
        ],
        [{"name": "CLKS_PER_BIT", "type": "int", "default": 434, "desc": "每个 bit 的时钟拍数"}],
        [
            "空闲态 tx=1、busy=0，等待 start",
            "start 有效时锁存 data 并拉高 busy，下一拍进入起始位（tx=0）",
            "每个位持续 CLKS_PER_BIT 拍，数据位 LSB 先行",
            "停止位 tx=1 持续 CLKS_PER_BIT 拍后回到空闲，busy 拉低",
        ],
        1,
        "start 被采样的那一拍之后开始计时；按位中点采样判据",
    ),
    "vga_timing": spec(
        "vga_timing", "vga_timing",
        "640x480@60 VGA 行场时序发生器：行 800 像素、场 525 行，同步信号低有效，de 在有效显示区为高。",
        [
            port("clk", "input", 1, "像素时钟 25MHz（本设计按 50MHz 板载时钟直接驱动）"),
            port("rst_n", "input", 1, "异步复位，低有效"),
            port("hsync", "input" if False else "output", 1, "行同步，低有效"),
            port("vsync", "output", 1, "场同步，低有效"),
            port("de", "output", 1, "数据有效（显示区）"),
            port("x", "output", 10, "当前像素列坐标"),
            port("y", "output", 10, "当前像素行坐标"),
        ],
        [
            {"name": "H_ACTIVE", "type": "int", "default": 640, "desc": "行有效像素"},
            {"name": "H_FRONT_PORCH", "type": "int", "default": 16, "desc": "行前肩"},
            {"name": "H_SYNC_PULSE", "type": "int", "default": 96, "desc": "行同步脉宽"},
            {"name": "H_BACK_PORCH", "type": "int", "default": 48, "desc": "行后肩"},
            {"name": "V_ACTIVE", "type": "int", "default": 480, "desc": "场有效行"},
            {"name": "V_FRONT_PORCH", "type": "int", "default": 10, "desc": "场前肩"},
            {"name": "V_SYNC_PULSE", "type": "int", "default": 2, "desc": "场同步脉宽"},
            {"name": "V_BACK_PORCH", "type": "int", "default": 33, "desc": "场后肩"},
        ],
        [
            "行计数 hcnt 从 0 计到 H_TOTAL-1 后回绕，同时场计数 vcnt 加 1",
            "场计数 vcnt 从 0 计到 V_TOTAL-1 后回绕",
            "hsync 在 [H_ACTIVE+H_FRONT_PORCH, H_ACTIVE+H_FRONT_PORCH+H_SYNC_PULSE) 区间内为 0，其余为 1",
            "vsync 在 [V_ACTIVE+V_FRONT_PORCH, V_ACTIVE+V_FRONT_PORCH+V_SYNC_PULSE) 区间内为 0，其余为 1",
            "de = (hcnt < H_ACTIVE) && (vcnt < V_ACTIVE)",
            "x = hcnt，y = vcnt",
        ],
        0,
        "所有输出为组合输出，与计数器同拍",
    ),
}


REFS = {
    "counter": """`timescale 1ns/1ps
// 参考实现：4 位可加载使能计数器
module counter (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       en,
    input  wire       load,
    input  wire [3:0] din,
    output reg  [3:0] q
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            q <= 4'd0;
        end else if (load) begin
            q <= din;
        end else if (en) begin
            q <= q + 4'd1;
        end
    end
endmodule
""",
    "pwm": """`timescale 1ns/1ps
// 参考实现：8 位 PWM
module pwm (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] duty,
    output wire       pwm_out
);
    reg [7:0] cnt;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt <= 8'd0;
        end else begin
            cnt <= cnt + 8'd1;
        end
    end

    assign pwm_out = (cnt < duty) ? 1'b1 : 1'b0;
endmodule
""",
    "crc32": """`timescale 1ns/1ps
// 参考实现：CRC-32 字节并行（poly 0x04C11DB7，MSB 先行）
module crc32 (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        valid,
    input  wire [7:0]  data,
    output reg  [31:0] crc
);
    reg [31:0] c;
    integer i;

    always @(*) begin
        c = crc ^ {data, 24'h000000};
        for (i = 0; i < 8; i = i + 1) begin
            if (c[31]) begin
                c = (c << 1) ^ 32'h04C11DB7;
            end else begin
                c = c << 1;
            end
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            crc <= 32'hFFFFFFFF;
        end else if (valid) begin
            crc <= c;
        end
    end
endmodule
""",
    "uart_tx": """`timescale 1ns/1ps
// 参考实现：UART 8N1 发送器
module uart_tx #(
    parameter CLKS_PER_BIT = 434
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       start,
    input  wire [7:0] data,
    output reg        tx,
    output reg        busy
);
    localparam S_IDLE  = 2'd0;
    localparam S_START = 2'd1;
    localparam S_DATA  = 2'd2;
    localparam S_STOP  = 2'd3;

    reg [1:0]  state;
    reg [15:0] cnt;
    reg [2:0]  bit_idx;
    reg [7:0]  shift;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state   <= S_IDLE;
            tx      <= 1'b1;
            busy    <= 1'b0;
            cnt     <= 16'd0;
            bit_idx <= 3'd0;
            shift   <= 8'd0;
        end else begin
            case (state)
                S_IDLE: begin
                    tx   <= 1'b1;
                    busy <= 1'b0;
                    cnt  <= 16'd0;
                    if (start) begin
                        shift   <= data;
                        busy    <= 1'b1;
                        bit_idx <= 3'd0;
                        state   <= S_START;
                    end
                end
                S_START: begin
                    tx <= 1'b0;
                    if (cnt == CLKS_PER_BIT - 1) begin
                        cnt   <= 16'd0;
                        state <= S_DATA;
                    end else begin
                        cnt <= cnt + 16'd1;
                    end
                end
                S_DATA: begin
                    tx <= shift[0];
                    if (cnt == CLKS_PER_BIT - 1) begin
                        cnt   <= 16'd0;
                        shift <= {1'b0, shift[7:1]};
                        if (bit_idx == 3'd7) begin
                            bit_idx <= 3'd0;
                            state   <= S_STOP;
                        end else begin
                            bit_idx <= bit_idx + 3'd1;
                        end
                    end else begin
                        cnt <= cnt + 16'd1;
                    end
                end
                S_STOP: begin
                    tx <= 1'b1;
                    if (cnt == CLKS_PER_BIT - 1) begin
                        cnt   <= 16'd0;
                        busy  <= 1'b0;
                        state <= S_IDLE;
                    end else begin
                        cnt <= cnt + 16'd1;
                    end
                end
                default: begin
                    state <= S_IDLE;
                    tx    <= 1'b1;
                    busy  <= 1'b0;
                    cnt   <= 16'd0;
                end
            endcase
        end
    end
endmodule
""",
    "vga_timing": """`timescale 1ns/1ps
// 参考实现：640x480@60 VGA 时序发生器
module vga_timing #(
    parameter H_ACTIVE      = 640,
    parameter H_FRONT_PORCH = 16,
    parameter H_SYNC_PULSE  = 96,
    parameter H_BACK_PORCH  = 48,
    parameter V_ACTIVE      = 480,
    parameter V_FRONT_PORCH = 10,
    parameter V_SYNC_PULSE  = 2,
    parameter V_BACK_PORCH  = 33
) (
    input  wire       clk,
    input  wire       rst_n,
    output reg        hsync,
    output reg        vsync,
    output reg        de,
    output reg  [9:0] x,
    output reg  [9:0] y
);
    localparam H_TOTAL = H_ACTIVE + H_FRONT_PORCH + H_SYNC_PULSE + H_BACK_PORCH;
    localparam V_TOTAL = V_ACTIVE + V_FRONT_PORCH + V_SYNC_PULSE + V_BACK_PORCH;

    reg [9:0] hcnt;
    reg [9:0] vcnt;
    wire h_wrap = (hcnt == H_TOTAL - 1);
    wire v_wrap = (vcnt == V_TOTAL - 1);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            hcnt <= 10'd0;
            vcnt <= 10'd0;
        end else if (h_wrap) begin
            hcnt <= 10'd0;
            if (v_wrap) begin
                vcnt <= 10'd0;
            end else begin
                vcnt <= vcnt + 10'd1;
            end
        end else begin
            hcnt <= hcnt + 10'd1;
        end
    end

    always @(*) begin
        hsync = ~((hcnt >= H_ACTIVE + H_FRONT_PORCH) && (hcnt < H_ACTIVE + H_FRONT_PORCH + H_SYNC_PULSE));
        vsync = ~((vcnt >= V_ACTIVE + V_FRONT_PORCH) && (vcnt < V_ACTIVE + V_FRONT_PORCH + V_SYNC_PULSE));
        de    = (hcnt < H_ACTIVE) && (vcnt < V_ACTIVE);
        x     = hcnt;
        y     = vcnt;
    end
endmodule
""",
}


def main() -> None:
    for name, data in SPECS.items():
        d = ROOT / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "spec.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (d / "ref.v").write_text(REFS[name], encoding="utf-8")
        print("written", d)
    # examples 总览
    lines = ["# 基准设计集（Benchmark Suite）", "", "| 设计 | 端口数 | 参数 | 说明 |", "| --- | --- | --- | --- |"]
    for name, data in SPECS.items():
        lines.append(f"| {name} | {len(data['ports'])} | {len(data['parameters'])} | {data['summary'][:40]} |")
    (ROOT / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
