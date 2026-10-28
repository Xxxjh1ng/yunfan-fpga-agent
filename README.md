# 云帆 YunFan

**面向国产高云（Gowin）FPGA 的多智能体全流程自动化设计框架**

> 需求解析 → 代码生成 → 仿真验证 → 调试修复 → 硬件部署
> 以「云源 EDA（gw_sh）+ Icarus Verilog + Gowin Programmer」构成真正可执行的 EDA-in-the-Loop 闭环。

---

## 1. 这是什么

云帆把「一句自然语言需求」变成「一块能烧进板子的比特流」，中间所有环节都由专业化
智能体协作完成，并且**每一步都以真实工具的输出为准**：

| 阶段 | 智能体 | 产出 | 真实工具 |
| --- | --- | --- | --- |
| 需求解析 | Design Requirement Analyst | 机器可读设计规格（JSON，含端口表/参数/验证意图） | 大模型 + 结构化校验 |
| 代码生成 | RTL Engineer | 可综合 Verilog-2001 | 大模型 + 净化 + 静态门控 |
| 仿真验证 | Verification Engineer | 自验证 Testbench（含逐拍期望值） | Icarus Verilog |
| 调试修复 | Debug Engineer | 最小编辑补丁 / 确定性修复 | 结构化诊断 + 大模型 |
| 硬件部署 | Deployment Engineer | `.cst`/`.sdc` 约束、比特流、烧录 | 云源 `gw_sh` + `programmer_cli` |

与原始单点方案（LLM 直接写 RTL + TB，失败后整段重写）相比，本工程的差异是**工程化**：

1. **规格即契约**：端口表在生成后被静态检查逐字复核，从根上消除「TB 找不到端口」这类失败；
2. **分级门控**：Lint（毫秒）→ 综合（秒）→ 仿真（秒）→ 布局布线（十秒），失败即早停；
3. **结构化诊断**：15 类错误分类体系，把日志变成可路由、可统计、可回归的对象；
4. **最小编辑修复**：优先确定性规则，其次 find/replace 补丁，最后才整模块重写；
5. **零第三方依赖**：仅用 Python 标准库（`urllib` 直连 OpenAI 兼容接口），
   不需要 `torch`/`autogen`/`transformers`，在普通笔记本上即可复现。

## 2. 快速开始

```powershell
# 1) 环境自检（自动发现云源 EDA / Icarus / Programmer / API Key）
python -m yunfan doctor

# 2) 用自然语言直接设计（全流程 + 生成比特流）
python -m yunfan design --text "设计一个 4 位可加载使能计数器，50MHz，低有效异步复位"

# 3) 用规格文件驱动（推荐：结果可复现）
python -m yunfan design --spec examples/counter/spec.json

# 4) 只生成约束，不跑流程
python -m yunfan constraints --spec examples/vga_timing/spec.json

# 5) 批量评测（论文实验）
python -m yunfan eval --designs counter,pwm,crc32,uart_tx,vga_timing --variants yunfan,baseline --seeds 3

# 6) 汇总已有记录
python -m yunfan table --dir runs/eval
```

配置放在项目根目录 `.env`（参考 `.env.example`）：

```ini
YUNFAN_API_KEY=sk-...
YUNFAN_BASE_URL=https://api.deepseek.com
YUNFAN_MODEL=deepseek-chat
```

工具路径无需配置：`yunfan doctor` 会在 `C:\Gowin\Gowin_V*\IDE\bin`、`C:\iverilog\bin`
等常见位置自动发现，也可用 `YUNFAN_GW_SH` / `YUNFAN_IVERILOG` 等环境变量覆盖。

## 3. 一次运行留下什么

每次设计都会在 `runs/<设计>_<变体>_s<种子>_<时间戳>/` 下留下完整证据链：

```
spec.json          锁定的设计规格（唯一事实源）
src/*.v,cst,sdc    最终 RTL / Testbench / 约束
yunfan_synthesize.tcl, yunfan_implement.tcl   自动生成的云源脚本
logs/*.log         每一步的工具原始输出
impl/gwsynthesis/  综合网表与报告
impl/pnr/*.fs      比特流；*.rpt.txt 资源；*_tr_content.html 时序
events.jsonl       逐事件轨迹（谁、在第几秒、做了什么、花了多少 token）
trace.json         同上（可直接被程序读取）
summary.md         人类可读的运行摘要
<run_id>.json      结构化运行记录（指标、诊断分布、修复方法序列）
```

## 4. 基准设计与评测

`examples/` 内置 5 个可直接上板的基准设计，覆盖从组合逻辑到多级状态机的难度梯度：

| 设计 | 规模 | 难度 | 关键考点 |
| --- | --- | --- | --- |
| `counter` | 6 端口 | 入门 | 异步复位、load/en 优先级、回绕 |
| `pwm` | 4 端口 | 入门 | 组合比较输出、占空比边界 |
| `crc32` | 5 端口 | 中等 | 多项式/位序、单拍延迟 |
| `uart_tx` | 6 端口 | 中等 | 状态机 + 位定时 + 握手 |
| `vga_timing` | 6 端口 | 较难 | 长周期计数、同步相位关系 |

每个设计都提供：

* `spec.json` —— 机器可读规格；
* `ref.v` —— 人工确认的参考实现（金标准）；
* 内置**周期精确参考模型**（`yunfan/rtl/oracles.py`），可用来自动生成自验证激励。

### 4.1 自检（不需要大模型）

```powershell
python scripts/selfcheck.py            # 5 个设计：参考 RTL → 自验证 TB → 真实综合/PnR
```

### 4.2 故障注入修复基准（衡量「修复能力」）

```powershell
python scripts/repair_bench.py counter crc32
python scripts/repair_bench.py --faults=F01,F05
```

对参考实现施加 9 类单点变异（缺 `endmodule`、`reg`→`wire`、插入 `#` 延时、
删 `end`、端口改名、位宽篡改、比较符/常量变异、非法引脚），观察闭环能否恢复。

### 4.3 实测结果（本机，2026-10）

实验条件：器件 GW5A-25A（`GW5A-LV25MG121NC1/I0`），云源 V1.9.9 Beta-4 Education，
Icarus Verilog v14，模型 `deepseek-chat`，温度 0.7，5 个设计 × 3 次采样 = 30 次运行。

| 指标 | 云帆 YunFan | 原始单点方案 |
| --- | --- | --- |
| 首次仿真通过率 | **100.0%** | 46.7% |
| 端到端成功率（含比特流） | **100.0%** | 60.0% |
| 平均修复轮次 | **0.00** | 0.20 |
| 平均 token / 次 | **1 696** | 7 392 |
| 平均耗时 / 次 | **7.2 s** | 13.8 s |

故障注入基准（37 个单点缺陷）：**36 个被自主修复（97.3%）**，其中
**17 个在调用 EDA 之前被静态门控拦截**，8 个由净化层直接修复；仅 1 例
（多级嵌套状态机丢失 `end`）未恢复，已在论文中作为限制讨论。

实测资源与时序（云源布局布线报告）：

| 设计 | 逻辑资源 | 寄存器 | Fmax (MHz) |
| --- | --- | --- | --- |
| counter | 7 | 4 | 465.3 |
| pwm | 18 | 8 | 633.2 |
| crc32 | 46 | 32 | 264.7 |
| uart_tx | 48 | 31 | 125.1 |
| vga_timing | 38 | 20 | 318.4 |

完整数据见 `evidence/eval_summary.json`、`evidence/repair_bench.json`，
论文（含 5 张图 / 8 张表）见 `docs/`；证据文件说明见 `evidence/README.md`。

## 5. 目录结构

```
yunfan/
├── yunfan/
│   ├── config.py            配置、器件/板卡/工具链发现
│   ├── cli.py               命令行入口
│   ├── llm/                 大模型接入（OpenAI 兼容 + 离线回放 + 提示词注册表）
│   ├── rtl/                 抽取/净化/接口解析/静态检查/自验证 TB/参考模型
│   ├── diagnostics/         15 类错误分类体系 + 多工具日志解析
│   ├── eda/                 云源适配层 / Icarus 引擎 / 约束生成 / Mock
│   ├── agents/              五类专业智能体
│   ├── orchestration/       规格、闭环管道、指标与 Pass@k
│   ├── eval/                批量评测与故障注入
│   └── data/                器件库、开发板库
├── examples/                5 个基准设计（规格 + 参考实现）
├── tests/                   50 个单元测试（无需网络与 EDA）
└── scripts/                 自检、故障注入基准、示例生成
```

## 6. 设计要点（为什么这样做）

**为什么要「规格即契约」？**
LLM 生成 RTL 时最常见的失败不是逻辑错，而是端口名/位宽漂移，导致 Testbench 无法绑定。
把端口表变成规格的一部分，并在生成后逐字比对，可以在毫秒级发现这类问题。

**为什么自验证 Testbench 由模板合成，而不是让模型写？**
验证的判据必须独立于被测对象。云帆把「激励计划」交给周期精确的参考模型，
Testbench 只负责按时序施加激励并比对期望值——判据不再由模型自己决定。

**为什么修复要「最小编辑」？**
整段重写会丢掉已经被验证正确的部分，还容易触发截断；find/replace 补丁让改动可审计、
可回滚，也显著降低 token 消耗。

**为什么还要确定性规则？**
`#` 延时、`wire` 被过程赋值、端口漂移这类问题是**机械可判定**的，用规则修复比调用大模型
更快、更稳、更省钱。云帆的修复顺序是：规则库 → 大模型补丁 → 整模块重写。

## 7. 已知限制

* 参考模型（oracle）目前覆盖 5 个基准设计族；新设计若没有 oracle，会回退到 LLM 直写 Testbench。
* 引脚自动分配只保证「合法可用」，不保证与真实板卡原理图一致；上板前请核对 `boards.json`。
* 云源教育版仅支持部分器件（GW5A-25A / GW2A-18C / GW1NSR-4C 等），
  换成商业版器件只需修改 `yunfan/data/devices.json`。
* 烧录默认 `dry-run`，真正烧录需显式 `--program-real`，避免误烧板卡。

## 8. 许可证与致谢

本工程为参赛作品，代码可自由用于学习与研究。
方法论上参考了 EDA-in-the-Loop 多智能体 RTL 自动化与 VHDL-Eval 的自验证激励思想，
工具链适配依托高云云源 EDA 的 Tcl 命令行接口。
