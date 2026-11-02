"""高云云源 EDA（Gowin EDA）适配层。

对接依据（云源 V1.9.9 用户指南 SUG100 第 8 章 Tcl 命令说明）：
    gw_sh.exe <script.tcl>
    set_device <part number> [-device_version C]
    add_file [-type] <file...>
    set_option -top_module <name> / -verilog_std v2001 / -output_base_name <name>
    run syn | run pnr | run all
因此本适配层只生成 Tcl 并解析输出，不依赖 GUI，也不绑定具体版本。
"""
from __future__ import annotations

import os
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..config import BoardProfile, DeviceProfile
from ..diagnostics.parser import Diagnostic, parse_log
from .base import Project, StepResult, decode_output, make_result, write_log

STAGE_MARK = "@@YUNFAN_STAGE:"


class GowinBackend:
    name = "gowin"

    def __init__(
        self,
        gw_sh: str,
        *,
        programmer_cli: Optional[str] = None,
        gowin_root: Optional[str] = None,
        synth_timeout_s: float = 600.0,
        pnr_timeout_s: float = 900.0,
    ) -> None:
        self.gw_sh = gw_sh
        self.programmer_cli = programmer_cli
        self.gowin_root = gowin_root
        self.synth_timeout_s = synth_timeout_s
        self.pnr_timeout_s = pnr_timeout_s

    # ------------------------------------------------------------------
    def available(self) -> bool:
        return bool(self.gw_sh and Path(self.gw_sh).exists())

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        # 控制台是 PyInstaller 打包的，会把 _PYI_* 标记传给子进程；而云源的
        # programmer_cli 自己也是 PyInstaller 程序，看到这些标记会误以为自己是
        # 「子进程」而不解包自身模块，报 "MAINCMD module not found"。
        for key in [k for k in env if k.startswith("_PYI_") or k == "_MEIPASS2"]:
            env.pop(key, None)
        extra: List[str] = []
        if self.gowin_root:
            extra += [
                str(Path(self.gowin_root) / "IDE" / "bin"),
                str(Path(self.gowin_root) / "Programmer" / "bin"),
            ]
        if self.gw_sh:
            extra.append(str(Path(self.gw_sh).parent))
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
        return env

    # ------------------------------------------------------------------
    def _device_names(self, part: str) -> List[str]:
        """从云源器件库查出同一 part number 对应的所有器件名。

        `IDE/share/device/device_info.csv` 是云源自己的权威表，列为：
        id, part_number, family, device_name, package, voltage, version, ...
        例如 GW5A-LV25MG121NC1/I0 同时属于 GW5A-25A 与 GW5A-25B。
        """
        if not self.gowin_root:
            return []
        csv_path = Path(self.gowin_root) / "IDE" / "share" / "device" / "device_info.csv"
        if not csv_path.is_file():
            return []
        target = part.strip().upper()
        names: List[str] = []
        try:
            with csv_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    cols = line.strip().split(",")
                    if len(cols) >= 4 and cols[1].strip().upper() == target:
                        names.append(cols[3].strip())
        except OSError:
            return []
        return names

    def _device_options(self, device: Optional[DeviceProfile]) -> List[str]:
        """生成 set_device 的消歧参数。

        云源的 `<part number>` 并不唯一：同一个 part number 可能对应多个器件名
        （GW5A-LV25MG121NC1/I0 → GW5A-25A / GW5A-25B；GW5AT-LV138PG484AC1/I0 →
        GW5AT-138B / GW5AT-138C），此时必须指定其中一个，否则 PnR 报
        "there are more than one device named ..."。
        两种消歧方式互斥且都有坑，所以按器件库实际内容决定：
          * `-name <器件名>`  —— 最直观，但器件名必须与库中完全一致
            （GW5AT-138 这个配置名在库中并不存在，会报 can't find device）；
          * `-device_version <C>` —— 只接受 NA|B|C|D 单字母，
            GW5A 的 "C1" 传进去会报 "has not version C1"。
        """
        if device is None:
            return []
        part = device.part_number or ""
        names = self._device_names(part) if part else []
        name = (device.name or "").strip()

        # 1) 配置里的器件名确实属于该 part number：直接用它，最稳。
        if name and name in names:
            return [f"-name {name}"]
        # 2) 该 part number 在本机库里唯一，不需要消歧。
        if len(names) == 1:
            return []
        # 3) 多版本器件：用单字母版本号（GW5AT-138 → B，已验证可用）。
        version = (device.device_version or "").strip()
        if len(version) == 1 and version in "NABCD":
            return [f"-device_version {version}"]
        # 4) 版本号不可用（如 GW5A 的 C1）：退回器件库里的第一个器件名。
        if names:
            return [f"-name {names[0]}"]
        # 5) 没有器件库（旧版本安装）：按目录存在性兜底。
        if name and self.gowin_root and (Path(self.gowin_root) / "IDE" / "share" / "device" / name).is_dir():
            return [f"-name {name}"]
        return []

    def render_tcl(self, project: Project, stages: Sequence[str]) -> str:
        device = project.device
        board = project.board
        part = device.part_number if device else "GW5A-LV25MG121NC1/I0"
        options = self._device_options(device)
        lines = [
            "# ==========================================================",
            "# 本脚本由 YunFan 部署智能体自动生成，请勿手工修改",
            f"# 工程: {project.name}   顶层: {project.top}   器件: {part}",
            "# ==========================================================",
            ("set_device " + " ".join([part] + options)),
            f"add_file {self._p(project.rtl)}",
        ]
        if project.cst and project.cst.exists():
            lines.append(f"add_file {self._p(project.cst)}")
        if project.sdc and project.sdc.exists():
            lines.append(f"add_file {self._p(project.sdc)}")
        lines += [
            f"set_option -top_module {project.top}",
            "set_option -verilog_std v2001",
            f"set_option -output_base_name {project.top}",
        ]
        for stage in stages:
            if stage == "syn":
                lines += [f'puts "{STAGE_MARK}syn:start@@"', "run syn", f'puts "{STAGE_MARK}syn:done@@"']
            elif stage == "pnr":
                lines += [f'puts "{STAGE_MARK}pnr:start@@"', "run pnr", f'puts "{STAGE_MARK}pnr:done@@"']
        lines.append(f'puts "{STAGE_MARK}all:done@@"')
        return "\n".join(lines) + "\n"

    @staticmethod
    def _p(path: Optional[Path]) -> str:
        return str(path).replace("\\", "/") if path else ""

    # ------------------------------------------------------------------
    def _run(self, project: Project, stages: Sequence[str], *, stage_name: str, timeout_s: float) -> StepResult:
        project.ensure_dirs()
        tcl_path = project.root / f"yunfan_{stage_name}.tcl"
        tcl_path.write_text(self.render_tcl(project, stages), encoding="utf-8")
        cmd = [self.gw_sh, str(tcl_path)]
        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(project.root),
                capture_output=True,
                timeout=timeout_s,
                env=self._env(),
            )
        except subprocess.TimeoutExpired as exc:
            log = decode_output(exc.stdout or b"") + "\n" + decode_output(exc.stderr or b"")
            log += f"\n[YunFan] {stage_name} 超时（>{timeout_s}s）"
            res = make_result(
                stage_name, ok=False, tool=self.name, cmd=cmd, returncode=-1, started=started,
                log=log, project=project, verdict="timeout", parse_tool="gowin",
            )
            return res
        except FileNotFoundError as exc:
            return StepResult(stage=stage_name, ok=False, tool=self.name, cmd=" ".join(cmd), log=str(exc), verdict="error")

        log = decode_output(proc.stdout) + "\n" + decode_output(proc.stderr)
        ok = proc.returncode == 0 and STAGE_MARK + "all:done" in log
        res = make_result(
            stage_name, ok=ok, tool=self.name, cmd=cmd, returncode=proc.returncode, started=started,
            log=log, project=project, verdict="ok" if ok else "error", parse_tool="gowin",
            artifacts={"tcl": str(tcl_path)},
        )
        if not ok:
            # 若工具已报错但解析器没抓到，补一条兜底诊断
            if not res.diagnostics:
                fail_lines = [ln for ln in log.splitlines() if "ERROR" in ln.upper() or "FATAL" in ln.upper()]
                res.diagnostics = parse_log("\n".join(fail_lines), tool="gowin")
        return res

    # ------------------------------------------------------------------
    def compile(self, project: Project) -> StepResult:
        """综合（syn）：语法/可综合性门控。"""
        res = self._run(project, ["syn"], stage_name="synthesize", timeout_s=self.synth_timeout_s)
        vg = list((project.root / "impl" / "gwsynthesis").glob("*.vg"))
        if vg:
            res.artifacts["netlist"] = str(vg[0])
        rpt = project.find_syn_report()
        if rpt:
            res.artifacts["syn_report"] = str(rpt)
        return res

    def implement(self, project: Project) -> StepResult:
        """综合 + 布局布线 + 比特流。"""
        res = self._run(project, ["syn", "pnr"], stage_name="implement", timeout_s=self.pnr_timeout_s)
        bit = project.find_bitstream()
        if bit:
            res.artifacts["bitstream"] = str(bit)
            res.metrics["bitstream_bytes"] = float(bit.stat().st_size)
        rpt = project.find_rpt_txt()
        if rpt:
            res.artifacts["pnr_report"] = str(rpt)
            res.metrics.update(self.parse_resources(rpt))
        tr = project.find_timing_report()
        if tr:
            res.artifacts["timing_report"] = str(tr)
            res.metrics.update(self.parse_timing(tr))
        pin = project.find_pin_report()
        if pin:
            res.artifacts["pin_report"] = str(pin)
        return res

    def simulate(self, project: Project) -> StepResult:
        return StepResult(stage="simulate", ok=False, tool=self.name, log="云源本身不内置仿真内核，仿真由 Icarus 引擎承担", verdict="skipped")

    # ------------------------------------------------------------------
    def program(self, project: Project, *, dry_run: bool = True) -> StepResult:
        """通过 Gowin Programmer 命令行下载比特流。

        dry_run 时**只扫描下载器**，绝不把 --fsFile 交给 programmer_cli——
        否则「校验」会真的把比特流写进板子。
        """
        bit = project.find_bitstream()
        if not bit:
            return StepResult(stage="program", ok=False, tool="programmer_cli", log="未找到比特流文件（.fs），无法烧录", verdict="error")
        if not self.programmer_cli or not Path(self.programmer_cli).exists():
            return StepResult(stage="program", ok=False, tool="programmer_cli", log="未找到 programmer_cli.exe", verdict="skipped")

        device = project.device.programmer_device if project.device else "GW5A-25A"
        started = time.time()
        project.ensure_dirs()
        log_path = project.log_dir / "program.log"

        def _run_direct(args: list) -> tuple:
            """直接 spawn：stdout 必须重定向到文件（用管道会触发 MAINCMD 报错）。"""
            with open(log_path, "w", encoding="utf-8", errors="replace") as fh:
                proc = subprocess.Popen(
                    [str(a) for a in args], stdout=fh, stderr=subprocess.STDOUT,
                )
                try:
                    proc.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    proc.kill()
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            return (proc.returncode if proc.returncode is not None else -1), text

        def _run_via_wmi(args: list) -> tuple:
            """用 WMI 创建进程：由 WmiPrvSE 派生，完全脱离当前进程树。

            打包成 exe 的控制台里，任何子进程再拉起 programmer_cli 都会报
            "MAINCMD module not found"；WMI 创建的进程不在这个进程树里，实测可正常烧录。
            """
            quoted = subprocess.list2cmdline([str(a) for a in args])
            cmdline = f'cmd.exe /c {quoted} > "{log_path}" 2>&1'
            tmp_cmd = log_path.with_name("_wmi_cmd.txt")
            tmp_ps = log_path.with_name("_wmi.ps1")
            tmp_cmd.write_text(cmdline, encoding="utf-8")
            tmp_ps.write_text(
                "$ErrorActionPreference='Stop';"
                f"$c = Get-Content -Raw -LiteralPath '{tmp_cmd}';"
                "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                f"-Arguments @{{CommandLine=$c.Trim(); CurrentDirectory='{project.root}'}};"
                "if (-not $r.ProcessId) { exit 9 };"
                "$r.ProcessId | Out-File -Encoding ascii "
                f"'{log_path.with_name('_wmi_pid.txt')}';"
                "exit 0",
                encoding="utf-8",
            )
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(tmp_ps)],
                timeout=60, stdin=subprocess.DEVNULL, capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # 轮询日志直到出现完成标记/超时
            deadline = time.time() + 300
            text = ""
            while time.time() < deadline:
                time.sleep(0.5)
                try:
                    text = log_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if "Finished" in text or "Error" in text or "Cost " in text:
                    time.sleep(1.0)
                    try:
                        text = log_path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        pass
                    break
            finished = ("Finished" in text) and ("100%" in text)
            return (0 if finished else 1), text

        def _run_to_file(args: list) -> tuple:
            """先直接跑；一旦遇到 MAINCMD（打包进程树问题）就改用 WMI 重跑一次。"""
            if os.name != "nt":
                return _run_direct(args)
            rc, text = _run_direct(args)
            if "MAINCMD" in text or (rc != 0 and "Cable" not in text):
                wmi_rc, wmi_text = _run_via_wmi(args)
                if wmi_text.strip():
                    return wmi_rc, wmi_text
            return rc, text

        # ---- dry-run：只扫描，不烧 ----
        if dry_run:
            scan_cmd = [self.programmer_cli, "--scan-cables"]
            try:
                rc, log = _run_to_file(scan_cmd)
            except Exception as exc:  # noqa: BLE001
                log, rc = f"下载器扫描异常：{exc}", -1
            cable = bool(re.search(r"(USB Cable|FT2CH|GWU2X|Jtag)", log, re.IGNORECASE))
            head = "[YunFan] dry-run：未烧录任何内容，仅扫描下载器。" + ("已检测到下载器" if cable else "未检测到下载器")
            res = make_result(
                "program", ok=True, tool="programmer_cli", cmd=scan_cmd, returncode=rc,
                started=started, log=head + "\n" + log, project=project, verdict="dry-run",
                parse_tool="programmer",
            )
            res.metrics["cable_detected"] = 1.0 if cable else 0.0
            res.artifacts["bitstream"] = str(bit)
            return res

        # ---- 真实烧录 ----
        cmd = [
            self.programmer_cli,
            "--device", device,
            "--run", "2",  # SRAM Program
            "--fsFile", str(bit),
        ]
        try:
            rc, log = _run_to_file(cmd)
        except Exception as exc:  # noqa: BLE001
            log = f"下载器调用异常：{exc}"
            return StepResult(stage="program", ok=False, tool="programmer_cli", cmd=" ".join(cmd), log=log, verdict="error")

        finished = ("Finished" in log) and ("100%" in log)
        # 有的版本哪怕烧成功也返回非 0，用日志里的完成标记兜底
        ok = rc == 0 or finished
        cable = bool(re.search(r"(USB Cable|FT2CH|GWU2X|Jtag)", log, re.IGNORECASE))
        res = make_result(
            "program", ok=ok, tool="programmer_cli", cmd=cmd, returncode=rc,
            started=started, log=f"[YunFan] 命令：{' '.join(cmd)}\n" + log,
            project=project, verdict="programmed" if ok else "error", parse_tool="programmer",
        )
        res.metrics["cable_detected"] = 1.0 if cable else 0.0
        res.artifacts["bitstream"] = str(bit)
        return res

    # ------------------------------------------------------------------
    @staticmethod
    def parse_resources(rpt_path: Path) -> Dict[str, float]:
        """解析 PnR 报告第 3 节「Resource Usage Summary」。"""
        try:
            text = rpt_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return {}
        out: Dict[str, float] = {}
        patterns = {
            "logic": r"^\s*Logic\s*\|\s*(\d+)\s*/\s*(\d+)",
            "register": r"^\s*Register\s*\|\s*(\d+)\s*/\s*(\d+)",
            "cls": r"^\s*CLS\s*\|\s*(\d+)\s*/\s*(\d+)",
            "io_port": r"^\s*I/O Port\s*\|\s*(\d+)",
            "bsram": r"^\s*BSRAM\s*\|\s*([\d.]+)%",
            "dsp": r"^\s*DSP\s*\|\s*([\d.]+)%",
            "pll": r"^\s*PLL\s*\|\s*(\d+)\s*/\s*(\d+)",
        }
        for key, pat in patterns.items():
            m = re.search(pat, text, re.MULTILINE)
            if not m:
                continue
            if key in ("logic", "register", "cls", "pll"):
                out[f"{key}_used"] = float(m.group(1))
                out[f"{key}_total"] = float(m.group(2))
                if key in ("logic", "register", "cls"):
                    out[f"{key}_pct"] = round(100.0 * float(m.group(1)) / max(1.0, float(m.group(2))), 2)
            elif key == "io_port":
                out["io_used"] = float(m.group(1))
            else:
                out[f"{key}_pct"] = float(m.group(1))
        m = re.search(r"--LUT,ALU,ROM16\s*\|\s*(\d+)\((\d+)\s*LUT,\s*(\d+)\s*ALU", text)
        if m:
            out["lut_used"] = float(m.group(2))
            out["alu_used"] = float(m.group(3))
        return out

    @staticmethod
    def parse_timing(tr_path: Path) -> Dict[str, float]:
        """解析时序报告「Max Frequency Summary」。"""
        try:
            text = tr_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return {}
        out: Dict[str, float] = {}
        m = re.search(r"Max Frequency Summary(.*?)(Total Negative Slack|Timing Details)", text, re.DOTALL)
        if m:
            seg = re.sub(r"<[^>]+>", " ", m.group(1))
            mm = re.search(r"clk[^0-9]*([\d.]+)\(MHz\)\s*([\d.]+)\(MHz\)\s*(\d+)", seg)
            if mm:
                out["clock_constraint_mhz"] = float(mm.group(1))
                out["fmax_mhz"] = float(mm.group(2))
                out["logic_level"] = float(mm.group(3))
        m2 = re.search(r"clk\s+Setup\s+([\-\d.]+)\s+(\d+)", re.sub(r"<[^>]+>", " ", text))
        if m2:
            out["setup_tns_ns"] = float(m2.group(1))
            out["setup_negative_endpoints"] = float(m2.group(2))
        return out


def default_sdc(clock_port: str, period_ns: float) -> str:
    half = period_ns / 2.0
    return (
        "# 由 YunFan 部署智能体生成：单时钟域约束\n"
        f"create_clock -name {clock_port} -period {period_ns} -waveform {{0 {half:g}}} [get_ports {{{clock_port}}}]\n"
    )
