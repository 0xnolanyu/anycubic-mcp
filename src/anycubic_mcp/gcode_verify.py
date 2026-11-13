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
