"""故障注入（Fault Injection）：用于客观度量「诊断—修复」闭环的能力。

做法：对**已验证正确的参考 RTL/约束**施加单点变异，每个变异精确对应一个
诊断类别（E01/E02/E04/E08/E09/E10/E11），然后让闭环从变异后的代码出发，
观察它能多快、以何种方式恢复。这比「让模型自由生成再看通过率」更能隔离
出「修复能力」这一变量。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class FaultCase:
    fid: str
    taxon: str
    name: str
    description: str
    target: str  # code / cst
    apply: Callable[[str, dict], Optional[str]]

    def run(self, text: str, spec: dict) -> Optional[str]:
        try:
            return self.apply(text, spec)
        except Exception:
            return None


def _header_span(code: str) -> Optional[Tuple[int, int]]:
    masked = re.sub(r"//[^\n]*", " ", code)
    m = re.search(r"\bmodule\b", masked)
    if not m:
        return None
    i = masked.find(";", m.end())
    return (m.end(), i) if i > 0 else None


def _f01_drop_endmodule(code: str, spec: dict) -> Optional[str]:
    idx = code.rfind("endmodule")
    if idx < 0:
        return None
    return code[:idx] + code[idx + len("endmodule"):]


def _f02_wire_for_reg(code: str, spec: dict) -> Optional[str]:
    if "output reg" not in code:
        return None
    return code.replace("output reg", "output wire", 1)


def _f03_hash_delay(code: str, spec: dict) -> Optional[str]:
    m = re.search(r"always\s*@\s*\([^)]*\)\s*begin", code)
    if not m:
        return None
    return code[: m.end()] + "\n        #5;" + code[m.end():]


def _f04_drop_end(code: str, spec: dict) -> Optional[str]:
    m = re.search(r"\n\s*end\s*\n", code)
    if not m:
        return None
    return code[: m.start()] + "\n" + code[m.end():]


def _f05_rename_port(code: str, spec: dict) -> Optional[str]:
    span = _header_span(code)
    if not span:
        return None
    start, end = span
    header = code[start:end]
    inputs = [p for p in spec.get("ports", []) if p.get("dir") == "input" and p.get("name") not in ("clk", "rst_n")]
    if not inputs:
        return None
    victim = inputs[-1]["name"]
    new_header, n = re.subn(rf"\b{re.escape(victim)}\b", f"{victim}_x", header, count=1)
    if n == 0:
        return None
    return code[:start] + new_header + code[end:]


def _f06_wrong_width(code: str, spec: dict) -> Optional[str]:
    span = _header_span(code)
    if not span:
        return None
    start, end = span
    header = code[start:end]
    vectors = [p for p in spec.get("ports", []) if int(p.get("width", 1)) > 1]
    if not vectors:
        return None
    victim = vectors[0]
    width = int(victim["width"])
    new_header, n = re.subn(
        rf"(\[\s*){width - 1}(\s*:\s*0\s*\]\s*{re.escape(victim['name'])})",
        rf"\g<1>{width + 1}\g<2>",
        header,
        count=1,
    )
    if n == 0:
        return None
    return code[:start] + new_header + code[end:]


def _f07_flip_compare(code: str, spec: dict) -> Optional[str]:
    m = re.search(r"([A-Za-z_]\w*)\s*<\s*([A-Za-z_]\w*)", code)
    if not m:
        return None
    return code[: m.start()] + f"{m.group(1)} <= {m.group(2)}" + code[m.end():]


def _f08_mutate_const(code: str, spec: dict) -> Optional[str]:
    for pat in (r"'d1\b", r"'d0\b"):
        m = re.search(pat, code)
        if m:
            repl = "'d1" if m.group(0) == "'d0" else "'d2"
            return code[: m.start()] + repl + code[m.end():]
    return None


def _f09_bad_pin(cst: str, spec: dict) -> Optional[str]:
    m = re.search(r'IO_LOC\s+"([^"]+)"\s+([A-Za-z0-9_]+)\s*;', cst)
    if not m:
        return None
    return cst[: m.start()] + f'IO_LOC "{m.group(1)}" 4;' + cst[m.end():]


FAULTS: List[FaultCase] = [
    FaultCase("F01", "E02", "删除 endmodule", "输出被截断，缺少模块结束标记", "code", _f01_drop_endmodule),
    FaultCase("F02", "E04", "reg 改 wire", "时序输出被声明为 wire，过程赋值非法", "code", _f02_wire_for_reg),
    FaultCase("F03", "E08", "插入 #延时", "可综合代码中出现仿真专用延时", "code", _f03_hash_delay),
    FaultCase("F04", "E01", "删除一个 end", "begin/end 不再配对", "code", _f04_drop_end),
    FaultCase("F05", "E09", "端口改名", "模块头端口名与规格书不一致", "code", _f05_rename_port),
    FaultCase("F06", "E09", "端口位宽篡改", "端口位宽与规格书不一致", "code", _f06_wrong_width),
    FaultCase("F07", "E11", "比较符变异", "比较逻辑改变，功能不再等价", "code", _f07_flip_compare),
    FaultCase("F08", "E11", "常量变异", "计数步长/初值改变，功能不再等价", "code", _f08_mutate_const),
    FaultCase("F09", "E10", "非法引脚", "IO_LOC 指向当前封装不存在的 pad", "cst", _f09_bad_pin),
]


def get_fault(fid: str) -> Optional[FaultCase]:
    for f in FAULTS:
        if f.fid == fid:
            return f
    return None


def applicable_faults(spec: dict, code: str, cst: str) -> List[FaultCase]:
    out = []
    for f in FAULTS:
        text = code if f.target == "code" else cst
        mutated = f.run(text, spec)
        if mutated is not None and mutated != text:
            out.append(f)
    return out
