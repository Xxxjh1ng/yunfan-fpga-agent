"""多工具日志解析：把 Icarus / 云源 / Vivado 的原始输出结构化为诊断对象。

支持三类工具方言：
* icarus   : ``dut.v:12: error: ...`` / ``dut.v:12: syntax error``
* gowin    : ``ERROR  (CT1113) : "x.cst":1 | Can't find pad location '4'``
* vivado   : ``ERROR: [Synth 8-327] ...``
另加 ``lint``（本地静态检查）与 ``sentinel``（自验证 TB 哨兵）两个来源。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .taxonomy import TAXONOMY, get_taxon

# (taxon, 正则, 说明) —— 顺序即优先级
_CLASSIFY: List[Tuple[str, str]] = [
    # 物理约束
    ("E10", r"can'?t find pad location|invalid (?:io_)?loc|pad location|io_loc.*(?:error|invalid)"),
    ("E10", r"ct1113|pr2017|pr2028|dedicated pin|cannot be placed according to constraint|constrained location is useless"),
    # 顶层/模块
    ("E02", r"endmodule expected|unexpected end of file|premature end|module .* not (?:found|defined)"),
    # 端口/接口
    ("E09", r"port (?:size|width)? ?mismatch|too (?:many|few) port|unknown port|port .*not found|"
            r"cannot (?:find|bind) (?:port|wire/reg)|expecting \d+ ports?|port connection"),
    # 未声明
    ("E03", r"not declared|undeclared|unknown identifier|is not defined|implicit (?:wire|declaration)|unable to (?:bind|find)|"
            r"cannot find (?:the )?(?:signal|identifier)|no such"),
    # wire/reg
    ("E04", r"wire.*(?:assigned|cannot be assigned|not a variable)|assignment to (?:a )?wire|"
            r"procedural assignment.*(?:wire|net)|illegal (?:left|right).*wire"),
    # 多驱动
    ("E05", r"multiple drivers|has multiple driver|net .* driven by"),
    # 锁存器
    ("E06", r"latch (?:is )?inferred|inferred (?:a )?latch|inferring (?:a )?latch|latch .*inferr|combinational loop feedback"),
    # 位宽
    ("E07", r"width mismatch|size mismatch|truncat|bit width|out of range"),
    # 不可综合
    ("E08", r"not synthesizable|cannot synthesize|ignoring (?:initial|delay)|delay (?:is )?not supported|"
            r"system task.*not (?:supported|synthesizable)"),
    # 资源 / 时序
    ("E13", r"exceeds? (?:the )?(?:available|device|capacity)|insufficient (?:resource|logic|io)|"
            r"over[- ]?utiliz|resource (?:overflow|exceed)"),
    ("E14", r"timing (?:violation|not met)|setup (?:time )?violation|hold (?:time )?violation|"
            r"max(?:imum)? frequency|slack"),
    # 功能不匹配（自验证判据）
    ("E11", r"final result:\s*failed|logical mismatch|check fail"),
    # 结构/语法
    ("E01", r"syntax error|unexpected (?:token|symbol|end)|expected ['\"]?[a-z;]|unbalanced|"
            r"missing (?:end|endcase|begin)|illegal character"),
]

_RULE_TAXON_ORDER = [
    ("E10", _CLASSIFY[0][1]),
    ("E10b", _CLASSIFY[1][1]),
]

_RE_ICARUS_ERR = re.compile(r"^(?P<file>[^\s:]+\.(?:v|sv|vh)):(?P<line>\d+):\s*(?P<kind>error|warning|sorry)?:?\s*(?P<msg>.+)$", re.IGNORECASE)
_RE_ICARUS_STRICT = re.compile(r"^(?P<file>[^\s:]+\.(?:v|sv|vh)):(?P<line>\d+):\s*(?P<msg>.+)$", re.IGNORECASE)
_RE_GOWIN = re.compile(r"^(?P<kind>ERROR|WARN(?:ING)?|NOTE)\s*\((?P<code>[A-Z]{2,4}\d{3,5})\)\s*:\s*(?P<rest>.+)$", re.IGNORECASE)
_RE_GOWIN_LOC = re.compile(r'^"?(?P<file>[^":]+)"?\s*:\s*(?P<line>\d+)\s*\|\s*(?P<msg>.+)$')
_RE_VIVADO = re.compile(r"^(?P<kind>ERROR|WARNING|CRITICAL WARNING):\s*\[(?P<code>[^\]]+)\]\s*(?P<msg>.+)$", re.IGNORECASE)

_RE_CHECK_FAIL = re.compile(r"CHECK FAIL cycle=(?P<cycle>\d+) (?P<sig>\w+)=(?P<got>-?\d+) expect=(?P<exp>-?\d+)")
_RE_SENTINEL = re.compile(r"FINAL RESULT:\s*(?P<verdict>PASSED|FAILED|TIMEOUT)")


@dataclass
class Diagnostic:
    taxon: str
    severity: str
    tool: str
    message: str
    file: str = ""
    line: int = 0
    code: str = ""
    raw: str = ""
    cycle: int = 0
    signal: str = ""
    got: str = ""
    expect: str = ""
    repair_hints: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.repair_hints:
            self.repair_hints = list(get_taxon(self.taxon).strategies)

    @property
    def name(self) -> str:
        return get_taxon(self.taxon).name_zh

    @property
    def deterministic(self) -> bool:
        return get_taxon(self.taxon).deterministic

    def as_dict(self) -> Dict[str, object]:
        return {
            "taxon": self.taxon,
            "name": self.name,
            "severity": self.severity,
            "tool": self.tool,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "code": self.code,
            "cycle": self.cycle,
            "signal": self.signal,
            "got": self.got,
            "expect": self.expect,
            "repair_hints": self.repair_hints,
            "deterministic": self.deterministic,
            "raw": self.raw[:400],
        }

    def as_text(self) -> str:
        loc = f" {self.file}:{self.line}" if self.file else ""
        extra = f" [cycle {self.cycle} {self.signal} got={self.got} expect={self.expect}]" if self.signal else ""
        return f"[{self.taxon} {self.name}]{loc}{extra} {self.message}"


def classify(message: str, *, tool: str = "", file: str = "") -> str:
    """按关键词把一条工具消息映射到诊断类别。"""
    text = (message or "").lower()
    fname = (file or "").lower()
    if "cst" in fname or "sdc" in fname:
        return "E10"
    for taxon_id, pattern in _CLASSIFY:
        if re.search(pattern, text, re.IGNORECASE):
            return taxon_id
    if tool == "gowin" and ".cst" in text:
        return "E10"
    return "E01"


def _mk(taxon: str, severity: str, tool: str, message: str, **kw) -> Diagnostic:
    return Diagnostic(taxon=taxon, severity=severity, tool=tool, message=message.strip(), **kw)


def parse_log(log: str, *, tool: str = "auto", source_file: str = "") -> List[Diagnostic]:
    """把一份工具日志解析为诊断列表（已去重、按严重度排序）。"""
    if not log:
        return []
    out: List[Diagnostic] = []
    lines = log.replace("\r\n", "\n").split("\n")

    for idx, raw in enumerate(lines):
        line = raw.rstrip()
        if not line.strip():
            continue

        # 1) 自验证 TB 哨兵
        m = _RE_SENTINEL.search(line)
        if m:
            verdict = m.group("verdict")
            if verdict == "FAILED":
                out.append(_mk("E11", "error", "sentinel", "自验证 Testbench 判定功能不匹配", raw=line))
            elif verdict == "TIMEOUT":
                out.append(_mk("E12", "error", "sentinel", "仿真看门狗触发：握手信号未在预算周期内变化", raw=line))
            continue

        cf = _RE_CHECK_FAIL.search(line)
        if cf:
            out.append(
                _mk(
                    "E11", "error", "sentinel",
                    f"周期 {cf.group('cycle')} 信号 {cf.group('sig')} 期望 {cf.group('exp')} 实得 {cf.group('got')}",
                    raw=line,
                    cycle=int(cf.group("cycle")),
                    signal=cf.group("sig"),
                    got=cf.group("got"),
                    expect=cf.group("exp"),
                )
            )
            continue

        # 2) 云源（Gowin）方言
        gm = _RE_GOWIN.match(line.strip())
        if gm:
            kind = gm.group("kind").upper()
            if kind.startswith("NOTE"):
                continue  # NOTE 是正常提示，不属于诊断
            code = gm.group("code")
            rest = gm.group("rest")
            fname, lineno, msg = "", 0, rest
            lm = _RE_GOWIN_LOC.match(rest)
            if lm:
                fname, lineno, msg = lm.group("file"), int(lm.group("line")), lm.group("msg")
            taxon = classify(f"{code} {msg}", tool="gowin", file=fname)
            sev = "error" if kind.startswith("ERROR") else ("warning" if kind.startswith("WARN") else "info")
            out.append(_mk(taxon, sev, "gowin", msg, file=fname, line=lineno, code=code, raw=line))
            continue

        # 3) Vivado 方言
        vm = _RE_VIVADO.match(line.strip())
        if vm:
            kind = vm.group("kind").upper()
            taxon = classify(vm.group("msg"), tool="vivado")
            sev = "error" if kind == "ERROR" else "warning"
            out.append(_mk(taxon, sev, "vivado", vm.group("msg"), code=vm.group("code"), raw=line))
            continue

        # 4) Icarus 方言
        im = _RE_ICARUS_ERR.match(line.strip())
        if not im:
            im = _RE_ICARUS_STRICT.match(line.strip())
        if im:
            kind = (im.groupdict().get("kind") or "").lower()
            msg = im.group("msg")
            is_warn = kind == "warning" or msg.lower().startswith("warning")
            taxon = classify(msg, tool="icarus", file=im.group("file"))
            out.append(
                _mk(
                    taxon,
                    "warning" if is_warn else "error",
                    "icarus",
                    msg,
                    file=im.group("file"),
                    line=int(im.group("line")),
                    raw=line,
                )
            )
            continue

        # 5) 无位置信息的显式错误行
        low = line.lower()
        if low.startswith("error") or " error:" in low or "fatal" in low:
            out.append(_mk(classify(line, tool=tool), "error", tool or "unknown", line.strip(), raw=line))

    # 去重：同类别+同消息+同行只保留一条
    seen = set()
    unique: List[Diagnostic] = []
    for d in out:
        key = (d.taxon, d.message[:80], d.file, d.line, d.cycle, d.signal)
        if key in seen:
            continue
        seen.add(key)
        unique.append(d)

    order = {"error": 0, "warning": 1, "info": 2}
    unique.sort(key=lambda d: (order.get(d.severity, 3), d.file, d.line))
    return unique


def summarize(diags: Sequence[Diagnostic]) -> Dict[str, object]:
    counts: Dict[str, int] = {}
    for d in diags:
        counts[d.taxon] = counts.get(d.taxon, 0) + 1
    return {
        "total": len(diags),
        "errors": sum(1 for d in diags if d.severity == "error"),
        "warnings": sum(1 for d in diags if d.severity == "warning"),
        "by_taxon": counts,
        "deterministic_fixable": sum(1 for d in diags if d.deterministic),
    }


def primary_taxon(diags: Sequence[Diagnostic]) -> str:
    """返回最需要优先修复的类别（错误优先，其次按出现顺序）。"""
    for d in diags:
        if d.severity == "error":
            return d.taxon
    return diags[0].taxon if diags else ""
