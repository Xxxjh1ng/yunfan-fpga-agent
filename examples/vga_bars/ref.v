`timescale 1ns/1ps
// 640x480@60 VGA 彩条：8 条竖条（白黄青绿品红蓝黑），像素时钟 = 50MHz / 2。
// 单时钟域实现：全部逻辑跑在 50MHz，phase 每拍翻转，phase=1 时推进一个像素，
// 同时把 phase 直接输出为 25MHz 像素时钟（避免用派生时钟做逻辑时钟）。
module vga_bars #(
    parameter integer H_ACTIVE = 640,
    parameter integer H_FP     = 16,
    parameter integer H_SYNC   = 96,
    parameter integer H_BP     = 48,
    parameter integer V_ACTIVE = 480,
    parameter integer V_FP     = 10,
    parameter integer V_SYNC   = 2,
    parameter integer V_BP     = 33
) (
    input  wire       clk,
    input  wire       rst_n,
    output wire       vga_clk,
    output reg        hsync,
    output reg        vsync,
    output wire [7:0] vga_r,
    output wire [7:0] vga_g,
    output wire [7:0] vga_b
);

    localparam integer H_TOTAL = H_ACTIVE + H_FP + H_SYNC + H_BP;  // 800
    localparam integer V_TOTAL = V_ACTIVE + V_FP + V_SYNC + V_BP;  // 525
    localparam integer BAR_W   = H_ACTIVE / 8;                     // 80

    // 半速相位：0/1 交替，兼作 25MHz 像素时钟输出
    reg phase;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) phase <= 1'b0;
        else        phase <= ~phase;
    end
    assign vga_clk = phase;

    // 每个 phase=1 的拍推进一个像素
    reg [9:0] hcnt;
    reg [9:0] vcnt;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            hcnt  <= 10'd0;
            vcnt  <= 10'd0;
            hsync <= 1'b1;
            vsync <= 1'b1;
        end else if (phase) begin
            if (hcnt == H_TOTAL - 1) begin
                hcnt <= 10'd0;
                vcnt <= (vcnt == V_TOTAL - 1) ? 10'd0 : (vcnt + 10'd1);
            end else begin
                hcnt <= hcnt + 10'd1;
            end
            hsync <= ~((hcnt >= H_ACTIVE + H_FP) && (hcnt < H_ACTIVE + H_FP + H_SYNC));
            vsync <= ~((vcnt >= V_ACTIVE + V_FP) && (vcnt < V_ACTIVE + V_FP + V_SYNC));
        end
    end

    // 彩条（全组合输出）
    wire de = (hcnt < H_ACTIVE) && (vcnt < V_ACTIVE);

    wire [2:0] bar = (hcnt < BAR_W * 1) ? 3'd0 :
                     (hcnt < BAR_W * 2) ? 3'd1 :
                     (hcnt < BAR_W * 3) ? 3'd2 :
                     (hcnt < BAR_W * 4) ? 3'd3 :
                     (hcnt < BAR_W * 5) ? 3'd4 :
                     (hcnt < BAR_W * 6) ? 3'd5 :
                     (hcnt < BAR_W * 7) ? 3'd6 : 3'd7;

    // 白 黄 青 绿 品红 红 蓝 黑
    assign vga_r = (de && ((bar == 3'd0) || (bar == 3'd1) || (bar == 3'd4) || (bar == 3'd5))) ? 8'hFF : 8'h00;
    assign vga_g = (de && ((bar == 3'd0) || (bar == 3'd1) || (bar == 3'd2) || (bar == 3'd3))) ? 8'hFF : 8'h00;
    assign vga_b = (de && ((bar == 3'd0) || (bar == 3'd2) || (bar == 3'd4) || (bar == 3'd6))) ? 8'hFF : 8'h00;

endmodule
