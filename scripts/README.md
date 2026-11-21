# scripts/ — KX 切片 + 发送门禁工具

这组脚本是 `anycubic-mcp` 的**实战配套**：Orca 切片、件↔色对齐、开打前五项检查、3mf 清理与排版。
它们不是 pip 包的一部分，直接 `python scripts/xxx.py` 调用。

## 环境变量

| 变量 | 用途 | 默认 |
|---|---|---|
| `KX_WS` | 工作区根（`_tmp3d/`、`00_待打印/` 相对它） | 本仓库父目录 |
| `KX_TOKEN_DIR` | Anycubic token 目录 | `$KX_WS/_tmp3d` |
| `KX_TMP` | 临时目录（中间 3mf、扫描结果） | `$KX_WS/_tmp3d` |
| `KX_PRINTER_ID` | 打印机 id | `0`（必填） |
| `ANYCUBIC_TOKEN` | 云端 token（优先于文件） | 读 `$KX_TOKEN_DIR/anycubic_session_token.txt` |
| `ORCA_API` | Orca REST base | `http://127.0.0.1:13130/api/v1` |
| `ORCA_API_TOKEN` | Orca `X-Api-Token` | 空（必填） |
| `KX_GCODE_VERIFY` | `gcode_verify.py` 路径 | `<repo>/src/anycubic_mcp/gcode_verify.py` |
| `KX_VLM_SSH` / `KX_VLM_URL` / `KX_VLM_MODEL` | 视觉核查（可选） | 空 → 跳过视觉 |
| `PACK_GAP` | 货架排版间距 mm | 80（低于 80 会被 Orca 判"太近"） |
| `PRINT_GATE_HEIGHT_ROD` | 换料横梁净空 mm | 30（**不要为了过检查而调大**） |

## 脚本

### `kx_mcp_server.py` — 云端直传 MCP server（stdio）
工具：`kx_status` / `kx_send` / `kx_upload` / `kx_print_cloud` / `kx_rack` / `kx_history` / `kx_bed_photo` …

`kx_send` 开打前依次：dup_check → **第六查换料横移碰撞** → 第七查首层附着 → **第八查件↔色↔槽颜色对齐** → **第九查五项自动检查** → precheck（忙/清盘/视觉）。
任何一查不过就 `ok:false` + `stage`，安全红线**不可 force**。

### `colour_map.py` — 件↔色↔槽 机械对齐
```bash
python colour_map.py --3mf "00_待打印/原始向日葵01+-+副本.3mf"        # 出权威色表
python colour_map.py --gcode X.gcode --write-sidecar                  # 写 sidecar
python colour_map.py --gate X.gcode --slots 0,3,1,2                   # 三查 A/B/C
python colour_map.py --declare "1=#FF8000,2=#DCF478"                  # 声明真实槽色
```
A 件名在表 / B 该件 T → header `filament_colour[T]` == 表 hex / C `slots[i]` 槽色 == 该 T hex。
无 sidecar 时只能查 C（件↔色是瞎的）。

### `print_gate.py` — 开打前五项检查
```bash
python print_gate.py X.gcode --slots 0,2 [--accept-any 0] [--json] [--declare "..."]
```
① by object ② 支撑段数与分布 ③ 两两平面间距（软线 60 / 硬线 45）④ 碰撞顺序（>30mm 必须最后）⑤ 颜色。
PASS=0，REFUSE=2。

### `slice_model.py` — 通用切片器
```bash
python slice_model.py 大哥1 "00_待打印/20261120/大哥.3mf" _plan_dg_1.json \
    --keep-size "57.9x65.7,47.8x45.7" --ext-size "43.1x35.1:3" \
    --out "../00_待打印/xxx.gcode"
```
plan.json：`{"colors":[...], "slots":[T i→物理槽 0 基], "map":{"原extruder":新extruder}}`。
内部：strip 脚本键 → patch plate `print_sequence` → patch extruder >4 → open → 切预设 → 按尺寸留件 → 搬 plate0 → 货架排版 → PUT config → slice（首切空转自动重试）→ 取 gcode → 写 sidecar → gate。

### `strip_3mf.py` — 去脚本键
`python strip_3mf.py <src.3mf> <dst.3mf>`，剔除 `machine_start_gcode` 等会让 `POST /project/open` 返回 422 `blocked_by_remote_api_policy` 的键。

### `scan_token_wide.py` — memscan 取云端 token
`python scan_token_wide.py <pid>`，ASCII + UTF-16LE 双扫进程内存，输出 `token_wide_<pid>.json`。
取 **236 长度**那条，用 `kx_mcp_server._hdr()` 请求 printersStatus 验 `code==1`，再**双写**两个 token 文件。
