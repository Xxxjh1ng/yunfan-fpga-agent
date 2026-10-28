"""编排层测试：用 MockFlow + 离线模型验证闭环逻辑，不依赖网络与 EDA。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yunfan.agents.debug import DebugAgent, close_end_before_case_item, close_missing_end, rewrite_ansi_header
from yunfan.config import Settings
from yunfan.diagnostics.parser import Diagnostic
from yunfan.eda.mock import MockFlow
from yunfan.llm.base import LLMResponse, TokenLedger
from yunfan.llm.offline import CallableModel
from yunfan.orchestration.metrics import RunRecord, aggregate, pass_at_k
from yunfan.orchestration.pipeline import Pipeline, PipelineConfig
from yunfan.orchestration.spec import DesignSpec
from yunfan.agents.base import AgentContext

COUNTER_SPEC = json.loads((ROOT / "examples" / "counter" / "spec.json").read_text(encoding="utf-8"))
COUNTER_REF = (ROOT / "examples" / "counter" / "ref.v").read_text(encoding="utf-8")


def counter_model():
    """任何 tag 都返回正确计数器代码的离线模型。

    debug 类调用返回一个合法的「最小编辑补丁」JSON，用于验证补丁应用链路。
    """

    patch = json.dumps(
        {
            "diagnosis": "计数表达式缺少括号（等价改写，用于测试补丁链路）",
            "fixes": [{"find": "q <= q + 4'd1;", "replace": "q <= (q + 4'd1);", "reason": "test"}],
            "confidence": 0.9,
        },
        ensure_ascii=False,
    )

    def fn(system: str, user: str, tag: str) -> str:
        if tag.startswith("debug"):
            return patch
        return COUNTER_REF

    return CallableModel(fn, ledger=TokenLedger())


class TestPassAtK(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(pass_at_k(10, 0, 1), 0.0)
        self.assertEqual(pass_at_k(10, 10, 1), 1.0)
        self.assertAlmostEqual(pass_at_k(10, 5, 1), 0.5, places=4)
        self.assertGreater(pass_at_k(10, 5, 5), pass_at_k(10, 5, 1))


class TestAggregate(unittest.TestCase):
    def test_aggregate_rates(self):
        r1 = RunRecord(design="counter", variant="yunfan", seed=0, success=True, sim_ok=True, repair_rounds=0, tokens={"total_tokens": 100})
        r2 = RunRecord(design="counter", variant="yunfan", seed=1, success=False, sim_ok=False, repair_rounds=3, tokens={"total_tokens": 300})
        agg = aggregate([r1, r2])
        self.assertEqual(agg["runs"], 2)
        self.assertAlmostEqual(agg["success_rate"], 0.5)
        self.assertAlmostEqual(agg["first_attempt_sim_pass_rate"], 0.5)
        self.assertAlmostEqual(agg["avg_tokens"], 200.0)


class TestRepairRules(unittest.TestCase):
    def test_close_end_before_case_item(self):
        broken = """module m(input clk, input a, output reg q);
    always @(posedge clk) begin
        case (a)
            1'b0: begin
                q <= 1'b0;
            1'b1: begin
                q <= 1'b1;
            end
            default: q <= 1'b0;
        endcase
    end
endmodule
"""
        fixed, n = close_end_before_case_item(broken)
        self.assertEqual(n, 1)
        self.assertIn("q <= 1'b0;\n            end", fixed)

    def test_rewrite_header_preserves_reg(self):
        code = COUNTER_REF.replace("input  wire       en,", "")
        fixed, notes = rewrite_ansi_header(code, COUNTER_SPEC["ports"])
        self.assertIn("input wire en", fixed)
        self.assertIn("output reg", fixed)
        self.assertIn("[3:0] q", fixed)

    def test_debug_agent_deterministic(self):
        settings = Settings.build()
        ctx = AgentContext(settings, counter_model(), verbose=False)
        agent = DebugAgent(ctx)
        spec = DesignSpec.from_dict(COUNTER_SPEC)
        broken = COUNTER_REF.replace("endmodule", "")
        diags = [Diagnostic("E02", "error", "lint", "缺少 endmodule")]
        rep = agent.repair(spec, broken, diags)
        self.assertEqual(rep.method, "deterministic")
        self.assertIn("endmodule", rep.code)

    def test_debug_agent_llm_patch(self):
        settings = Settings.build()
        patch = '{"diagnosis": "计数步长错误", "fixes": [{"find": "q + 4\'d1", "replace": "q + 4\'d2"}], "confidence": 0.9}'
        model = CallableModel(lambda system, user, tag: patch)
        ctx = AgentContext(settings, model, verbose=False)
        agent = DebugAgent(ctx)
        spec = DesignSpec.from_dict(COUNTER_SPEC)
        diags = [Diagnostic("E11", "error", "sentinel", "功能不匹配")]
        rep = agent.repair(spec, COUNTER_REF, diags)
        self.assertEqual(rep.method, "llm-patch")
        self.assertIn("q + 4'd2", rep.code)


class TestPipelineMock(unittest.TestCase):
    def _run(self, flow: MockFlow, cfg: PipelineConfig | None = None):
        settings = Settings.build()
        settings.out_dir = Path(tempfile.mkdtemp(prefix="yunfan_test_"))
        cfg = cfg or PipelineConfig(mode="sim-only", max_repair_rounds=3)
        pipeline = Pipeline(settings, counter_model(), config=cfg, flow=flow, verbose=False)
        spec = DesignSpec.from_dict(COUNTER_SPEC)
        return pipeline.run(spec=spec, seed=0)

    def test_pass_first_try(self):
        flow = MockFlow()
        rec = self._run(flow)
        self.assertTrue(rec.success)
        self.assertEqual(rec.repair_rounds, 0)
        self.assertEqual(flow.call_log.count("simulate"), 1)
        self.assertEqual(rec.tb_method, "template")
        self.assertEqual(rec.tb_checkpoints, 44)

    def test_converge_after_one_fix(self):
        flow = MockFlow(sim_script=[{"ok": False, "verdict": "fail", "log": "FINAL RESULT: FAILED"}])
        rec = self._run(flow)
        self.assertTrue(rec.success)
        self.assertEqual(rec.repair_rounds, 1)
        self.assertEqual(flow.call_log.count("simulate"), 2)

    def test_give_up_after_budget(self):
        flow = MockFlow(sim_script=[{"ok": False, "verdict": "fail", "log": "FINAL RESULT: FAILED"}] * 10)
        rec = self._run(flow)
        self.assertFalse(rec.success)
        self.assertLessEqual(rec.repair_rounds, 3)

    def test_lint_gate_blocks_before_eda(self):
        # 预置带 #延时的代码：确定性净化层应在进入 EDA 之前就删除它
        flow = MockFlow()
        settings = Settings.build()
        settings.out_dir = Path(tempfile.mkdtemp(prefix="yunfan_test_"))
        cfg = PipelineConfig(mode="sim-only", max_repair_rounds=2)
        pipeline = Pipeline(settings, counter_model(), config=cfg, flow=flow, verbose=False)
        spec = DesignSpec.from_dict(COUNTER_SPEC)
        broken = COUNTER_REF.replace("q <= q + 4'd1;", "#3 q <= q + 4'd1;")
        rec = pipeline.run(spec=spec, preset_code=broken, seed=0)
        self.assertTrue(rec.success)
        self.assertIn("strip_delay", rec.sanitize_fixes)
        self.assertNotIn("#3", (Path(rec.artifacts["run_dir"]) / "src" / "counter.v").read_text(encoding="utf-8"))

    def test_artifact_written(self):
        flow = MockFlow()
        rec = self._run(flow)
        run_dir = Path(rec.artifacts["run_dir"])
        self.assertTrue((run_dir / "spec.json").exists())
        self.assertTrue((run_dir / "summary.md").exists())
        self.assertTrue((run_dir / "src" / "tb_counter.v").exists())


if __name__ == "__main__":
    unittest.main()
