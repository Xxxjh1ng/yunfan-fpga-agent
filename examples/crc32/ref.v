`timescale 1ns/1ps
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
