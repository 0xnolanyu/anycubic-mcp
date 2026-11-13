---
name: kx-print-pipeline
description: KX（Anycubic Kobra X）多色打印全流程经验：Orca 切片策略（逐层/逐件/分板决策树）、逐件模式 150mm 间距规则、首层散点图+T序列双验证、发送五查防呆、slots 基数、API 链路等实机验证教训。当任务涉及 KX/Anycubic 多色打印、Orca MCP 切片、发送 G-code 到打印机、排查打印事故时使用。
---

# KX（Anycubic Kobra X）打印全流程实战经验

适用：Anycubic Kobra X + ACE 四色 + Orca MCP（MaxEllis 定制版 2.5，127.0.0.1:13130）+ anycubic-mcp（云端直传）。以下结论经 2026-11-11~13 旋风豌豆/魅惑菇/沥水篮/毁灭菇系列实机验证。

## 工具链与路径

- Orca MCP driver：`E:\workspaces\dsh\3d打印\_tmp3d\orca_mcp_driver.js`（读 JSON steps；NODE_PATH=C:\Users\Administrator\.dsh\profiles\desktop\node_modules）
- 发送 MCP：`anycubic-mcp`（printer_status / send_print_job / verify_gcode / queue_*）
- 摆盘防重叠：`E:\workspaces\dsh\3d打印\脚本\orca_layout.py`（check/fix/move）
- G-code 源目录：C:\Users\Administrator\.orcaslicer-mcp\gcode\（save_gcode 固定写这里，再复制到工作区）

## 切片参数（KX 预设必改项）

- 预设「Anycubic Kobra X 0.4 nozzle」+「0.16/0.20mm」工艺：默认速度超流量上限（check_profile_physics 报 flow_ceiling fail）。降速表：gap_infill/sparse_infill/internal_solid_infill=170、inner_wall=180、outer_wall/top_surface=140、initial_layer=45。
- 默认墙 2 → 重要件改 3；支撑 enable_support=1、threshold_angle=30、tree(auto)。
- KX 固件宏：开头 `G9111 bedTemp=60 extruderTemp=215/220`；切片必须用 Orca 2.5 + 导入的官方 KX 预设（gcode_flavor=klipper），别用泛用 Marlin 配置。

## 多色分板策略（核心决策树）

1. **逐层模式（by layer）**：所有件同板逐层换色。换料次数≈层内跨色次数×层数（曾 267 次/2h31m/40.8g）。适合件都矮、颜色穿插。
2. **逐件模式（by object）**：`print_sequence="by object"`，打完一件换料再打下一件。实测 267→2 次换料、省 53 分钟+14.5g、无擦料塔（与塔互斥）。适合件数≤4 且能拉开间距。
3. **分板单色**：每板一色零换料。适合颜色数≤槽位数且件允许分组。
- 混色残留缓解：换色顺序安排**同色系相邻**（青→黄绿→黄 几乎无残留）。
- 上机前必算：换料次数 = G-code 独立 T 切换数 - 1（首 T 是初始装料）。

## 逐件模式摆盘规则（血泪教训）

- **间距 150mm+ 对角摆**：Orca 碰撞校验口径比 extruder_clearance_radius=65 严格（疑似 2×65=130 膨胀口径），90mm 净空仍报「组合体太高，会发生碰撞」。
- **先矮后高**：矮于 30mm（height_to_rod）的件不挡喷嘴组件横移；两个高件摆最远对角。Orca 自动排序通常已最优。
- 参考摆法：三件 = 左下（最高）/右上（次高）/左上（最矮），各占一角。

## 交付前双验证（必做，已有工具）

1. **verify_gcode**（anycubic-mcp 内置）：首层散点聚类 + 工具同区互渗检测（≥50% 互渗率判重叠；擦料塔是多工具共用区不会误报）。糊板事故文件 ok=False，好版 ok=True，4 真样本验证。
2. **T 序列统计**：verify_gcode 返回 t_sequence + tool_changes，对照预期色→槽映射。
3. `move_object_to_plate` 只改板归属**不重排 XY**——移板后必跑 orca_layout check 或 verify_gcode（曾因此糊板报废）。
4. `check_placement` 用世界坐标判 fits（不扣板 origin 312 偏移），小床误报可忽略，以切片成功为准。

## 发送五查（anycubic-mcp 已内置，缺一不可）

send_print_job 自动执行：①is_printing==1（1=空闲待命，2=忙碌）②视觉清盘（Agora 截帧→litellm deepseek-v4.1-flash，true/false 严格协议：首行只回小写 true/false）③历史查重（同名 finished→拒；stoped→放行=需重打）④耗材映射（**slots 1 基**为公共接口语义；API 0 基转换只在 _slots_to0 内部发生）⑤上传链路 lockStorageSpace→PUT preSignUrl→newUploadFile→5×3s 重试避开 No slicer found→sendOrder。

- **dry_run=True**：跑全部检查返回检查单，不上传不实发——给用户过目后再 dry_run=False。
- 云端有忙时防呆（Device busy, operation unavailable），但客户端五查仍是第一道闸。
- 开打不可逆：除非用户已明确说「发」，先 dry_run。

## 已知坑速查

- slots 基数混乱是两场事故根源：人类说"槽1红"是 1 基，Anycubic API 是 0 基——公共接口统一 1 基，返回 slot_map 实际映射表。
- 重复开打事故（11-13）：把重叠事故记错板次+没查 print_history 就重发。教训：开打前必查历史第一条（dup_check 已内置）。
- ps1 脚本必须纯 ASCII（PowerShell 5.1 ANSI 坑）；python 命令行路径用正斜杠。
- dsh-ops exec-tree 损坏（并发 bug）：job_kill 全部自建 job → `node dsh-ops.mjs cache reclaim --kind exec-tree --all-hosts-stopped --yes`。
- 视觉模型：qwen2.5vl:3b 描述性幻觉严重（弃用）；用 litellm deepseek-v4.1-flash。摄像头色彩失真（红→蓝）——只判有无，不判颜色。
- 打印中勿切 LAN 模式（断云+断任务）；U 盘只认 .gcode、英文数字名、目录≤2 层。
