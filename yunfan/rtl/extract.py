"""从模型输出中稳健地抽取 Verilog 代码，并做结构完整性判定。

设计动机
--------
大模型输出常见三类噪声：(1) Markdown 围栏；(2) 解释性前后缀；
(3) 因 max_tokens 截断导致 begin/end、case/endcase 不闭合。
本模块用「注释/字符串掩码 + 关键字计数」在不依赖编译器的情况下快速判定，
把问题拦在 EDA 调用之前（对应论文中的静态门控）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*$")
_THINK_RE = re.compile(r"<think(?:ing)?>.*?(?:</think(?:ing)?>|$)", re.DOTALL | re.IGNORECASE)

_PAIRS = {
    "module": "endmodule",
    "begin": "end",
    "case": "endcase",
    "casex": "endcase",
    "casez": "endcase",
    "function": "endfunction",
    "task": "endtask",
    "generate": "endgenerate",
    "fork": "join",
}


def strip_thinking(text: str) -> str:
    """去掉推理模型的思维链（避免其中的假 module/endmodule 干扰抽取）。"""
    if not text:
        return ""
    return _THINK_RE.sub("", text)


def strip_fences(text: str) -> str:
    """去掉 Markdown 围栏行（即使围栏未闭合也能处理）。"""
    lines = [ln for ln in (text or "").replace("\r\n", "\n").split("\n") if not _FENCE_RE.match(ln)]
    return "\n".join(lines)


def mask_noncode(text: str) -> str:
    """把注释与字符串内容替换为空格，保持字符下标与换行不变。"""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif ch == "/" and nxt == "*":
            out[i] = " "
            out[i + 1] = " "
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
        elif ch == '"':
            out[i] = " "
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    out[i] = " "
                    i += 1
                if i < n:
                    if text[i] != "\n":
                        out[i] = " "
                    i += 1
            if i < n:
                out[i] = " "
                i += 1
        else:
            i += 1
    return "".join(out)


@dataclass
class StructureReport:
    ok: bool
    issues: List[str] = field(default_factory=list)
    has_module: bool = False
    has_endmodule: bool = False
    module_name: str = ""
    module_count: int = 0
    counts: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "issues": self.issues,
            "module_name": self.module_name,
            "module_count": self.module_count,
            "counts": self.counts,
        }


def keyword_counts(masked: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for kw in set(list(_PAIRS.keys()) + list(_PAIRS.values())):
        counts[kw] = len(re.findall(rf"\b{kw}\b", masked))
    return counts


def find_modules(text: str) -> List[Tuple[str, int, int]]:
    """返回 [(模块名, 起始下标, 结束下标(独占))]，仅用于单模块场景的稳健切片。"""
    masked = mask_noncode(text)
    modules: List[Tuple[str, int, int]] = []
    for m in re.finditer(r"\bmodule\s+([A-Za-z_]\w*)", masked):
        header_start = m.start()
        depth = 0
        i = m.end()
        n = len(masked)
        # 跳过 #(...) 与端口列表，直到模块头结束的分号
        while i < n:
            c = masked[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth = max(0, depth - 1)
            elif c == ";" and depth == 0:
                break
            i += 1
        # 找到配对的 endmodule
        sub = masked[i:]
        endm = re.search(r"\bendmodule\b", sub)
        if endm:
            end_idx = i + endm.end()
            modules.append((m.group(1), header_start, end_idx))
        else:
            modules.append((m.group(1), header_start, len(text)))
    return modules


def inspect_structure(code: str) -> StructureReport:
    if not code or not code.strip():
        return StructureReport(ok=False, issues=["代码为空"])
    masked = mask_noncode(code)
    counts = keyword_counts(masked)
    issues: List[str] = []

    if counts.get("module", 0) == 0:
        issues.append("缺少 module 关键字")
    if counts.get("module", 0) > counts.get("endmodule", 0):
        issues.append(f"module({counts['module']}) 与 endmodule({counts['endmodule']}) 数量不匹配——输出可能被截断")

    # begin/end：end 计数需扣除 endmodule/endcase/endfunction/endtask/endgenerate
    if counts.get("begin", 0) > counts.get("end", 0):
        issues.append(f"begin({counts['begin']}) 多于 end({counts['end']})——缺少配对的 end")

    for open_kw in ("case", "casex", "casez"):
        if counts.get(open_kw, 0) > counts.get("endcase", 0):
            issues.append(f"{open_kw} 缺少 endcase")
            break
    if counts.get("function", 0) > counts.get("endfunction", 0):
        issues.append("function 缺少 endfunction")

    names = find_modules(code)
    primary = ""
    if names:
        # 选带端口的模块作为 DUT（优先第一个含 input/output 的模块）
        primary = names[0][0]
        for name, start, end in names:
            body = masked[start:end]
            if re.search(r"\b(input|output|inout)\b", body):
                primary = name
                break

    ok = not issues and bool(names)
    return StructureReport(
        ok=ok,
        issues=issues,
        has_module=counts.get("module", 0) > 0,
        has_endmodule=counts.get("endmodule", 0) > 0,
        module_name=primary,
        module_count=len(names),
        counts=counts,
    )


def is_truncated(code: str) -> bool:
    rep = inspect_structure(code)
    if not rep.has_endmodule:
        return True
    return any("截断" in i or "配对" in i or "缺少" in i for i in rep.issues)


def extract_verilog(text: str, *, prefer_name: Optional[str] = None) -> str:
    """从任意模型输出中抽取最像 DUT 的一段 Verilog。"""
    cleaned = strip_fences(strip_thinking(text or "")).strip()
    if not cleaned:
        return ""

    # 优先取带 ```verilog 围栏的内容（围栏已被剥离，这里按模块名选择）
    modules = find_modules(cleaned)
    if not modules:
        return cleaned

    if prefer_name:
        for name, start, end in modules:
            if name == prefer_name:
                return cleaned[start:end].strip()

    # 若存在多个模块，优先返回含端口声明的第一个
    masked = mask_noncode(cleaned)
    for name, start, end in modules:
        if re.search(r"\b(input|output|inout)\b", masked[start:end]):
            return cleaned[start:end].strip()

    name, start, end = modules[0]
    return cleaned[start:end].strip()


def module_name_of(code: str) -> str:
    return inspect_structure(code).module_name
