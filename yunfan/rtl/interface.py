"""Verilog 模块接口（端口/参数）解析与「规格↔代码」一致性检查。

这是「契约驱动」的核心组件：规格书里的端口表在生成后必须被逐字复核，
从根上消除原始方案中 Testbench 找不到 DUT 端口这一整类失败。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .extract import mask_noncode

_DIR = r"(input|output|inout)"
_DECL_RE = re.compile(rf"\b{_DIR}\b\s*(?:wire|reg|logic|signed|unsigned)*\s*(\[[^\]]*\])?\s*([A-Za-z_,\s\[\]0-9:]+);", re.IGNORECASE)


@dataclass
class Port:
    name: str
    direction: str = "input"
    width: int = 1
    msb: Optional[int] = None
    lsb: Optional[int] = None
    width_expr: str = ""
    raw: str = ""

    @property
    def normalized_dir(self) -> str:
        return {"input": "input", "output": "output", "inout": "inout"}.get(self.direction.lower(), self.direction.lower())


@dataclass
class ModuleInterface:
    name: str = ""
    ports: List[Port] = field(default_factory=list)
    parameters: Dict[str, int] = field(default_factory=dict)

    def port_map(self) -> Dict[str, Port]:
        return {p.name.lower(): p for p in self.ports}

    @property
    def inputs(self) -> List[Port]:
        return [p for p in self.ports if p.normalized_dir == "input"]

    @property
    def outputs(self) -> List[Port]:
        return [p for p in self.ports if p.normalized_dir == "output"]


def _eval_width(expr: str, params: Dict[str, int]) -> Tuple[int, Optional[int], Optional[int]]:
    """把 [7:0] / [WIDTH-1:0] 解析为 (width, msb, lsb)；无法求值时 width=0。"""
    if not expr:
        return 1, None, None
    body = expr.strip().lstrip("[").rstrip("]")
    if ":" not in body:
        return 0, None, None
    left, _, right = body.partition(":")
    env = dict(params)

    def ev(s: str) -> Optional[int]:
        s = s.strip()
        if re.fullmatch(r"\d+", s):
            return int(s)
        for k, v in env.items():
            s = re.sub(rf"\b{re.escape(k)}\b", str(v), s)
        if re.fullmatch(r"[\d+\-*/() ]+", s):
            try:
                return int(eval(s, {"__builtins__": {}}, {}))  # noqa: S307 - 仅整数四则运算
            except Exception:
                return None
        return None

    msb, lsb = ev(left), ev(right)
    if msb is None or lsb is None:
        return 0, None, None
    return abs(msb - lsb) + 1, msb, lsb


def _split_names(chunk: str) -> List[str]:
    names = []
    for part in chunk.split(","):
        name = re.sub(r"\[[^\]]*\]", "", part).strip()
        name = name.split()[-1] if name.split() else ""
        if re.fullmatch(r"[A-Za-z_]\w*", name or ""):
            names.append(name)
    return names


def parse_interface(code: str) -> ModuleInterface:
    masked = mask_noncode(code)
    iface = ModuleInterface()

    m = re.search(r"\bmodule\s+([A-Za-z_]\w*)", masked)
    if not m:
        return iface
    iface.name = m.group(1)

    # 参数表：#(parameter A = 1, parameter B = 2)
    header_end = m.end()
    depth = 0
    i = m.end()
    n = len(masked)
    while i < n:
        c = masked[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
            if depth == 0:
                # 可能是 #(...) 结束，继续找模块头的分号
                j = i + 1
                while j < n and masked[j] in " \t\r\n":
                    j += 1
                if j < n and masked[j] == ";":
                    header_end = j + 1
                    break
        elif c == ";" and depth == 0:
            header_end = i + 1
            break
        i += 1

    header = masked[m.start():header_end]
    for pm in re.finditer(r"\bparameter\s+(?:integer\s+)?(?:\[[^\]]*\]\s*)?([A-Za-z_]\w*)\s*=\s*([^,;)]+)", header, re.IGNORECASE):
        raw = pm.group(2).strip()
        try:
            if re.fullmatch(r"\d+", raw):
                iface.parameters[pm.group(1)] = int(raw)
            elif re.fullmatch(r"\d+'[bdhBDH][0-9a-fA-F_]+", raw):
                base = {"b": 2, "d": 10, "h": 16}[raw.split("'")[1][0].lower()]
                iface.parameters[pm.group(1)] = int(raw.split("'")[1][1:].replace("_", ""), base)
        except Exception:
            pass

    ports: List[Port] = []
    # ANSI 风格：模块头里直接声明
    ansi_decl = re.findall(rf"{_DIR}\b([^;)]*)", header, re.IGNORECASE)
    if ansi_decl and re.search(rf"{_DIR}\b", header):
        # 逐段解析：以方向关键字切分
        segments = re.split(rf"(?={_DIR}\b)", header, flags=re.IGNORECASE)
        for seg in segments:
            sm = re.match(rf"\s*{_DIR}\b(.*)$", seg, re.IGNORECASE | re.DOTALL)
            if not sm:
                continue
            direction = sm.group(1).lower()
            rest = sm.group(2)
            rest = rest.split(")")[0]
            decl = re.match(r"\s*(?:(?:wire|reg|logic|signed|unsigned)\s+)*(\[[^\]]*\])?\s*(.+)$", rest, re.DOTALL | re.IGNORECASE)
            if not decl:
                continue
            width_expr = decl.group(1) or ""
            width, msb, lsb = _eval_width(width_expr, iface.parameters)
            for name in _split_names(decl.group(2)):
                ports.append(Port(name=name, direction=direction, width=width, msb=msb, lsb=lsb, width_expr=width_expr))

    if not ports:
        # 非 ANSI 风格：在模块体内找声明直到第一个非声明语句
        body = masked[header_end:]
        for dm in _DECL_RE.finditer(body):
            direction = dm.group(1).lower()
            width_expr = dm.group(2) or ""
            width, msb, lsb = _eval_width(width_expr, iface.parameters)
            for name in _split_names(dm.group(3)):
                ports.append(Port(name=name, direction=direction, width=width, msb=msb, lsb=lsb, width_expr=width_expr))
            if len(ports) > 400:
                break

    # 去重（同名保留第一次）
    seen = set()
    unique = []
    for p in ports:
        if p.name.lower() in seen:
            continue
        seen.add(p.name.lower())
        unique.append(p)
    iface.ports = unique
    return iface


@dataclass
class PortMismatch:
    kind: str  # missing / extra / direction / width
    port: str
    detail: str

    def as_text(self) -> str:
        return f"[{self.kind}] {self.port}: {self.detail}"


def compare_ports(spec_ports: Sequence[dict], iface: ModuleInterface) -> List[PortMismatch]:
    """规格端口表 vs 实际 RTL 端口。"""
    out: List[PortMismatch] = []
    got = iface.port_map()
    spec_names = set()

    for sp in spec_ports:
        name = str(sp.get("name", "")).strip()
        if not name:
            continue
        spec_names.add(name.lower())
        want_dir = str(sp.get("dir", "input")).lower()
        want_width = int(sp.get("width", 1) or 1)
        p = got.get(name.lower())
        if p is None:
            out.append(PortMismatch("missing", name, f"规格要求 {want_dir}[{want_width}]，RTL 中不存在"))
            continue
        if p.normalized_dir != want_dir:
            out.append(PortMismatch("direction", name, f"规格 {want_dir}，RTL {p.normalized_dir}"))
        if p.width and p.width != want_width:
            out.append(PortMismatch("width", name, f"规格 {want_width} 位，RTL {p.width} 位"))

    for p in iface.ports:
        if p.name.lower() not in spec_names:
            out.append(PortMismatch("extra", p.name, f"RTL 多出端口（{p.normalized_dir}），规格未定义"))
    return out


def iface_to_table(iface: ModuleInterface) -> str:
    lines = [f"module {iface.name} ("]
    for p in iface.ports:
        lines.append(f"    {p.normalized_dir:<6} {('[' + p.width_expr.strip('[]') + ']') if p.width_expr else '':<10} {p.name},")
    lines.append(");")
    return "\n".join(lines)
