"""诊断层：错误分类体系 + 多工具日志解析。"""

from __future__ import annotations

from .parser import Diagnostic, classify, parse_log, primary_taxon, summarize
from .taxonomy import TAXONOMY, Taxon, all_taxa, deterministic_taxa, get_taxon, strategies_for, taxonomy_table

__all__ = [
    "Diagnostic",
    "classify",
    "parse_log",
    "primary_taxon",
    "summarize",
    "TAXONOMY",
    "Taxon",
    "all_taxa",
    "deterministic_taxa",
    "get_taxon",
    "strategies_for",
    "taxonomy_table",
]
