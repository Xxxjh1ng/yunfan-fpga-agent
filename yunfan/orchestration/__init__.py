"""编排层：规格、流水线、指标。

注意：``pipeline`` 依赖 ``agents``，而 ``agents`` 需要 ``orchestration.spec``，
因此这里用惰性导入避免循环依赖。
"""

from __future__ import annotations

from typing import Any

from .metrics import RunRecord, aggregate, compare_variants, pass_at_k, to_markdown
from .spec import DesignSpec

__all__ = [
    "DesignSpec",
    "RunRecord",
    "aggregate",
    "compare_variants",
    "pass_at_k",
    "to_markdown",
    "Pipeline",
    "PipelineConfig",
]


def __getattr__(name: str) -> Any:  # pragma: no cover - 惰性导入
    if name in ("Pipeline", "PipelineConfig"):
        from . import pipeline as _pipeline

        return getattr(_pipeline, name)
    raise AttributeError(name)
