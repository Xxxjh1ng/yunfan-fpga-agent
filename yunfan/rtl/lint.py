"""轻量级 Verilog 静态检查（Lint）——EDA 调用之前的第一道门。

目标不是替代综合器，而是以毫秒级代价拦截大模型的高频错误，
从而减少 EDA 调用次数（论文中「静态门控」收益的度量基础）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .extract import inspect_structure, mask_noncode
from .interface import ModuleInterface, compare_ports, parse_interface
from .sanitize import _always_body

_RESERVED = {
    "module", "endmodule", "input", "output", "inout", "wire", "reg", "integer", "parameter",
    "localparam", "always", "assign", "begin", "end", "if", "else", "case", "casex", "casez",
    "endcase", "default", "for", "while", "repeat", "forever", "function", "endfunction", "task",
    "endtask", "posedge", "negedge", "or", "and", "not", "xor", "signed", "unsigned", "generate",
    "endgenerate", "initial", "genvar", "real", "time", "tri", "supply0", "supply1", "inout",
    "always_ff", "always_comb", "logic", "bit", "byte", "int", "localparam",
}


@dataclass
class Violation:
    code: str  # L01..L12（检查项）
    taxon: str  # E01..E15（诊断类别）
    severity: str  # error | warning
    message: str
    line: int = 0
    hint: str = ""
    snippet: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "code": self.code,
            "taxon": self.taxon,
            "severity": self.severity,
            "message": self.message,
            "line": self.line,
            "hint": self.hint,
            "snippet": self.snippet,
        }

    def as_text(self) -> str:
        loc = f"L{self.line}" if self.line else "-"
        return f"{self.severity.upper():<7} {self.code}@{loc} {self.message}" + (f"（建议：{self.hint}）" if self.hint else "")


@dataclass
class LintReport:
    violations: List[Violation] = field(default_factory=list)
    interface: Optional[ModuleInterface] = None
    checked_bytes: int = 0

    @property
    def errors(self) -> List[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> List[Violation]:
        return [v for v in self.violations if v.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def by_taxon(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for v in self.violations:
            out[v.taxon] = out.get(v.taxon, 0) + 1
        return out

    def summary(self, max_items: int = 8) -> str:
        if not self.violations:
            return "静态检查通过（无违规）"
        lines = [v.as_text() for v in self.violations[:max_items]]
        if len(self.violations) > max_items:
            lines.append(f"... 另有 {len(self.violations) - max_items} 条")
        return "\n".join(lines)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _always_blocks(masked: str) -> List[Tuple[str, int, str]]:
    """返回 [(敏感列表文本, 起始下标, 语句体文本)]。"""
    out = []
    for m in re.finditer(r"\balways\b", masked):
        body = _always_body(masked, m.start())
        if not body:
            continue
        head_end = masked.find("@", m.start())
        paren_start = masked.find("(", head_end)
        head = masked[paren_start:paren_start + 200] if paren_start > 0 else ""
        out.append((head, m.start(), body))
    return out


def _module_body(masked: str) -> str:
    """返回模块体（模块头分号之后的部分），避免把 ANSI 端口声明误判为 wire/reg 声明。"""
    m = re.search(r"\bmodule\b", masked)
    if not m:
        return masked
    i, depth, n = m.end(), 0, len(masked)
    while i < n:
        c = masked[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == ";" and depth == 0:
            return masked[i + 1:]
        i += 1
    return masked[m.end():]


def _declared_names(masked: str, kind: str) -> set:
    """收集 `wire` / `reg` / `integer` 声明中的信号名（忽略初始化表达式与位宽）。"""
    names: set = set()
    for m in re.finditer(rf"\b{kind}\b([^;]*);", masked):
        seg = m.group(1).split("=")[0]  # 去掉初始化表达式，避免把 RHS 信号误当声明
        seg = re.sub(r"\[[^\]]*\]", " ", seg)  # 去掉位宽
        for t in re.findall(r"[A-Za-z_]\w*", seg):
            if t.lower() in ("signed", "unsigned", "wire", "reg", "integer", "scalared", "vectored"):
                continue
            names.add(t.lower())
    return names


def lint(
    code: str,
    *,
    spec_ports: Optional[Sequence[dict]] = None,
    strict_style: bool = True,
) -> LintReport:
    rep = LintReport(checked_bytes=len(code or ""))
    if not (code or "").strip():
        rep.violations.append(Violation("L02", "E02", "error", "RTL 为空", 0, "重新生成完整模块"))
        return rep

    masked = mask_noncode(code)
    rep.interface = parse_interface(code)

    # L01 / L02：结构完整性
    st = inspect_structure(code)
    for issue in st.issues:
        taxon = "E02" if ("截断" in issue or "module" in issue) else "E01"
        rep.violations.append(Violation("L01" if taxon == "E01" else "L02", taxon, "error", issue, 0, "补齐缺失的 end/endcase/endmodule"))
    if st.module_count > 1:
        rep.violations.append(
            Violation("L02", "E02", "warning", f"输出包含 {st.module_count} 个模块，仅第一个作为 DUT", 0, "只输出一个 DUT 模块")
        )

    # L03：wire 被 always 赋值 / 多驱动
    assigned_in_always: Dict[str, int] = {}
    for head, start, blk in _always_blocks(masked):
        for am in re.finditer(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(<=|=)(?!=)", blk):
            sig = am.group(1).lower()
            if sig in _RESERVED:
                continue
            assigned_in_always[sig] = assigned_in_always.get(sig, 0) + 1

    body = _module_body(masked)
    declared_wire = _declared_names(body, "wire")
    # 模块头里声明为 wire 的输出端口，同样不能在 always 中被过程赋值
    for pm in re.finditer(r"\boutput\s+(?:wire|signed)\b([^;,]*)", masked):
        seg = re.sub(r"\[[^\]]*\]", " ", pm.group(1))
        for token in re.findall(r"[A-Za-z_]\w*", seg):
            if token.lower() in ("signed", "wire", "reg", "output", "inout", "input"):
                continue
            declared_wire.add(token.lower())
    for sig in assigned_in_always:
        if sig in declared_wire:
            rep.violations.append(
                Violation("L03", "E04", "error", f"wire `{sig}` 在 always 块中被赋值（必须声明为 reg）", 0, f"将 `{sig}` 的声明改为 reg")
            )

    port_names = {p.name.lower() for p in (rep.interface.ports if rep.interface else [])}
    for head, start, blk in _always_blocks(masked):
        rhs_to = [s for s in {m.group(1).lower() for m in re.finditer(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(<=|=)(?!=)", blk)} if s in port_names]
        for sig in rhs_to:
            p = rep.interface.port_map().get(sig) if rep.interface else None
            if p and p.normalized_dir == "input" and any(sig == n for n in rhs_to):
                rep.violations.append(
                    Violation("L03", "E04", "error", f"输入端口 `{sig}` 被过程赋值", _line_of(masked, start), "输入端口不可赋值，请检查是否写错信号名")
                )

    # L04 / L05：赋值风格（组合用 <=、时序用 =）
    for head, start, blk in _always_blocks(masked):
        is_seq = bool(re.search(r"\b(posedge|negedge)\b", head))
        has_nba = bool(re.search(r"<=", blk))
        has_blocking = bool(re.search(r"(?<![<>=!])=(?!=)", blk))
        lineno = _line_of(masked, start)
        if not is_seq and has_nba and strict_style:
            rep.violations.append(
                Violation("L04", "E15", "warning", "组合 always 块中出现非阻塞赋值 `<=`", lineno, "组合逻辑使用阻塞赋值 `=`")
            )
        if is_seq and has_blocking and strict_style:
            rep.violations.append(
                Violation("L05", "E15", "warning", "时序 always 块中出现阻塞赋值 `=`", lineno, "时序逻辑统一使用非阻塞赋值 `<=`")
            )

    # L06：不可综合结构
    for pat, desc in [
        (r"#\s*\d+", "`#` 延时"),
        (r"\binitial\b", "initial 块"),
        (r"\$(display|monitor|finish|stop|dumpfile|dumpvars|fatal)\b", "系统任务"),
        (r"\breal\b", "real 类型"),
        (r"\bwhile\s*\(\s*1\s*\)", "while(1) 死循环"),
    ]:
        for m in re.finditer(pat, masked):
            rep.violations.append(
                Violation("L06", "E08", "error", f"可综合代码中出现不可综合结构：{desc}", _line_of(masked, m.start()), "删除该结构")
            )

    # L07：锁存器推断（组合 always 的 if 无 else / case 无 default）
    for head, start, blk in _always_blocks(masked):
        if re.search(r"\b(posedge|negedge)\b", head):
            continue
        lineno = _line_of(masked, start)
        if re.search(r"\bif\b", blk) and not re.search(r"\belse\b", blk):
            rep.violations.append(
                Violation("L07", "E06", "warning", "组合 always 中的 if 无 else 分支，可能推断出锁存器", lineno, "补 else 或在块首赋默认值")
            )
        if re.search(r"\bcase[xz]?\b", blk) and not re.search(r"\bdefault\b", blk):
            rep.violations.append(
                Violation("L07", "E06", "warning", "case 语句缺少 default 分支", lineno, "补 default 分支")
            )

    # L08：多驱动（同一信号出现在多个 always 中）
    driver_blocks: Dict[str, int] = {}
    for head, start, blk in _always_blocks(masked):
        for sig in {m.group(1).lower() for m in re.finditer(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(<=|=)(?!=)", blk)}:
            driver_blocks[sig] = driver_blocks.get(sig, 0) + 1
    for sig, cnt in driver_blocks.items():
        if cnt > 1 and sig not in _RESERVED:
            rep.violations.append(
                Violation("L08", "E05", "error", f"信号 `{sig}` 被 {cnt} 个 always 块驱动", 0, "合并驱动逻辑")
            )

    # L09：未声明的左值
    if rep.interface and rep.interface.ports:
        declared = set(port_names)
        for kind in ("reg", "wire", "integer"):
            declared |= _declared_names(body, kind)
        for sig in driver_blocks:
            if sig in _RESERVED or sig in declared:
                continue
            # 数组下标 / 位选 / 函数调用一律跳过
            if re.search(rf"\b{re.escape(sig)}\s*\(", masked):
                continue
            rep.violations.append(
                Violation("L09", "E03", "error", f"信号 `{sig}` 被赋值但未声明", 0, "补 reg/wire 声明")
            )

    # L10：位宽不匹配（右侧常量明显超出左侧位宽）
    for m in re.finditer(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(?:<=|=)\s*(\d+)'([bdh])([0-9a-fA-F_]+)", masked):
        sig, width, base, digits = m.group(1), 0, m.group(3).lower(), m.group(4)
        p = rep.interface.port_map().get(sig.lower()) if rep.interface else None
        if p and p.width:
            width = p.width
        elif re.search(rf"\breg\b[^;]*\b{re.escape(sig)}\b", masked):
            dm = re.search(rf"\breg\s*(\[[^\]]*\])?\s*[^;]*\b{re.escape(sig)}\b", masked)
            if dm and dm.group(1):
                from .interface import _eval_width

                width, _, _ = _eval_width(dm.group(1), rep.interface.parameters if rep.interface else {})
        if not width:
            continue
        try:
            value = int(digits.replace("_", ""), {"b": 2, "d": 10, "h": 16}[base])
        except Exception:
            continue
        if value >= (1 << width):
            rep.violations.append(
                Violation("L10", "E07", "warning", f"常量 {value} 超出 `{sig}` 的 {width} 位位宽", _line_of(masked, m.start()), "显式截断或加宽信号")
            )

    # L11：规格端口一致性
    if spec_ports:
        for mm in compare_ports(spec_ports, rep.interface):
            sev = "error" if mm.kind in ("missing", "direction", "width") else "warning"
            rep.violations.append(
                Violation("L11", "E09", sev, f"端口不一致 {mm.as_text()}", 0, "按规格书端口表逐字修正模块头")
            )

    # 去重（同类同消息只保留一条）
    seen = set()
    unique: List[Violation] = []
    for v in rep.violations:
        key = (v.code, v.taxon, v.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(v)
    rep.violations = unique
    return rep
