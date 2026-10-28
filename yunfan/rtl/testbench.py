"""自验证 Testbench 生成器（Self-Verifying Testbench）。

继承 VHDL-Eval 的「自验证激励」思想，但把激励与判据从「人工编写」升级为
「由设计规格 + 参考模型自动合成」：

    设计规格 ──▶ 端口/位宽（保证接口 100% 对齐）
             └─▶ 时序计划（drive/check 向量，由 oracle 参考模型给出期望值）

因此 Testbench 的编译通过率不再依赖模型运气，判据也不再是「模型自己说了算」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .interface import ModuleInterface, parse_interface

PASS_TOKEN = "FINAL RESULT: PASSED"
FAIL_TOKEN = "FINAL RESULT: FAILED"
TIMEOUT_TOKEN = "FINAL RESULT: TIMEOUT"


@dataclass
class Drive:
    cycle: int
    values: Dict[str, int]


@dataclass
class Check:
    cycle: int
    values: Dict[str, int]


@dataclass
class TestPlan:
    """测试计划：cycle 语义为「第 c 个时钟上升沿之后」。"""

    period_ns: int = 20
    reset_port: str = "rst_n"
    reset_active_low: bool = True
    reset_cycles: int = 3
    drives: List[Drive] = field(default_factory=list)
    checks: List[Check] = field(default_factory=list)
    max_cycles: int = 5000
    notes: str = ""

    def last_cycle(self) -> int:
        cycles = [d.cycle for d in self.drives] + [c.cycle for c in self.checks] + [self.reset_cycles]
        return max(cycles) if cycles else 0


def _fmt(value: int, width: int) -> str:
    if width <= 1:
        return f"1'b{int(value) & 1}"
    return f"{width}'d{int(value) & ((1 << width) - 1)}"


def _decl(sig: str, width: int, kind: str) -> str:
    """kind: reg / wire"""
    rng = "" if width <= 1 else f"[{width - 1}:0] "
    return f"  {kind} {rng}{sig};"


def render_tb(
    spec: dict,
    plan: TestPlan,
    *,
    top_module: Optional[str] = None,
    dut_instance: Optional[str] = None,
    dump_wave: bool = False,
) -> str:
    """由规格与测试计划渲染自验证 Testbench。"""
    top = top_module or spec.get("top_module") or spec.get("name") or "dut"
    inst = dut_instance or top
    tb_name = f"tb_{top}"

    ports: List[dict] = list(spec.get("ports") or [])
    if not ports:
        raise ValueError("规格缺少 ports，无法生成 Testbench")
    iface = ModuleInterface(name=top)
    for p in ports:
        name = str(p.get("name"))
        width = int(p.get("width", 1) or 1)
        direction = str(p.get("dir", "input")).lower()
        from .interface import Port

        iface.ports.append(Port(name=name, direction=direction, width=width))

    inputs = iface.inputs
    outputs = iface.outputs
    clock = next((p for p in inputs if p.name.lower() in ("clk", "clock", "clk_i")), None)
    reset = next((p for p in inputs if p.name.lower() == plan.reset_port.lower()), None)

    lines: List[str] = []
    lines.append("`timescale 1ns/1ps")
    lines.append("// 本 Testbench 由 YunFan 验证智能体自动合成：")
    lines.append(f"//   设计规格: {spec.get('name', top)}   顶层模块: {top}")
    lines.append(f"//   参考模型: {plan.notes or 'spec-driven plan'}")
    lines.append(f"module {tb_name};")
    lines.append("")

    # 时钟与复位
    if clock:
        lines.append(f"  reg {clock.name} = 1'b0;")
        lines.append(f"  always #{plan.period_ns // 2} {clock.name} = ~{clock.name};")
    else:
        lines.append("  reg clk = 1'b0;")
        lines.append(f"  always #{plan.period_ns // 2} clk = ~clk;")
    lines.append("")

    for p in inputs:
        if clock and p.name == clock.name:
            continue
        # 复位初始为「有效」电平，首拍即进入确定的复位状态
        if p.name.lower() == plan.reset_port.lower():
            init = 0 if plan.reset_active_low else 1
        else:
            init = 0
        rng = "" if p.width <= 1 else f"[{p.width - 1}:0] "
        lines.append(f"  reg {rng}{p.name} = {_fmt(init, p.width)};")
    for p in outputs:
        rng = "" if p.width <= 1 else f"[{p.width - 1}:0] "
        lines.append(f"  wire {rng}{p.name};")
    lines.append("  integer errors = 0;")
    lines.append("  integer checks = 0;")
    lines.append("  integer i;")
    lines.append("")

    # 例化
    lines.append(f"  {inst} u_dut (")
    conns = []
    for p in iface.ports:
        conns.append(f"    .{p.name}({p.name})")
    lines.append(",\n".join(conns))
    lines.append("  );")
    lines.append("")

    # 向量发射
    events: Dict[int, Tuple[List[str], List[str]]] = {}
    for d in plan.drives:
        events.setdefault(d.cycle, ([], []))[0].append(d)
    for c in plan.checks:
        events_for_cycle = events.setdefault(c.cycle, ([], []))
        events_for_cycle[1].append(c)

    watchdog_ns = max(1, int((plan.last_cycle() + 10) * plan.period_ns * 2))

    lines.append("  initial begin")
    if dump_wave:
        lines.append('    $dumpfile("wave.vcd");')
        lines.append(f"    $dumpvars(0, {tb_name});")
    for p in inputs:
        if clock and p.name == clock.name:
            continue
        if p.name.lower() == plan.reset_port.lower():
            init = 0 if plan.reset_active_low else 1
        else:
            init = 0
        lines.append(f"    {p.name} = {_fmt(init, p.width)};")
    lines.append("")

    width_of = {p.name: p.width for p in iface.ports}
    current = -1
    ordered_cycles = sorted(events.keys())

    for cycle in ordered_cycles:
        if cycle < 0:
            continue
        gap = cycle - current
        if gap <= 0:
            continue
        if gap == 1:
            lines.append("    @(posedge clk); #1;")
        else:
            lines.append(f"    repeat ({gap}) @(posedge clk); #1;")
        current = cycle

        drives, checks = events[cycle]
        for d in drives:
            for sig, val in d.values.items():
                if clock and sig == clock.name:
                    continue  # 时钟由 always 发生器独占，禁止激励覆盖
                w = width_of.get(sig, 1)
                lines.append(f"    {sig} = {_fmt(int(val), w)};  // cycle {cycle} drive")
        if checks:
            lines.append(f"    // ---- cycle {cycle} ----")
            for c in checks:
                for sig, val in c.values.items():
                    w = width_of.get(sig, 1)
                    exp = _fmt(int(val), w)
                    lines.append(f"    checks = checks + 1;")
                    lines.append(
                        f'    if ({sig} !== {exp}) begin errors = errors + 1; '
                        f'$display("CHECK FAIL cycle={cycle} {sig}=%0d expect={int(val)}", {sig}); end'
                    )

    # 尾部：推进复位周期后结束
    if current < plan.reset_cycles:
        lines.append(f"    repeat ({plan.reset_cycles - current}) @(posedge clk); #1;")
    lines.append("")
    lines.append('    if (errors == 0) begin')
    lines.append(f'      $display("CHECKS TOTAL: %0d", checks);')
    lines.append(f'      $display("{PASS_TOKEN}");')
    lines.append("    end else begin")
    lines.append(f'      $display("CHECKS TOTAL: %0d, FAILED: %0d", checks, errors);')
    lines.append(f'      $display("{FAIL_TOKEN}");')
    lines.append("    end")
    lines.append("    $finish;")
    lines.append("  end")
    lines.append("")
    lines.append("  // 看门狗：防止握手信号永不拉高导致的仿真挂死")
    lines.append("  initial begin")
    lines.append(f"    #{watchdog_ns};")
    lines.append(f'    $display("{TIMEOUT_TOKEN}");')
    lines.append("    $finish;")
    lines.append("  end")
    lines.append("")
    lines.append(f"endmodule  // {tb_name}")
    return "\n".join(lines) + "\n"


def judge_sim_output(stdout: str, stderr: str = "") -> Tuple[str, str]:
    """判定仿真结果：返回 (verdict, detail)，verdict ∈ pass/fail/timeout/error。"""
    text = f"{stdout}\n{stderr}"
    if PASS_TOKEN in text and FAIL_TOKEN not in text:
        return "pass", "自验证 Testbench 全部检查点通过"
    if TIMEOUT_TOKEN in text:
        return "timeout", "触发射仿看门狗：握手/完成信号未在预算周期内拉高"
    if FAIL_TOKEN in text:
        fails = [ln for ln in text.splitlines() if "CHECK FAIL" in ln or FAIL_TOKEN in ln]
        return "fail", "功能不匹配：" + " | ".join(fails[:5])
    return "error", "仿真未产生判定哨兵（可能编译失败或仿真提前终止）"
