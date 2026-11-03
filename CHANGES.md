# 本次更新：控制台并入项目 + 图形化配置（2026-11-03）

## 变更

- **控制台从根目录 `gui/` 迁入 `yunfan/console/` 子包**，成为项目的一等模块：
  - 新增 `yunfan/console/__init__.py`（`make_server` / `serve`）与 `__main__.py`；
  - 新增命令行入口：`python -m yunfan.console` 与 `yunfan console [--port] [--open]`；
  - `pyproject.toml` 的 package-data 增加 `console/*.html`、`*.ico`、`*.png`，`pip install -e .` 后直接可用；
  - `app.py`（桌面版）与 `控制台.cmd` 同步改为新路径，exe 已重新打包验证。
- **新增设置面板**（评委/用户可自助填写）：
  - 右上角「⚙ 设置」：API Key、Provider、BASE_URL、MODEL、`YUNFAN_GW_SH` / `YUNFAN_IVERILOG` / `YUNFAN_PROGRAMMER_CLI`；
  - 保存写入项目根目录 `.env`（保留其它自定义键），已配置的 Key 以 `sk-007**********5cb9` 形式回显、留空不覆盖；
  - 「测试环境」按钮后台运行 `yunfan doctor`，把四项检查结果回显在面板内；
  - 新增接口：`GET /api/config`、`POST /api/config`、`POST /api/config/test`；
  - 未配置 Key 时页面顶部常驻黄色提醒条；深链接 `#settings` 可直接打开面板。
- 节点状态全量落盘到 `auto.json`，历史回放时可完整复原九节点状态；
- 新增深链接 `#run=<运行目录>[&tab=wave]`，打开即回放某次运行。

## 默认目标改为 ACG720

- `devices.json` / `boards.json` 顺序调整：**GW5AT-138 与 acg720 排在第一位**，
  界面下拉、CLI、编程器在未显式指定时都落到这块板子；
- `config.py` 的内置默认值同步改为 `GW5AT-138` / `acg720`；
- 控制台 `/api/meta` 增加 `defaults` 字段，前端在无本地记录时按它预选；
- 修复：未显式传 `--device` 时设计用 138、烧录却回落到 `GW5A-25A` 导致 `ID code mismatch`
  （现在设计、烧录、编程器三处共用同一个默认器件解析）。

## 引脚映射补齐

- 语义表新增通用控制/数据/输出端口：`en|load|we` → 按键，`din|data` → 拨码，`q|dout|count` → LED；
- 语义命中但引脚已被占用时，自动顺延到同族的下一个空闲引脚（例如第二个按键）。
  效果：`examples/counter` 在 ACG720 上从 58% 覆盖率提升到 **100%**，无需厂商封装库也能映射。

## 修复

- **Testbench 节点误显示为失败**：无参考模型时流程走「LLM 直写 Testbench」分支，
  该分支此前没有发出完成事件，收尾时被误判为失败；已在控制台补齐该分支的节点状态。
- **烧录失败提示按原因区分**：`device-mismatch` 时节点显示「器件与板卡不匹配」，
  底部提示直接给出改法（把「目标器件」改成 GW5AT-138、板卡选 acg720），不再笼统地说“下载器没连”。
- **记住上次的器件与板卡**：两者都写入 localStorage，下次打开自动回填，避免重复选错。
- **器件选错时的烧录报错难以理解**：`programmer_cli` 只回一行 `Error: ID code mismatch.`。
  现在会先打印中文说明并给出 `verdict=device-mismatch`：
  「本次按 GW5A-25A 生成比特流，但下载器回读的芯片 ID 不同，已中止烧录；
  请在「目标器件 / 目标板卡」中选择板子实际型号（ACG20 → GW5AT-138 / acg720）」。

## 文档

- `README.md`：新增 §2.1「可视化控制台」（入口、九节点、设置面板）；
- `docs/本地部署与使用说明.md`：路径更新为 `yunfan/console/`，补充设置面板说明；
- `云帆YunFan_技术报告.docx/.pdf`：控制台相关表述同步为子包与设置面板（PDF 重新渲染，10 页）。

---

# 上一次更新：可视化控制台 + 板卡自检示例（2026-11-02）

## 新增

- **`gui/` 本地控制台**（零第三方依赖，只用标准库）
  - `gui/server.py`：HTTP + SSE 服务，编排 `yunfan design` 子进程、仿真与烧录作业
  - `gui/index.html`：单页前端，九节点流程可视化、实时日志、波形 canvas、历史回看
  - `gui/vcd.py`：极简 VCD 解析器（把波形转成前端可画的结构）
  - `gui/eda_bridge.py`：在项目 venv 内执行仿真/烧录的桥接脚本（控制台 exe 不带 yunfan 包）
- **桌面版**：`app.py`（pywebview + WebView2 原生窗口）、`控制台.cmd`（浏览器入口）、`gui/make_icon.py`
- **三个可上板示例**（`spec.json` + `ref.v` + 周期精确参考模型）
  - `examples/led_water/`：8 位流水灯，`DIV=12500000`（0.25 s）
  - `examples/key_count/`：拨码计数，`DIV=5000000`（0.1 s），拨下保持
  - `examples/vga_bars/`：640x480 VGA 彩条，单时钟域 + phase 二分频
- **`implementation_rules` 字段**（`DesignSpec`）：把实现约定（时钟域、打拍方式等）作为
  `extra_rules` 传给 RTL 智能体，避免「行为等价但时序约定不同」导致验证失败

## 修改

- `yunfan/rtl/testbench.py`：支持 `verification.param_overrides`，Testbench 例化时覆盖参数
  （如 VGA 彩条仿真用 `DIV=4`、上板用默认值）
- `yunfan/eda/constraints.py`：新增 `sw` / `vga_r` / `vga_g` / `vga_b` / `hsync` / `vsync` / `vga_clk`
  语义映射；标量信号支持直接按引脚名匹配
- `yunfan/eda/gowin.py`
  - `set_device` 改读云源自带 `device_info.csv` 消歧（GW5A-25A/25B、GW5AT-138B/138C 共用 part number）
  - `program()` 重写：dry-run 只扫描下载器、不执行烧录命令；真实烧录 stdout 重定向到文件；
    失败时用 WMI 创建进程重试（打包 exe 场景）
- `yunfan/data/boards.json`：acg720 补 28 个 VGA 引脚（GM7123 模块，取自通用管脚分配表）
- `pyproject.toml`：去掉 UTF-8 BOM（`tomllib` 不接受 BOM，否则 `pip install -e .` 失败）

## 默认目标改为 ACG720

- `devices.json` / `boards.json` 顺序调整：**GW5AT-138 与 acg720 排在第一位**，
  界面下拉、CLI、编程器在未显式指定时都落到这块板子；
- `config.py` 的内置默认值同步改为 `GW5AT-138` / `acg720`；
- 控制台 `/api/meta` 增加 `defaults` 字段，前端在无本地记录时按它预选；
- 修复：未显式传 `--device` 时设计用 138、烧录却回落到 `GW5A-25A` 导致 `ID code mismatch`
  （现在设计、烧录、编程器三处共用同一个默认器件解析）。

## 引脚映射补齐

- 语义表新增通用控制/数据/输出端口：`en|load|we` → 按键，`din|data` → 拨码，`q|dout|count` → LED；
- 语义命中但引脚已被占用时，自动顺延到同族的下一个空闲引脚（例如第二个按键）。
  效果：`examples/counter` 在 ACG720 上从 58% 覆盖率提升到 **100%**，无需厂商封装库也能映射。

## 修复

- 打包 exe 里烧录报 `Error: MAINCMD module not found.` —— 改用 WMI 创建进程后可正常烧录
- dry-run 实际会执行烧录命令（会误写板卡）—— 改为只做下载器扫描
- 器件名歧义导致所有设计 PnR 失败 —— 已按器件库自动消歧

## 实测（本机 ACG720 / GW5AT-138B / FT2CH）

| 设计 | 来源 | Fmax | 资源 | 烧录 |
| --- | --- | --- | --- | --- |
| led_water | 模型生成 | 191.0 MHz | LUT 55 / FF 32 | ✅ |
| key_count | 模型生成 | 165.7 MHz | LUT 66 / FF 27 | ✅ |
| vga_bars | 模型生成（带 implementation_rules） | 213.3 MHz | LUT 47 / FF 23 | ✅ |
| vga_bars | 参考实现 | 195.4 MHz | LUT 48 / FF 23 | ✅ |

全自动链路（设计 → 仿真 → 烧录）实测 40–52 s/次。

无敏感信息：不含 `.env`、不含 license 文件、不含本机绝对路径。

---

# 上一次更新：真机验证（2026-10-29）

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
