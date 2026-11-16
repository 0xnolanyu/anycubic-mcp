"""G-code delivery verification — first-layer scatter overlap + tool-sequence stats.

Born from two real incidents (2026-11-12/13):
- move_object_to_plate moves plate membership but NOT XY -> parts stacked on top of
  each other; slicer happily sliced the overlap and the print came out as one blob.
- a plate was re-sent because nobody checked print history first.

verify() is the one-shot gate: overlap clusters in the first layer + tool sequence
across the whole file. Pure stdlib, no numpy/PIL needed for the verdict.
"""
from __future__ import annotations

import re
from pathlib import Path

_E = re.compile(r"\bE(-?\d*\.?\d+)")
_XY = re.compile(r"\bX(-?\d*\.?\d+)\s+Y(-?\d*\.?\d+)")
_TOOL = re.compile(r"^T\d+$")


def _extruding(s: str) -> bool:
    m = _E.search(s)
    return m is not None and float(m.group(1)) > 0


def parse_first_layer(path: str) -> list[tuple[float, float, str]]:
    """Extruding X/Y/tool points between the 1st and 2nd ;LAYER_CHANGE (travel ignored)."""
    pts: list[tuple[float, float, str]] = []
    layer = 0
    tool: str | None = None
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            s = line.strip()
            if s.startswith(";LAYER_CHANGE"):
                layer += 1
                if layer > 1:
                    break
                continue
            if _TOOL.match(s):
                tool = s
                continue
            if layer != 1 or not s.startswith("G1"):
                continue
            if not _extruding(s):
                continue
            m = _XY.search(s)
            if m:
                pts.append((float(m.group(1)), float(m.group(2)), tool or "?"))
    return pts


def parse_tool_points(path: str) -> tuple[list[str], dict[str, list[tuple[float, float]]]]:
    """Tool sequence (distinct, in order) + extruding points per tool across the file."""
    seq: list[str] = []
    per: dict[str, list[tuple[float, float]]] = {}
    tool: str | None = None
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            s = line.strip()
            if _TOOL.match(s):
                tool = s
                if s not in seq:
                    seq.append(s)
                per.setdefault(s, [])
                continue
            if not s.startswith("G1") or tool is None:
                continue
            if not _extruding(s):
                continue
            m = _XY.search(s)
            if m:
                per[tool].append((float(m.group(1)), float(m.group(2))))
    return seq, per


def cluster(points: list[tuple[float, float]], grid: float = 20.0) -> list[dict]:
    """Grid-bucket clustering with 8-neighbour flood fill."""
    if not points:
        return []
    buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for x, y in points:
        buckets.setdefault((int(x // grid), int(y // grid)), []).append((x, y))
    seen: set[tuple[int, int]] = set()
    clusters: list[dict] = []
    for start in buckets:
        if start in seen:
            continue
        stack, group = [start], []
        seen.add(start)
        while stack:
            bx, by = stack.pop()
            group.extend(buckets[(bx, by)])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (bx + dx, by + dy)
                    if n in buckets and n not in seen:
                        seen.add(n)
                        stack.append(n)
        xs = [p[0] for p in group]
        ys = [p[1] for p in group]
        clusters.append({"n": len(group),
                         "x0": round(min(xs), 1), "x1": round(max(xs), 1),
                         "y0": round(min(ys), 1), "y1": round(max(ys), 1),
                         "cx": round(sum(xs) / len(xs), 1), "cy": round(sum(ys) / len(ys), 1)})
    clusters.sort(key=lambda c: -c["n"])
    return clusters


def _bbox_overlap(a: dict, b: dict, gap: float = 2.0) -> bool:
    return not (a["x1"] + gap < b["x0"] or b["x1"] + gap < a["x0"]
                or a["y1"] + gap < b["y0"] or b["y1"] + gap < a["y0"])


def _bbox_of(pts: list[tuple[float, float]]) -> dict:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return {"x0": round(min(xs), 1), "x1": round(max(xs), 1),
            "y0": round(min(ys), 1), "y1": round(max(ys), 1)}


def verify(path: str) -> dict:
    """One-shot G-code sanity gate. Raises FileNotFoundError for missing files."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(path)
    first = parse_first_layer(path)
    clusters = cluster([(x, y) for x, y, _ in first])
    overlaps = [[i, j] for i, a in enumerate(clusters) for j, b in enumerate(clusters)
                if i < j and _bbox_overlap(a, b)]
    # first-layer tool regions: two tools extruding into the SAME area is the
    # signature of the stacked-plates incident. The prime tower is a shared zone
    # (every tool prints its slice of it), so require heavy mutual渗透 (>50% of
    # the smaller tool's points inside the bbox intersection), not just bbox touch.
    per_tool: dict[str, list[tuple[float, float]]] = {}
    for x, y, t in first:
        per_tool.setdefault(t, []).append((x, y))
    fb = {t: _bbox_of(v) for t, v in per_tool.items() if len(v) >= 20}
    tool_overlap = []
    for a in fb:
        for b in fb:
            if a >= b:
                continue
            ix0, ix1 = max(fb[a]["x0"], fb[b]["x0"]), min(fb[a]["x1"], fb[b]["x1"])
            iy0, iy1 = max(fb[a]["y0"], fb[b]["y0"]), min(fb[a]["y1"], fb[b]["y1"])
            if ix0 >= ix1 or iy0 >= iy1:
                continue
            na = sum(1 for x, y in per_tool[a] if ix0 <= x <= ix1 and iy0 <= y <= iy1)
            nb = sum(1 for x, y in per_tool[b] if ix0 <= x <= ix1 and iy0 <= y <= iy1)
            if min(na / len(per_tool[a]), nb / len(per_tool[b])) >= 0.5:
                tool_overlap.append([a, b])
    seq, per = parse_tool_points(path)
    bboxes = {t: (_bbox_of(v) if v else None) for t, v in per.items()}
    big = [c for c in clusters if c["n"] >= 40]
    ok = not overlaps and not tool_overlap and len(big) >= 1
    return {"ok": ok,
            "first_layer_points": len(first),
            "clusters": clusters,
            "overlaps": overlaps,
            "first_layer_tool_overlap": tool_overlap,
            "t_sequence": seq,
            "tool_changes": max(0, len(seq) - 1),
            "tool_bboxes": bboxes}


# ---------- 换料横移碰撞检查 (verify_switch_safety) ----------
# 背景: KX 固件 ACE 换料例程会自主降 Z 横移到擦料位(黑盒路径)。
# 喷嘴上方 X 横梁距喷嘴约 30mm (height_to_rod); 换料横移时横梁扫过高度
# 最坏 ~Z+30。若床上有已完成件顶高超过该高度且横移路径可能穿过 -> 剐蹭。
# 撞机实证: 山竹旧版 T3 换料时紫帽38mm 在床, 横梁剐中帽顶 (m21488)。
BEAM_CLEARANCE = 30.0   # X横梁距喷嘴高度 (height_to_rod)
BEAM_FLOOR_Z   = 3.0    # 最坏假设: 固件换料横移时喷嘴降至该高度
COMPONENT_BOTTOM = 10.0 # 假说B: 组件底距喷嘴 (G-code Z 不降时的平移)


def _parse_objects_and_switches(txt):
    """返回 (tool_bbox, switch_events)
    tool_bbox: Tn -> [x0,x1,y0,y1,zmax]
    switch_events: [(tool, z_before, x_before, y_before)] 按出现序去重相邻
    """
    obj_re = re.compile(r';\s*printing object (\S+) id')
    tool_bbox = {}
    cur_tool = None
    cur_z = 0.0
    events = []
    for ln in txt.splitlines():
        s = ln.strip()
        mo = obj_re.match(ln)
        if mo:
            continue
        t = re.match(r'(T\d)$', s)
        if t:
            name = t.group(1)
            if cur_tool is not None and name != cur_tool:
                events.append(name)
            elif cur_tool is None:
                events.append(name)
            cur_tool = name
            tool_bbox.setdefault(name, [1e9, -1e9, 1e9, -1e9, 0.0])
            continue
        if s.startswith(';'):
            continue
        g = re.match(r'G[0123] ', s)
        if not g:
            continue
        xm = re.search(r'X(-?[\d.]+)', s)
        ym = re.search(r'Y(-?[\d.]+)', s)
        zm = re.search(r'Z(-?[\d.]+)', s)
        em = re.search(r'\bE(-?[\d.]+)', s)
        if zm and float(zm.group(1)) < 300:
            cur_z = float(zm.group(1))
        if cur_tool and xm and ym and em and float(em.group(1)) > 0:
            x = float(xm.group(1)); y = float(ym.group(1))
            bb = tool_bbox[cur_tool]
            bb[0] = min(bb[0], x); bb[1] = max(bb[1], x)
            bb[2] = min(bb[2], y); bb[3] = max(bb[3], y)
            bb[4] = max(bb[4], cur_z)
    return tool_bbox, events


def verify_switch_safety(path):
    """检查多色 by-object G-code 的每次换料横移是否会剐蹭床上件。

    机理(撞机实证 m21488): 固件 ACE 换料例程从「刚打完件」顶部出发自主降 Z
    横移到擦料位(黑盒)。喷嘴上方 X 横梁距喷嘴 30mm, 换料横移最坏扫过
    Z~3+30=33mm 高度带。
    分档:
      origin_hit: 换料起点件(刚打完的件)顶高 >33mm -> 高危 BLOCK
                  (山竹撞机=起点紫帽38被横梁剐中; 该件就在横移启动带上)
      path_hit:   其他床上已完成件顶高 >33mm -> 中危 WARN(横移路径黑盒,
                  4 个历史成功案例属此类=赌赢, 不保证下次)
      warn:       组件底=Z_before+10 假说下会被扫到的矮件 -> WARN
    返回 {ok, verdict, risk_level, switches:[...]}
    """
    txt = open(path, encoding='utf-8', errors='ignore').read()
    # by-object 检测: ; printing object 名字序列出现切换才是逐件模式。
    # 逐层多色(魅惑菇/旋风豌豆一板等十几例)换料后回原位继续打, 历史零撞 -> PASS。
    obj_seq = re.findall(r';\s*printing object (\S+) id', txt)
    n_parts = len(set(obj_seq))
    n_sw = sum(1 for a, b in zip(obj_seq, obj_seq[1:]) if a != b)
    # by-object: 名字切换次数 <= 件数(每件连续打完才换下一件);
    # 逐层: 名字频繁来回切换(每层都遍历各件) -> n_sw 远超 n_parts。
    by_object = bool(obj_seq) and n_sw <= max(1, n_parts)
    if not by_object:
        n_switch = len(re.findall(r'(?m)^T\d\s*$', txt))
        return {'ok': True, 'verdict': 'PASS', 'risk_level': 'PASS',
                'mode': 'by-layer', 'note': f'逐层模式({n_switch}次选料): 换料后返回当前层原位, 历史零撞机',
                'switches': [], 'tool_bbox': {}}
    tool_bbox, _ = _parse_objects_and_switches(txt)
    lines = txt.splitlines()
    cur_z = 0.0; cur_x = 0.0; cur_y = 0.0
    cur_tool = None
    events = []
    for ln in lines:
        s = ln.strip()
        if s.startswith(';'):
            continue
        t = re.match(r'(T\d)$', s)
        if t:
            name = t.group(1)
            if cur_tool is None or name != cur_tool:
                events.append((name, cur_z, cur_x, cur_y))
            cur_tool = name
            continue
        g = re.match(r'G[0123] ', s)
        if not g:
            continue
        xm = re.search(r'X(-?[\d.]+)', s)
        ym = re.search(r'Y(-?[\d.]+)', s)
        zm = re.search(r'Z(-?[\d.]+)', s)
        em = re.search(r'\bE(-?[\d.]+)', s)
        if zm and float(zm.group(1)) < 300:
            cur_z = float(zm.group(1))
        if xm:
            cur_x = float(xm.group(1))
        if ym:
            cur_y = float(ym.group(1))
        if cur_tool and xm and ym and em and float(em.group(1)) > 0:
            bb = tool_bbox[cur_tool]
            bb[0] = min(bb[0], cur_x); bb[1] = max(bb[1], cur_x)
            bb[2] = min(bb[2], cur_y); bb[3] = max(bb[3], cur_y)
            bb[4] = max(bb[4], cur_z)
    beam_floor = BEAM_FLOOR_Z + BEAM_CLEARANCE
    switches = []
    seen = []
    ok = True
    worst = 'PASS'
    for i, (name, z, x, y) in enumerate(events):
        if name not in seen:
            seen.append(name)
        origin = seen[-2] if len(seen) >= 2 else None
        others = [p for p in seen if p != name and p != origin]
        origin_hits = []
        path_hits = []
        warn_parts = []
        if origin:
            bb = tool_bbox.get(origin)
            if bb and bb[4] > beam_floor:
                origin_hits.append(f"{origin}(起点件顶{bb[4]:.0f}mm>横梁{beam_floor:.0f})")
        for d in others:
            bb = tool_bbox.get(d)
            if not bb:
                continue
            if bb[4] > beam_floor:
                path_hits.append(f"{d}(床上{bb[4]:.0f}mm>横梁{beam_floor:.0f})")
            elif bb[4] > z + COMPONENT_BOTTOM:
                warn_parts.append(f"{d}(顶{bb[4]:.0f}>组件底{z + COMPONENT_BOTTOM:.0f})")
        status = 'PASS'
        if origin_hits:
            status = 'BLOCK'; ok = False
            worst = 'BLOCK'
        elif path_hits or warn_parts:
            status = 'WARN'
            if worst != 'BLOCK':
                worst = 'WARN'
        switches.append({
            'tool': name, 'z_before': round(z, 2),
            'origin_part': origin, 'done_parts': [p for p in seen if p != name],
            'origin_hits': origin_hits, 'path_hits': path_hits,
            'warn_parts': warn_parts, 'status': status,
        })
    return {'ok': ok, 'verdict': worst, 'risk_level': worst, 'mode': 'by-object',
            'switches': switches, 'tool_bbox': {
                k: [round(v, 1) for v in bb] for k, bb in tool_bbox.items()}}

def _read_lines(path: str) -> list:
    return open(path, encoding='utf-8', errors='ignore').read().splitlines()

_EXTR_RE = re.compile(r'G1 .*E')
_E_RE = re.compile(r'\bE(-?[\d.]+)')


def verify_first_layer_anchoring(path: str) -> dict:
    """Per-object first-layer adhesion check.

    For each by-object part, inspect ITS first layer (the layer where the part
    starts printing) and classify adhesion:
      PASS  - brim/skirt present, or enough body perimeter+infill points
      WARN  - no brim and adhesion relies mainly on support
      BLOCK - no brim and body points < BODY_MIN (thin arms, will peel off)

    Born from the 2026-11-16 shanzhu green-leaf crash: star-shaped leaf with
    9% first-layer footprint, sliced with no_brim, peeled off at layer ~103
    and stuck to the nozzle. Calibration set: that part MUST block; every
    historically successful part must pass.
    """
    BODY_MIN = 100
    lines = _read_lines(path)
    obj_re = re.compile(r'; printing object (\S+) id')
    seen: dict = {}
    cur = None
    layer = 0
    cur_type = 'none'
    for ln in lines:
        mo = obj_re.match(ln)
        if mo:
            cur = mo.group(1)
            if cur not in seen:
                seen[cur] = {'layer': layer, 'brim': 0, 'body': 0, 'support': 0, 'ended': False}
            continue
        if ln.startswith(';LAYER_CHANGE'):
            layer += 1
        tm = re.match(r';TYPE:(\S+)', ln)
        if tm:
            cur_type = tm.group(1)
        if _EXTR_RE.match(ln) and cur in seen and not seen[cur]['ended']:
            em = _E_RE.search(ln)
            if em and float(em.group(1)) > 0:
                if cur_type in ('Brim', 'Skirt'):
                    seen[cur]['brim'] += 1
                elif cur_type.startswith('Support'):
                    seen[cur]['support'] += 1
                else:
                    seen[cur]['body'] += 1
        if ln.startswith('; stop printing object') and cur in seen:
            seen[cur]['ended'] = True
    objects = []
    for name, st in seen.items():
        if st['brim'] > 0:
            verdict, reason = 'PASS', f"brim={st['brim']}"
        elif st['body'] < BODY_MIN:
            verdict, reason = 'BLOCK', (f"no brim, body={st['body']}<{BODY_MIN} thin-part bare first layer "
                                        "(2026-11-16 shanzhu leaf peeled off at this pattern)")
        elif st['support'] > st['body'] * 2:
            verdict, reason = 'WARN', f"adhesion relies on support (sup={st['support']} body={st['body']})"
        else:
            verdict, reason = 'PASS', f"body={st['body']}"
        objects.append({'name': name, 'layer': st['layer'], 'brim': st['brim'],
                        'body': st['body'], 'support': st['support'],
                        'verdict': verdict, 'reason': reason})
    blocked = [o for o in objects if o['verdict'] == 'BLOCK']
    return {'ok': not blocked, 'objects': objects,
            'blocked': [o['name'] for o in blocked]}
