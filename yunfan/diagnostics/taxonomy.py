"""错误分类体系（Error Taxonomy）。

把「工具日志」映射为 15 个稳定的诊断类别，每类绑定一套修复策略。
这是「日志结构化诊断」的核心：把非结构化文本变成可路由、可统计、
可回归的工程对象。类别编号会出现在论文表格与实验数据中。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Taxon:
    id: str
    name_zh: str
    name_en: str
    severity: str
    deterministic: bool  # 是否可由确定性规则自动修复
    strategies: Tuple[str, ...]
    stage: str  # lint / synth / sim / pnr
    note: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "name_zh": self.name_zh,
            "name_en": self.name_en,
            "severity": self.severity,
            "deterministic": self.deterministic,
            "strategies": list(self.strategies),
            "stage": self.stage,
            "note": self.note,
        }


TAXONOMY: Dict[str, Taxon] = {
    "E01": Taxon(
        "E01", "结构/语法不配对", "StructuralSyntax", "error", True,
        ("补齐缺失的 end / endcase", "删除多余的关键字", "重新生成完整模块"),
        "lint", "begin/end、case/endcase、括号不配对或关键词拼写错误",
    ),
    "E02": Taxon(
        "E02", "模块不完整/被截断", "ModuleIncomplete", "error", True,
        ("在末尾补齐 endmodule", "续写被截断的函数体", "重新生成完整模块"),
        "lint", "输出被 max_tokens 截断，缺少 endmodule 或函数体",
    ),
    "E03": Taxon(
        "E03", "标识符未声明", "UndeclaredIdentifier", "error", False,
        ("按用途补 reg/wire/integer 声明", "修正拼写错的信号名"),
        "synth", "工具报 not declared / unknown identifier",
    ),
    "E04": Taxon(
        "E04", "wire 被过程赋值", "WireAssignedInAlways", "error", True,
        ("把该声明改为 reg", "或改为 assign 连续赋值"),
        "lint", "always 块内对 wire 赋值，是 LLM 最高频的语法错误",
    ),
    "E05": Taxon(
        "E05", "多驱动冲突", "MultipleDrivers", "error", False,
        ("合并到同一个 always 块", "改为单一 assign 驱动", "引入中间信号"),
        "lint", "同一信号被多个 always/assign 驱动",
    ),
    "E06": Taxon(
        "E06", "锁存器推断", "LatchInference", "warning", False,
        ("补 else 分支", "在组合块开头赋默认值", "case 补 default"),
        "synth", "组合逻辑条件不完整导致的电平敏感锁存器",
    ),
    "E07": Taxon(
        "E07", "位宽不匹配", "WidthMismatch", "warning", False,
        ("显式补齐位宽", "用 {N{1'b0}} 扩展", "调整信号声明"),
        "synth", "赋值两侧位宽不一致，可能导致静默截断",
    ),
    "E08": Taxon(
        "E08", "不可综合结构", "NonSynthesizable", "error", True,
        ("删除 # 延时", "删除 initial 块", "删除 $display/$finish", "替换 real/time 类型"),
        "synth", "延时、系统任务、initial 等只存在于仿真域的构造",
    ),
    "E09": Taxon(
        "E09", "端口/接口不匹配", "PortMismatch", "error", True,
        ("按规格书端口表重写模块头", "按端口表重写例化语句"),
        "lint", "端口名、方向、位宽与规格书不一致；Testbench 无法绑定",
    ),
    "E10": Taxon(
        "E10", "物理约束非法", "PinConstraintInvalid", "error", True,
        ("从器件合法引脚表重新选择引脚", "修正 IO_LOC 语法", "核对 cst 文件"),
        "pnr", "IO_LOC 指向当前封装不存在的 pad（CT1113）",
    ),
    "E11": Taxon(
        "E11", "功能不匹配", "FunctionalMismatch", "error", False,
        ("依据首次失配周期定位阶段", "修正计数上限/移位方向/握手时序"),
        "sim", "仿真跑通但期望值与实际值不符",
    ),
    "E12": Taxon(
        "E12", "仿真超时/挂死", "SimulationTimeout", "error", False,
        ("检查 done/ready 是否会拉高", "排除组合环", "补全状态机出口"),
        "sim", "看门狗触发，握手信号未在预算周期内变化",
    ),
    "E13": Taxon(
        "E13", "资源超限", "ResourceOverflow", "error", False,
        ("降低并行度", "复用运算单元", "改用串行实现"),
        "pnr", "LUT/FF/BSRAM/DSP 超出器件容量",
    ),
    "E14": Taxon(
        "E14", "时序违例", "TimingViolation", "warning", False,
        ("插入流水线寄存器", "拆分长组合路径", "降低时钟频率"),
        "pnr", "建立/保持时间不满足，最大频率低于约束",
    ),
    "E15": Taxon(
        "E15", "赋值风格错误", "SequencingStyle", "warning", True,
        ("时序块改非阻塞 <=", "组合块改阻塞 ="),
        "lint", "阻塞/非阻塞混用导致仿真与综合语义不一致",
    ),
}

# 兼容别名
ALIASES = {
    "E16": "E02",
    "E17": "E11",
    "UNKNOWN": "E01",
}


def get_taxon(taxon_id: str) -> Taxon:
    tid = (taxon_id or "").upper()
    tid = ALIASES.get(tid, tid)
    return TAXONOMY.get(tid, TAXONOMY["E01"])


def all_taxa() -> List[Taxon]:
    return [TAXONOMY[k] for k in sorted(TAXONOMY)]


def deterministic_taxa() -> List[str]:
    return [t.id for t in all_taxa() if t.deterministic]


def strategies_for(taxon_id: str) -> Tuple[str, ...]:
    return get_taxon(taxon_id).strategies


def taxonomy_table() -> List[Dict[str, object]]:
    return [t.as_dict() for t in all_taxa()]
