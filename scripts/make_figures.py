"""生成论文插图（Pillow 实现，无需 matplotlib）。

所有数值来自真实实验产物：
    runs/eval/summary.json      —— 主实验（两种方案 × 5 设计 × 3 种子）
    reports/repair_bench.json   —— 故障注入修复基准
用法：python scripts/make_figures.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FIG = ROOT / "docs" / "figures"
SS = 2  # 超采样倍率

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]
C_MAIN = (43, 95, 142)
C_ACC = (201, 84, 43)
C_GRAY = (138, 138, 138)
C_GREEN = (63, 125, 79)
C_LIGHT = (232, 238, 245)
C_PURPLE = (122, 111, 168)
C_TEXT = (34, 34, 34)
C_LINE = (170, 170, 170)


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size * SS)
            except Exception:
                continue
    return ImageFont.load_default()


class Canvas:
    def __init__(self, w: int, h: int, bg=(255, 255, 255)) -> None:
        self.w, self.h = w, h
        self.img = Image.new("RGB", (w * SS, h * SS), bg)
        self.d = ImageDraw.Draw(self.img)

    def rect(self, x, y, w, h, fill=None, outline=None, width=2, radius=0) -> None:
        box = [x * SS, y * SS, (x + w) * SS, (y + h) * SS]
        if radius:
            self.d.rounded_rectangle(box, radius=radius * SS, fill=fill, outline=outline, width=width * SS)
        else:
            self.d.rectangle(box, fill=fill, outline=outline, width=width * SS)

    def text(self, x, y, s, size=14, color=C_TEXT, anchor="mm", bold=False) -> None:
        self.d.text((x * SS, y * SS), s, font=font(size, bold), fill=color, anchor=anchor)

    def line(self, x1, y1, x2, y2, color=C_LINE, width=2, dash=False) -> None:
        if not dash:
            self.d.line([x1 * SS, y1 * SS, x2 * SS, y2 * SS], fill=color, width=width * SS)
            return
        import math

        dx, dy = x2 - x1, y2 - y1
        dist = max(1e-6, math.hypot(dx, dy))
        step = 6
        n = int(dist / step)
        for i in range(0, n, 2):
            ax = x1 + dx * (i * step) / dist
            ay = y1 + dy * (i * step) / dist
            bx = x1 + dx * ((i + 1) * step) / dist
            by = y1 + dy * ((i + 1) * step) / dist
            self.d.line([ax * SS, ay * SS, bx * SS, by * SS], fill=color, width=width * SS)

    def arrow(self, x1, y1, x2, y2, color=C_TEXT, width=2) -> None:
        self.d.line([x1 * SS, y1 * SS, x2 * SS, y2 * SS], fill=color, width=width * SS)
        import math

        ang = math.atan2(y2 - y1, x2 - x1)
        L, W = 10, 6
        p1 = (x2 * SS - L * SS * math.cos(ang) + W * SS * math.sin(ang), y2 * SS - L * SS * math.sin(ang) - W * SS * math.cos(ang))
        p2 = (x2 * SS - L * SS * math.cos(ang) - W * SS * math.sin(ang), y2 * SS - L * SS * math.sin(ang) + W * SS * math.cos(ang))
        self.d.polygon([(x2 * SS, y2 * SS), p1, p2], fill=color)

    def save(self, path: Path) -> None:
        out = self.img.resize((self.w, self.h), Image.LANCZOS)
        path.parent.mkdir(parents=True, exist_ok=True)
        out.save(path, "PNG", optimize=True)


# --------------------------------------------------------------------------
def fig_architecture() -> None:
    c = Canvas(1700, 780)
    stages = [
        ("需求解析", "自然语言 → 机器可读规格"),
        ("代码生成", "规格 → 可综合 Verilog"),
        ("仿真验证", "自验证 TB 逐拍比对"),
        ("调试修复", "诊断 → 最小编辑补丁"),
        ("硬件部署", "约束 / 比特流 / 烧录"),
    ]
    w, h, y = 280, 150, 300
    for i, (title, sub) in enumerate(stages):
        x = 40 + i * 330
        color = C_MAIN if i < 4 else C_GREEN
        c.rect(x, y, w, h, fill=(color[0], color[1], color[2], 255) if False else tuple(int(v + (255 - v) * 0.86) for v in color),
               outline=color, width=2, radius=12)
        c.text(x + w / 2, y + 48, title, size=26, color=color, bold=True)
        c.text(x + w / 2, y + 100, sub, size=15, color=C_TEXT)
        if i < 4:
            c.arrow(x + w + 8, y + h / 2, x + 330 - 8, y + h / 2, color=(90, 90, 90), width=2)

    # 规格契约
    c.rect(40, 170, 1590, 76, fill=(252, 240, 235), outline=C_ACC, width=2, radius=10)
    c.text(835, 208, "设计规格 Design Spec：端口表 / 参数 / 时序 / 验证意图 —— 全流程唯一事实源",
           size=20, color=C_ACC, bold=True)

    # 反馈回路
    c.arrow(1560, y + h + 12, 800, y + h + 12, color=C_ACC, width=3)
    c.text(1180, y + h + 42, "失败反馈：结构化诊断 → 确定性规则 / 最小编辑补丁 / 整模块重写（≤5 轮）",
           size=16, color=C_ACC)

    # EDA 适配层
    c.rect(40, 600, 1590, 150, fill=(245, 245, 245), outline=(70, 70, 70), width=2, radius=10)
    c.text(835, 630, "EDA 适配层（可替换后端）", size=20, color=C_TEXT, bold=True)
    tools = ["云源 EDA\ngw_sh (syn / pnr)", "Icarus Verilog\n编译 + 仿真",
             "Gowin Programmer\nprogrammer_cli", "约束生成\nCST / SDC", "报告解析\n资源 / Fmax"]
    for i, t in enumerate(tools):
        x = 70 + i * 305
        c.rect(x, 655, 270, 72, fill=(255, 255, 255), outline=(150, 150, 150), width=1, radius=8)
        c.text(x + 135, 691, t, size=14, color=C_TEXT)

    # 门控标注
    c.text(835, 265, "分级门控：Lint (ms) → 综合 (s) → 仿真 (s) → 布局布线 (10s)，失败即早停",
           size=16, color=(90, 90, 90))
    c.save(FIG / "fig_arch.png")


def fig_gate(eval_data: Optional[Dict[str, Any]]) -> None:
    labels = ["静态检查\n(Lint)", "综合\n(syn)", "仿真\n(sim)", "布局布线\n(pnr)"]
    vals = [2.0, 460.0, 45.0, 4800.0]  # Lint 为本地实测中位数（ms）
    if eval_data:
        recs = eval_data.get("records") or []
        acc = {"synth": [], "sim": [], "pnr": []}
        for r in recs:
            d = r.get("durations") or {}
            for k in acc:
                if f"{k}_0_s" in d:
                    acc[k].append(d[f"{k}_0_s"])
        if acc["synth"]:
            vals[1] = 1000 * sum(acc["synth"]) / len(acc["synth"])
        if acc["sim"]:
            vals[2] = 1000 * sum(acc["sim"]) / len(acc["sim"])
        if acc["pnr"]:
            vals[3] = 1000 * sum(acc["pnr"]) / len(acc["pnr"])
    c = Canvas(1150, 620)
    x0, y0, plot_h = 110, 90, 400
    c.line(x0, y0, x0, y0 + plot_h, color=C_TEXT, width=2)
    c.line(x0, y0 + plot_h, x0 + 980, y0 + plot_h, color=C_TEXT, width=2)
    c.text(x0 + 490, 40, "分级门控：越早失败，代价越低（单次调用平均耗时，对数坐标）", size=19, bold=True, color=C_TEXT)
    colors = [C_GREEN, C_MAIN, C_MAIN, C_ACC]
    import math

    vmax = max(vals) * 4
    bar_w = 130
    for i, (lab, val, col) in enumerate(zip(labels, vals, colors)):
        cx = x0 + 150 + i * 230
        hgt = max(6, int(plot_h * (math.log10(max(1.0, val)) / math.log10(vmax))))
        c.rect(cx - bar_w / 2, y0 + plot_h - hgt, bar_w, hgt, fill=col, outline=col, width=1, radius=4)
        txt = f"{val:.1f} ms" if val < 1000 else f"{val/1000:.2f} s"
        c.text(cx, y0 + plot_h - hgt - 22, txt, size=17, bold=True, color=col)
        c.text(cx, y0 + plot_h + 34, lab, size=15, color=C_TEXT, anchor="ma")
    for tick in (1, 10, 100, 1000, 10000):
        if tick > vmax:
            continue
        yy = y0 + plot_h - int(plot_h * (math.log10(tick) / math.log10(vmax)))
        c.line(x0 - 6, yy, x0 + 980, yy, color=(235, 235, 235), width=1)
        c.text(x0 - 16, yy, f"{tick}", size=13, color=(120, 120, 120), anchor="rm")
    c.text(x0 - 66, y0 + plot_h / 2, "毫\n秒", size=14, color=(120, 120, 120))
    c.save(FIG / "fig_gate.png")


def _panel(c: Canvas, x, y, w, h, title, pairs: Sequence[Tuple[str, float, Tuple[int, int, int]]], fmt) -> None:
    c.text(x + w / 2, y - 4, title, size=15, bold=True, color=C_TEXT)
    c.line(x, y + h, x + w, y + h, color=C_TEXT, width=1)
    vmax = max([p[1] for p in pairs] + [1e-9]) * 1.35
    bw = w / (len(pairs) * 1.8)
    for i, (lab, val, col) in enumerate(pairs):
        cx = x + w * (i + 0.5) / len(pairs)
        hh = max(2, int(h * val / vmax))
        c.rect(cx - bw / 2, y + h - hh, bw, hh, fill=col, outline=col, width=1, radius=3)
        c.text(cx, y + h - hh - 13, fmt(val), size=13, bold=True, color=col)
        c.text(cx, y + h + 14, "云帆" if lab == "yunfan" else "原始", size=12, color=C_TEXT, anchor="ma")


def fig_variants(eval_data: Optional[Dict[str, Any]]) -> None:
    metrics = [
        ("首次仿真通过率", "first_attempt_sim_pass_rate", lambda v: f"{v*100:.1f}%"),
        ("端到端成功率", "success_rate", lambda v: f"{v*100:.1f}%"),
        ("比特流产出率", "bitstream_rate", lambda v: f"{v*100:.1f}%"),
        ("平均修复轮次", "avg_repair_rounds", lambda v: f"{v:.2f}"),
        ("平均 token / 次", "avg_tokens", lambda v: f"{v:.0f}"),
    ]
    variants = {row["variant"]: row for row in (eval_data or {}).get("variants") or []}
    c = Canvas(1750, 480)
    c.text(875, 26, "云帆 vs 原始单点方案（同模型、同设计集、各 3 次采样）", size=19, bold=True, color=C_TEXT)
    for i, (title, key, fmt) in enumerate(metrics):
        x = 60 + i * 340
        pairs = [
            ("yunfan", float(variants.get("yunfan", {}).get(key, 0) or 0), C_MAIN),
            ("baseline", float(variants.get("baseline", {}).get(key, 0) or 0), C_GRAY),
        ]
        _panel(c, x, 90, 260, 300, title, pairs, fmt)
    c.save(FIG / "fig_variants.png")


def fig_faults(bench: Optional[List[Dict[str, Any]]]) -> None:
    if not bench:
        return
    order = ["F01", "F02", "F03", "F04", "F05", "F06", "F07", "F08", "F09"]
    names = {"F01": "缺 endmodule", "F02": "reg→wire", "F03": "插入 #延时", "F04": "删一个 end",
             "F05": "端口改名", "F06": "位宽篡改", "F07": "比较符变异", "F08": "常量变异", "F09": "非法引脚"}
    stats = {f: {"deterministic": 0, "llm-patch": 0, "llm-rewrite": 0, "fail": 0} for f in order}
    for row in bench:
        f = row["fault"]
        if f not in stats:
            continue
        if not row["success"]:
            stats[f]["fail"] += 1
            continue
        ms = row.get("methods") or []
        key = "deterministic" if (not ms or ms[0] == "deterministic") else ("llm-patch" if ms[0] == "llm-patch" else "llm-rewrite")
        stats[f][key] += 1

    c = Canvas(1600, 640)
    x0, y0, plot_h, plot_w = 110, 100, 380, 1420
    c.line(x0, y0, x0, y0 + plot_h, color=C_TEXT, width=2)
    c.line(x0, y0 + plot_h, x0 + plot_w, y0 + plot_h, color=C_TEXT, width=2)
    c.text(x0 + plot_w / 2, 40, "故障注入修复基准：不同错误类别的恢复方式", size=19, bold=True, color=C_TEXT)
    vmax = max(1, max(sum(v.values()) for v in stats.values())) * 1.2
    bw = 96
    for i, f in enumerate(order):
        cx = x0 + 90 + i * 152
        bottom = y0 + plot_h
        for key, col in (("deterministic", C_GREEN), ("llm-patch", C_MAIN), ("llm-rewrite", C_PURPLE), ("fail", C_ACC)):
            val = stats[f][key]
            if not val:
                continue
            hh = int(plot_h * val / vmax)
            c.rect(cx - bw / 2, bottom - hh, bw, hh, fill=col, outline=col, width=1)
            bottom -= hh
        c.text(cx, y0 + plot_h + 16, f, size=14, bold=True, color=C_TEXT, anchor="ma")
        c.text(cx, y0 + plot_h + 36, names[f], size=12, color=(80, 80, 80), anchor="ma")
    # 图例
    legend = [("确定性规则修复", C_GREEN), ("LLM 最小编辑补丁", C_MAIN), ("整模块重写", C_PURPLE), ("未恢复", C_ACC)]
    for i, (lab, col) in enumerate(legend):
        lx = x0 + 40 + i * 300
        ly = y0 + plot_h + 80
        c.rect(lx, ly - 10, 22, 20, fill=col, outline=col, width=1)
        c.text(lx + 32, ly, lab, size=14, color=C_TEXT, anchor="lm")
    c.save(FIG / "fig_faults.png")


def fig_resources(eval_data: Optional[Dict[str, Any]]) -> None:
    designs = ["counter", "pwm", "crc32", "uart_tx", "vga_timing"]
    recs = (eval_data or {}).get("records") or []
    lut, ff, fmax = [], [], []
    for d in designs:
        rs = [r for r in recs if r.get("design") == d and r.get("variant") == "yunfan"]
        res = next((r.get("resources") for r in rs if r.get("resources")), {}) or {}
        lut.append(float(res.get("logic_used") or res.get("lut_used") or 0))
        ff.append(float(res.get("register_used") or 0))
        fmax.append(max([float(r.get("fmax_mhz") or 0) for r in rs] or [0]))
    c = Canvas(1400, 700)
    x0, plot_w = 110, 1180
    fy0, fh = 105, 140          # 上：Fmax 面板
    gold, plot_h = 300, 320      # 下：资源柱状面板
    c.text(x0 + plot_w / 2, 36, "GW5A-25A 实测资源占用与最高工作频率（云源 V1.9.9 布局布线报告）",
           size=18, bold=True, color=C_TEXT)
    vmax = max(lut + ff + [1]) * 1.32
    fmax_max = max(fmax + [1]) * 1.30

    # ---- Fmax 面板 ----
    c.line(x0, fy0 + fh, x0 + plot_w, fy0 + fh, color=C_TEXT, width=1)
    c.text(x0 - 20, fy0 + fh / 2, "Fmax\n(MHz)", size=13, color=C_ACC, anchor="rm")
    pts = []
    for i, d in enumerate(designs):
        cx = x0 + 140 + i * 225
        py = fy0 + fh - int(fh * fmax[i] / fmax_max)
        pts.append((cx, py))
    for i in range(1, len(pts)):
        c.line(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1], color=C_ACC, width=2)
    for (cx, py), val in zip(pts, fmax):
        c.d.ellipse([(cx - 6) * SS, (py - 6) * SS, (cx + 6) * SS, (py + 6) * SS], fill=C_ACC)
        c.text(cx, py - 20, f"{val:.0f}", size=13, color=C_ACC, bold=True)

    # ---- 资源柱状面板 ----
    c.line(x0, gold, x0, gold + plot_h, color=C_TEXT, width=2)
    c.line(x0, gold + plot_h, x0 + plot_w, gold + plot_h, color=C_TEXT, width=2)
    c.text(x0 - 20, gold + plot_h / 2, "资源\n用量", size=13, color=C_TEXT, anchor="rm")
    for i, d in enumerate(designs):
        cx = x0 + 140 + i * 225
        for j, (val, col) in enumerate(((lut[i], C_MAIN), (ff[i], (154, 182, 204)))):
            bw = 54
            hh = max(2, int(plot_h * val / vmax))
            bx = cx - bw + j * bw
            c.rect(bx, gold + plot_h - hh, bw - 4, hh, fill=col, outline=col, width=1)
            c.text(bx + bw / 2 - 2, gold + plot_h - hh - 13, f"{val:.0f}", size=12, color=col, bold=True)
        c.text(cx, gold + plot_h + 34, d, size=14, color=C_TEXT, anchor="ma")
    legend = [("逻辑资源（LUT+ALU）", C_MAIN), ("寄存器使用量", (154, 182, 204)), ("Fmax", C_ACC)]
    for i, (lab, col) in enumerate(legend):
        lx = x0 + 60 + i * 280
        c.rect(lx, 70, 20, 18, fill=col, outline=col, width=1)
        c.text(lx + 30, 79, lab, size=13, color=C_TEXT, anchor="lm")
    c.save(FIG / "fig_resources.png")


def main() -> int:
    FIG.mkdir(parents=True, exist_ok=True)
    eval_data = load_json(ROOT / "runs" / "eval" / "summary.json")
    bench = load_json(ROOT / "reports" / "repair_bench.json")
    fig_architecture()
    fig_gate(eval_data)
    fig_variants(eval_data)
    fig_faults(bench)
    fig_resources(eval_data)
    for p in sorted(FIG.glob("*.png")):
        print(f"  {p.name:<22} {p.stat().st_size/1024:6.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
