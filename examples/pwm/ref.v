`timescale 1ns/1ps
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
