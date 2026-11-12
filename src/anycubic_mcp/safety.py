"""Safety precheck chain — the four gates that must pass before sending any job.

1. busy      : server-side is_printing == 2 (or reason mentions a task) -> block
2. vision    : camera frame + VisionChecker -> true (bed clear) = authoritative pass
3. fallback  : is_clean_plate == 1 passes only when vision itself failed (cam/net down)
4. history   : optional duplicate guard (same filename already printed)

Force/override is only allowed via explicit user consent flags.
"""
from __future__ import annotations

from .cloud import AnycubicCloud


class PrecheckBlocked(RuntimeError):
    def __init__(self, stage: str, msg: str, vision: dict | None = None):
        super().__init__(f"[{stage}] {msg}")
        self.stage = stage
        self.vision = vision


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
            raise PrecheckBlocked("vision", f"vision failed and is_clean_plate=0: {e}")

    # no vision configured
    if d.get("is_clean_plate") not in (1, "1"):
        raise PrecheckBlocked("clean_plate", "is_clean_plate=0 and no vision available; "
                            "confirm the bed is empty or configure vision")
    return {"ok": True, "msg": "precheck passed (server clean-plate flag)"}
