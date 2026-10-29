`timescale 1ns/1ps
// 上板自检实例：50 MHz 时钟分频，产生 1 Hz 方波驱动 LED
// 设计为「无复位」：计数器自由运行，LED 每个 HALF 拍翻转一次，
// 因此不依赖板卡的复位按键，接上时钟与 LED 两根线即可看到效果。
module blink #(
    parameter integer HALF = 25_000_000   // 50MHz / (2*1Hz)
) (
    input  wire clk,
    output reg  led
);
    reg [31:0] cnt;

    always @(posedge clk) begin
        if (cnt >= HALF - 1) begin
            cnt <= 32'd0;
            led <= ~led;
        end else begin
            cnt <= cnt + 32'd1;
        end
    end
endmodule
