"""TDD: 1-based slots conversion, duplicate-history gate, dry-run checklist."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

import pytest

from anycubic_mcp.server import _slots_to0
from anycubic_mcp.safety import PrecheckBlocked, dup_check


def test_slots_human_1based_to_api_0based():
    assert _slots_to0([1, 2]) == [0, 1]
    assert _slots_to0([4]) == [3]


def test_slots_reject_zero_and_out_of_range():
    with pytest.raises(ValueError):
        _slots_to0([0])          # 0 基残留：人类语义从 1 开始
    with pytest.raises(ValueError):
        _slots_to0([9])


class FakeCloud:
    def __init__(self, rows):
        self.rows = rows
    def print_history(self, page, limit):
        return self.rows


def test_dup_finished_blocks():
    c = FakeCloud([{"gcode_name": "cube.gcode", "print_status": "finished"}])
    with pytest.raises(PrecheckBlocked) as e:
        dup_check(c, r"E:\anywhere\cube.gcode", 1)
    assert e.value.stage == "dup"
    assert e.value.action  # 中文行动建议必须存在


def test_dup_suffix_and_case_insensitive():
    c = FakeCloud([{"gcode_name": "Cube (1).GCODE", "print_status": "finished"}])
    with pytest.raises(PrecheckBlocked):
        dup_check(c, "cube.gcode", 1)


def test_dup_stopped_allows_resend():
    c = FakeCloud([{"gcode_name": "cube.gcode", "print_status": "stoped"}])
    assert dup_check(c, "cube.gcode", 1)["ok"] is True


def test_dup_no_history_passes():
    assert dup_check(FakeCloud([]), "cube.gcode", 1)["ok"] is True
