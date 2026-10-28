`timescale 1ns/1ps
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
