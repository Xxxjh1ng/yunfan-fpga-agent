`timescale 1ns/1ps
// 8 位流水灯：复位后仅 led[0] 亮，每 DIV 个时钟左移一位，最高位回绕到最低位。
// 参数 DIV 默认 12500000（50MHz 下约 0.25 s）；Testbench 会把它改小以便快速仿真。
module led_water #(
    parameter integer DIV = 12_500_000
) (
    input  wire       clk,
    input  wire       rst_n,
    output reg  [7:0] led
);

    reg [24:0] cnt;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt <= 25'd0;
            led <= 8'b0000_0001;
        end else if (cnt >= DIV - 1) begin
            cnt <= 25'd0;
            led <= {led[6:0], led[7]};
        end else begin
            cnt <= cnt + 25'd1;
        end
    end

endmodule
