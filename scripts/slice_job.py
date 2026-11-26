#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单工程多单切片器（茄子忍者事故后重写）

关键修复：
1) /objects 已不再返回 config.extruder（2026-11-22 实测）→ 原 slice_model 的 ext 映射全部失效，
   改为按 size 显式 PUT /objects/{id}/config {"extruder": T}（T 为 1 基，T0 → 1）。
2) 3mf 被 strip 掉 before_layer_change_gcode 后，KX 预设缺少 G92 E0 → validate 报
   「相对挤出机寻址要求在每层重置挤出机位置」，slice 永远 valid=False（API 表现为 409 job_running）。
   → 打开工程后必须先 PUT before_layer_change_gcode="G92 E0"。

用法：
  python slice_job.py <job名> <src.3mf> <job.json> [--out xxx.gcode]
job.json:
  {"colors": ["#FFFFFF","#161616"], "slots": [0,1],
   "objects": [{"size": [54.8, 23.8], "T": 1}, {"size": [21.3, 5.1], "T": 2}]}
"""
import sys, os, json, time, re, hashlib
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.abspath(os.environ.get("KX_WS") or os.path.join(os.path.dirname(HERE), ".."))
TMPDIR = os.path.abspath(os.environ.get("KX_TMP") or os.path.join(WS, "_tmp3d"))
os.makedirs(TMPDIR, exist_ok=True)
OUTDIR = os.path.join(WS, "00_待打印")
NF = {"X-Api-Token": os.environ.get("ORCA_API_TOKEN", ""), "Content-Type": "application/json"}
B = os.environ.get("ORCA_API", "http://127.0.0.1:13130/api/v1")
TOL = 0.8


def api(method, path, **kw):
    kw.setdefault("timeout", 60)
    r = requests.request(method, B + path, headers=NF, **kw)
    return r


def wait_api(n=40):
    for _ in range(n):
        try:
            if api("GET", "/status", timeout=5).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def select_presets():
    for t, name in (
        ("printer", "Anycubic Kobra X 0.4 nozzle"),
        ("print", "0.16mm High Quality @Anycubic Kobra X 0.4 nozzle"),
        ("filament", "Anycubic PLA @Anycubic Kobra X 0.4 nozzle"),
    ):
        r = api("PUT", "/preset", json={"type": t, "name": name}, timeout=120)
        print(f"[preset] {t} {r.status_code} {r.text[:120]}")
        if r.status_code != 200:
            sys.exit(f"预设切换失败: {t} {name}")


def strip_3mf(src, dst):
    sys.path.insert(0, HERE)
    import strip_3mf as s3
    s3.strip(src, dst)
    # 关键：plate 级元数据 print_sequence=by layer 会压过 PUT /config，必须静态改成 by object
    import slice_model as sm
    sm.patch_plate_sequence(dst)
    print("[plate] print_sequence -> by object")


def list_objects():
    return api("GET", "/objects", timeout=60).json()["objects"]


def cur_offset(oid):
    """每次排版前重新读当前 offset，避免用旧值做相对位移把件推到床外。"""
    for o in list_objects():
        if o["id"] == oid:
            return o["transform"]["offset"]
    raise KeyError(oid)


def put_at(oid, x, y):
    cur = cur_offset(oid)
    api("POST", f"/objects/{oid}/transform",
        json={"translate": [x - cur[0], y - cur[1], 0]}, timeout=30)


def pack(objs, gap=80.0, bed=255.0, margin=float(__import__("os").environ.get("PACK_MARGIN", 25.0))):
    """货架排版：一列一列往下堆，列宽 = 该列最宽件，列间留 gap。"""
    cols = []
    for o in objs:
        w, h = o["size_mm"][0], o["size_mm"][1]
        placed = False
        for c in cols:
            y = margin + sum(i[1] + gap for i in c["items"])
            if c["x0"] + c["maxw"] + gap <= bed - margin + 0.01:
                if y + h <= bed - margin + 0.01:
                    c["items"].append((w, h))
                    put_at(o["id"], c["x0"], y)
                    placed = True
                    break
        if placed:
            continue
        if cols:
            x0 = cols[-1]["x0"] + cols[-1]["maxw"] + gap
            if x0 + w > bed - margin:
                raise SystemExit(f"排版放不下: 第 {len(cols)+1} 列 x0={x0:.1f} w={w:.1f}")
        else:
            x0 = margin
        cols.append({"x0": x0, "maxw": w, "items": [(w, h)]})
        put_at(o["id"], x0, margin)
    return [(o["id"], o["name"]) for o in objs]


def main():
    name = sys.argv[1]
    src = os.path.abspath(sys.argv[2])
    plan = json.load(open(sys.argv[3], encoding="utf-8"))
    out = None
    for i, a in enumerate(sys.argv[4:]):
        if a == "--out":
            out = sys.argv[4:][i + 1]
    colors = plan["colors"]
    slots = plan["slots"]
    want = plan["objects"]

    clean = os.path.join(TMPDIR, f"{name}_clean.3mf")
    strip_3mf(src, clean)
    if not wait_api():
        sys.exit("Orca API 未就绪")
    r = api("POST", "/project/open", json={"path": clean, "if_unsaved": "discard"}, timeout=240)
    print("[open]", r.status_code, r.text[:160])
    if r.status_code != 200:
        sys.exit("打开工程失败")
    select_presets()
    # 关键修复：补 G92 E0，否则 validate 永远不过
    rc = api("PUT", "/config", json={"before_layer_change_gcode": "G92 E0\n"}, timeout=60)
    print("[fix] before_layer_change_gcode", rc.status_code, rc.text[:100])

    objs = list_objects()
    keep_ids = []
    for o in objs:
        w, h = o["size_mm"][0], o["size_mm"][1]
        hit = None
        for e in want:
            tw, th = e["size"]
            if abs(w - tw) < TOL and abs(h - th) < TOL:
                hit = e
                break
        if hit is None:
            d = api("DELETE", f"/objects/{o['id']}", timeout=60)
            print(f"  del {o['id']} {w:.1f}x{h:.1f} -> {d.status_code}")
        else:
            keep_ids.append((o, hit))
    if len(keep_ids) != len(want):
        sys.exit(f"保留件数不符: 期望 {len(want)} 实得 {len(keep_ids)}")

    for o, e in keep_ids:
        api("POST", f"/objects/{o['id']}/plate", json={"plate": 0}, timeout=60)
    pack([o for o, _ in keep_ids])
    # 必须把"当前板"切到 0，否则 slice 报 plate cannot be sliced (object outside bed or invalid state)
    r = api("POST", "/plates/0/select", timeout=30)
    print("[plate] select 0", r.status_code)
    for o, e in keep_ids:
        r = api("PUT", f"/objects/{o['id']}/config", json={"extruder": int(e["T"])}, timeout=60)
        print(f"  T {o['id']} {o['size_mm'][0]:.1f}x{o['size_mm'][1]:.1f} -> T{int(e['T']) - 1} {r.status_code}")

    st = api("GET", "/status", timeout=30).json()
    nf = max(1, len((st.get("presets") or {}).get("filaments") or [])) or 4
    pad = colors + [colors[-1]] * (nf - len(colors)) if nf > len(colors) else colors[:nf]
    body = {
        "filament_colour": ";".join(pad),
        "nozzle_temperature": "205", "hot_plate_temp": "60",
        "layer_height": "0.16", "initial_layer_print_height": "0.2",
        "wall_loops": 3, "sparse_infill_density": "15%",
        "outer_wall_speed": 100, "inner_wall_speed": 120,
        "top_surface_speed": 100, "sparse_infill_speed": 170,
        "initial_layer_speed": 45,
        "brim_type": "outer_only", "brim_width": 5,
        "enable_support": "1", "support_type": "tree(auto)", "support_threshold_angle": 25,
        "print_sequence": "by object",
        "before_layer_change_gcode": "G92 E0\n",
    }
    rc = api("PUT", "/config", json=body, timeout=120)
    print("[config]", rc.status_code, rc.text[:200])
    if rc.status_code != 200:
        sys.exit("配置下发失败")

    for attempt in range(6):
        r = api("POST", "/slice", json={"plate": 0}, timeout=300)
        print(f"[slice] try{attempt} {r.status_code} {r.text[:100]}")
        if r.status_code == 422 and attempt == 0:
            # 摆放被 Orca 判为越界/非法时，交给 Orca 自己的 arrange 重排一次再切
            api("POST", "/arrange", json={"started": True}, timeout=120)
            time.sleep(20)
            api("POST", "/plates/0/select", timeout=30)
            print("[arrange] 兜底重排完成")
            continue
        ok = False
        for _ in range(60):
            st = api("GET", "/status", timeout=20).json()
            if not st.get("slicing") and st.get("slice_result_valid"):
                ok = True
                break
            time.sleep(4)
        if ok:
            break
        # 读 Orca 的 validate 错误（GUI 日志）
    else:
        sys.exit("切片未成功")

    r = api("GET", "/gcode?plate=0", timeout=180)
    if r.status_code != 200:
        sys.exit(f"取 gcode 失败 {r.status_code} {r.text[:120]}")
    out = os.path.abspath(out or os.path.join(os.path.dirname(HERE), "00_待打印", f"{name}.gcode"))
    open(out, "wb").write(r.content)
    print(f"[gcode] {out} {len(r.content)} bytes")

    # —— 自检：逐对象 T 与色表 ——
    txt = r.content.decode("utf-8", errors="ignore")
    col = re.search(r"; filament_colour = ([^\n]*)", txt)
    seq = re.search(r"; print_sequence = ([^\n]*)", txt)
    tcmds = re.findall(r"^T(\d+)\s*$", txt, re.M)
    zs = {}
    for name2, body2 in re.findall(r"EXCLUDE_OBJECT_START NAME=([^\n]*)\n(.*?)EXCLUDE_OBJECT_END", txt, re.S):
        z = max([float(v) for v in re.findall(r"Z([\d.]+)", body2)] or [0])
        zs[name2.strip()] = max(zs.get(name2.strip(), 0), z)
    print(f"[check] print_sequence={seq.group(1) if seq else None} T={tcmds}")
    print(f"[check] filament_colour={col.group(1) if col else None}")
    print(f"[check] objects={ {k: round(v, 2) for k, v in zs.items()} }")
    if seq is None or seq.group(1).strip() != "by object":
        print("[check] !! 不是 by object")
    if len(tcmds) != len(want):
        print(f"[check] !! T 命令数 {len(tcmds)} != 件数 {len(want)}")

    import colour_map as _cm
    _gp = _cm.parse_gcode(out)
    _tab = {}
    for _n, _ts in _gp["objects"].items():
        if _ts:
            _tab[_n] = {"hex": _gp["filament_colour"][_ts[0]], "T": _ts[0]}
    side = {"objects": _tab, "colors": _gp["filament_colour"], "slots": slots, "source": "切片时指定"}
    json.dump(side, open(out + ".colourmap.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("[path]", out)


if __name__ == "__main__":
    main()
