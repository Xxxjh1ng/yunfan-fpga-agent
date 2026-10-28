# evidence —— 实验结果原始记录

| 文件 | 内容 |
| --- | --- |
| `eval_summary.json` | 主实验汇总：5 个基准设计 × 3 次采样 × 2 种方案（云帆 / 原始单点方案），含每次运行的指标、资源、时序与事件流 |
| `eval_records.jsonl` | 同上，逐行一条运行记录（便于 `jq`/脚本处理） |
| `repair_bench.json` | 故障注入修复基准：37 个单点缺陷的注入类别、首次捕获阶段、修复方式与轮次 |
| `diagnostics_eval.json` | 日志诊断语料回归结果（15 类 × 3 种工具方言） |
| `selfcheck.json` | 参考实现 → 自验证 Testbench → 云源综合/布局布线的自检结果 |

这些文件由下列命令生成，可直接复跑：

```powershell
python -m yunfan eval --designs counter,pwm,crc32,uart_tx,vga_timing `
       --variants yunfan,baseline --seeds 3 --out runs/eval
python scripts/repair_bench.py
python scripts/eval_diagnostics.py
python scripts/selfcheck.py
```

> 说明：仓库未包含 `runs/` 下的中间产物（比特流每个约 5.7 MB、网表与报告），只保留上述结论级证据，以便仓库体积可控。
