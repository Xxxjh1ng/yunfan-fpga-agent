`timescale 1ns/1ps
// 拨码计数：SW 拨上时每 DIV 拍 led[3:0] 加 1 并回绕，拨下保持；异步低有效复位。
// 参数 DIV 默认 5000000（50MHz 下 0.1 s）；Testbench 会把它改小以便快速仿真。
module key_count #(
    parameter integer DIV = 5_000_000
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       sw,
    output reg  [3:0] led
);

    reg [22:0] cnt;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt <= 23'd0;
            led <= 4'd0;
        end else if (sw) begin
            if (cnt >= DIV - 1) begin
                cnt <= 23'd0;
                led <= led + 4'd1;
            end else begin
                cnt <= cnt + 23'd1;
            end
        end
        // sw=0：cnt 与 led 都保持不变
    end

endmodule
