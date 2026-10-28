"""物理约束（.cst）与时序约束（.sdc）生成。

两条路径：
1. **语义映射**：按端口名的工程语义匹配开发板资源（clk/led/uart/seg/key…），
   这是最可靠的方式，且换板卡只改 boards.json；
2. **自动分配**：对无法语义匹配的端口，从器件封装库中挑选空闲 IO，
   保证「任何端口都能落到合法 pad」，避免 CT1113 这类错误。

封装引脚库直接读云源安装目录下的 ``IDE/data/device/<器件>/<封装>.json``，
因此引脚合法性由工具链自己的数据库保证，而不是靠猜。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..config import BoardProfile, DeviceProfile

# 端口名 -> 开发板资源键
_SEMANTIC: List[Tuple[str, str]] = [
    (r"^(clk|clock|clk_i|sys_clk|clk_in)$", "clk"),
    (r"^(rst|rst_n|reset|reset_n|nrst|sys_rst)$", "rst_n"),
    (r"^(led|led_o|leds?)(\[(\d+)\])?$", "led"),
    (r"^(key|btn|button|key_n|keys?)(\[(\d+)\])?$", "key"),
    (r"^(uart_tx|tx|txd|uart_txd)$", "uart_tx"),
    (r"^(uart_rx|rx|rxd|uart_rxd)$", "uart_rx"),
    (r"^(seg|seg_o|seven_seg)(\[(\d+)\])?$", "seg"),
    (r"^(buzzer|beep|buzz)$", "buzzer"),
]


@dataclass
class PinAssignment:
    port: str
    pin: str
    io_type: str = "LVCMOS33"
    source: str = "board"  # board / auto / manual
    note: str = ""


@dataclass
class ConstraintResult:
    cst: str = ""
    sdc: str = ""
    assignments: List[PinAssignment] = field(default_factory=list)
    unmapped: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "assignments": [
                {"port": a.port, "pin": a.pin, "io_type": a.io_type, "source": a.source, "note": a.note}
                for a in self.assignments
            ],
            "unmapped": self.unmapped,
            "warnings": self.warnings,
        }

    @property
    def coverage(self) -> float:
        total = len(self.assignments) + len(self.unmapped)
        return round(len(self.assignments) / total, 4) if total else 1.0


# --------------------------------------------------------------------------
# 器件封装引脚库
# --------------------------------------------------------------------------
_PIN_CACHE: Dict[str, List[Dict[str, str]]] = {}


def load_package_pins(device: Optional[DeviceProfile], gowin_root: Optional[str]) -> Optional[List[Dict[str, str]]]:
    """读取封装库中的合法 IO 引脚；不可用时返回 None。"""
    if not device or not gowin_root:
        return None
    key = f"{device.package}"
    if key in _PIN_CACHE:
        return _PIN_CACHE[key]
    candidate = Path(gowin_root) / "IDE" / "data" / "device" / device.name / f"{device.package}.json"
    if not candidate.is_file():
        return None
    try:
        data = json.loads(candidate.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    pins: List[Dict[str, str]] = []
    for item in data.get("PIN_DATA") or []:
        if item.get("TYPE") != "I/O":
            continue
        index = str(item.get("INDEX") or "")
        cfg = str(item.get("CFG") or "").strip()
        # 只保留「纯普通 IO」与「纯全局时钟」两类引脚：
        # 其余带复用功能的引脚（JTAG/SSPI/I2C/CPU/MIPI…）在布局布线上可能被
        # 判为 dedicated pin，从而触发 PR2017/PR2028 错误。
        is_gclk = bool(re.fullmatch(r"GCLK[TC]_\d+[A-Z]?", cfg))
        if cfg and not is_gclk:
            continue
        pins.append(
            {
                "index": index,
                "name": str(item.get("NAME") or ""),
                "bank": str(item.get("BANK") or ""),
                "cfg": cfg,
                "is_gclk": is_gclk,
            }
        )
    _PIN_CACHE[key] = pins
    return pins


def _io_type(board: Optional[BoardProfile], portable: bool = True) -> str:
    return board.io_standard if board else "LVCMOS33"


# --------------------------------------------------------------------------
# 端口展开
# --------------------------------------------------------------------------
def expand_ports(spec_ports: Sequence[dict]) -> List[Tuple[str, int, str]]:
    """把端口展开为 (名字, 位宽, 方向)；向量不展开位，映射时按位处理。"""
    out = []
    for p in spec_ports:
        try:
            width = int(p.get("width") or 1)
        except Exception:
            width = 1
        out.append((str(p.get("name")), width, str(p.get("dir") or "input")))
    return out


def _bits(name: str, width: int) -> List[str]:
    if width <= 1:
        return [name]
    return [f"{name}[{i}]" for i in range(width)]


def _match_semantic(bit_name: str, board: BoardProfile) -> Optional[str]:
    base = re.sub(r"\[\d+\]$", "", bit_name)
    idx = None
    m = re.search(r"\[(\d+)\]$", bit_name)
    if m:
        idx = int(m.group(1))
    for pattern, key in _SEMANTIC:
        if not re.match(pattern, base, re.IGNORECASE):
            continue
        if key in ("clk", "rst_n", "uart_tx", "uart_rx", "buzzer"):
            candidate = board.pins.get(key) or (
                board.clock_pin if key == "clk" else board.reset_pin if key == "rst_n" else None
            )
            if candidate and idx is None:
                return candidate
            continue
        if idx is not None:
            candidate = board.pins.get(f"{key}[{idx}]")
            if candidate:
                return candidate
        # 单 bit 的 led / key（无下标）
        if idx is None:
            candidate = board.pins.get(f"{key}[0]")
            if candidate:
                return candidate
    return None


def build_constraints(
    spec: dict,
    board: Optional[BoardProfile],
    device: Optional[DeviceProfile],
    gowin_root: Optional[str] = None,
    *,
    auto_assign: bool = True,
) -> ConstraintResult:
    """生成 .cst 与 .sdc。"""
    res = ConstraintResult()
    ports = expand_ports(spec.get("ports") or [])
    if not ports:
        res.warnings.append("规格中没有端口，无法生成约束")
        return res

    used: set = set()
    pins_db = load_package_pins(device, gowin_root)
    io_type = _io_type(board)

    # 1) 语义映射
    for name, width, direction in ports:
        if direction == "inout":
            res.warnings.append(f"端口 {name} 为 inout，已按普通 IO 处理，请人工确认三态需求")
        for bit in _bits(name, width):
            pin = _match_semantic(bit, board) if board else None
            if pin and pin not in used:
                res.assignments.append(
                    PinAssignment(port=bit, pin=pin, io_type=io_type, source="board", note="按端口语义匹配板载资源")
                )
                used.add(pin)
            elif pin and pin in used:
                res.warnings.append(f"{bit} 语义匹配到已被占用的引脚 {pin}，转入自动分配")
                res.unmapped.append(bit)
            else:
                res.unmapped.append(bit)

    # 2) 自动分配空闲 IO
    if auto_assign and res.unmapped and pins_db:
        free = [p for p in pins_db if p["index"] not in used]
        # 时钟优先给 GCLK 引脚
        for bit in list(res.unmapped):
            need_gclk = bool(re.match(r"^(clk|clock)$", re.sub(r"\[\d+\]$", "", bit), re.IGNORECASE))
            pool = [p for p in free if p["is_gclk"]] if need_gclk else free
            if not pool:
                continue
            chosen = pool[0]
            free.remove(chosen)
            res.assignments.append(
                PinAssignment(port=bit, pin=chosen["index"], io_type=io_type, source="auto", note=f"自动分配（{chosen['name']} bank{chosen['bank']}）")
            )
            used.add(chosen["index"])
            res.unmapped.remove(bit)
    elif res.unmapped and not pins_db:
        res.warnings.append("未找到器件封装库，无法自动分配引脚；请手工在 boards.json 中补充")

    # 3) 渲染 CST
    lines = [
        f"// 由 YunFan 部署智能体生成：{spec.get('name', '')} -> {board.name if board else 'unknown board'}",
        f"// 映射覆盖率 {len(res.assignments)}/{len(res.assignments) + len(res.unmapped)}",
    ]
    for a in res.assignments:
        lines.append(f'IO_LOC "{a.port}" {a.pin};')
        pull = "UP" if re.match(r"^(rst|key|btn)", re.sub(r"\[\d+\]$", "", a.port), re.IGNORECASE) else "NONE"
        drive = " DRIVE=8" if re.match(r"^(led|seg|uart_tx)", re.sub(r"\[\d+\]$", "", a.port), re.IGNORECASE) else ""
        lines.append(f'IO_PORT "{a.port}" IO_TYPE={a.io_type}{drive} PULL_MODE={pull} BANK_VCCIO=3.3;')
    res.cst = "\n".join(lines) + "\n"

    # 4) 渲染 SDC（单时钟域）
    clock_name = str((spec.get("clock") or {}).get("name") or "clk")
    freq = float((spec.get("clock") or {}).get("freq_mhz") or (board.clock_mhz if board else 50.0))
    period = 1000.0 / max(0.1, freq)
    half = period / 2.0
    res.sdc = (
        f"// 由 YunFan 部署智能体生成：{freq:g} MHz 单时钟域约束\n"
        f"create_clock -name {clock_name} -period {period:.3f} -waveform {{0 {half:.3f}}} [get_ports {{{clock_name}}}]\n"
    )
    return res


# --------------------------------------------------------------------------
# E10 修复：把非法 pad 重新映射到合法 pad
# --------------------------------------------------------------------------
_RE_IO_LOC = re.compile(r'IO_LOC\s+"([^"]+)"\s+([A-Za-z0-9_]+)\s*;', re.IGNORECASE)


def remap_invalid_pins(cst_text: str, device: Optional[DeviceProfile], gowin_root: Optional[str]) -> Tuple[str, List[str]]:
    """把器件封装库中不存在的 pad 换成空闲合法 pad。返回 (新文本, 变更说明)。"""
    pins_db = load_package_pins(device, gowin_root)
    if not pins_db:
        return cst_text, []
    valid = {p["index"] for p in pins_db}
    used = {m.group(2) for m in _RE_IO_LOC.finditer(cst_text)}
    free = [p["index"] for p in pins_db if p["index"] not in used]
    changes: List[str] = []
    out_lines = []
    for line in cst_text.split("\n"):
        m = _RE_IO_LOC.search(line)
        if m and m.group(2) not in valid:
            if not free:
                out_lines.append(line)
                continue
            new_pin = free.pop(0)
            changes.append(f"{m.group(1)}: {m.group(2)} -> {new_pin}（原 pad 不在封装库中）")
            line = _RE_IO_LOC.sub(f'IO_LOC "{m.group(1)}" {new_pin};', line)
        out_lines.append(line)
    return "\n".join(out_lines), changes
