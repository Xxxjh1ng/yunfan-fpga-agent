"""RTL 确定性净化：在不改变设计语义的前提下修掉大模型的高频「格式病」。

每一步都返回是否实际生效，便于在 trace 中统计各修复项的触发频率
（论文中「确定性预修复」一节的量化依据）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .extract import extract_verilog, mask_noncode, module_name_of

_KEYWORDS = {
    "module", "endmodule", "input", "output", "inout", "wire", "reg", "integer", "parameter",
    "localparam", "always", "assign", "begin", "end", "if", "else", "case", "casex", "casez",
    "endcase", "default", "for", "while", "repeat", "function", "endfunction", "task", "endtask",
    "posedge", "negedge", "or", "and", "not", "signed", "unsigned", "generate", "endgenerate",
    "initial", "genvar", "real", "time", "integer", "supply0", "supply1", "tri", "wand", "wor",
}


@dataclass
class Fix:
    kind: str
    detail: str
    count: int = 1

    def as_text(self) -> str:
        return f"{self.kind}: {self.detail}"


@dataclass
class SanitizeReport:
    code: str = ""
    fixes: List[Fix] = field(default_factory=list)

    def add(self, kind: str, detail: str, count: int = 1) -> None:
        if count <= 0:
            return
        for f in self.fixes:
            if f.kind == kind and f.detail == detail:
                f.count += count
                return
        self.fixes.append(Fix(kind, detail, count))

    @property
    def changed(self) -> bool:
        return bool(self.fixes)

    def summary(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for f in self.fixes:
            out[f.kind] = out.get(f.kind, 0) + f.count
        return out


def ensure_timescale(code: str) -> Tuple[str, bool]:
    if re.search(r"`timescale", code):
        return code, False
    return "`timescale 1ns/1ps\n\n" + code.lstrip(), True


def ensure_endmodule(code: str) -> Tuple[str, bool]:
    masked = mask_noncode(code)
    if "module" in masked and "endmodule" not in masked:
        return code.rstrip() + "\n\nendmodule // auto-closed by YunFan\n", True
    return code, False


def strip_hash_delays(code: str) -> Tuple[str, bool]:
    """删除可综合代码中的 #延时（Testbench 不经过本函数）。"""
    masked = mask_noncode(code)
    hits = [m for m in re.finditer(r"#\s*\(?\s*\d+(?:\.\d+)?\s*\)?", masked)]
    if not hits:
        return code, False
    chars = list(code)
    for m in reversed(hits):
        for i in range(m.start(), m.end()):
            chars[i] = " "
    return "".join(chars), True


def strip_display_in_dut(code: str) -> Tuple[str, bool]:
    """删除 DUT 中的 $display/$finish/$stop/$dumpfile 调用行。"""
    lines = code.split("\n")
    out, removed = [], 0
    for ln in lines:
        if re.search(r"\$(display|finish|stop|dumpfile|dumpvars|monitor|fatal|error)\b", ln):
            removed += 1
            continue
        out.append(ln)
    return "\n".join(out), removed > 0


def int_to_integer(code: str) -> Tuple[str, bool]:
    new = re.sub(r"\bint\b", "integer", code)
    return new, new != code


def fix_wire_assigned_in_always(code: str) -> Tuple[str, bool]:
    """把「在 always 中被赋值的 wire 声明」改为 reg。"""
    masked = mask_noncode(code)
    assigned = set()
    for m in re.finditer(r"\balways\b", masked):
        body = _always_body(masked, m.start())
        if not body:
            continue
        for am in re.finditer(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*<=", body):
            assigned.add(am.group(1).lower())
        for am in re.finditer(r"(?<![<>=!])\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*=(?!=)", body):
            assigned.add(am.group(1).lower())

    if not assigned:
        return code, False

    changed = False
    lines = code.split("\n")
    for idx, ln in enumerate(lines):
        # 只处理 wire 声明行
        if not re.search(r"\bwire\b", ln):
            continue
        decl_only = ln.split("=")[0]  # 去掉初始化表达式，避免把 RHS 中的信号误判为被声明信号
        decl_only = re.sub(r"\[[^\]]*\]", " ", decl_only)
        decl_names = re.findall(r"[A-Za-z_]\w*", decl_only)
        if decl_names and decl_names[0] != "wire":
            # 例如 "input wire [7:0] a" -> 只处理纯 wire 声明
            continue
        names = [n.lower() for n in decl_names[1:] if n.lower() not in ("signed", "unsigned")]
        if any(n in assigned for n in names):
            lines[idx] = re.sub(r"\bwire\b", "reg ", ln, count=1)
            changed = True
    return "\n".join(lines), changed


def _always_body(masked: str, start: int, limit: int = 20000) -> Optional[str]:
    """返回 always 语句体（begin..end 或单语句）的文本区间。"""
    i = masked.find("@", start)
    if i < 0:
        return None
    j = i + 1
    n = min(len(masked), start + limit)
    # 事件控制
    if j < n and masked[j] == "(":
        depth = 0
        while j < n:
            if masked[j] == "(":
                depth += 1
            elif masked[j] == ")":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
    elif j < n and masked[j] == "*":
        j += 2
    while j < n and masked[j] in " \t\r\n":
        j += 1
    if masked.startswith("begin", j):
        depth = 0
        k = j
        while k < n:
            m = re.match(r"\b(begin|end)\b", masked[k:])
            if m:
                depth += 1 if m.group(1) == "begin" else -1
                k += m.end()
                if depth == 0:
                    return masked[j:k]
                continue
            k += 1
        return masked[j:n]
    # 单语句
    k = masked.find(";", j)
    return masked[j:k + 1] if k > 0 else None


def remove_stray_prose(code: str) -> Tuple[str, bool]:
    """删除代码块之外的解释性文字（例如 "Here is the code:"）。"""
    masked = mask_noncode(code)
    start = masked.find("module")
    if start < 0:
        return code, False
    end = masked.rfind("endmodule")
    if end < 0:
        return code[start:], start > 0
    end += len("endmodule")
    trimmed = code[start:end]
    return trimmed, (start > 0 or end < len(code.strip()))


def rename_top(code: str, target: str) -> Tuple[str, bool]:
    if not target:
        return code, False
    masked = mask_noncode(code)
    m = re.search(r"\bmodule\s+([A-Za-z_]\w*)", masked)
    if not m:
        return code, False
    current = m.group(1)
    if current == target:
        return code, False
    chars = list(code)
    # 只替换模块头中的模块名（以及 endmodule 后的注释不动）
    chars[m.start(1):m.end(1)] = list(target)
    return "".join(chars), True


def sanitize(
    raw: str,
    *,
    target_name: Optional[str] = None,
    strip_non_synth: bool = True,
) -> SanitizeReport:
    """完整净化链：抽取 -> 去噪 -> 结构补全 -> 语义安全修复 -> 重命名。"""
    rep = SanitizeReport()
    code = extract_verilog(raw or "")
    if not code:
        return rep

    code, ch = remove_stray_prose(code)
    rep.add("remove_prose", "剥离代码块外的解释文字", 1 if ch else 0)

    code, ch = int_to_integer(code)
    rep.add("int_to_integer", "int -> integer", 1 if ch else 0)

    if strip_non_synth:
        code, ch = strip_hash_delays(code)
        rep.add("strip_delay", "删除 #延时", 1 if ch else 0)
        code, ch = strip_display_in_dut(code)
        rep.add("strip_system_task", "删除 $display 等系统任务", 1 if ch else 0)

    code, ch = fix_wire_assigned_in_always(code)
    rep.add("wire_to_reg", "always 赋值的 wire 改为 reg", 1 if ch else 0)

    code, ch = ensure_endmodule(code)
    rep.add("close_module", "补齐 endmodule", 1 if ch else 0)

    code, ch = ensure_timescale(code)
    rep.add("add_timescale", "补充 timescale", 1 if ch else 0)

    if target_name:
        code, ch = rename_top(code, target_name)
        rep.add("rename_top", f"顶层模块重命名为 {target_name}", 1 if ch else 0)

    rep.code = code.strip() + "\n"
    return rep
