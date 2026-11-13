"""Safety precheck chain — the gates that must pass before sending any job.

1. busy      : server-side is_printing == 2 (or reason mentions a task) -> block
2. vision    : camera frame + VisionChecker -> true (bed clear) = authoritative pass
3. fallback  : is_clean_plate == 1 passes only when vision itself failed (cam/net down)
4. history   : duplicate guard (same filename already finished -> block; stopped -> allow)

Force/override is only allowed via explicit user consent flags.
Every block carries a Chinese `action` hint (what to do next).
"""
from __future__ import annotations

import re
from pathlib import Path

from .cloud import AnycubicCloud

_ACTIONS = {
    "busy": "等当前任务完成后再发，或用 queue_add 排队等待",
    "vision": "床面检测到物体：取走成品后重试；确认床已空可用 force=True（用户明确同意）",
    "vision_down": "视觉服务不可用：重启拉流器或确认床空后 force=True",
    "clean_plate": "在打印机屏幕点『已清床』，或配置视觉（anycubic-setup）",
    "dup": "同名文件已打印过（>=95%）：确认要重打用 force=True，否则换文件名",
    "status": "打印机离线：检查网络与云连接",
}


class PrecheckBlocked(RuntimeError):
    def __init__(self, stage: str, msg: str, vision: dict | None = None):
        super().__init__(f"[{stage}] {msg}")
        self.stage = stage
        self.vision = vision
        self.action = _ACTIONS.get(stage, "检查后重试")


def precheck(cloud: AnycubicCloud, printer_id: int, vision,
             frame_path: str | None, *, skip_vision: bool = False,
             force: bool = False) -> dict:
    """Returns {'ok': True, 'msg': ...} or raises PrecheckBlocked."""
    if force:
        return {"ok": True, "msg": "force: checks skipped (user consented)"}

    printers = cloud.printers_status([printer_id])
    if not printers:
        raise PrecheckBlocked("status", "printer not found / offline")
    d = printers[0]
    if d.get("is_printing") == 2 or "任务" in (d.get("reason") or ""):
        raise PrecheckBlocked("busy", f"printer busy: {d.get('reason') or ''}")

    if vision is not None and frame_path is not None and not skip_vision:
        try:
            v = vision.check(frame_path)
            if v["bed_clear"] is False:
                raise PrecheckBlocked("vision", f"object on bed: {v.get('desc','')[:150]}", v)
            return {"ok": True, "msg": "precheck passed (vision confirmed empty bed)", "vision": v}
        except PrecheckBlocked:
            raise
        except Exception as e:  # vision infra down -> server flag fallback
            if d.get("is_clean_plate") in (1, "1"):
                return {"ok": True, "msg": f"vision failed ({e}); passed via server clean-plate flag"}
            raise PrecheckBlocked("vision_down", f"vision failed and is_clean_plate=0: {e}")

    # no vision configured
    if d.get("is_clean_plate") not in (1, "1"):
        raise PrecheckBlocked("clean_plate", "is_clean_plate=0 and no vision available; "
                            "confirm the bed is empty or configure vision")
    return {"ok": True, "msg": "precheck passed (server clean-plate flag)"}


def _norm(name: str) -> str:
    """Lowercase + strip .gcode + strip trailing (1)/(2) copy suffixes."""
    n = name.strip().lower()
    n = re.sub(r"\.gcode$", "", n)
    return re.sub(r"\s*\(\d+\)$", "", n).strip()


def dup_check(cloud: AnycubicCloud, file: str, printer_id: int) -> dict:
    """Block re-sending a file whose name already finished 100% (11-13 incident).

    A 'stoped' entry means it needs a resend -> allow.
    """
    want = _norm(Path(file).name)
    if not want:
        return {"ok": True, "msg": "no filename"}
    for h in cloud.print_history(1, 10):
        got = _norm(str(h.get("gcode_name") or ""))
        if got != want:
            continue
        st = str(h.get("print_status") or "")
        if "finish" in st.lower() or "100" in str(h.get("progress") or ""):
            raise PrecheckBlocked(
                "dup", f"{Path(file).name} 已在历史中标记完成 ({st})，"
                       f"结束于 {h.get('end_time') or '?'}")
        # stopped / failed -> resend expected
        return {"ok": True, "msg": f"同名记录状态为 {st}，允许重发"}
    return {"ok": True, "msg": "历史中无同名完成记录"}
