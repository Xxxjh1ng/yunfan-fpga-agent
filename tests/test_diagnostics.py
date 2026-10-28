"""诊断层、约束层与规格层的单元测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yunfan.config import BoardProfile, DeviceProfile, Settings
from yunfan.diagnostics.parser import classify, parse_log, primary_taxon, summarize
from yunfan.diagnostics.taxonomy import TAXONOMY, deterministic_taxa, get_taxon
from yunfan.eda.constraints import build_constraints, load_package_pins, remap_invalid_pins
from yunfan.orchestration.spec import DesignSpec
from yunfan.eval.faults import FAULTS, get_fault

SETTINGS = Settings.build()
COUNTER_SPEC = json.loads((ROOT / "examples" / "counter" / "spec.json").read_text(encoding="utf-8"))


class TestTaxonomy(unittest.TestCase):
    def test_all_taxa_well_formed(self):
        self.assertGreaterEqual(len(TAXONOMY), 15)
        for tid, taxon in TAXONOMY.items():
            self.assertEqual(tid, taxon.id)
            self.assertIn(taxon.severity, ("error", "warning"))
            self.assertTrue(taxon.strategies)

    def test_deterministic_subset(self):
        det = set(deterministic_taxa())
        self.assertTrue({"E01", "E02", "E04", "E08", "E10"}.issubset(det))
        self.assertNotIn("E11", det)


class TestLogParsing(unittest.TestCase):
    def test_icarus_syntax_error(self):
        log = "dut.v:12: syntax error\nI give up."
        diags = parse_log(log, tool="icarus")
        self.assertTrue(diags)
        self.assertEqual(diags[0].file, "dut.v")
        self.assertEqual(diags[0].line, 12)
        self.assertEqual(diags[0].taxon, "E01")

    def test_icarus_undeclared(self):
        log = "dut.v:2: error: Unable to bind wire/reg/memory `foo' in `dut'"
        self.assertEqual(parse_log(log, tool="icarus")[0].taxon, "E03")

    def test_gowin_pin_error(self):
        log = 'ERROR  (CT1113) : "top.cst":1 | Can\'t find pad location \'4\' in current package'
        d = parse_log(log, tool="gowin")[0]
        self.assertEqual(d.taxon, "E10")
        self.assertEqual(d.code, "CT1113")
        self.assertEqual(d.file, "top.cst")

    def test_gowin_dedicated_pin(self):
        log = "ERROR  (PR2017) : 'en' cannot be placed according to constraint, for the location is a dedicated pin (JTAG)"
        self.assertEqual(parse_log(log, tool="gowin")[0].taxon, "E10")

    def test_vivado_error(self):
        log = "ERROR: [Synth 8-327] inferring latch for variable 'q_reg'"
        d = parse_log(log, tool="vivado")[0]
        self.assertEqual(d.tool, "vivado")
        self.assertIn(d.taxon, ("E06", "E01"))

    def test_sentinel_fail(self):
        log = 'CHECK FAIL cycle=9 q=3 expect=4\nCHECKS TOTAL: 44, FAILED: 1\nFINAL RESULT: FAILED'
        diags = parse_log(log, tool="icarus")
        taxa = {d.taxon for d in diags}
        self.assertIn("E11", taxa)
        fail = [d for d in diags if d.cycle == 9][0]
        self.assertEqual(fail.signal, "q")
        self.assertEqual(fail.got, "3")

    def test_timeout_sentinel(self):
        self.assertEqual(parse_log("FINAL RESULT: TIMEOUT", tool="icarus")[0].taxon, "E12")

    def test_summary_and_primary(self):
        log = "dut.v:1: warning: something\ndut.v:2: syntax error"
        diags = parse_log(log, tool="icarus")
        self.assertEqual(primary_taxon(diags), "E01")
        self.assertEqual(summarize(diags)["total"], len(diags))

    def test_classify_cst_file(self):
        self.assertEqual(classify("some odd message", tool="gowin", file="a.cst"), "E10")


class TestConstraints(unittest.TestCase):
    def test_every_port_mapped(self):
        res = build_constraints(COUNTER_SPEC, SETTINGS.board, SETTINGS.device, SETTINGS.tools.gowin_root)
        self.assertEqual(res.unmapped, [])
        self.assertEqual(res.coverage, 1.0)
        for p in COUNTER_SPEC["ports"]:
            width = int(p.get("width") or 1)
            expected = [p["name"]] if width == 1 else [f"{p['name']}[{i}]" for i in range(width)]
            for bit in expected:
                self.assertIn(bit, [a.port for a in res.assignments])

    def test_sdc_contains_clock(self):
        res = build_constraints(COUNTER_SPEC, SETTINGS.board, SETTINGS.device, SETTINGS.tools.gowin_root)
        self.assertIn("create_clock", res.sdc)
        self.assertIn("20.000", res.sdc)  # 50MHz -> 20ns

    def test_semantic_pins_match_board(self):
        res = build_constraints(COUNTER_SPEC, SETTINGS.board, SETTINGS.device, SETTINGS.tools.gowin_root)
        m = {a.port: a.pin for a in res.assignments}
        self.assertEqual(m["clk"], SETTINGS.board.clock_pin)
        self.assertEqual(m["rst_n"], SETTINGS.board.reset_pin)

    @unittest.skipUnless(SETTINGS.tools.gowin_root, "需要云源 EDA 安装")
    def test_remap_invalid_pin(self):
        cst = 'IO_LOC "clk" 4;\nIO_PORT "clk" IO_TYPE=LVCMOS33;'
        fixed, changes = remap_invalid_pins(cst, SETTINGS.device, SETTINGS.tools.gowin_root)
        self.assertTrue(changes)
        self.assertNotIn('"clk" 4', fixed)

    @unittest.skipUnless(SETTINGS.tools.gowin_root, "需要云源 EDA 安装")
    def test_pin_library_excludes_dedicated(self):
        pins = load_package_pins(SETTINGS.device, SETTINGS.tools.gowin_root)
        self.assertTrue(pins)
        self.assertFalse([p for p in pins if p["cfg"] and "SSPI" in p["cfg"]])


class TestSpec(unittest.TestCase):
    def test_valid_spec(self):
        spec = DesignSpec.from_dict(COUNTER_SPEC)
        self.assertEqual(spec.validate(), [])
        self.assertEqual(spec.clock_name, "clk")
        self.assertTrue(spec.reset_active_low)

    def test_missing_clock_rejected(self):
        data = json.loads(json.dumps(COUNTER_SPEC))
        data["ports"] = [p for p in data["ports"] if p["name"] != "clk"]
        issues = DesignSpec.from_dict(data).validate()
        self.assertTrue(any("时钟" in i for i in issues))

    def test_unspecified_rejected(self):
        data = json.loads(json.dumps(COUNTER_SPEC))
        data["top_module"] = "UNSPECIFIED"
        self.assertFalse(DesignSpec.from_dict(data).ok())

    def test_json_roundtrip(self):
        spec = DesignSpec.from_dict(COUNTER_SPEC)
        again = DesignSpec.from_json(spec.to_json())
        self.assertEqual(again.to_dict()["ports"], spec.to_dict()["ports"])


class TestFaults(unittest.TestCase):
    def test_all_faults_have_unique_ids(self):
        ids = [f.fid for f in FAULTS]
        self.assertEqual(len(ids), len(set(ids)))
        for f in FAULTS:
            self.assertIn(f.taxon, TAXONOMY)

    def test_fault_mutations_change_text(self):
        ref = (ROOT / "examples" / "counter" / "ref.v").read_text(encoding="utf-8")
        for f in FAULTS:
            if f.target != "code":
                continue
            out = f.run(ref, COUNTER_SPEC)
            if out is None:
                continue
            self.assertNotEqual(out, ref, msg=f.fid)
            self.assertTrue(isinstance(out, str))


if __name__ == "__main__":
    unittest.main()
