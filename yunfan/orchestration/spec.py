"""设计规格（Design Spec）：多智能体之间唯一的事实源。

规格一旦锁定，就同时约束三件事：
1. RTL 的端口表（生成后由 lint 强制复核）；
2. Testbench 的端口连接与期望值（模板合成，不再由模型自由发挥）；
3. 板级约束的引脚映射（按端口名语义匹配开发板资源）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

VALID_DIRS = {"input", "output", "inout"}


@dataclass
class DesignSpec:
    name: str = ""
    top_module: str = ""
    summary: str = ""
    clock: Dict[str, Any] = field(default_factory=lambda: {"name": "clk", "freq_mhz": 50.0, "edge": "posedge"})
    reset: Dict[str, Any] = field(default_factory=lambda: {"name": "rst_n", "polarity": "active_low", "style": "async"})
    ports: List[Dict[str, Any]] = field(default_factory=list)
    parameters: List[Dict[str, Any]] = field(default_factory=list)
    behavior: List[str] = field(default_factory=list)
    timing: Dict[str, Any] = field(default_factory=dict)
    verification: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "DesignSpec":
        if not isinstance(data, dict):
            raise ValueError("规格必须是 JSON 对象")
        spec = DesignSpec()
        spec.raw = data
        spec.name = str(data.get("name") or "").strip()
        spec.top_module = str(data.get("top_module") or spec.name or "").strip()
        spec.summary = str(data.get("summary") or "").strip()
        if isinstance(data.get("clock"), dict):
            spec.clock = data["clock"]
        if isinstance(data.get("reset"), dict):
            spec.reset = data["reset"]
        spec.ports = [p for p in (data.get("ports") or []) if isinstance(p, dict)]
        spec.parameters = [p for p in (data.get("parameters") or []) if isinstance(p, dict)]
        spec.behavior = [str(b) for b in (data.get("behavior") or [])]
        spec.timing = data.get("timing") or {}
        spec.verification = data.get("verification") or {}

        # 归一化端口
        for p in spec.ports:
            p["name"] = str(p.get("name") or "").strip()
            p["dir"] = str(p.get("dir") or "input").lower().strip()
            try:
                p["width"] = int(p.get("width") or 1)
            except Exception:
                p["width"] = 1
            p.setdefault("desc", "")

        for p in spec.parameters:
            p["name"] = str(p.get("name") or "").strip()
            try:
                p["default"] = int(p.get("default") or 0)
            except Exception:
                p["default"] = 0
            p.setdefault("type", "int")
            p.setdefault("desc", "")

        if not spec.name and spec.top_module:
            spec.name = spec.top_module
        if not spec.top_module and spec.name:
            spec.top_module = spec.name
        return spec

    @staticmethod
    def from_json(text: str) -> "DesignSpec":
        return DesignSpec.from_dict(json.loads(text))

    @staticmethod
    def load(path) -> "DesignSpec":
        from pathlib import Path

        p = Path(path)
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() in (".yaml", ".yml"):
            raise ValueError("暂不支持 YAML，请使用 JSON 规格")
        return DesignSpec.from_json(text)

    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "top_module": self.top_module,
            "summary": self.summary,
            "clock": self.clock,
            "reset": self.reset,
            "ports": self.ports,
            "parameters": self.parameters,
            "behavior": self.behavior,
            "timing": self.timing,
            "verification": self.verification,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    # ------------------------------------------------------------------
    @property
    def clock_name(self) -> str:
        return str(self.clock.get("name") or "clk")

    @property
    def clock_freq_mhz(self) -> float:
        try:
            return float(self.clock.get("freq_mhz") or 50.0)
        except Exception:
            return 50.0

    @property
    def reset_name(self) -> str:
        return str(self.reset.get("name") or "rst_n")

    @property
    def reset_active_low(self) -> bool:
        return str(self.reset.get("polarity") or "active_low").lower() != "active_high"

    @property
    def inputs(self) -> List[Dict[str, Any]]:
        return [p for p in self.ports if p.get("dir") == "input"]

    @property
    def outputs(self) -> List[Dict[str, Any]]:
        return [p for p in self.ports if p.get("dir") == "output"]

    def port_names(self) -> List[str]:
        return [str(p.get("name")) for p in self.ports]

    def param_map(self) -> Dict[str, int]:
        return {str(p.get("name")): int(p.get("default") or 0) for p in self.parameters}

    # ------------------------------------------------------------------
    def validate(self) -> List[str]:
        """返回问题列表；空列表代表可进入下一阶段。"""
        issues: List[str] = []
        if not self.top_module:
            issues.append("缺少 top_module")
        elif not re.fullmatch(r"[A-Za-z_]\w*", self.top_module):
            issues.append(f"top_module 不是合法标识符：{self.top_module!r}")
        if self.top_module.upper() == "UNSPECIFIED":
            issues.append("核心功能未明确（模型判定为 UNSPECIFIED）")
        if not self.ports:
            issues.append("端口表为空")
            return issues

        seen = set()
        for p in self.ports:
            name = str(p.get("name") or "")
            if not re.fullmatch(r"[A-Za-z_]\w*", name):
                issues.append(f"端口名非法：{name!r}")
                continue
            if name.lower() in seen:
                issues.append(f"端口重复：{name}")
            seen.add(name.lower())
            if p.get("dir") not in VALID_DIRS:
                issues.append(f"端口 {name} 方向非法：{p.get('dir')!r}")
            width = int(p.get("width") or 0)
            if width <= 0 or width > 4096:
                issues.append(f"端口 {name} 位宽非法：{width}")

        if not any(p.get("dir") == "input" for p in self.ports):
            issues.append("没有输入端口")
        if not any(p.get("dir") == "output" for p in self.ports):
            issues.append("没有输出端口")
        if not any(str(p.get("name")) == self.clock_name for p in self.inputs):
            issues.append(f"缺少时钟端口 {self.clock_name!r}")
        return issues

    def ok(self) -> bool:
        return not self.validate()

    # ------------------------------------------------------------------
    def brief(self) -> str:
        ports = ", ".join(f"{p['dir'][:2]}:{p['name']}[{p['width']}]" for p in self.ports[:8])
        if len(self.ports) > 8:
            ports += f", ...(共 {len(self.ports)} 个端口)"
        return (
            f"设计 {self.name}/{self.top_module}｜{self.clock_freq_mhz:g}MHz｜端口 {ports}\n"
            f"  摘要：{self.summary[:120]}"
        )

    def board_clock_mhz(self, default: float = 50.0) -> float:
        return self.clock_freq_mhz or default
