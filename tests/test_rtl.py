"""RTL 处理层单元测试：抽取 / 接口 / 净化 / 静态检查 / 激励生成。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yunfan.rtl.extract import extract_verilog, inspect_structure, is_truncated, strip_fences, strip_thinking
from yunfan.rtl.interface import compare_ports, parse_interface
from yunfan.rtl.lint import lint
from yunfan.rtl.oracles import build_plan
from yunfan.rtl.sanitize import sanitize
from yunfan.rtl.testbench import render_tb

COUNTER_SPEC = json.loads((ROOT / "examples" / "counter" / "spec.json").read_text(encoding="utf-8"))
COUNTER_REF = (ROOT / "examples" / "counter" / "ref.v").read_text(encoding="utf-8")


class TestExtract(unittest.TestCase):
    def test_strip_fences_and_thinking(self):
        raw = "<think>我来写一个模块</think>\n```verilog\nmodule a; endmodule\n```\n解释"
        code = extract_verilog(raw)
        self.assertIn("module a", code)
        self.assertNotIn("```", code)
        self.assertNotIn("think", code)

    def test_truncation_detected(self):
        self.assertTrue(is_truncated("module a(input clk);\n always @(posedge clk) begin\n x <= 1;"))
        self.assertFalse(is_truncated("module a; endmodule"))

    def test_structure_counts(self):
        rep = inspect_structure("module a;\n always @(posedge c) begin\n end\nendmodule")
        self.assertTrue(rep.ok)
        self.assertEqual(rep.module_name, "a")

    def test_extract_prefers_named_module(self):
        text = "module helper; endmodule\nmodule dut(input clk, output q); endmodule"
        self.assertEqual(inspect_structure(extract_verilog(text, prefer_name="dut")).module_name, "dut")


class TestInterface(unittest.TestCase):
    def test_parse_ansi(self):
        iface = parse_interface(COUNTER_REF)
        self.assertEqual(iface.name, "counter")
        self.assertEqual([p.name for p in iface.ports], ["clk", "rst_n", "en", "load", "din", "q"])
        din = iface.port_map()["din"]
        self.assertEqual(din.width, 4)
        self.assertEqual(din.normalized_dir, "input")

    def test_compare_detects_missing_and_extra(self):
        code = COUNTER_REF.replace("input  wire       en,", "input  wire       en_x,")
        mism = compare_ports(COUNTER_SPEC["ports"], parse_interface(code))
        kinds = {(m.kind, m.port) for m in mism}
        self.assertIn(("missing", "en"), kinds)
        self.assertIn(("extra", "en_x"), kinds)


class TestSanitize(unittest.TestCase):
    def test_wire_assigned_becomes_reg(self):
        code = "module m(input clk, input d, output q);\n  wire q;\n  always @(posedge clk) q <= d;\nendmodule\n"
        rep = sanitize(code)
        self.assertIn("reg ", rep.code)
        self.assertIn("wire_to_reg", rep.summary())

    def test_strip_delay_and_system_task(self):
        code = "module m(input clk);\n always @(posedge clk) begin #5 $display(\"x\"); end\nendmodule\n"
        rep = sanitize(code)
        self.assertNotIn("#5", rep.code)
        self.assertNotIn("$display", rep.code)

    def test_wire_with_initializer_preserved(self):
        """回归：wire x = expr 不能被误改成 reg（RHS 信号名曾导致误判）。"""
        code = (
            "module m(input wire clk, output reg hsync);"
            "reg [9:0] hcnt;"
            "wire h_wrap = (hcnt == 10'd799);"
            "always @(posedge clk) begin"
            "  if (h_wrap) hcnt <= 10'd0; else hcnt <= hcnt + 10'd1;"
            "end"
            "always @(*) begin hsync = ~h_wrap; end"
            "endmodule"
        )
        rep = sanitize(code)
        self.assertIn("wire h_wrap", rep.code)

    def test_rename_and_close_module(self):
        rep = sanitize("module wrong_name(input clk);\nendmodule", target_name="counter")
        self.assertIn("module counter", rep.code)

    def test_truncated_module_closed(self):
        rep = sanitize("module m(input clk);\n always @(posedge clk) begin", target_name="m")
        self.assertIn("endmodule", rep.code)


class TestLint(unittest.TestCase):
    def test_reference_rtl_is_clean(self):
        rep = lint(COUNTER_REF, spec_ports=COUNTER_SPEC["ports"])
        self.assertTrue(rep.ok, msg=rep.summary())

    def test_output_wire_assigned(self):
        code = COUNTER_REF.replace("output reg  [3:0] q", "output wire [3:0] q")
        rep = lint(code, spec_ports=COUNTER_SPEC["ports"])
        self.assertFalse(rep.ok)
        self.assertIn("E04", {v.taxon for v in rep.errors})

    def test_hash_delay_flagged(self):
        code = COUNTER_REF.replace("q <= q + 4'd1;", "#3 q <= q + 4'd1;")
        rep = lint(code, spec_ports=COUNTER_SPEC["ports"])
        self.assertIn("E08", {v.taxon for v in rep.errors})

    def test_port_mismatch_flagged(self):
        code = COUNTER_REF.replace("input  wire       en,", "")
        rep = lint(code, spec_ports=COUNTER_SPEC["ports"])
        self.assertIn("E09", {v.taxon for v in rep.violations})

    def test_missing_endmodule_flagged(self):
        rep = lint(COUNTER_REF.replace("endmodule", ""), spec_ports=COUNTER_SPEC["ports"])
        self.assertFalse(rep.ok)


class TestTestbench(unittest.TestCase):
    def test_plan_and_tb(self):
        plan = build_plan(COUNTER_SPEC)
        self.assertIsNotNone(plan)
        self.assertGreater(len(plan.checks), 10)
        tb = render_tb(COUNTER_SPEC, plan)
        self.assertIn("module tb_counter", tb)
        self.assertIn("counter u_dut", tb)
        self.assertIn("always #10 clk", tb)
        self.assertIn("FINAL RESULT: PASSED", tb)
        self.assertIn("FINAL RESULT: FAILED", tb)
        # 时钟不得出现在激励里
        for d in plan.drives:
            self.assertNotIn("clk", d.values)

    def test_oracle_counter_sequence(self):
        plan = build_plan(COUNTER_SPEC)
        seq = {c.cycle: c.values["q"] for c in plan.checks}
        self.assertEqual(seq[0], 0)
        self.assertEqual(seq[4], 1)
        self.assertEqual(seq[12], 9)  # 12 拍计数（回绕后）
        self.assertTrue(all(0 <= v <= 15 for v in seq.values()))


if __name__ == "__main__":
    unittest.main()
