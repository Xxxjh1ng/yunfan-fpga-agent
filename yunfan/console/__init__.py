"""云帆可视化控制台。

把「设计 → 仿真 → 烧录」编排成九个可观测节点，提供实时日志、波形查看与历史回放。
零第三方依赖，仅用标准库（HTTP + SSE）。

用法
----
    python -m yunfan.console                 # 起在 127.0.0.1:8765
    python -m yunfan.console --port 9000 --open
    yunfan console --open                    # 等价入口
"""
from .server import make_server, serve  # noqa: F401

__all__ = ["make_server", "serve"]
