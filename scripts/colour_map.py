#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""colour_map.py —— 件 ↔ 颜色 ↔ 料槽 的机械对齐与发送前拦截。

设计原则（2026-11-19 事故复盘后定稿）：
  颜色需求只有一个权威来源：**原始 3mf 工程的元数据**
      Metadata/model_settings.config   每个 object/part 的 extruder 编号
      Metadata/project_settings.config filament_colour 数组（编号 → hex）
  在这两处解释一次，生成 sidecar `<gcode>.colourmap.json`，之后全程查表，
  任何环节不再重新翻译编号。

三道发送前核对（任一不符即 REFUSE）：
  A 件名：gcode 里每个 EXCLUDE_OBJECT_START 的件名必须在 sidecar 里
  B 件色：该件在 gcode 里用的 T 号 → header filament_colour[T] == sidecar 期望 hex
  C 槽色：T i 对应的槽槽（slots[i]，0 基）→ 该槽实际料色 == sidecar 期望 hex
          （槽色优先取 rack_actual.json 的人工声明，其次取云端 kx_rack，均不符即 REFUSE）

用法：
  # 1) 生成表（贴在发单报告里）
  python colour_map.py --3mf "原始向日葵01+-+副本.3mf" --objects stl_5,stl_8

  # 2) 切片后写 sidecar（期望：哪件必须是什么色）
  python colour_map.py --3mf X.3mf --gcode X.gcode --write-sidecar

  # 3) 发送前核对（kx_send 内部自动调用同一函数）
  python colour_map.py --gate X.gcode --slots 1,3

  # 4) 人工声明真实料槽（云端色是元数据，换料后常不同步）
  python colour_map.py --declare "1=#FF8000,2=#DCF478"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

try:  # Windows 控制台默认 cp1252，中文会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
RACK_ACTUAL = os.path.join(HERE, "rack_actual.json")


# ---------------------------------------------------------------- 颜色工具
def norm_hex(h: str) -> str:
    h = (h or "").strip().upper()
    if not h.startswith("#"):
        h = "#" + h
    return h[:7]


def hex_dist(a: str, b: str) -> float:
    """两色 RGB 欧氏距离（0-441）。"""
    a, b = norm_hex(a), norm_hex(b)
    try:
        ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
        br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    except Exception:
        return 999.0
    return ((ar - br) ** 2 + (ag - bg) ** 2 + (ab - bb) ** 2) ** 0.5


def same_colour(a: str, b: str, tol: float = 40.0) -> bool:
    return hex_dist(a, b) <= tol


# ---------------------------------------------------------------- 3mf → 表
def read_3mf_palette(path: str) -> list[str]:
    z = zipfile.ZipFile(path)
    cfg = json.loads(z.read("Metadata/project_settings.config").decode("utf-8", "ignore"))
    return [norm_hex(c) for c in cfg.get("filament_colour", [])]


def build_table(path: str) -> dict[str, dict]:
    """返回 {件名: {"extruder": n, "hex": "#RRGGBB", "source": path}}"""
    z = zipfile.ZipFile(path)
    palette = read_3mf_palette(path)
    root = ET.fromstring(z.read("Metadata/model_settings.config").decode("utf-8", "ignore"))
    table: dict[str, dict] = {}

    def put(name: str, ext: str | None):
        if not name or not ext:
            return
        try:
            idx = int(ext) - 1
        except ValueError:
            return
        if not (0 <= idx < len(palette)):
            return
        # 同名多部件时保留首个（同色）
        table.setdefault(name, {"extruder": int(ext), "hex": palette[idx], "source": os.path.basename(path)})

    for obj in root.findall("object"):
        metas = {m.get("key"): m.get("value") for m in obj.findall("metadata")}
        put(metas.get("name"), metas.get("extruder"))
        for p in obj.findall("part"):
            pm = {m.get("key"): m.get("value") for m in p.findall("metadata")}
            put(pm.get("name"), pm.get("extruder") or metas.get("extruder"))
    return table


# ---------------------------------------------------------------- gcode 解析
_OBJ_RE = re.compile(r"EXCLUDE_OBJECT_START NAME=(\S+)")
_T_RE = re.compile(r"^T(\d+)\b")
_COL_RE = re.compile(r"^;\s*filament_colour\s*=\s*(.+)$")


def normalise_object(name: str) -> str:
    """原始向日葵01.stl_5_id_0_copy_0 → 原始向日葵01.stl_5"""
    return re.sub(r"_id_\d+_copy_\d+$", "", name.strip())


def parse_gcode(path: str) -> dict:
    """返回 {objects:{件名:{T 集合}}, filament_colour:[hex], first_layer_count}"""
    objects: dict[str, set] = {}
    colours: list[str] = []
    cur_t = 0
    cur_obj: str | None = None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not colours:
                m = _COL_RE.match(line)
                if m:
                    colours = [norm_hex(c) for c in m.group(1).split(";") if c.strip()]
                    continue
            m = _OBJ_RE.search(line)
            if m:
                cur_obj = m.group(1)
                objects.setdefault(cur_obj, set()).add(cur_t)
                continue
            if re.match(r";?EXCLUDE_OBJECT_END", line):
                cur_obj = None
                continue
            m = _T_RE.match(line)
            if m:
                cur_t = int(m.group(1))
                if cur_obj:
                    objects[cur_obj].add(cur_t)
                continue
            if cur_obj is not None and ("G1 " in line or "G0 " in line) and " E" in line:
                objects[cur_obj].add(cur_t)
    return {"objects": {k: sorted(v) for k, v in objects.items()}, "filament_colour": colours}


# ---------------------------------------------------------------- 槽色真值
def load_rack_actual() -> dict[int, str]:
    if not os.path.exists(RACK_ACTUAL):
        return {}
    try:
        d = json.load(open(RACK_ACTUAL, encoding="utf-8"))
        return {int(k): norm_hex(v) for k, v in d.items()}
    except Exception:
        return {}


def save_rack_actual(pairs: dict[int, str]) -> str:
    json.dump({str(k): norm_hex(v) for k, v in pairs.items()}, open(RACK_ACTUAL, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return RACK_ACTUAL


def rack_colours(live: dict[int, str] | None = None) -> tuple[dict[int, str], str]:
    """0 基槽位 → hex。优先人工声明，否则云端，否则空。返回 (表, 来源说明)。"""
    actual = load_rack_actual()
    if actual:
        return actual, "人工声明 rack_actual.json"
    if live:
        return {int(k): norm_hex(v) for k, v in live.items()}, "云端 kx_rack（换料后可能不同步）"
    return {}, "无"


# ---------------------------------------------------------------- 核对
def gate(gcode: str, slots: list[int], table: dict[str, dict] | None = None,
         live_rack: dict[int, str] | None = None, tol: float = 40.0,
         accept_any: list[int] | None = None) -> dict:
    """三道核对。table 为空时退回 sidecar `<gcode>.colourmap.json`。"""
    side = gcode + ".colourmap.json"
    if table is None and os.path.exists(side):
        table = json.load(open(side, encoding="utf-8")).get("objects", {})
        if isinstance(table, list):          # slice_model.py 写的 sidecar 是 id/ext 列表，没有件名
            table = {}
    table = table or {}
    parsed = parse_gcode(gcode)
    objs, colours = parsed["objects"], parsed["filament_colour"]
    fails, warns = [], []

    if not table:
        warns.append("没有 sidecar/表：只能核对槽色，无法核对『件 ↔ 色』")
    if not colours:
        fails.append("gcode header 里没有 filament_colour")

    # A 件名 + B 件色
    for name, ts in objs.items():
        if not ts:
            continue
        exp = table.get(name) or table.get(normalise_object(name))
        if table and not exp:
            fails.append(f"A 件名不在表里: {name}（gcode 里出现但原始工程没有/名字不同）")
            continue
        if exp:
            for t in ts:
                if t >= len(colours):
                    fails.append(f"B T{t} 超出 filament_colour 长度({len(colours)}) [{name}]")
                elif not same_colour(colours[t], exp["hex"], tol):
                    fails.append(f"B 件色不符: {name} 用 T{t}={colours[t]}，原始工程要求 {exp['hex']}")

    # C 槽色（只核对 gcode 里真正用到的 T；没用到的 T 不参与，避免误报）
    rack, rack_src = rack_colours(live_rack)
    if rack:
        used_t = sorted({t for ts in objs.values() for t in ts})
        if not used_t:
            used_t = list(range(min(len(slots), len(colours))))
        for t in used_t:
            if t >= len(slots) or t >= len(colours):
                continue
            s = slots[t]
            want = colours[t]
            got = rack.get(s)
            if got is None:
                warns.append(f"C 槽{s}(T{t}) 没有实际色记录（{rack_src}），跳过")
            elif not same_colour(got, want, tol):
                if t in (accept_any or []):
                    warns.append(f"C 槽色例外放行: T{t} 用槽{s}={got}，设计色 {want}（已列入 accept_any）")
                else:
                    fails.append(f"C 槽色不符: T{t} 用槽{s}={got}，但 gcode 该 T 是 {want}")
    else:
        warns.append("C 无槽色真值（先 --declare 或读云端），槽色这一步没核对")

    return {"ok": not fails, "fails": fails, "warns": warns,
            "rack_source": rack_src, "objects": sorted(objs), "slots": slots,
            "colours": colours, "table": table}


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="3mf → 件/色/槽 机械对齐 + 发送前拦截")
    ap.add_argument("--3mf", dest="mf", help="原始 3mf 工程（颜色权威来源）")
    ap.add_argument("--gcode", help="要核对的 gcode")
    ap.add_argument("--objects", help="只输出这些件（逗号分隔，可含 stl_5 之类短名）")
    ap.add_argument("--slots", help="0 基槽位列表，逗号分隔，slots[i] ↔ T i")
    ap.add_argument("--write-sidecar", action="store_true", help="把表写成 <gcode>.colourmap.json")
    ap.add_argument("--gate", metavar="GCODE", help="只做发送前核对")
    ap.add_argument("--declare", help='人工声明真实料槽，如 "1=#FF8000,2=#DCF478"（1 基槽号）')
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.declare:
        pairs = {}
        for item in a.declare.split(","):
            k, v = item.split("=")
            pairs[int(k.strip()) - 1] = norm_hex(v.strip())  # 1 基输入 → 0 基存储
        p = save_rack_actual(pairs)
        print(json.dumps({"declared": {k + 1: v for k, v in pairs.items()}, "file": p}, ensure_ascii=False))
        return 0

    if a.gate:
        slots = [int(x) for x in a.slots.split(",")] if a.slots else [0]
        res = gate(a.gate, slots)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            for f in res["fails"]:
                print("REFUSE " + f)
            for w in res["warns"]:
                print("WARN   " + w)
            print(("PASS   颜色对齐通过" if res["ok"] else "REFUSE 颜色对齐失败") +
                  f"  [槽色来源: {res['rack_source']}]")
        return 0 if res["ok"] else 2

    if not a.mf:
        ap.error("需要 --3mf")
    table = build_table(a.mf)
    if a.objects:
        want = [x.strip() for x in a.objects.split(",")]
        table = {k: v for k, v in table.items() if any(k.endswith(w) or k == w for w in want)}
    if a.gcode and a.write_sidecar:
        side = a.gcode + ".colourmap.json"
        json.dump({"objects": table, "source_3mf": os.path.abspath(a.mf)}, open(side, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        if not a.json:
            print("sidecar → " + side)
    if a.json:
        print(json.dumps(table, ensure_ascii=False, indent=1))
    else:
        for name, v in sorted(table.items()):
            print(f"{name:<32} ext {v['extruder']:<3} {v['hex']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
