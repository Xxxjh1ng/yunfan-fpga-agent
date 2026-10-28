"""参考模型（Golden Model）与测试计划合成。

每个 oracle 都是**周期精确**的行为模型：给定某一拍生效的输入，返回该拍
时钟沿之后的输出。它同时服务于三件事：

1. 为自验证 Testbench 提供期望值（自验证激励的判据来源）；
2. 作为论文实验中的「金标准」，与模型生成的 RTL 逐拍比对；
3. 作为回归测试的断言来源（不依赖任何 EDA 工具）。

约定
----
* cycle c 指第 c 个时钟上升沿；
* ``Drive(cycle=c)`` 中的输入在 c 沿之后施加，作用于 c+1 沿；
* ``Check(cycle=c)`` 期望的是 c 沿之后的输出值。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .testbench import Check, Drive, TestPlan


def spec_params(spec: dict) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for p in spec.get("parameters") or []:
        name = str(p.get("name"))
        try:
            out[name] = int(p.get("default", 0))
        except Exception:
            out[name] = 0
    return out


def spec_ports(spec: dict) -> List[dict]:
    return list(spec.get("ports") or [])


class CycleOracle:
    """周期精确参考模型基类。"""

    name = "generic"
    description = "通用参考模型"

    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.params = spec_params(spec)
        self.total_cycles = 100
        self.reset_cycles = 3
        self.reset_state()

    # -- 子类实现 ------------------------------------------------------
    def reset_state(self) -> None:
        raise NotImplementedError

    def init_inputs(self) -> Dict[str, int]:
        clock_name = str((self.spec.get("clock") or {}).get("name") or "clk").lower()
        reset_name = str((self.spec.get("reset") or {}).get("name") or "rst_n").lower()
        active_low = str((self.spec.get("reset") or {}).get("polarity") or "active_low") == "active_low"
        inputs: Dict[str, int] = {}
        for p in spec_ports(self.spec):
            if str(p.get("dir")) != "input":
                continue
            name = str(p.get("name"))
            low = name.lower()
            if low == clock_name:
                continue  # 时钟由 Testbench 的时钟发生器驱动，不能出现在激励里
            if low == reset_name:
                inputs[name] = 0 if active_low else 1  # 初始处于复位态
            else:
                inputs[name] = 0
        return inputs

    def step(self, inp: Dict[str, int]) -> Dict[str, int]:
        raise NotImplementedError

    def schedule(self) -> Dict[int, Dict[str, int]]:
        return {}

    def check_cycles(self) -> List[int]:
        return []

    # -- 计划合成 ------------------------------------------------------
    def build_plan(self, *, period_ns: int = 20) -> TestPlan:
        drives: List[Drive] = []
        checks: List[Check] = []
        cur = self.init_inputs()
        schedule = self.schedule()
        checks_at = set(self.check_cycles())
        wanted_outputs = [str(p.get("name")) for p in spec_ports(self.spec) if str(p.get("dir")) == "output"]

        last_drive: Dict[str, int] = dict(cur)
        for c in range(self.total_cycles):
            out = self.step(cur)
            if c in checks_at:
                vals = {k: int(out.get(k, 0)) for k in wanted_outputs if k in out}
                if vals:
                    checks.append(Check(c, vals))
            if c in schedule:
                cur = dict(cur)
                cur.update(schedule[c])
                if cur != last_drive:
                    drives.append(Drive(c, {k: v for k, v in cur.items()}))
                    last_drive = dict(cur)

        reset_port = str((self.spec.get("reset") or {}).get("name") or "rst_n")
        active_low = str((self.spec.get("reset") or {}).get("polarity") or "active_low") == "active_low"
        return TestPlan(
            period_ns=period_ns,
            reset_port=reset_port,
            reset_active_low=active_low,
            reset_cycles=self.reset_cycles,
            drives=drives,
            checks=checks,
            max_cycles=self.total_cycles,
            notes=self.description,
        )


# --------------------------------------------------------------------------
# 1. 计数器
# --------------------------------------------------------------------------
class CounterOracle(CycleOracle):
    name = "counter"
    description = "4 位可加载使能计数器：load 优先于 en，异步低有效复位"

    def reset_state(self) -> None:
        self.q = 0
        self.width = 4
        for p in spec_ports(self.spec):
            if str(p.get("name")) in ("q", "q_o", "count"):
                self.width = int(p.get("width", 4) or 4)
        self.mask = (1 << self.width) - 1
        self.total_cycles = 44
        self.reset_cycles = 3

    def step(self, inp: Dict[str, int]) -> Dict[str, int]:
        if not inp.get("rst_n", 1):
            self.q = 0
        elif inp.get("load", 0):
            self.q = int(inp.get("din", 0)) & self.mask
        elif inp.get("en", 0):
            self.q = (self.q + 1) & self.mask
        return {"q": self.q}

    def schedule(self) -> Dict[int, Dict[str, int]]:
        return {
            0: {"rst_n": 0, "en": 0, "load": 0, "din": 0},
            3: {"rst_n": 1, "en": 1},
            12: {"load": 1, "din": 9},
            13: {"load": 0},
            20: {"en": 0},
            24: {"en": 1},
            30: {"load": 1, "din": self.mask},
            31: {"load": 0},
        }

    def check_cycles(self) -> List[int]:
        return list(range(0, 44))


# --------------------------------------------------------------------------
# 2. PWM
# --------------------------------------------------------------------------
class PwmOracle(CycleOracle):
    name = "pwm"
    description = "8 位自由计数器 + 占空比比较，组合输出 pwm_out"

    def reset_state(self) -> None:
        self.cnt = 0
        self.width = 8
        for p in spec_ports(self.spec):
            if str(p.get("name")) == "duty":
                self.width = int(p.get("width", 8) or 8)
        self.mask = (1 << self.width) - 1
        self.total_cycles = 792
        self.reset_cycles = 3

    def step(self, inp: Dict[str, int]) -> Dict[str, int]:
        if not inp.get("rst_n", 1):
            self.cnt = 0
        else:
            self.cnt = (self.cnt + 1) & self.mask
        duty = int(inp.get("duty", 0)) & self.mask
        return {"pwm_out": 1 if self.cnt < duty else 0}

    def schedule(self) -> Dict[int, Dict[str, int]]:
        return {
            0: {"rst_n": 0, "duty": 0},
            3: {"rst_n": 1, "duty": 128},
            300: {"duty": 64},
            600: {"duty": 0},
            700: {"duty": 255},
        }

    def check_cycles(self) -> List[int]:
        pts = [0, 1, 2, 3, 4, 5, 8, 64, 127, 128, 129, 200]
        pts += list(range(300, 792, 16))
        return sorted(set(pts))


# --------------------------------------------------------------------------
# 3. CRC32（字节并行，MSB 先行）
# --------------------------------------------------------------------------
class Crc32Oracle(CycleOracle):
    name = "crc32"
    description = "CRC-32（poly 0x04C11DB7，MSB 先行，不反转，初值 0xFFFFFFFF，无最终异或），单拍延迟"

    POLY = 0x04C11DB7

    def reset_state(self) -> None:
        self.crc = 0xFFFFFFFF
        self.idx = 0
        self.pattern = [0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x00, 0xFF, 0x5A]
        self.total_cycles = 30
        self.reset_cycles = 3

    def _next(self, crc: int, byte: int) -> int:
        c = (crc ^ ((byte & 0xFF) << 24)) & 0xFFFFFFFF
        for _ in range(8):
            c = ((c << 1) ^ self.POLY) & 0xFFFFFFFF if (c & 0x80000000) else (c << 1) & 0xFFFFFFFF
        return c & 0xFFFFFFFF

    def step(self, inp: Dict[str, int]) -> Dict[str, int]:
        if not inp.get("rst_n", 1):
            self.crc = 0xFFFFFFFF
        elif inp.get("valid", 0):
            self.crc = self._next(self.crc, int(inp.get("data", 0)))
        return {"crc": self.crc}

    def schedule(self) -> Dict[int, Dict[str, int]]:
        sched: Dict[int, Dict[str, int]] = {0: {"rst_n": 0, "valid": 0, "data": 0}, 3: {"rst_n": 1}}
        for i, b in enumerate(self.pattern):
            sched[4 + i] = {"valid": 1, "data": b}
            sched[5 + i] = {"valid": 0, "data": b}
        return sched

    def check_cycles(self) -> List[int]:
        return list(range(0, 30))


# --------------------------------------------------------------------------
# 4. UART 发送器（8N1）
# --------------------------------------------------------------------------
class UartTxOracle(CycleOracle):
    name = "uart_tx"
    description = "8N1 UART 发送器：LSB 先行，1 起始位 + 8 数据位 + 1 停止位，busy 在发送期间为高"

    def reset_state(self) -> None:
        self.cpb = int(self.params.get("CLKS_PER_BIT", 8) or 8)
        self.state = "IDLE"
        self.tx = 1
        self.busy = 0
        self.cnt = 0
        self.bit_idx = 0
        self.shift = 0
        # 两次完整字节传输 + 余量
        self.total_cycles = 40 + 2 * (10 * self.cpb + 12)
        self.reset_cycles = 3

    def step(self, inp: Dict[str, int]) -> Dict[str, int]:
        if not inp.get("rst_n", 1):
            self.state, self.tx, self.busy, self.cnt, self.bit_idx, self.shift = "IDLE", 1, 0, 0, 0, 0
            return {"tx": self.tx, "busy": self.busy}

        cpb = self.cpb
        if self.state == "IDLE":
            self.tx = 1
            self.busy = 0
            self.cnt = 0
            if inp.get("start", 0):
                self.shift = int(inp.get("data", 0)) & 0xFF
                self.state = "START"
                self.busy = 1
                self.bit_idx = 0
        elif self.state == "START":
            self.tx = 0
            if self.cnt == cpb - 1:
                self.cnt = 0
                self.state = "DATA"
            else:
                self.cnt += 1
        elif self.state == "DATA":
            self.tx = self.shift & 1
            if self.cnt == cpb - 1:
                self.cnt = 0
                self.shift = (self.shift >> 1) & 0xFF
                if self.bit_idx == 7:
                    self.bit_idx = 0
                    self.state = "STOP"
                else:
                    self.bit_idx += 1
            else:
                self.cnt += 1
        elif self.state == "STOP":
            self.tx = 1
            if self.cnt == cpb - 1:
                self.cnt = 0
                self.state = "IDLE"
                self.busy = 0
            else:
                self.cnt += 1
        return {"tx": self.tx, "busy": self.busy}

    def schedule(self) -> Dict[int, Dict[str, int]]:
        cpb = self.cpb
        first = 6
        second = first + 10 * cpb + 8
        return {
            0: {"rst_n": 0, "start": 0, "data": 0},
            3: {"rst_n": 1},
            5: {"start": 1, "data": 0xA5},
            first: {"start": 0},
            second: {"start": 1, "data": 0x3C},
            second + 1: {"start": 0},
        }

    def check_cycles(self) -> List[int]:
        cpb = self.cpb
        first = 6
        second = first + 10 * cpb + 8
        pts: List[int] = [3, 4, 5, first, first + 1]
        # 起始位中点
        pts.append(first + 1 + cpb // 2)
        # 8 个数据位中点
        for i in range(8):
            pts.append(first + 1 + cpb + i * cpb + cpb // 2)
        # 停止位中点
        pts.append(first + 1 + cpb * 9 + cpb // 2)
        # 传输结束 busy 归零
        pts.append(first + 1 + cpb * 10 + 2)
        pts += [second, second + 1, second + 1 + cpb // 2, second + 1 + cpb + cpb // 2]
        return sorted(set(p for p in pts if 0 <= p < self.total_cycles))


# --------------------------------------------------------------------------
# 5. VGA 行场时序
# --------------------------------------------------------------------------
class VgaTimingOracle(CycleOracle):
    name = "vga_timing"
    description = "640x480@60 VGA 时序：行 800 像素、场 525 行，同步信号低有效"

    def reset_state(self) -> None:
        self.h_active = int(self.params.get("H_ACTIVE", 640) or 640)
        self.h_fp = int(self.params.get("H_FRONT_PORCH", 16) or 16)
        self.h_sync = int(self.params.get("H_SYNC_PULSE", 96) or 96)
        self.h_bp = int(self.params.get("H_BACK_PORCH", 48) or 48)
        self.v_active = int(self.params.get("V_ACTIVE", 480) or 480)
        self.v_fp = int(self.params.get("V_FRONT_PORCH", 10) or 10)
        self.v_sync = int(self.params.get("V_SYNC_PULSE", 2) or 2)
        self.v_bp = int(self.params.get("V_BACK_PORCH", 33) or 33)
        self.h_total = self.h_active + self.h_fp + self.h_sync + self.h_bp
        self.v_total = self.v_active + self.v_fp + self.v_sync + self.v_bp
        self.hcnt = 0
        self.vcnt = 0
        self.total_cycles = self.h_total * self.v_total + self.h_total
        self.reset_cycles = 3

    def step(self, inp: Dict[str, int]) -> Dict[str, int]:
        if not inp.get("rst_n", 1):
            self.hcnt = 0
            self.vcnt = 0
        else:
            if self.hcnt == self.h_total - 1:
                self.hcnt = 0
                self.vcnt = 0 if self.vcnt == self.v_total - 1 else self.vcnt + 1
            else:
                self.hcnt += 1
        hsync = 0 if (self.h_active + self.h_fp) <= self.hcnt < (self.h_active + self.h_fp + self.h_sync) else 1
        vsync = 0 if (self.v_active + self.v_fp) <= self.vcnt < (self.v_active + self.v_fp + self.v_sync) else 1
        de = 1 if (self.hcnt < self.h_active and self.vcnt < self.v_active) else 0
        return {"hsync": hsync, "vsync": vsync, "de": de, "x": self.hcnt, "y": self.vcnt}

    def schedule(self) -> Dict[int, Dict[str, int]]:
        return {0: {"rst_n": 0}, 3: {"rst_n": 1}}

    def check_cycles(self) -> List[int]:
        H, V = self.h_total, self.v_total
        pts = [3, 4, 5, 100, self.h_active - 1, self.h_active, self.h_active + self.h_fp - 1,
               self.h_active + self.h_fp, self.h_active + self.h_fp + self.h_sync - 1,
               self.h_active + self.h_fp + self.h_sync, H - 1, H, H + 1, 2 * H - 1]
        # 场同步附近
        for row in (self.v_active - 1, self.v_active, self.v_active + self.v_fp,
                    self.v_active + self.v_fp + self.v_sync - 1, V - 1):
            pts.append(row * H + 10)
            pts.append(row * H + self.h_active + self.h_fp + 2)
        pts.append(V * H - 1)
        return sorted(set(p for p in pts if 0 <= p < self.total_cycles))


ORACLES = {
    "counter": CounterOracle,
    "pwm": PwmOracle,
    "crc32": Crc32Oracle,
    "uart_tx": UartTxOracle,
    "vga_timing": VgaTimingOracle,
}


def get_oracle(spec: dict) -> Optional[CycleOracle]:
    key = str(spec.get("name") or spec.get("top_module") or "").lower()
    cls = ORACLES.get(key)
    if cls is None:
        return None
    return cls(spec)


def build_plan(spec: dict, *, period_ns: int = 20) -> Optional[TestPlan]:
    oracle = get_oracle(spec)
    if oracle is None:
        return None
    return oracle.build_plan(period_ns=period_ns)
