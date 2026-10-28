`timescale 1ns/1ps
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
