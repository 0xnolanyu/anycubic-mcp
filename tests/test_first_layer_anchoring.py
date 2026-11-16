"""First-layer adhesion gate tests (2026-11-16 shanzhu leaf crash)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anycubic_mcp.gcode_verify import verify_first_layer_anchoring


def test_blocks_bare_thin_part(tmp_path):
    g = tmp_path / "t.gcode"
    g.write_text(
        ";LAYER_CHANGE\n"
        ";TYPE:Outer\n"
        "; printing object star.stl id:0\n"
        "G1 X10 Y10 E.1\nG1 X11 Y10 E.2\n"
        "; stop printing object star.stl id:0\n",
        encoding="utf-8")
    r = verify_first_layer_anchoring(str(g))
    assert r["ok"] is False and r["blocked"] == ["star.stl"]


def test_passes_with_brim(tmp_path):
    g = tmp_path / "t.gcode"
    g.write_text(
        ";LAYER_CHANGE\n"
        ";TYPE:Brim\n"
        "; printing object star.stl id:0\n"
        "G1 X10 Y10 E.1\nG1 X11 Y10 E.2\n"
        ";TYPE:Outer\n"
        "G1 X12 Y10 E.3\n",
        encoding="utf-8")
    r = verify_first_layer_anchoring(str(g))
    assert r["ok"] is True


def test_passes_solid_part_without_brim(tmp_path):
    g = tmp_path / "t.gcode"
    body = "\n".join(f"G1 X{10 + i} Y10 E{1 + i * 0.1:.1f}" for i in range(150))
    g.write_text(
        ";LAYER_CHANGE\n"
        ";TYPE:Inner\n"
        "; printing object block.stl id:0\n" + body + "\n",
        encoding="utf-8")
    r = verify_first_layer_anchoring(str(g))
    assert r["ok"] is True
