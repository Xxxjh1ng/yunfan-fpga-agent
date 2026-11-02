r"""云帆 YunFan 本地控制台后端（零依赖，仅标准库）。

三个能力
--------
1. 设计：调 `python -m yunfan design ...`，跑完整五智能体闭环；
2. 仿真：把 RTL/Testbench 交给 Icarus Verilog，出 PASS/FAIL 与 VCD 波形；
3. 烧录：调 Gowin programmer_cli，默认 dry-run，勾「真实烧录」才下载。

启动
----
    .\.venv\Scripts\python.exe gui\server.py --port 8765
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
GUI = Path(__file__).resolve().parent
sys.path.insert(0, str(GUI))

PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.is_file():
    PY = Path(sys.executable)
RUNS = ROOT / "runs"
SPECS = ["counter", "pwm", "crc32", "uart_tx", "vga_timing", "led_water", "key_count", "vga_bars"]
MARK = "@@RESULT@@"
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

# 流程节点（前端按这个顺序渲染时间线）
NODES = ["spec", "rtl", "gate", "tb", "sim", "repair", "pnr", "sim2", "prog"]


def _clean_detail(line: str) -> str:
    """把一行日志压成一句可展示的细节（路径只留文件名，超长截断）。"""
    s = re.sub(r"^\[\d{2}:\d{2}:\d{2}\]\s*", "", line)
    s = re.sub(r"^\[[^\]]+\]\s*", "", s)
    s = re.sub(r"^[—\-–]\s*", "", s.strip())
    s = re.sub(r"[A-Za-z]:\\[^\s｜|]+", lambda m: Path(m.group(0)).name, s)
    s = s.strip()
    return s if len(s) <= 72 else s[:71] + "…"


def _push_node(job: dict, line: str) -> None:
    """从工具输出里识别流程节点，推给前端画进度。"""
    q = job["queue"]

    def emit(node_id: str, state: str, detail: str = "") -> None:
        job["current_node"] = node_id
        q.put({"t": "node", "id": node_id, "state": state, "detail": detail})

    if "[需求解析]" in line:
        emit("spec", "running")
    if "规格锁定" in line:
        emit("spec", "done", _clean_detail(line))
    if "[代码生成]" in line and "LLM 调用" in line:
        emit("rtl", "running", _clean_detail(line))
    if "RTL 通过静态门控" in line:
        emit("rtl", "done")
        emit("gate", "done", _clean_detail(line))
    elif "未通过静态门控" in line:
        emit("gate", "running", _clean_detail(line))
    if "[仿真验证]" in line:
        emit("tb", "running")
    if "自验证激励生成" in line:
        emit("tb", "done", _clean_detail(line))
    if "仿真通过" in line:
        emit("sim", "done", _clean_detail(line))
    elif "仿真失败" in line or "仿真未通过" in line:
        emit("sim", "failed", _clean_detail(line))
    if "[调试修复]" in line:
        emit("repair", "running", _clean_detail(line))
    if "实现完成" in line:
        emit("pnr", "done", _clean_detail(line))
    elif "[硬件部署]" in line and "约束生成" in line:
        emit("pnr", "running", _clean_detail(line))


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _read(path: Path, limit: int = 400_000) -> str:
    try:
        return path.read_bytes()[:limit].decode("utf-8", errors="replace")
    except OSError:
        return ""


def _json_file(path: Path) -> dict:
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return json.loads(path.read_text(encoding=enc))
        except Exception:
            continue
    return {}


def _snapshot() -> set:
    if not RUNS.is_dir():
        return set()
    return {p.name for p in RUNS.iterdir() if p.is_dir()}


def _run_path(name: str) -> Path:
    return RUNS / name


def _safe_run(name: str) -> Path | None:
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    base = _run_path(name).resolve()
    if not str(base).startswith(str(RUNS.resolve())) or not base.is_dir():
        return None
    return base


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


# --------------------------------------------------------------------------
# 通用作业：spawn 子进程 → 流式推日志 → 结束时给结构化结果
# --------------------------------------------------------------------------
def _spawn(cmd: list[str], *, label: str, kind: str, run_dir: str = "", before: set | None = None) -> dict:
    # 给子进程一个隐藏的控制台：云源的 programmer_cli 在没有控制台的进程树里会
    # 报 "MAINCMD module not found"（PyInstaller 打包的 GUI 程序会传递这种环境）。
    if os.name == "nt":
        _si = subprocess.STARTUPINFO()
        _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        _si.wShowWindow = 0
        creation = subprocess.CREATE_NEW_CONSOLE
        _startupinfo = _si
    else:
        creation = 0
        _startupinfo = None
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env=_env(), creationflags=creation, startupinfo=_startupinfo,
    )
    job_id = uuid.uuid4().hex[:12]
    q: queue.Queue = queue.Queue()
    job = {
        "id": job_id, "proc": proc, "queue": q, "label": label, "kind": kind,
        "started": time.time(), "run_dir": run_dir, "returncode": None, "result": {},
        "before": before or set(),
    }
    with JOBS_LOCK:
        JOBS[job_id] = job

    def pump() -> None:
        assert proc.stdout is not None
        payload: dict = {}
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line.startswith(MARK):
                try:
                    payload = json.loads(line[len(MARK):])
                except json.JSONDecodeError:
                    pass
                continue
            q.put({"t": "log", "line": line})
        proc.wait()

        run_dir_final = run_dir
        if not run_dir_final and job["before"] is not None:
            new = sorted(_snapshot() - job["before"])
            run_dir_final = new[-1] if new else ""
        job["returncode"] = proc.returncode
        job["run_dir"] = run_dir_final
        if not payload:
            payload = {"ok": proc.returncode == 0, "run_dir": run_dir_final}
        payload.setdefault("run_dir", run_dir_final)
        job["result"] = payload
        q.put({
            "t": "end", "code": proc.returncode, "run_dir": run_dir_final,
            "elapsed": round(time.time() - job["started"], 1),
            "ok": bool(payload.get("ok", proc.returncode == 0)),
            "result": _enrich(payload),
        })

    threading.Thread(target=pump, daemon=True).start()
    return {"job": job_id, "label": label}


def _enrich(payload: dict) -> dict:
    """把运行目录里的摘要/源码补进结果，供前端渲染。"""
    out = dict(payload)
    run_dir = out.get("run_dir") or ""
    base = _safe_run(run_dir)
    if not base:
        return out
    out.setdefault("summary", _read(base / "summary.md"))
    src = base / "src"
    files = sorted(p.name for p in src.glob("*.v")) if src.is_dir() else []
    out.setdefault("files", files)
    if "rtl" not in out:
        rtl = next((f for f in files if not f.startswith("tb_")), "")
        if rtl:
            out["rtl_name"] = rtl
            out["rtl"] = _read(src / rtl)
    if "tb" not in out:
        tb = "tb_" + out.get("rtl_name", "").replace(".v", "") + ".v"
        if tb in files:
            out["tb_name"] = tb
            out["tb"] = _read(src / tb)
    pnr = base / "impl" / "pnr"
    if pnr.is_dir():
        bits = sorted(pnr.glob("*.fs")) or sorted(pnr.glob("*.bin"))
        if bits:
            out.setdefault("bitstream", bits[0].name)
            out.setdefault("bitstream_mb", round(bits[0].stat().st_size / 1048576, 2))
    wave = base / "wave.vcd"
    out["has_wave"] = (base / "wave.json").is_file() or wave.is_file()
    auto_meta = base / "auto.json"
    if auto_meta.is_file():
        try:
            info = json.loads(auto_meta.read_text(encoding="utf-8"))
            for key in ("stages", "sim_verdict", "program_verdict", "programmed"):
                if info.get(key) is not None and key not in out:
                    out[key] = info[key]
        except (OSError, json.JSONDecodeError):
            pass
    return out


# --------------------------------------------------------------------------
# 具体动作
# --------------------------------------------------------------------------
def _design_cmd(payload: dict) -> tuple[list[str], str]:
    """把前端参数翻译成 yunfan design 的命令行。"""
    mode = payload.get("mode") or "text"
    variant = payload.get("variant") or "yunfan"
    cmd = [str(PY), "-u", "-m", "yunfan", "design"]
    if mode == "spec":
        spec = payload.get("spec") or ""
        if spec not in SPECS:
            raise ValueError("未知的示例规格")
        cmd += ["--spec", f"examples/{spec}/spec.json"]
        label = f"设计 · 规格 {spec}"
    else:
        text = (payload.get("text") or "").strip()
        if not text:
            raise ValueError("需求不能为空")
        cmd += ["--text", text]
        label = "设计 · " + text[:24] + ("…" if len(text) > 24 else "")
    if payload.get("sim_only"):
        cmd += ["--mode", "sim-only"]
    if payload.get("device"):
        cmd += ["--device", str(payload["device"])]
    if payload.get("board"):
        cmd += ["--board", str(payload["board"])]
    if variant == "baseline":
        cmd += ["--variant", "baseline"]
    if payload.get("max_repair"):
        cmd += ["--max-repair", str(int(payload["max_repair"]))]
    return cmd, label


def start_design(payload: dict) -> dict:
    cmd, label = _design_cmd(payload)
    return _spawn(cmd, label=label, kind="design", before=_snapshot())


def _new_sim_dir(name: str) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", name or "sim")[:40] or "sim"
    path = RUNS / f"_sim_{safe}_{stamp}"
    (path / "src").mkdir(parents=True, exist_ok=True)
    return path


def start_simulate(payload: dict) -> dict:
    source = payload.get("source") or "code"
    explicit_name = (payload.get("name") or "").strip()
    meta: dict = {"source": source}

    if source == "run":
        src_run = payload.get("run") or ""
        base = _safe_run(src_run)
        if not base:
            raise ValueError("源运行记录不存在")
        src = base / "src"
        rtl_files = sorted(p.name for p in src.glob("*.v") if not p.name.startswith("tb_"))
        tb_files = sorted(p.name for p in src.glob("tb_*.v"))
        if not rtl_files or not tb_files:
            raise ValueError("该运行记录里没有可仿真的 RTL/Testbench")
        rtl_text = _read(src / rtl_files[0])
        tb_text = _read(src / tb_files[0])
        top = payload.get("top") or rtl_files[0].replace(".v", "")
        rtl_name, tb_name = rtl_files[0], tb_files[0]
        name = explicit_name or src_run
        meta["from_run"] = src_run
    else:
        rtl_text = payload.get("rtl") or ""
        tb_text = payload.get("tb") or ""
        top = (payload.get("top") or "").strip()
        if not rtl_text.strip():
            raise ValueError("RTL 代码不能为空")
        if not tb_text.strip():
            raise ValueError("Testbench 不能为空")
        if not IDENT.match(top):
            raise ValueError("顶层模块名不合法（需为 Verilog 标识符）")
        rtl_name, tb_name = f"{top}.v", f"tb_{top}.v"
        name = explicit_name or top

    if "$dumpfile" not in tb_text:
        tb_text = _inject_dump(tb_text)

    dest = _new_sim_dir(name)
    rtl_path = dest / "src" / rtl_name
    tb_path = dest / "src" / tb_name
    rtl_path.write_text(rtl_text, encoding="utf-8")
    tb_path.write_text(tb_text, encoding="utf-8")
    meta.update({"top": top, "name": name})
    (dest / "sim.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    label = f"仿真 · {name}"
    cmd = [str(PY), "-u", str(GUI / "eda_bridge.py"), "simulate",
           "--dir", str(dest), "--top", top, "--rtl", str(rtl_path), "--tb", str(tb_path)]
    return _spawn(cmd, label=label, kind="simulate", run_dir=dest.name)


def _inject_dump(tb: str) -> str:
    """给用户 TB 自动补 $dumpfile/$dumpvars，否则没有波形可看。"""
    if tb.count("endmodule") != 1:
        return tb
    m = list(re.finditer(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)", tb))
    if not m:
        return tb
    top = m[-1].group(1)
    snippet = (
        "\n    initial begin\n"
        '        $dumpfile("wave.vcd");\n'
        f"        $dumpvars(0, {top});\n"
        "    end\n"
    )
    idx = tb.rfind("endmodule")
    return tb[:idx] + snippet + tb[idx:]


def start_program(payload: dict) -> dict:
    run = payload.get("run") or ""
    base = _safe_run(run)
    if not base:
        raise ValueError("运行记录不存在")
    bits = sorted((base / "impl" / "pnr").glob("*.fs")) + sorted((base / "impl" / "pnr").glob("*.bin"))
    if not bits:
        raise ValueError("该运行没有比特流（.fs），先跑一次完整设计")
    device = (payload.get("device") or "").strip()
    real = bool(payload.get("real"))
    top = payload.get("top") or bits[0].stem
    cmd = [str(PY), "-u", str(GUI / "eda_bridge.py"), "program",
           "--dir", str(base), "--top", top]
    if device:
        cmd += ["--device", device]
    if real:
        cmd += ["--real"]
    label = f"烧录 · {bits[0].name}" + ("（真实烧录）" if real else "（dry-run）")
    return _spawn(cmd, label=label, kind="program", run_dir=base.name)


# --------------------------------------------------------------------------
# 全自动：一次授权 → 设计 → 仿真 → 烧录
# --------------------------------------------------------------------------
def _spawn_task(label: str, kind: str, task) -> dict:
    """把一个 Python 任务包成和子进程一样的作业（同样的 SSE 事件流）。"""
    job_id = uuid.uuid4().hex[:12]
    q: queue.Queue = queue.Queue()
    job = {
        "id": job_id, "proc": None, "queue": q, "label": label, "kind": kind,
        "started": time.time(), "run_dir": "", "returncode": None, "result": {},
        "before": set(),
    }
    with JOBS_LOCK:
        JOBS[job_id] = job

    def log(line: str) -> None:
        q.put({"t": "log", "line": line})

    def runner() -> None:
        try:
            payload = task(log, job) or {}
        except Exception as exc:  # noqa: BLE001
            log(f"[错误] {exc}")
            payload = {"ok": False, "error": str(exc)}
        job["returncode"] = 0 if payload.get("ok") else 1
        job["run_dir"] = payload.get("run_dir", job.get("run_dir", ""))
        job["result"] = payload
        q.put({
            "t": "end", "code": job["returncode"], "run_dir": job["run_dir"],
            "elapsed": round(time.time() - job["started"], 1),
            "ok": bool(payload.get("ok")), "result": _enrich(payload),
        })

    threading.Thread(target=runner, daemon=True).start()
    return {"job": job_id, "label": label}


def _run_step(cmd: list[str], log, job: dict) -> tuple[int, dict]:
    """跑一个子步骤，把输出实时转进作业队列，并捕获 @@RESULT@@ 结构化结果。"""
    if os.name == "nt":
        _si = subprocess.STARTUPINFO()
        _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        _si.wShowWindow = 0
        creation = subprocess.CREATE_NEW_CONSOLE
        _startupinfo = _si
    else:
        creation = 0
        _startupinfo = None
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env=_env(), creationflags=creation, startupinfo=_startupinfo,
    )
    job["proc"] = proc
    payload: dict = {}
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        if line.startswith(MARK):
            try:
                payload = json.loads(line[len(MARK):])
            except json.JSONDecodeError:
                pass
            continue
        if not line.startswith("@@YUNFAN_STAGE"):
            _push_node(job, line)
        # 带上当前节点，前端据此做「点开某个节点看该步日志」
        job["queue"].put({"t": "log", "line": line, "node": job.get("current_node", "")})
        log(line)
    proc.wait()
    job["proc"] = None
    return proc.returncode, payload


def _infer_board(device_name: str) -> str:
    """按器件反查开发板档案（引脚表），保证约束用的是对的板子。"""
    if not device_name:
        return ""
    boards = _json_file(ROOT / "yunfan" / "data" / "boards.json").get("boards", [])
    for b in boards:
        if b.get("device") == device_name:
            return str(b.get("name") or "")
    return ""


def start_auto(payload: dict) -> dict:
    """一次授权后无人值守地跑完 设计 → 仿真 → 烧录。"""
    if not payload.get("authorized"):
        raise ValueError("全自动执行需要先授权")
    device = (payload.get("device") or "").strip()
    # 设计阶段必须用同一颗器件 + 对应板卡引脚，否则比特流与板子不匹配
    design_payload = dict(payload)
    if device and not design_payload.get("board"):
        design_payload["board"] = _infer_board(device)
    cmd_design, _ = _design_cmd(design_payload)
    want_program = bool(payload.get("program"))
    real = bool(payload.get("real"))
    bridge = str(GUI / "eda_bridge.py")

    def task(log, job):
        stages: list[dict] = []
        before = _snapshot()

        def node(nid: str, state: str, detail: str = "") -> None:
            job["queue"].put({"t": "node", "id": nid, "state": state, "detail": detail})

        def finish(res: dict) -> dict:
            """把本次全自动的阶段结果落到运行目录，便于历史回看。"""
            try:
                b = _safe_run(res.get("run_dir") or "")
                if b:
                    (b / "auto.json").write_text(
                        json.dumps({
                            "ok": res.get("ok"),
                            "stages": res.get("stages"),
                            "sim_verdict": res.get("sim_verdict"),
                            "program_verdict": res.get("program_verdict"),
                            "programmed": res.get("programmed"),
                            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            except OSError:
                pass
            return res

        # ---- ① 设计 ----
        retry = bool(payload.get("retry", True))
        attempts = 2 if retry else 1
        log("[全自动] ① 设计：需求解析 → RTL → 仿真 → 综合 → 布局布线")
        t0 = time.time()
        design_ok = False
        run_dir = ""
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                log(f"[重试] ① 设计失败，自动重跑（第 {attempt}/{attempts} 次）")
                for nid in ("spec", "rtl", "gate", "tb", "sim", "repair", "pnr"):
                    job["queue"].put({"t": "node", "id": nid, "state": "pending", "detail": "重试中…"})
            before = _snapshot()
            code, _ = _run_step(cmd_design, log, job)
            new = sorted(_snapshot() - before)
            if new:
                run_dir = new[-1]
                job["run_dir"] = run_dir
            design_ok = code == 0 and bool(run_dir)
            if design_ok:
                break
        stages.append({"name": "设计", "ok": design_ok, "s": round(time.time() - t0, 1)})
        if not design_ok:
            log("[全自动] 停在①：设计/实现失败，后续步骤不再执行")
            return finish({"ok": False, "stage": "设计", "run_dir": run_dir, "stages": stages,
                    "error": "设计或实现阶段失败，已中止后续步骤"})

        base = _safe_run(run_dir)
        src = base / "src" if base else None
        rtl_files = sorted(p.name for p in src.glob("*.v") if not p.name.startswith("tb_")) if src else []
        tb_files = sorted(p.name for p in src.glob("tb_*.v")) if src else []
        if not rtl_files:
            return finish({"ok": False, "stage": "设计", "run_dir": run_dir, "stages": stages,
                    "error": "运行目录里没有 RTL，无法继续"})
        top = rtl_files[0][:-2]

        # ---- ② 仿真 ----
        sim: dict = {}
        if tb_files:
            log("[全自动] ② 仿真：iverilog 编译 + 运行 + 解析波形")
            node("sim2", "running")
            t1 = time.time()
            code2, sim = _run_step(
                [str(PY), "-u", bridge, "simulate", "--dir", str(base), "--top", top,
                 "--rtl", str(src / rtl_files[0]), "--tb", str(src / tb_files[0])],
                log, job,
            )
            sim_ok = code2 == 0 and bool(sim.get("ok"))
            waves = (sim.get("waveform") or {}).get("signals", 0)
            node("sim2", "done" if sim_ok else "failed",
                 f"{sim.get('verdict', '')} · {waves} 个信号")
            stages.append({"name": "仿真", "ok": sim_ok, "s": round(time.time() - t1, 1),
                           "verdict": sim.get("verdict")})
            if not sim_ok:
                log("[全自动] 停在②：仿真未通过，不烧录未验证的设计")
                return finish({"ok": False, "stage": "仿真", "run_dir": run_dir, "stages": stages,
                        "sim_verdict": sim.get("verdict"), "error": "仿真未通过"})
        else:
            stages.append({"name": "仿真", "ok": True, "skipped": True})
            node("sim2", "skipped", "该运行没有 Testbench")
            log("[全自动] ② 跳过仿真：该运行没有 Testbench")

        # ---- ③ 烧录 ----
        prog: dict = {}
        prog_ok = False
        if want_program:
            log("[全自动] ③ 烧录：" + ("真实写入板卡 SRAM" if real else "dry-run 校验"))
            t2 = time.time()
            cmd3 = [str(PY), "-u", bridge, "program", "--dir", str(base), "--top", top]
            if device:
                cmd3 += ["--device", device]
            if real:
                cmd3 += ["--real"]
            for attempt in range(1, attempts + 1):
                if attempt > 1:
                    log(f"[重试] ③ 烧录失败，自动重试（第 {attempt}/{attempts} 次）")
                    node("prog", "running", "重试中")
                else:
                    node("prog", "running", "真实烧录" if real else "dry-run")
                code3, prog = _run_step(cmd3, log, job)
                prog_ok = code3 == 0 and bool(prog.get("ok"))
                if prog_ok:
                    break
            cable = "已检测下载器" if prog.get("cable_detected") else "未检测到下载器"
            node("prog", "done" if prog_ok else "failed",
                 f"{prog.get('verdict', '')} · {cable}")
            stages.append({"name": "烧录", "ok": prog_ok, "s": round(time.time() - t2, 1),
                           "verdict": prog.get("verdict")})
            if not prog_ok:
                log("[全自动] 停在③：烧录失败（多半是下载器没连或驱动未装）")
                return finish({"ok": False, "stage": "烧录", "run_dir": run_dir, "stages": stages,
                        "sim_verdict": sim.get("verdict"), "error": "烧录失败"})
        else:
            stages.append({"name": "烧录", "ok": True, "skipped": True})
            node("prog", "skipped", "未启用烧录")

        log("[全自动] 全部完成 ✅")
        return finish({
            "ok": True,
            "run_dir": run_dir,
            "stages": stages,
            "sim_verdict": sim.get("verdict"),
            "program_verdict": prog.get("verdict"),
            "cable_detected": prog.get("cable_detected"),
            "programmed": bool(real and prog.get("ok")),
        })

    label = "全自动 · " + ("真实烧录" if (want_program and real) else "演练")
    return _spawn_task(label, "auto", task)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "YunFanConsole/2.0"

    def log_message(self, fmt: str, *args) -> None:
        pass

    # ---- 输出 ----
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    # ---- GET ----
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send(200, (GUI / "index.html").read_bytes(), "text/html; charset=utf-8")
            return

        if path == "/api/state":
            with JOBS_LOCK:
                running = [{"id": j["id"], "label": j["label"], "kind": j["kind"]}
                           for j in JOBS.values() if j["returncode"] is None]
            recent = []
            if RUNS.is_dir():
                items = [p for p in RUNS.iterdir() if p.is_dir()]
                items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                for p in items[:14]:
                    has_bits = any((p / "impl" / "pnr").glob("*.fs")) if (p / "impl" / "pnr").is_dir() else False
                    recent.append({
                        "name": p.name,
                        "kind": "sim" if p.name.startswith("_sim_") else "design",
                        "bitstream": has_bits,
                        "mtime": int(p.stat().st_mtime),
                    })
            self._json(200, {"running": running, "recent": recent})
            return

        if path == "/api/meta":
            dev = _json_file(ROOT / "yunfan" / "data" / "devices.json").get("devices", [])
            boards = _json_file(ROOT / "yunfan" / "data" / "boards.json").get("boards", [])
            self._json(200, {
                "devices": [{"name": d.get("name"), "part": d.get("part_number"),
                             "version": d.get("device_version")} for d in dev],
                "boards": [{"name": b.get("name"), "device": b.get("device")} for b in boards],
                "specs": SPECS,
            })
            return

        if path == "/api/runinfo":
            base = _safe_run(unquote((qs.get("run") or [""])[0]))
            if not base:
                self._json(404, {"error": "run 不存在"})
                return
            self._json(200, _enrich({"run_dir": base.name}))
            return

        if path == "/api/file":
            base = _safe_run(unquote((qs.get("run") or [""])[0]))
            rel = unquote((qs.get("path") or [""])[0])
            if not base:
                self._json(404, {"error": "run 不存在"})
                return
            target = (base / rel).resolve()
            if not str(target).startswith(str(base)) or not target.is_file():
                self._json(404, {"error": "文件不存在"})
                return
            self._send(200, _read(target).encode("utf-8"), "text/plain; charset=utf-8")
            return

        if path == "/api/wave":
            base = _safe_run(unquote((qs.get("run") or [""])[0]))
            if not base:
                self._json(404, {"error": "run 不存在"})
                return
            cache = base / "wave.json"
            if cache.is_file():
                self._send(200, cache.read_bytes())
                return
            vcd = base / "wave.vcd"
            if not vcd.is_file():
                self._json(404, {"error": "没有波形文件（TB 里需要 $dumpfile/$dumpvars）"})
                return
            try:
                from vcd import parse_vcd

                data = parse_vcd(vcd)
                cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                self._json(200, data)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": f"波形解析失败：{exc}"})
            return

        if path == "/api/stream":
            with JOBS_LOCK:
                job = JOBS.get((qs.get("job") or [""])[0])
            if not job:
                self._json(404, {"error": "job 不存在"})
                return
            self._stream(job)
            return

        self._json(404, {"error": "not found"})

    # ---- SSE ----
    def _stream(self, job: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        q: queue.Queue = job["queue"]

        def push(obj) -> bool:
            try:
                self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                return False

        try:
            while True:
                try:
                    item = q.get(timeout=20)
                except queue.Empty:
                    if not push({"t": "ping"}):
                        return
                    continue
                if not push(item):
                    return
                if item.get("t") == "end":
                    return
        finally:
            self.close_connection = True

    # ---- POST ----
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "JSON 解析失败"})
            return

        routes = {
            "/api/run": start_design,
            "/api/auto": start_auto,
            "/api/simulate": start_simulate,
            "/api/program": start_program,
        }
        if parsed.path in routes:
            try:
                self._json(200, routes[parsed.path](payload or {}))
            except (ValueError, RuntimeError, OSError) as exc:
                self._json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/stop":
            with JOBS_LOCK:
                job = JOBS.get((payload or {}).get("job", ""))
            if not job or job["returncode"] is not None:
                self._json(404, {"error": "任务不存在或已结束"})
                return
            proc = job.get("proc")
            try:
                if proc is not None:
                    proc.kill()
            except OSError:
                pass
            self._json(200, {"stopped": True})
            return

        self._json(404, {"error": "not found"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    RUNS.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"YunFan 控制台已启动： http://{args.host}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
