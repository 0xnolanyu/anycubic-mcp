#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开打前五项自动检查（碰撞 / 支撑 / 逐件 / 间距 / 颜色）。

设计原则：全部由 gcode 实测数据判定，不看切片器"以为"的参数。
判定失败即拒绝发送（kx_send 已把它挂成第九查）。

用法：
  python print_gate.py <gcode> --slots 3,0,2 [--accept-any 0] [--declare "1=#8000FF,..."]
  python print_gate.py <gcode> --slots 3,0,2 --json     # 只输出 JSON（给程序用）
"""
from __future__ import annotations
import os, sys, os, re, json, math, argparse, collections

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Orca/Anycubic 机器净空（extruder clearance for Kobra X）
CLEAR_RADIUS = 45.0        # 挤出机横向半径：两件平面最短距离须 >= 该值
CLEAR_HEIGHT_TO_ROD = float(os.environ.get("PRINT_GATE_HEIGHT_ROD", 30.0))  # 龙门净空；Kobra X 为床移动式，可放宽到 200
SOFT_GAP = 60.0            # 我们自己的排版安全线（Orca 实测 60 会报"太近"，80 通过）


def _f(s, key):
    try:
        return float(s.split(key)[1].split()[0])
    except Exception:
        return None


def parse_gcode(path: str) -> dict:
    """逐对象抽取：XY 范围、最高 Z、支撑段数、所属 T、出现顺序。"""
    objs = collections.OrderedDict()
    cur = None
    last_t = 0
    header = {}
    keep_keys = ("print_sequence", "filament_colour", "layer_height", "initial_layer_print_height",
                 "nozzle_temperature", "hot_plate_temp", "total layer number", "printable_area",
                 "enable_support", "brim_type", "brim_width")
    t_seq = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if s.startswith(";") and "=" in s:
                k, _, v = s[1:].partition("=")
                k = k.strip(); v = v.strip()
                if k in keep_keys and k not in header:
                    header[k] = v
            m = re.match(r"^T(\d+)$", s)
            if m:
                last_t = int(m.group(1)); t_seq.append(last_t)
                continue
            if s.startswith("EXCLUDE_OBJECT_START"):
                name = s.split("NAME=")[-1].strip()
                if name not in objs:
                    objs[name] = {"name": name, "t": last_t, "zmax": 0.0, "z_last": 0.0, "support": 0,
                                  "xmin": 9e9, "xmax": -9e9, "ymin": 9e9, "ymax": -9e9}
                cur = objs[name]
                continue
            if s.startswith("EXCLUDE_OBJECT_END"):
                cur = None
                continue
            if cur is None:
                continue
            if s.startswith(";TYPE:Support"):
                cur["support"] += 1
                continue
            if s.startswith("G1") or s.startswith("G0"):
                z = _f(s, " Z") if " Z" in s else None
                if z is not None:
                    cur["z_last"] = z
                if " E" in s:
                    # 只在「挤出移动」时记 Z：空驶的抬升 Z 会把矮件误报成高件
                    cur["zmax"] = max(cur["zmax"], z if z is not None else cur.get("z_last", 0.0))
                x = _f(s, " X"); y = _f(s, " Y")
                if x is not None:
                    cur["xmin"] = min(cur["xmin"], x); cur["xmax"] = max(cur["xmax"], x)
                if y is not None:
                    cur["ymin"] = min(cur["ymin"], y); cur["ymax"] = max(cur["ymax"], y)
    return {"objs": objs, "header": header, "t_seq": t_seq}


def _gap(a, b):
    dx = max(b["xmin"] - a["xmax"], a["xmin"] - b["xmax"], 0.0)
    dy = max(b["ymin"] - a["ymax"], a["ymin"] - b["ymax"], 0.0)
    return math.hypot(dx, dy), dx, dy


def check(gcode: str, slots: list[int], accept_any: list[int] | None = None,
          table: dict | None = None) -> dict:
    accept_any = list(accept_any or [])
    p = parse_gcode(gcode)
    objs = list(p["objs"].values())
    h = p["header"]
    fails, warns, info = [], [], {}

    # ---- 1. 逐件打印 ----
    seq = (h.get("print_sequence") or "").strip()
    info["print_sequence"] = seq or "(未声明)"
    if seq != "by object":
        fails.append(f"[逐件] print_sequence={seq or '(空)'}，不是 by object（会变成逐层换料上百次）")

    # ---- 2. 支撑 ----
    total_support = sum(o["support"] for o in objs)
    info["support_segments"] = total_support
    info["support_per_object"] = {o["name"]: o["support"] for o in objs}
    if total_support == 0:
        warns.append("[支撑] gcode 里没有任何支撑段；若模型有悬垂需确认是否真的不需要")
    elif len(objs) > 1 and total_support < len(objs):
        warns.append(f"[支撑] 只有 {total_support} 段支撑覆盖 {len(objs)} 件，确认悬垂件是否被覆盖")

    # ---- 3/4. 间距 + 碰撞（合并成几何检查，分开报）----
    pairs, min_gap = [], None
    for i in range(len(objs)):
        for j in range(i + 1, len(objs)):
            g, dx, dy = _gap(objs[i], objs[j])
            pairs.append({"a": objs[i]["name"], "b": objs[j]["name"],
                          "gap": round(g, 2), "dx": round(dx, 2), "dy": round(dy, 2)})
            min_gap = g if min_gap is None else min(min_gap, g)
            if g < CLEAR_RADIUS:
                fails.append(f"[碰撞] {objs[i]['name']} <-> {objs[j]['name']} 平面最短 {g:.1f}mm < {CLEAR_RADIUS}mm")
    info["pairs"] = pairs
    info["min_gap"] = round(min_gap, 2) if min_gap is not None else None

    # 高度-顺序耦合：任一"非最后一件"高过龙门净空即失败
    order_desc = []
    for idx, o in enumerate(objs):
        order_desc.append({"i": idx + 1, "name": o["name"], "t": o["t"], "zmax": round(o["zmax"], 2),
                           "support": o["support"],
                           "xy": [round(o["xmin"], 1), round(o["ymin"], 1), round(o["xmax"], 1), round(o["ymax"], 1)]})
        if idx < len(objs) - 1 and o["zmax"] > CLEAR_HEIGHT_TO_ROD:
            fails.append(f"[碰撞] 第 {idx+1} 件 {o['name']} 高 {o['zmax']:.1f}mm > 龙门净空 {CLEAR_HEIGHT_TO_ROD}mm，后面还有件")
    info["order"] = order_desc

    if min_gap is not None:
        if min_gap < SOFT_GAP and min_gap >= CLEAR_RADIUS:
            warns.append(f"[间距] 最紧 {min_gap:.1f}mm 低于排版安全线 {SOFT_GAP}mm（Orca 可能自行收紧/告警）")

    # ---- 5. 颜色（走 colour_map 三查）----
    colour = {}
    try:
        import colour_map as cm
        g = cm.gate(gcode, slots, table=table)
        colour = {"ok": g.get("ok"), "fails": g.get("fails", []), "warns": g.get("warns", []),
                  "colours": g.get("colours"), "rack_source": g.get("rack_source")}
        for f_ in colour["fails"]:
            tk = re.search(r"T(\d+)", f_)
            if tk and int(tk.group(1)) in accept_any:
                warns.append(f"[颜色] 已按例外放行 {f_}")
                continue
            fails.append("[颜色] " + f_)
        warns += colour["warns"]
    except Exception as e:
        fails.append(f"[颜色] 颜色核对执行失败: {type(e).__name__}: {e}")

    ok = not fails
    return {"ok": ok, "gcode": os.path.basename(gcode), "slots": slots,
            "accept_any": accept_any, "fails": fails, "warns": warns, "info": info,
            "colour": colour}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gcode")
    ap.add_argument("--slots", required=True, help="T i 对应物理槽（0 基），如 3,0,2")
    ap.add_argument("--accept-any", default="", help="允许颜色不符的 T 号（逗号分隔），如 0")
    ap.add_argument("--declare", default="", help='临时声明槽色 1 基: "1=#FFFFFF,2=#DCF478"')
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.declare:
        import subprocess
        subprocess.run([sys.executable, os.path.join(HERE, "colour_map.py"), "--declare", a.declare], check=False)
    slots = [int(x) for x in a.slots.split(",") if x.strip()]
    anyt = [int(x) for x in a.accept_any.split(",") if x.strip()]
    r = check(a.gcode, slots, anyt)
    if a.json:
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r["ok"] else 2
    print(f"\n=== 开打前五项检查: {r['gcode']} ===")
    print(f"1) 逐件打印   : {'OK' if r['info']['print_sequence'] == 'by object' else 'FAIL'}  ({r['info']['print_sequence']})")
    print(f"2) 支撑       : {r['info']['support_segments']} 段  {r['info']['support_per_object']}")
    print(f"3) 间距       : 最紧 {r['info']['min_gap']}mm（安全线 {SOFT_GAP} / 硬线 {CLEAR_RADIUS}）")
    for p in r["info"]["pairs"]:
        print(f"              {p['a']} <-> {p['b']}: {p['gap']}mm (dx {p['dx']}, dy {p['dy']})")
    print("4) 碰撞/顺序  :")
    for o in r["info"]["order"]:
        print(f"              {o['i']}. {o['name']} T{o['t']} 高 {o['zmax']}mm 支撑 {o['support']}段 XY{o['xy']}")
    print(f"5) 颜色       : {r['colour'].get('ok')}  {r['colour'].get('colours')}  槽源={r['colour'].get('rack_source')}")
    for w in r["warns"]:
        print("  WARN " + w)
    for f_ in r["fails"]:
        print("  FAIL " + f_)
    print("=== 结论:", "PASS 可发送" if r["ok"] else "REFUSE 不可发送", "===\n")
    return 0 if r["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
