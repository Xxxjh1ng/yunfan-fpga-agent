r"""云帆 YunFan 桌面控制台（原生窗口）。

把 yunfan/console/server.py 起在本地端口，再用 WebView2 原生窗口承载同一个界面：
没有地址栏、没有标签页，关掉窗口即退出，行为就是一个普通桌面软件。

用法
----
    .\.venv\Scripts\python.exe app.py            # 原生窗口
    .\.venv\Scripts\python.exe app.py --browser  # 退化成默认浏览器打开
    .\.venv\Scripts\python.exe app.py --headless # 只起服务（调试用）

打包成 exe 见 build_app.ps1。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def project_root() -> Path:
    """定位项目根目录（含 yunfan/console/server.py 的那一层）。

    打包成 exe 后从 exe 所在目录向上找，所以 exe 放在项目根目录或 dist-app 里都能跑。
    """
    if getattr(sys, "frozen", False):
        start = Path(sys.executable).resolve().parent
    else:
        start = Path(__file__).resolve().parent
    for cand in [start, *start.parents]:
        if (cand / "yunfan" / "console" / "server.py").is_file():
            return cand
    return start


ROOT = project_root()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "yunfan" / "console"))


def pick_port(preferred: int) -> int:
    for port in [preferred, 0]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("无法分配本地端口")


def wait_ready(url: str, timeout: float = 10.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def _hide_own_console() -> None:
    """打包成控制台程序后，启动瞬间把黑窗口藏起来。

    保留控制台是刻意的：云源的 programmer_cli 在没有控制台的进程树里会报
    "MAINCMD module not found"，所以 exe 必须以「有控制台」的方式启动，
    再把自己的窗口隐藏，效果与 GUI 程序一致。
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def main() -> int:
    _hide_own_console()
    ap = argparse.ArgumentParser(description="云帆 YunFan 桌面控制台")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--browser", action="store_true", help="用默认浏览器打开，而不是原生窗口")
    ap.add_argument("--headless", action="store_true", help="只启动服务，不开窗口")
    args = ap.parse_args()

    if not (ROOT / "yunfan" / "console" / "server.py").is_file():
        print(f"[错误] 找不到 {ROOT / 'yunfan' / 'console' / 'server.py'}，请把程序放在项目根目录", file=sys.stderr)
        return 2

    from server import Handler, ThreadingHTTPServer  # yunfan/console/server.py

    port = pick_port(args.port)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{port}"
    if not wait_ready(url):
        print("[错误] 本地服务未能就绪", file=sys.stderr)
        return 3
    print(f"控制台已就绪：{url}", flush=True)

    if args.headless:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return 0

    if args.browser:
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return 0

    try:
        import webview
    except Exception as exc:  # 没装 pywebview 时优雅退化
        print(f"[提示] 原生窗口不可用（{exc}），改用浏览器打开", file=sys.stderr)
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return 0

    webview.create_window(
        "云帆 YunFan · 高云 FPGA 自动化设计控制台",
        url,
        width=1380,
        height=920,
        min_size=(1020, 720),
        background_color="#0e1118",
    )
    try:
        webview.start()
    finally:
        # 关窗口时把还在跑的 EDA 子进程一起收掉，避免残留 gw_sh / iverilog
        try:
            import server as _srv

            for job in list(_srv.JOBS.values()):
                proc = job.get("proc")
                if proc is not None and proc.poll() is None:
                    proc.kill()
        except Exception:
            pass
        srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
