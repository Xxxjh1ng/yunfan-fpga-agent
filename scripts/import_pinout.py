"""从「管脚分配表」导入开发板引脚档案。

支持芯路恒/小梅哥常见的引脚表格式（xlsx / csv）：
    接口名称 | 外设端口 | FPGA管脚 | 方向 | 电平 | 备注
    系统时钟50MHz | FPGA_GCLK1 | Y18 | I | LVCMOS33 |
    LED | LED0 | M22 | O | LVCMOS33 |

用法
----
    # 只看解析结果，不写文件（推荐先跑这个核对）
    python scripts/import_pinout.py "C:\\fpga\\04_引脚功能信息\\ACG720管脚分配表.xlsx" --dry-run

    # 写入 boards.json 的 acg720 档案
    python scripts/import_pinout.py <xlsx> --board acg720 --write

注意：引脚表必须来自**你手上那块板**。ACX720 是 Xilinx Artix-7（管脚名如 Y18/M22），
ACG720 是高云 GW5AT-138（管脚名如 T14/R15），两者不能混用。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
BOARDS_PATH = ROOT / "yunfan" / "data" / "boards.json"

# 一行候选：外设端口名 + 管脚名
_PIN_RE = re.compile(r"^[A-Za-z]{1,3}\d{1,3}[A-Za-z]?(_[A-Za-z0-9]+)?$")


def read_rows(path: Path) -> List[Tuple[str, str, str]]:
    """返回 [(接口名称, 外设端口, 管脚)]，接口名称向下继承合并单元格。"""
    rows: List[Tuple[str, str, str]] = []
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise SystemExit("需要 openpyxl 才能读 xlsx：pip install openpyxl")
        wb = openpyxl.load_workbook(path, data_only=True)
        for ws in wb.worksheets:
            section = ""
            for row in ws.iter_rows(values_only=True):
                if not row or not any(row):
                    continue
                cells = [str(c).strip() if c is not None else "" for c in row[:6]]
                a, b, c = (cells + ["", "", ""])[:3]
                if a and not b and not c:
                    section = a  # 只用于分组显示
                    continue
                rows.append((section, b, c))
                if a and b:  # 兼容"接口名称与外设端口同一行"
                    rows[-1] = (a, b, c)
    else:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for rec in csv.reader(fh):
                if len(rec) >= 3:
                    rows.append((rec[0].strip(), rec[1].strip(), rec[2].strip()))
    return rows


def looks_like_pin(s: str) -> bool:
    return bool(s) and bool(_PIN_RE.match(s.strip()))


def extract(rows: List[Tuple[str, str, str]]) -> Dict[str, object]:
    """按语义抽取 clk / led[i] / key[i] / uart / 其他。"""
    clock: Optional[str] = None
    clock_label = ""
    leds: Dict[int, str] = {}
    keys: Dict[int, str] = {}
    uart: Dict[str, str] = {}
    other: List[Tuple[str, str]] = []

    for section, port, pin in rows:
        port_s, pin_s = port.strip(), pin.strip()
        # 有时两列写反（外设端口里放的是管脚名）
        if looks_like_pin(port_s) and not looks_like_pin(pin_s) and pin_s:
            port_s, pin_s = pin_s, port_s
        if not port_s or not looks_like_pin(pin_s):
            continue
        up = (section + " " + port_s).upper()

        if clock is None and ("CLK" in up or "时钟" in section) and ("GCLK" in up or "50M" in up or "CLK" in up):
            clock, clock_label = pin_s, f"{section}/{port_s}".strip("/")
            continue

        m = re.search(r"LED\s*[_]?(\d+)", up)
        if m:
            leds.setdefault(int(m.group(1)), pin_s)
            continue

        m = re.search(r"(?:KEY|BTN|BUTTON)\s*[_]?(\d+)", up)
        if m:
            keys.setdefault(int(m.group(1)), pin_s)
            continue

        if re.search(r"UART", up) or re.search(r"\b(TXD|RXD)\b", up):
            if re.search(r"TXD|TX\b|_TX", up):
                uart.setdefault("uart_tx", pin_s)
            elif re.search(r"RXD|RX\b|_RX", up):
                uart.setdefault("uart_rx", pin_s)
            continue

        other.append((f"{section}/{port_s}".strip("/"), pin_s))

    pins: Dict[str, str] = {}
    if clock:
        pins["clk"] = clock
    for i in sorted(leds):
        pins[f"led[{i}]"] = leds[i]
    for i in sorted(keys):
        pins[f"key[{i}]"] = keys[i]
    pins.update(uart)

    return {
        "clock_pin": clock or "",
        "clock_label": clock_label,
        "reset_pin": keys.get(0, "") if keys else "",
        "pins": pins,
        "leds": leds,
        "keys": keys,
        "uart": uart,
        "other_count": len(other),
        "other_sample": other[:8],
    }


def render_cst(info: Dict[str, object], io: str = "LVCMOS33") -> str:
    lines = ["// 由 import_pinout.py 从管脚分配表生成"]
    if info["pins"].get("clk"):
        lines += [
            f'IO_LOC "clk" {info["pins"]["clk"]};',
            f'IO_PORT "clk" IO_TYPE={io} PULL_MODE=NONE BANK_VCCIO=3.3;',
        ]
    for key in sorted(k for k in info["pins"] if k.startswith("led[")):
        lines += [
            f'IO_LOC "{key}" {info["pins"][key]};',
            f'IO_PORT "{key}" IO_TYPE={io} DRIVE=8 PULL_MODE=NONE BANK_VCCIO=3.3;',
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="从管脚分配表导入开发板引脚")
    ap.add_argument("table", help="管脚分配表 xlsx/csv 路径")
    ap.add_argument("--board", default="acg720", help="写入 boards.json 的板卡名")
    ap.add_argument("--write", action="store_true", help="写回 boards.json（默认只预览）")
    ap.add_argument("--dry-run", action="store_true", help="仅预览（默认行为）")
    args = ap.parse_args()

    path = Path(args.table)
    if not path.exists():
        print(f"找不到文件：{path}")
        return 2

    rows = read_rows(path)
    info = extract(rows)
    print("=" * 66)
    print(f"引脚表：{path.name}（解析 {len(rows)} 行）")
    print("=" * 66)
    if not info["pins"]:
        print("✗ 没有解析到 clk/led/key 引脚，表结构可能不同，请把表头发我。")
        return 1

    print(f"时钟 ：{info['pins'].get('clk', '未找到')}   （来源 {info['clock_label'] or '-'}）")
    for k in sorted(k for k in info["pins"] if k.startswith("led[")):
        print(f"{k:<8}{info['pins'][k]}")
    for k in sorted(k for k in info["pins"] if k.startswith("key[")):
        print(f"{k:<8}{info['pins'][k]}")
    for k in ("uart_tx", "uart_rx"):
        if k in info["pins"]:
            print(f"{k:<8}{info['pins'][k]}")
    print(f"其它信号：{info['other_count']} 条（未纳入档案）")

    print("\n生成的 .cst 预览：")
    print(render_cst(info))

    # 判断是否像是 Xilinx 板（管脚名风格：纯字母+2位数字，如 Y18 / M22）
    # 仅凭管脚名无法区分厂商：高云 GW5AT-138 的 PG484 封装同样是"字母+数字"（如 Y18/M22）。
    # 因此只在表名里出现 ACX / Xilinx 时提示，避免把正确的表误判成 Xilinx 版。
    fname = path.name.lower()
    if "acx" in fname or "xilinx" in fname:
        print("⚠ 表名含 ACX/Xilinx：若你的板是 ACG720（高云），这份表可能来自 Xilinx 版，注意核对。")

    if not args.write:
        print("\n（预览模式，未写入 boards.json；确认无误后加 --write）")
        return 0

    boards = json.loads(BOARDS_PATH.read_text(encoding="utf-8"))
    for b in boards["boards"]:
        if b["name"] == args.board:
            b["clock_pin"] = info["pins"].get("clk", b.get("clock_pin", ""))
            b["reset_pin"] = info["reset_pin"] or b.get("reset_pin", "")
            merged = dict(b.get("pins") or {})
            merged.update(info["pins"])
            b["pins"] = merged
            b["notes"] = (b.get("notes", "") + " 引脚由 import_pinout.py 从 " + path.name + " 导入。").strip()
            break
    else:
        print(f"\n✗ boards.json 里没有 {args.board}，请先在 boards.json 添加板卡条目。")
        return 1

    BOARDS_PATH.write_text(json.dumps(boards, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n✓ 已写入 boards.json：{args.board}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
