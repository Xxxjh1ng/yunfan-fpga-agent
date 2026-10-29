"""全局配置与工具链自动发现.

设计要点
--------
1. 器件 / 开发板 / 工具链全部外置为 JSON，换芯片或换工具版本只改数据不改代码；
2. 工具路径支持「环境变量 > PATH > 常见安装目录」三级发现，缺工具时降级为
   MockBackend 而不是直接崩溃；
3. LLM 配置支持 deepseek / openai 兼容 / offline 三种 provider，
   offline 用于无网络环境下的回归测试与实验复现。
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

PKG_ROOT = Path(__file__).resolve().parent
DATA_DIR = PKG_ROOT / "data"
DEFAULT_WORKSPACE = PKG_ROOT.parent


# --------------------------------------------------------------------------
# .env 解析（不引入第三方依赖）
# --------------------------------------------------------------------------
def load_dotenv(path: Optional[Path] = None) -> Dict[str, str]:
    """极简 .env 解析：KEY=VALUE，忽略注释与空行，不覆盖已存在的环境变量。"""
    candidates: List[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        env_root = os.getenv("YUNFAN_ENV_FILE")
        if env_root:
            candidates.append(Path(env_root))
        candidates.append(Path.cwd() / ".env")
        candidates.append(DEFAULT_WORKSPACE / ".env")

    loaded: Dict[str, str] = {}
    for f in candidates:
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8-sig")
        except Exception:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
            loaded[key] = value
    return loaded


# --------------------------------------------------------------------------
# 工具发现
# --------------------------------------------------------------------------
def _first_existing(patterns: List[str]) -> Optional[str]:
    for pattern in patterns:
        for hit in sorted(glob.glob(pattern), reverse=True):
            if os.path.isfile(hit):
                return os.path.abspath(hit)
    return None


def _version_score(path: str) -> tuple:
    """把安装路径转成可比较的版本权重：主版本号优先，"Education" 降权。"""
    m = re.search(r"V(\d+)\.(\d+)(?:\.(\d+))?", path, re.IGNORECASE)
    parts = tuple(int(x) if x else 0 for x in (m.groups() if m else (0, 0, 0)))
    edu = -1000 if "education" in path.lower() else 0
    return parts + (edu,)


def find_gowin_root() -> Optional[str]:
    """定位 Gowin 云源安装目录（含 gw_sh.exe 的 IDE/bin 的父目录）。

    同时存在教育版与完整版时，优先选**版本更高且非 Education** 的那个。
    """
    env = os.getenv("YUNFAN_GOWIN_ROOT")
    if env and Path(env).is_dir():
        return os.path.abspath(env)
    patterns = [
        r"C:\\Gowin\\Gowin_V*\\IDE\\bin",
        r"D:\\Gowin\\Gowin_V*\\IDE\\bin",
        r"E:\\Gowin\\Gowin_V*\\IDE\\bin",
        r"C:\\Program Files\\Gowin\\Gowin_V*\\IDE\\bin",
        r"C:\\Program Files (x86)\\Gowin\\Gowin_V*\\IDE\\bin",
        "/opt/gowin/*/IDE/bin",
    ]
    hits = []
    for p in patterns:
        for exe in ("gw_sh.exe", "gw_sh"):
            for hit in glob.glob(os.path.join(p, exe)):
                if os.path.isfile(hit):
                    hits.append(str(Path(hit).parent.parent.parent))
    if not hits:
        return None
    hits = sorted(set(hits), key=_version_score, reverse=True)
    return os.path.abspath(hits[0])


def find_tool(name: str, extra: Optional[List[str]] = None) -> Optional[str]:
    """在 PATH、环境变量、常见目录中查找可执行文件。"""
    env_key = "YUNFAN_" + name.upper().replace("-", "_").replace(".", "_")
    env_val = os.getenv(env_key)
    if env_val and Path(env_val).exists():
        return os.path.abspath(env_val)

    hit = shutil.which(name)
    if hit:
        return os.path.abspath(hit)

    if extra:
        got = _first_existing(extra)
        if got:
            return got

    root = find_gowin_root()
    if root:
        candidates = [
            Path(root) / "IDE" / "bin" / name,
            Path(root) / "Programmer" / "bin" / name,
        ]
        for c in candidates:
            if c.is_file():
                return str(c)
    return None


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class DeviceProfile:
    """FPGA 器件档案。"""

    name: str
    family: str
    part_number: str
    package: str
    logic: int
    registers: int
    cls: int
    bsram_kb: int
    dsp: int
    pll: int
    io_max: int
    device_version: str = "C1"
    programmer_device: str = ""
    notes: str = ""

    @staticmethod
    def load(name: str) -> "DeviceProfile":
        data = json.loads((DATA_DIR / "devices.json").read_text(encoding="utf-8"))
        for item in data["devices"]:
            if name.lower() in (item["name"].lower(), item["family"].lower()):
                return DeviceProfile(**item)
        available = ", ".join(d["name"] for d in data["devices"])
        raise KeyError(f"未知器件 {name!r}，可选：{available}")


@dataclass(frozen=True)
class BoardProfile:
    """开发板档案：引脚约束 + 板载资源。"""

    name: str
    vendor: str
    device: str
    clock_mhz: float
    clock_pin: str
    reset_pin: str
    reset_active_low: bool
    io_standard: str
    pins: Dict[str, str]
    notes: str = ""

    @staticmethod
    def load(name: str) -> "BoardProfile":
        data = json.loads((DATA_DIR / "boards.json").read_text(encoding="utf-8"))
        for item in data["boards"]:
            if item["name"].lower() == name.lower():
                return BoardProfile(**item)
        available = ", ".join(b["name"] for b in data["boards"])
        raise KeyError(f"未知开发板 {name!r}，可选：{available}")


@dataclass
class LLMConfig:
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout_s: float = 180.0
    retries: int = 3
    concurrency: int = 4

    def resolved(self) -> "LLMConfig":
        return LLMConfig(
            provider=self.provider,
            model=os.getenv("YUNFAN_MODEL", self.model),
            api_key=self.api_key or os.getenv("YUNFAN_API_KEY", "") or os.getenv("MY_API_KEY", ""),
            base_url=(os.getenv("YUNFAN_BASE_URL") or os.getenv("MY_BASE_URL") or self.base_url).rstrip("/"),
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout_s=self.timeout_s,
            retries=self.retries,
            concurrency=self.concurrency,
        )


@dataclass
class Toolchain:
    """工具链路径集合；缺项为 None。"""

    gw_sh: Optional[str] = None
    programmer_cli: Optional[str] = None
    iverilog: Optional[str] = None
    vvp: Optional[str] = None
    vivado: Optional[str] = None
    gowin_root: Optional[str] = None

    @property
    def has_synth(self) -> bool:
        return bool(self.gw_sh) or bool(self.vivado)

    @property
    def has_sim(self) -> bool:
        return bool(self.iverilog and self.vvp)

    @property
    def has_programmer(self) -> bool:
        return bool(self.programmer_cli)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "gw_sh": self.gw_sh,
            "programmer_cli": self.programmer_cli,
            "iverilog": self.iverilog,
            "vvp": self.vvp,
            "vivado": self.vivado,
            "gowin_root": self.gowin_root,
            "has_synth": self.has_synth,
            "has_sim": self.has_sim,
            "has_programmer": self.has_programmer,
        }


def discover_toolchain() -> Toolchain:
    root = find_gowin_root()
    iv_candidates = [
        str(DEFAULT_WORKSPACE / "tools" / "iverilog" / "bin"),
        r"C:\iverilog\bin",
        r"C:\Program Files\iverilog\bin",
        "/usr/bin",
        "/usr/local/bin",
    ]
    iv_extra = [os.path.join(p, "iverilog.exe") for p in iv_candidates] + [os.path.join(p, "iverilog") for p in iv_candidates]
    vvp_extra = [os.path.join(p, "vvp.exe") for p in iv_candidates] + [os.path.join(p, "vvp") for p in iv_candidates]
    return Toolchain(
        gw_sh=find_tool("gw_sh.exe", [os.path.join(root, "IDE", "bin", "gw_sh.exe")] if root else None),
        programmer_cli=find_tool(
            "programmer_cli.exe",
            [os.path.join(root, "Programmer", "bin", "programmer_cli.exe")] if root else None,
        ),
        iverilog=find_tool("iverilog.exe", iv_extra),
        vvp=find_tool("vvp.exe", vvp_extra),
        vivado=find_tool("vivado.bat"),
        gowin_root=root,
    )


@dataclass
class Settings:
    workspace: Path = field(default_factory=lambda: DEFAULT_WORKSPACE)
    out_dir: Path = field(default_factory=lambda: DEFAULT_WORKSPACE / "runs")
    device: DeviceProfile = field(default_factory=lambda: DeviceProfile.load("GW5A-25A"))
    board: BoardProfile = field(default_factory=lambda: BoardProfile.load("xm_gw5a25"))
    tools: Toolchain = field(default_factory=discover_toolchain)
    llm: LLMConfig = field(default_factory=lambda: LLMConfig().resolved())
    max_lint_rounds: int = 3
    max_repair_rounds: int = 5
    max_tokens_budget: int = 400_000
    sim_timeout_s: float = 120.0
    synth_timeout_s: float = 600.0
    seed: int = 0

    @staticmethod
    def build(**overrides: Any) -> "Settings":
        load_dotenv()
        cfg = Settings()
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        cfg.llm = cfg.llm.resolved()
        if not isinstance(cfg.out_dir, Path):
            cfg.out_dir = Path(cfg.out_dir)
        if not isinstance(cfg.workspace, Path):
            cfg.workspace = Path(cfg.workspace)
        return cfg

    def describe(self) -> str:
        return json.dumps(
            {
                "device": self.device.name,
                "part_number": self.device.part_number,
                "board": self.board.name,
                "tools": self.tools.as_dict(),
                "llm": {
                    "provider": self.llm.provider,
                    "model": self.llm.model,
                    "base_url": self.llm.base_url,
                    "api_key_set": bool(self.llm.api_key),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
