`timescale 1ns/1ps
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
