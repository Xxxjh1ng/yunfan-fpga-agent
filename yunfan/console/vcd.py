"""极简 VCD（Value Change Dump）解析器：把 iverilog 的波形转成前端能画的结构。

只做「够看波形」这件事：不处理 $dumpoff/$dumpon 之外的高级语义，
向量统一按二进制字符串返回，时间轴取整数 tick。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

MAX_BYTES = 12_000_000
MAX_SIGNALS = 60
MAX_CHANGES = 3000


def _pretty_scope(scope: List[str], name: str) -> str:
    return ".".join([*scope, name]) if scope else name


def parse_vcd(path: Path, *, max_signals: int = MAX_SIGNALS, max_changes: int = MAX_CHANGES) -> dict:
    raw = Path(path).read_bytes()[:MAX_BYTES]
    text = raw.decode("utf-8", errors="replace")
    tokens = text.split()

    scope: List[str] = []
    vardefs: Dict[str, dict] = {}
    changes: Dict[str, List[Tuple[int, str]]] = {}
    timescale = ""
    now = 0
    i = 0
    n = len(tokens)
    skip_depth = 0

    while i < n:
        tok = tokens[i]

        if tok == "$timescale":
            i += 1
            buf = []
            while i < n and tokens[i] != "$end":
                buf.append(tokens[i])
                i += 1
            timescale = " ".join(buf)
            i += 1
            continue

        if tok == "$scope":
            # $scope module <name> $end
            if i + 2 < n:
                scope.append(tokens[i + 2])
            i += 4
            continue

        if tok == "$upscope":
            if scope:
                scope.pop()
            i += 2
            continue

        if tok == "$var":
            i += 1
            j = i
            while j < n and tokens[j] != "$end":
                j += 1
            body = tokens[i:j]
            if len(body) >= 4:
                width, ident, name = body[1], body[2], body[3]
                try:
                    width_i = int(width)
                except ValueError:
                    width_i = 1
                # 只保留顶层可见的信号，避免把 testbench 内部全展开
                vardefs[ident] = {
                    "name": name,
                    "scope": _pretty_scope(scope, "").strip("."),
                    "width": width_i,
                }
            i = j + 1
            continue

        if tok in ("$dumpvars", "$dumpall", "$dumpon", "$dumpoff"):
            skip_depth += 1
            i += 1
            continue

        if tok == "$end":
            if skip_depth:
                skip_depth -= 1
            i += 1
            continue

        if tok.startswith("$"):
            # 其他 $xxx 关键字，跳到 $end
            while i < n and tokens[i] != "$end":
                i += 1
            i += 1
            continue

        if tok.startswith("#"):
            try:
                now = int(tok[1:])
            except ValueError:
                pass
            i += 1
            continue

        # 值变化
        ident = ""
        value = ""
        if tok[0] == "b" and len(tok) > 1:
            value = tok[1:]
            ident = tokens[i + 1] if i + 1 < n else ""
            i += 2
        elif tok[0] == "r" and len(tok) > 1:
            value = tok[1:]
            ident = tokens[i + 1] if i + 1 < n else ""
            i += 2
        elif tok[0] in "01xzXZ" and len(tok) > 1:
            value = tok[0].lower()
            ident = tok[1:]
            i += 1
        else:
            i += 1
            continue

        if ident not in vardefs:
            continue
        series = changes.setdefault(ident, [])
        if series and series[-1][1] == value:
            continue
        series.append((now, value))

    signals = []
    for ident, meta in vardefs.items():
        series = changes.get(ident) or []
        if not series:
            continue
        if len(series) > max_changes:  # 均匀抽样，保住首尾
            step = len(series) / max_changes
            sampled = [series[int(k * step)] for k in range(max_changes)]
            sampled[-1] = series[-1]
            series = sampled
        name = f"{meta['scope']}.{meta['name']}" if meta["scope"] else meta["name"]
        signals.append({
            "name": name,
            "width": meta["width"],
            "changes": [[t, v] for t, v in series],
        })
        if len(signals) >= max_signals:
            break

    # 时钟类的放前面，方便一眼看节拍
    signals.sort(key=lambda s: (len(s["name"]), s["name"]))
    tmax = max((s["changes"][-1][0] for s in signals), default=0)
    return {"timescale": timescale or "1ns", "tmax": tmax, "signals": signals}
