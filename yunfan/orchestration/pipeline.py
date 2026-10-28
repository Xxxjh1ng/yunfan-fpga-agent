"""编排管道：把五个智能体串成「生成—验证—反馈—修复」的自主闭环。

闭环的关键不在"有循环"，而在**反馈的质量与代价**：
    Lint（毫秒） → 综合（秒） → 仿真（秒） → 布局布线（十秒）
失败即早停，并把失败转换为结构化诊断回灌；修复优先走确定性规则，
其次走最小编辑补丁，最后才整模块重写。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..agents.base import AgentContext, AgentEvent
from ..agents.debug import DebugAgent
from ..agents.deploy import DeployAgent
from ..agents.requirement import RequirementAgent
from ..agents.rtl_gen import RtlAgent
from ..agents.verify import VerifyAgent
from ..config import Settings
from ..diagnostics.parser import Diagnostic, summarize
from ..eda import Project, build_flow
from ..llm.base import ChatModel
from ..llm.prompts import PROMPT_VERSION
from ..rtl.lint import lint
from ..rtl.sanitize import sanitize
from .metrics import RunRecord
from .spec import DesignSpec


@dataclass
class PipelineConfig:
    """闭环行为开关；同时充当消融实验的控制器。"""

    mode: str = "full"  # full / sim-only / lint-only
    variant: str = "yunfan"
    tb_mode: str = "template"  # template / llm
    use_lint_gate: bool = True
    use_structured_diag: bool = True
    use_deterministic_repair: bool = True
    pre_synth: bool = True
    max_repair_rounds: int = 5
    max_rtl_attempts: int = 3
    period_ns: int = 20
    program: bool = False
    dry_run_program: bool = True
    token_budget: int = 400_000
    max_wall_s: float = 1800.0
    temperature: float = 0.2

    @staticmethod
    def baseline() -> "PipelineConfig":
        """复刻原始单点方案：无静态门控、无结构化诊断、TB 全由 LLM 写。"""
        return PipelineConfig(
            variant="baseline",
            tb_mode="llm",
            use_lint_gate=False,
            use_structured_diag=False,
            use_deterministic_repair=False,
            pre_synth=False,
            max_rtl_attempts=1,
        )


class Pipeline:
    name = "编排器"

    def __init__(
        self,
        settings: Settings,
        model: ChatModel,
        *,
        config: Optional[PipelineConfig] = None,
        flow=None,
        verbose: bool = True,
    ) -> None:
        self.settings = settings
        self.model = model
        self.cfg = config or PipelineConfig()
        self.flow = flow or build_flow(settings)
        self.verbose = verbose
        self.ctx = AgentContext(settings, model, verbose=verbose)
        self.req_agent = RequirementAgent(self.ctx, settings.device, settings.board)
        self.rtl_agent = RtlAgent(self.ctx)
        self.verify_agent = VerifyAgent(self.ctx)
        self.debug_agent = DebugAgent(self.ctx)
        self.deploy_agent = DeployAgent(self.ctx, settings.device, settings.board, settings.tools.gowin_root)

    # ------------------------------------------------------------------
    def run(
        self,
        *,
        user_text: Optional[str] = None,
        spec: Optional[DesignSpec] = None,
        design_name: Optional[str] = None,
        seed: int = 0,
        preset_code: Optional[str] = None,
        preset_cst: Optional[str] = None,
        preset_tb: Optional[str] = None,
        variant_label: Optional[str] = None,
    ) -> RunRecord:
        started = time.time()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        name = (spec.name if spec else None) or design_name or "design"
        run_id = f"{name}_{self.cfg.variant}_s{seed}_{stamp}"
        run_dir = (Path(self.settings.out_dir).expanduser() / run_id).resolve()
        (run_dir / "src").mkdir(parents=True, exist_ok=True)

        rec = RunRecord(
            run_id=run_id,
            design=name,
            seed=seed,
            mode=self.cfg.mode,
            variant=variant_label or self.cfg.variant,
            provider=getattr(self.model, "name", ""),
            model=getattr(self.model, "model", ""),
            prompt_version=PROMPT_VERSION,
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._attach_trace(rec, run_dir)

        try:
            self._run_inner(
                rec, run_dir, user_text=user_text, spec=spec,
                preset_code=preset_code, preset_cst=preset_cst, preset_tb=preset_tb,
            )
        except Exception as exc:  # noqa: BLE001
            rec.error = f"{type(exc).__name__}: {exc}"
            rec.success = False
            self.ctx.emit(AgentEvent(agent=self.name, action="运行异常", detail=rec.error, level="error"))
        finally:
            rec.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            rec.durations["total_s"] = round(time.time() - started, 3)
            rec.tokens = self.ctx.ledger.as_dict()
            rec.events = [e.as_dict() for e in self.ctx.events]
            # 落盘
            rec.save(run_dir)
            (run_dir / "trace.json").write_text(
                json.dumps([e.as_dict() for e in self.ctx.events], ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (run_dir / "summary.md").write_text(self._render_summary(rec), encoding="utf-8")
        return rec

    # ------------------------------------------------------------------
    def _run_inner(
        self,
        rec: RunRecord,
        run_dir: Path,
        *,
        user_text: Optional[str],
        spec: Optional[DesignSpec],
        preset_code: Optional[str] = None,
        preset_cst: Optional[str] = None,
        preset_tb: Optional[str] = None,
    ) -> None:
        # ---------- 1. 需求解析 ----------
        if spec is None:
            if not user_text:
                raise ValueError("必须提供 user_text 或 spec")
            res = self.req_agent.analyze(user_text)
            rec.spec_source = "llm"
            rec.spec_issues = res.issues
            if not res.ok:
                rec.spec_ok = False
                raise RuntimeError("需求解析未产出合法规格：" + "; ".join(res.issues[:3]))
            spec = res.spec
        else:
            rec.spec_source = "file"
            rec.spec_issues = spec.validate()
            if rec.spec_issues:
                rec.spec_ok = False
                raise RuntimeError("传入规格不合法：" + "; ".join(rec.spec_issues[:3]))
        rec.spec_ok = True
        rec.design = spec.name
        (run_dir / "spec.json").write_text(spec.to_json(), encoding="utf-8")
        self.ctx.emit(AgentEvent(agent=self.name, action="规格锁定", detail=spec.brief()))

        project = Project(
            name=spec.name,
            top=spec.top_module,
            root=run_dir,
            rtl=run_dir / "src" / f"{spec.top_module}.v",
            tb=run_dir / "src" / f"tb_{spec.top_module}.v",
            cst=run_dir / "src" / f"{spec.top_module}.cst",
            sdc=run_dir / "src" / f"{spec.top_module}.sdc",
            device=self.settings.device,
            board=self.settings.board,
        )
        rec.artifacts["run_dir"] = str(run_dir)

        # ---------- 2. 约束生成（部署智能体前置，供综合/布线使用） ----------
        constraint = self.deploy_agent.prepare_constraints(spec)
        cst_text, sdc_text = constraint.cst, constraint.sdc
        if preset_cst:
            cst_text = preset_cst

        # ---------- 3. RTL 生成（含静态门控） ----------
        t0 = time.time()
        if preset_code:
            from ..agents.rtl_gen import GenerationResult

            san = sanitize(preset_code, target_name=spec.top_module)
            gen = GenerationResult(
                code=san.code,
                attempts=0,
                ok=True,
                lint=lint(san.code, spec_ports=spec.ports),
                sanitize=san,
            )
            self.ctx.emit(AgentEvent(agent=self.name, action="使用预置 RTL（故障注入/回放）", detail=f"{len(san.code.splitlines())} 行"))
        else:
            gen = self.rtl_agent.generate(
                spec,
                max_attempts=self.cfg.max_rtl_attempts if self.cfg.use_lint_gate else 1,
                temperature=self.cfg.temperature,
            )
        rec.durations["rtl_gen_s"] = round(time.time() - t0, 3)
        rec.rtl_attempts = gen.attempts
        rec.rtl_lines = len(gen.code.splitlines())
        rec.sanitize_fixes = gen.sanitize.summary() if gen.sanitize else {}
        if gen.lint:
            rec.lint_errors = len(gen.lint.errors)
            rec.lint_warnings = len(gen.lint.warnings)
        if not gen.code.strip():
            raise RuntimeError("RTL 生成为空")
        if self.cfg.use_lint_gate and gen.lint and not gen.lint.ok:
            # 硬门控：静态检查不通过不允许进入 EDA
            self.ctx.emit(
                AgentEvent(agent=self.name, action="静态门控拦截", detail=gen.lint.summary(4), level="warning")
            )

        code = gen.code
        tb_result = None

        # ---------- 4. 闭环 ----------
        repair_rounds = 0
        no_change = 0
        sim_ok = False
        last_fail = None

        for round_idx in range(self.cfg.max_repair_rounds + 1):
            if self.ctx.ledger.total_tokens > self.cfg.token_budget:
                raise RuntimeError(f"token 预算超限（{self.ctx.ledger.total_tokens}）")
            if time.time() - time.mktime(time.strptime(rec.started_at, "%Y-%m-%d %H:%M:%S")) > self.cfg.max_wall_s:
                raise RuntimeError("墙钟预算超限")

            # 4.1 生成/刷新 Testbench
            if tb_result is None and preset_tb:
                from ..agents.verify import TbResult

                tb_result = TbResult(code=preset_tb, method="preset")
            if tb_result is None or (self.cfg.tb_mode == "llm" and round_idx > 0):
                if self.cfg.tb_mode == "template":
                    tb_result = self.verify_agent.build_tb(spec, period_ns=self.cfg.period_ns)
                    if not tb_result.code:
                        tb_result = self.verify_agent.build_llm_tb(spec, code)
                else:
                    tb_result = self.verify_agent.build_llm_tb(spec, code)
                rec.tb_method = tb_result.method
                rec.tb_checkpoints = tb_result.checkpoints

            self._write_sources(project, code, tb_result.code, cst_text, sdc_text)

            # 4.2 静态门控（毫秒级，拦住可机械判定的错误，避免消耗 EDA 时间）
            if self.cfg.use_lint_gate:
                rep_lint = lint(code, spec_ports=spec.ports)
                rec.lint_errors = len(rep_lint.errors)
                rec.lint_warnings = len(rep_lint.warnings)
                if not rep_lint.ok:
                    from ..eda.base import StepResult

                    fail = StepResult(
                        stage="lint",
                        ok=False,
                        tool="lint",
                        log=rep_lint.summary(30),
                        diagnostics=self._lint_diagnostics(rep_lint),
                        verdict="error",
                    )
                    self._record_taxa(rec, fail.diagnostics)
                    from ..eda.base import write_log as _write_log

                    _write_log(project, f"lint_r{round_idx}", rep_lint.summary(50))
                    self.ctx.emit(
                        AgentEvent(agent=self.name, action="静态门控拦截（未调用 EDA）", detail=rep_lint.summary(3), level="warning")
                    )
                    last_fail = fail
                    if not self._do_repair(rec, spec, project, code, cst_text, fail, round_idx + 1):
                        break
                    code, cst_text = rec._code, rec._cst  # type: ignore[attr-defined]
                    repair_rounds += 1
                    continue

            # 4.3 综合门控（可选）
            if self.cfg.mode == "full" and self.cfg.pre_synth:
                t0 = time.time()
                synth = self.flow.compile(project)
                rec.durations[f"synth_{round_idx}_s"] = round(time.time() - t0, 3)
                self._record_taxa(rec, synth.diagnostics)
                if not synth.ok:
                    last_fail = synth
                    self.ctx.emit(AgentEvent(agent=self.name, action=f"综合失败（第 {round_idx} 轮）", detail=synth.brief(), level="warning"))
                    if not self._do_repair(rec, spec, project, code, cst_text, last_fail, round_idx + 1):
                        break
                    code, cst_text = rec._code, rec._cst  # type: ignore[attr-defined]
                    no_change = 0 if rec.repair_methods else no_change + 1
                    repair_rounds += 1
                    continue
                rec.synth_ok = True
                rec.compile_ok = True

            # 4.3 仿真
            if self.cfg.mode != "lint-only":
                t0 = time.time()
                sim = self.flow.simulate(project)
                rec.durations[f"sim_{round_idx}_s"] = round(time.time() - t0, 3)
                self._record_taxa(rec, sim.diagnostics)
                rec.sim_verdict = sim.verdict
                if not sim.ok:
                    last_fail = sim
                    self.ctx.emit(AgentEvent(agent=self.name, action=f"仿真未通过（第 {round_idx} 轮）", detail=sim.brief(), level="warning"))
                    if not self._do_repair(rec, spec, project, code, cst_text, last_fail, round_idx + 1):
                        break
                    code, cst_text = rec._code, rec._cst  # type: ignore[attr-defined]
                    repair_rounds += 1
                    continue
                sim_ok = True
                self.ctx.emit(AgentEvent(agent=self.name, action="仿真通过", detail=f"{rec.tb_checkpoints} 个检查点"))

            # 4.4 布局布线 + 比特流
            if self.cfg.mode == "full":
                t0 = time.time()
                impl = self.flow.implement(project)
                rec.durations[f"pnr_{round_idx}_s"] = round(time.time() - t0, 3)
                self._record_taxa(rec, impl.diagnostics)
                rec.resources = {k: v for k, v in impl.metrics.items() if k != "fmax_mhz"}
                rec.fmax_mhz = float(impl.metrics.get("fmax_mhz") or 0.0)
                if not impl.ok:
                    last_fail = impl
                    self.ctx.emit(AgentEvent(agent=self.name, action=f"布局布线失败（第 {round_idx} 轮）", detail=impl.brief(), level="warning"))
                    if not self._do_repair(rec, spec, project, code, cst_text, last_fail, round_idx + 1):
                        break
                    code, cst_text = rec._code, rec._cst  # type: ignore[attr-defined]
                    repair_rounds += 1
                    continue
                rec.pnr_ok = True
                rec.compile_ok = True  # 布局布线成功意味着综合必然通过
                rec.bitstream = impl.artifacts.get("bitstream", "")
                rec.artifacts.update(impl.artifacts)
                self.ctx.emit(AgentEvent(agent=self.name, action="实现完成", detail=self.deploy_agent.describe_bitstream(impl.artifacts, impl.metrics)))

                if self.cfg.program and rec.bitstream:
                    prog = self.flow.program(project, dry_run=self.cfg.dry_run_program)
                    rec.programmed = prog.ok and prog.verdict == "programmed"
                    self.ctx.emit(AgentEvent(agent=self.name, action="烧录", detail=prog.verdict))

            rec.success = sim_ok or self.cfg.mode == "lint-only"
            break

        rec.repair_rounds = repair_rounds
        rec.sim_ok = sim_ok
        if not rec.success and last_fail is not None:
            rec.error = f"闭环未收敛：{last_fail.brief()}"
        rec.artifacts["rtl"] = str(project.rtl)
        if project.tb:
            rec.artifacts["tb"] = str(project.tb)

    # ------------------------------------------------------------------
    def _do_repair(
        self,
        rec: RunRecord,
        spec: DesignSpec,
        project: Project,
        code: str,
        cst: str,
        fail,
        attempt: int,
    ) -> bool:
        diags: List[Diagnostic] = list(fail.diagnostics or [])
        if not diags:
            diags = [Diagnostic(taxon="E01", severity="error", tool=fail.tool, message=fail.log[-500:] or "未知错误")]

        if self.cfg.use_structured_diag:
            rep = self.debug_agent.repair(
                spec,
                code,
                diags,
                cst=cst,
                log_tail=fail.log,
                attempt=attempt,
                device=self.settings.device,
                gowin_root=self.settings.tools.gowin_root,
            )
        else:
            rep = self.debug_agent.repair_raw(spec, code, fail.log, attempt=attempt)

        rec.repair_methods.append(rep.method)
        new_code = rep.code or code
        new_cst = rep.cst or cst
        rec._code = new_code  # type: ignore[attr-defined]
        rec._cst = new_cst  # type: ignore[attr-defined]
        if new_code == code and new_cst == cst:
            self.ctx.emit(AgentEvent(agent=self.name, action="修复无变更", detail="终止闭环", level="error"))
            return False
        return True

    # ------------------------------------------------------------------
    @staticmethod
    def _lint_diagnostics(rep) -> List[Diagnostic]:
        """把静态检查违规转成统一诊断对象，供修复智能体消费。"""
        return [
            Diagnostic(
                taxon=v.taxon,
                severity=v.severity,
                tool="lint",
                message=v.message,
                line=v.line,
                raw=f"{v.code} {v.message} {v.hint}",
            )
            for v in rep.errors
        ]

    @staticmethod
    def _write_sources(project: Project, code: str, tb: str, cst: str, sdc: str) -> None:
        project.ensure_dirs()
        project.rtl.write_text(code, encoding="utf-8")
        if project.tb is not None and tb:
            project.tb.write_text(tb, encoding="utf-8")
        if project.cst is not None and cst:
            project.cst.write_text(cst, encoding="utf-8")
        if project.sdc is not None and sdc:
            project.sdc.write_text(sdc, encoding="utf-8")

    @staticmethod
    def _record_taxa(rec: RunRecord, diags: Sequence[Diagnostic]) -> None:
        for d in diags:
            if d.severity not in ("error", "warning"):
                continue
            rec.taxonomy_hits[d.taxon] = rec.taxonomy_hits.get(d.taxon, 0) + 1

    # ------------------------------------------------------------------
    def _attach_trace(self, rec: RunRecord, run_dir: Path) -> None:
        path = run_dir / "events.jsonl"

        def sink(event: AgentEvent) -> None:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.as_dict(), ensure_ascii=False) + "\n")

        self.ctx.set_sink(sink)

    def _render_summary(self, rec: RunRecord) -> str:
        lines = [
            f"# 运行摘要：{rec.design}（{rec.variant}）",
            "",
            f"- 运行 ID：`{rec.run_id}`",
            f"- 模型：{rec.model or rec.provider}｜提示词版本：{rec.prompt_version}",
            f"- 起始/结束：{rec.started_at} → {rec.finished_at}",
            f"- 结果：{'成功' if rec.success else '失败'}"
            + (f"（{rec.error}）" if rec.error else ""),
            "",
            "## 关键指标",
            f"- RTL 尝试次数：{rec.rtl_attempts}（{rec.rtl_lines} 行）",
            f"- 静态检查：{rec.lint_errors} 错误 / {rec.lint_warnings} 警告",
            f"- Testbench：{rec.tb_method}｜检查点 {rec.tb_checkpoints}",
            f"- 修复轮次：{rec.repair_rounds}｜方法序列：{rec.repair_methods}",
            f"- 综合：{rec.synth_ok}｜布局布线：{rec.pnr_ok}｜比特流：{rec.bitstream or '无'}",
            f"- Fmax：{rec.fmax_mhz} MHz",
            f"- token：{rec.tokens.get('total_tokens', 0)}（{rec.tokens.get('calls', 0)} 次调用）",
            f"- 总耗时：{rec.durations.get('total_s')} s",
            "",
            "## 诊断类别命中",
        ]
        if rec.taxonomy_hits:
            lines += ["| 类别 | 次数 |", "| --- | --- |"]
            for k, v in sorted(rec.taxonomy_hits.items()):
                lines.append(f"| {k} | {v} |")
        else:
            lines.append("（无）")
        lines += ["", "## 事件流", "", "| 时间 | 智能体 | 动作 | 详情 |", "| --- | --- | --- | --- |"]
        for e in rec.events:
            detail = str(e.get("detail", "")).replace("|", "/")[:120]
            lines.append(f"| {e.get('ts')} | {e.get('agent')} | {e.get('action')} | {detail} |")
        return "\n".join(lines) + "\n"
