# 本次更新：真机验证（2026-10-29）

板卡：芯路恒 ACG720-V3-BJ（高云 GW5AT-LV138PG484AC1/I0，板载 FT2CH 下载器）

## 新增

- `scripts/hw_demo.py`——上板自检（探测下载器 → 综合布线 → 烧录 SRAM/Flash）
- `scripts/hw_run.py`——真机演示运行器（流水灯 / 拨码 PWM 调光 / 按键计数器）
- `scripts/import_pinout.py`——从厂商管脚分配表 xlsx 自动导入板卡引脚档案
- `examples/blink/`——最小上板实例（2 引脚、无复位依赖的 1 Hz 闪灯）

## 修改

- `docs/上板指南.md`——整篇重写：ACG720 实测引脚、电源规格、驱动坑、license 两种路线、
  4 个演示命令、8 条踩坑记录、结束测试流程
- `README.md`——新增 §4.4 真机验证（工具链、Fmax、烧录耗时、确认引脚）
- `yunfan/data/devices.json`——新增 GW5AT-138（GW5AT-LV138PG484AC1/I0，Version B）
- `yunfan/data/boards.json`——新增 acg720 档案（clk/led/sw/key/uart 全部实测引脚）
- `yunfan/config.py`——工具链发现改为版本号自然排序，且优先非 Education 安装
- `yunfan/eda/gowin.py`——`set_device` 支持 `-device_version`（138B/138C 共用 part number 时必须）

## 实测结果

| 设计 | Fmax | 比特流 | 烧录 |
| --- | --- | --- | --- |
| blink（1 Hz） | 117.7 MHz | 33.08 MB | 7.5 s |
| led_water（流水灯） | 187.6 MHz | 33.08 MB | ~8 s |
| pwm_sw（拨码调光） | 589.5 MHz | 33.08 MB | ~8 s |

无敏感信息：不含 `.env`、不含 license 文件、不含本机路径。
