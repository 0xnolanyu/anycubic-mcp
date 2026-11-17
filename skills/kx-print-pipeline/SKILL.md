## 逐件模式撞件事故（11-13 南瓜灯实机教训）
菱形四角布局（中心距 140mm）Orca 校验放行，但实际撞了：**打印头组件横向半径 ~65mm（clearance_radius）**，主体 48mm 高先打完，后打的组合体（Z≤26.5mm）打印时 X 杆在 Z+30≈30-56mm 高度横扫，主体-组合体**边缘距只有 62mm < 65 → 杆扫掉主体上部几层（位移）**。
- 自检标准（代替"中心距 150"旧经验）：**后打件的全部喷嘴轨迹点，到先打高件 bbox 的最小距离必须 > 65mm+余量（建议 ≥80mm）**。判定脚本：G-code 里后打工具的全部挤出点算 bbox 点距。
- 真正的安全序：**最高件最后打**（其后打件少），或保证 3D 边缘距离。
- Y 挪 7mm（组合体 200,200→200,207）即达 111.9mm 距离——微小移动大效果，先算距离再重切。

## 发送前人工确认门（m16135）
队列任务五查+槽色核对全过后置 **ready_confirm**，不自动发送；等用户明确说「确认/发送」才调用 `kx_queue_confirm(qid)` 真发。半点轮询 prompt 已同步：ready_confirm 必须通知用户，绝不自行确认。

---
name: kx-print-pipeline
user-invocable: true
description: KX（Anycubic Kobra X）多色打印全流程经验：Orca 切片策略（逐层/逐件/分板决策树）、逐件模式 150mm 间距规则、首层散点图+T序列双验证、发送五查防呆、slots 基数、API 链路等实机验证教训。当任务涉及 KX/Anycubic 多色打印、Orca MCP 切片、发送 G-code 到打印机、排查打印事故时使用。
---

# KX（Anycubic Kobra X）打印全流程实战经验

适用：Anycubic Kobra X + ACE 四色 + Orca MCP（MaxEllis 定制版 2.5，127.0.0.1:13130）+ anycubic-mcp（云端直传）。以下结论经 2026-11-11~13 旋风豌豆/魅惑菇/沥水篮/毁灭菇系列实机验证。

## 工具链与路径

- Orca MCP driver：`E:\workspaces\dsh\3d打印\_tmp3d\orca_mcp_driver.js`（读 JSON steps；NODE_PATH=C:\Users\Administrator\.dsh\profiles\desktop\node_modules）
- 发送 MCP：`anycubic-mcp`（printer_status / send_print_job / verify_gcode / queue_*）
- 摆盘防重叠：`E:\workspaces\dsh\3d打印\脚本\orca_layout.py`（check/fix/move）
- G-code 源目录：C:\Users\Administrator\.orcaslicer-mcp\gcode\（save_gcode 固定写这里，再复制到工作区）


### 切片警告门（必执行）
切片完成后**必须**调 MCP `get_slice_warnings` 拉取 Orca 全部警告/错误，逐条处理完才算完成：
- slice 失败（如「对象太近会碰撞」）：必须消除（调布局/参数重切）或给出量化安全证据（G-code 轨迹点距离验证）并**向用户列出警告+依据，用户同意后才可绕过**——禁止静默放宽 extruder_clearance_radius 绕过后不告知。
- slice 成功但有警告：逐条定性。无害可放行（如 bed_temperature_too_high 对开放机箱、not_generate_timelapse），但要在交付说明里列出；level>=3 或 error 级必须消除。
- `out_of_date: true` 的结果不能引用，须重切后再拉警告。
- 原则：Orca 的提示=前人踩坑经验，宁可误报不可漏报；绕过必须有量化证据+用户知情。

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

0. **排板完成必发编辑视图截图给用户（用户明令 m15886）**：`render_plate(view=editor, frame=plate, angle=iso)` → base64 解码存 `_tmp3d\<名>_plate_iso.png` → present 给用户；再投 101 litellm deepseek-v4.1-flash 复核（首行 true/false：互不重叠且在板内）。注意渲染显示色是软件默认色非实际料色，须向用户说明。
1. **verify_gcode**（anycubic-mcp 内置）：首层散点聚类 + 工具同区互渗检测（≥50% 互渗率判重叠；擦料塔是多工具共用区不会误报）。糊板事故文件 ok=False，好版 ok=True，4 真样本验证。
2. **T 序列统计**：verify_gcode 返回 t_sequence + tool_changes，对照预期色→槽映射。
3. `move_object_to_plate` 只改板归属**不重排 XY**——移板后必跑 orca_layout check 或 verify_gcode（曾因此糊板报废）。
4. `check_placement` 用世界坐标判 fits（不扣板 origin 312 偏移），小床误报可忽略，以切片成功为准。

## 换料对齐与槽位防呆（南瓜灯事故后新增，必守）
- **云端槽色直读**（getPrinters 破解后新增）：`/work/printer/getPrinters` 返回 data[0].color 前4槽 RGB（-2,-2,-2=空）。kx_rack() 工具直读；_slot_map 优先云端真色回退台账；kx_send(expect={槽号:期望hex}) 发送前色距>150 拦截（stage=rack_colour）。
- **料架台账** `脚本/kx_rack.json`：槽号→颜色名+hex，用户每次换料口述后立即更新。
- **slots 顺序语义**：库把 mapping 按槽号排序后关联 T0..Tn，**T(i) ↔ sorted(slots)[i]**，传乱序无效（南瓜灯 [3,0]≡[0,3] 事故实锤）。_start 已自动 sort，kx_send 回执带 slot_map「T0 -> 槽1(橙黄)」。
- **Orca 显示色对齐**：切完后 set_config filament_colour=台账 hex（分号分隔），使 Orca 截图显示=真实料架颜色，用户可人工核对。send 前把 slot_map 与截图一起给用户。

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

## 塔与件碰撞检查（每次带塔切片必做，勿等用户提醒）
- 塔的真实范围 ≠ 配置里的 wipe_tower_x：从 G-code `;TYPE:Prime` 段提取塔实际 X/Y bbox 与最高 Z（prime_tower_width 默认 30mm，实际占地常达 40×45mm）。
- 判据：**最后打的件**（或任何与塔同高/更高的件）距塔边缘欧氏距离 ≥65mm（喷嘴组件扫掠半径），建议 ≥80mm；塔高通常=最高换料件高度。
- 同时复查高件对：后打件全部轨迹点到先打高件（>30mm）bbox 的欧氏距离 >65mm（L∞ 口径会漏判，南瓜灯 62mm 撞件实证）。
- Orca 自动摆塔不做件距校验，塔位贴件时用 transform_object 挪件（挪矮件/最后打的件，别动先打高件）。
- 示例脚本（塔 bbox + 件距 + 高件对间距三合一）：见本 skill 仓库 verify 脚本的 tower_check 模式。

## 发送前四项强制报告（用户 m19391 定死）

每次调整建模文件、每次打印发送之前，必须向用户报告以下四项，缺一不可：

1. **逐件/逐层 + 料塔**：print_sequence 实测值（以 G-code 对象标记块状验证为准，不信切片器显示）+ 是否有擦料塔（;TYPE:Prime 段实测）
2. **碰撞检查结果**：两两边缘距（高件对 >65mm 安全线）、塔与最后打件距离（>65mm）、by object 回退检查（旧 Bambu 工程碰撞失败会静默回退逐层！对象标记必须块状非交替）
3. **颜色映射表**：每件 → T 指令 → 槽位 → 实物卷色（单工具文件恒槽1；多色 T=槽号-1；云端槽色是记录值，以用户口述/目视为准）
4. **分板策略**：每板几件几色、换料次数、预计时间/耗材

未报全四项不得发送。

## 首层附着检查（第七查，2026-11-16 山竹绿叶粘头事故固化）
- 事故：星形叶首层覆盖 9% 细臂 + no_brim → 103 层被撬起粘头。黑件 37% 实心块同板稳过=对比铁证。
- 判据（verify_first_layer_anchoring 第三版，按件独立检查其起始层）：brim>0 → PASS；body<=2 且 sup>=50 → WARN（弧底小件=支撑锚定，如眼球）；body>20 且 sup>=body×3 且 sup>=200 → WARN（弧壳宽边+支撑网主导，如山竹红身）；brim=0 且 body<100 → BLOCK（细臂裸打，不可 force）。
- 物理依据：body>20=壳边是宽连续线（支撑网格可锚定）；body 稀疏点状细臂支撑救不了（绿叶事故实证：当年 body=10 且有支撑照样剥落）。
- 细臂/星形/辐条/圆环件切片必加：brim_type=outer_only + brim_width=8（注意合法值是 outer_only，不是 outer_brim）。
- brim 对纯支撑首层不生成（brim 依附模型轮廓，弧底全支撑件 auto 判不需要）——这类件靠支撑网锚定。
- 单件工程的对象工具号不重编号（extruder=4 切出来就是 T3，发送 slots 按 T 序传 0 基索引即可）。
- 回测：事故板 BLOCK 精准命中绿件；南瓜灯/旋风豌豆/魅惑菇/沥水篮全 PASS 零误杀；红身/眼球 WARN 放行。

## Bambu 工程 by object 六连败与 add_plate 破局（2026-11-17 裂瓣花固化）
- 症状：Bambu 工程去脚本副本 open_project 后，set_config 主包/单包、apply_and_slice、工程注入（重打包 config print_sequence）、间距重摆、print 预设切换——**全部无效，切片永远逐层**（判据：T 事件 47-48 次 vs by-object 应为换色数；首层多件同印；对象标记交替 89 次）。
- 根因：**原 Bambu 工程的旧打印板带锁死的板级 by-layer 配置，全局 set_config 被板级覆盖**。
- 破局：`add_plate` 新建一块干净板 → move_object_to_plate 把件搬进去 → 全局 by object 直通生效（第八次实验证实）。
- 判别法：切完 grep T 事件计数——by-object 板 T 事件=换色次数（N 色 N-1 次左右），逐层板 T 事件=每层换色累计（几十次）。
- 关联坑：new_project+load_model 路线在该批 STL 上有「正在对网格切片」卡 5% 死锁暗病（6 实例全卡）；open_project 旧板是稳妥路线但 by object 必须 add_plate。
- move 到不存在的板静默失败（unknown_plate 在 driver 批里不报错）——move 后必须 list_objects 复核板归属。

## Orca 重启后 token 全流程（Next 2.0.0.6）
- Next 重启后云 C++ 模块不自启（cloud_sdk_cpp 日志 0 字节）——请用户点开工作台/云文件页激活，再 memscan。
- memscan 正则长度上限要 >1300（论坛 RS256 token 1265 会混入）；真会话 token 是 236 长度 ['jwt','xtoken'] 那个。
- 双 token 文件必须同步写同一个 236：_tmp3d/anycubic_session_token.txt（自研 _api）与 _tmp3d/anycubic_token.txt（库上传链路）——只写一个分别报「登录信息失效」或「Authentication failed」。
- 对象 id 每次新建工程递增，猜 id 必 unknown_object——先 list_objects。
