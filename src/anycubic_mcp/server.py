"""FastMCP server entry — all tools exposed for any MCP client.

Run: anycubic-mcp            (stdio transport, config auto-loaded)
"""
from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import config as cfgmod
from .camera import capture_frame
from .cloud import AnycubicCloud
from .queue import Queue
from .safety import PrecheckBlocked, precheck
from .vision import VisionChecker, from_config as vision_from_config

mcp = FastMCP("anycubic-mcp")

_cfg = cfgmod.load()
_cloud: AnycubicCloud | None = None
_vision: VisionChecker | None = vision_from_config(_cfg)
_queue = Queue(_cfg["queue_file"])


def _api() -> AnycubicCloud:
    global _cloud
    if _cloud is None:
        if not _cfg["token"]:
            raise RuntimeError("no token: run anycubic-setup or set ANYCUBIC_TOKEN")
        _cloud = AnycubicCloud(_cfg["token"])
    return _cloud


def _mapping(printers_payload: dict, slots: list[int]) -> list[dict]:
    """Build ams_box_mapping from printer payload slots + requested slot order."""
    boxes = printers_payload.get("multi_color_box") or []
    mapping = []
    for i, slot in enumerate(slots):
        box = boxes[slot // 4] if slot // 4 < len(boxes) else (boxes[0] if boxes else {})
        s = ((box.get("slots") or [{}] * 4)[slot % 4]) if box else {}
        mapping.append({
            "spool_index": slot,
            "filament_used": "1",
            "material_type": "PLA",
            "color_red": s.get("color_red", 255),
            "color_green": s.get("color_green", 255),
            "color_blue": s.get("color_blue", 255),
            "paint_index": i,
        })
    return mapping


# ---------------------------------------------------------------- tools ----

@mcp.tool()
def printer_status() -> dict:
    """Printer live status: busy/idle, temps, clean-plate flag, firmware."""
    d = _api().printers_status([_cfg["printer_id"]])[0]
    return {"is_printing": d.get("is_printing"),
            "is_clean_plate": d.get("is_clean_plate"),
            "reason": d.get("reason"),
            "nozzle_temp": d.get("parameter", {}).get("curr_nozzle_temp"),
            "bed_temp": d.get("parameter", {}).get("curr_hotbed_temp"),
            "firmware": d.get("version", {}).get("firmware_version")}


@mcp.tool()
def print_history(page: int = 1, limit: int = 20) -> dict:
    """Recent print jobs (name/state/progress/times) — use to avoid duplicates."""
    rows = _api().print_history(page, limit)
    out = []
    for h in rows:
        out.append({"name": h.get("gcode_name"), "status": h.get("print_status"),
                    "start": h.get("create_time"), "end": h.get("end_time"),
                    "reason": h.get("reason")})
    return {"items": out}


@mcp.tool()
def bed_photo(vision: bool = True) -> dict:
    """Capture one camera frame; optionally run the vision bed-check."""
    c = _api()
    frame = capture_frame(c, _cfg["printer_id"], _cfg["kx_cam_exe"])
    if not vision or _vision is None:
        return {"frame": frame}
    v = _vision.check(frame)
    return {"frame": frame, **v}


@mcp.tool()
def upload_gcode(file: str) -> dict:
    """Upload a G-code file to Anycubic cloud; returns cloud_file_id."""
    return {"cloud_file_id": _api().upload_gcode(file)}


@mcp.tool()
def send_print_job(file: str, slots: list[int], *, skip_vision: bool = False,
                   force: bool = False) -> dict:
    """Precheck (busy/vision/clean-plate) -> upload -> start print. Safe default."""
    c = _api()
    frame = None
    try:
        if _cfg["camera_enabled"] and _cfg["kx_cam_exe"]:
            frame = capture_frame(c, _cfg["printer_id"], _cfg["kx_cam_exe"])
    except Exception as e:
        frame = None  # vision infra down -> safety falls back to server flag
        _last_cam_err = str(e)
    pre = precheck(c, _cfg["printer_id"], _vision, frame,
                   skip_vision=skip_vision, force=force)
    fid = c.upload_gcode(file)
    printers = c.printers_status([_cfg["printer_id"]])
    mapping = _mapping(printers[0], slots)
    c.start_print(_cfg["printer_id"], fid, mapping)
    return {"ok": True, "cloud_file_id": fid, "precheck": pre["msg"]}


@mcp.tool()
def cancel_print() -> dict:
    """Cancel the current print job."""
    return {"ok": True, "result": _api().cancel_print(_cfg["printer_id"])}


# ------------------------------------------------------- local queue ----

@mcp.tool()
def queue_add(file: str, slots: list[int], note: str = "") -> dict:
    """Append a job to the local queue."""
    global _queue
    _queue = Queue(_cfg["queue_file"])
    t = _queue.add(file, slots, note)
    return {"queued": t["id"], "file": file}


@mcp.tool()
def queue_list() -> dict:
    return {"items": Queue(_cfg["queue_file"]).items}


@mcp.tool()
def queue_remove(id: int) -> dict:
    q = Queue(_cfg["queue_file"])
    return {"removed": q.remove(id)}


@mcp.tool()
def queue_process() -> dict:
    """One queue iteration (safe to call on a timer)."""
    q = Queue(_cfg["queue_file"])
    head = q.head()
    if head is None:
        return {"action": "empty"}
    c = _api()
    d = c.printers_status([_cfg["printer_id"]])[0]
    if head["status"] == "running":
        if d.get("is_printing") == 2:
            return {"action": "waiting_print", "id": head["id"]}
        q.mark(head["id"], "done")
        return queue_process()
    # pending -> try start
    try:
        frame = None
        if _cfg["camera_enabled"] and _cfg["kx_cam_exe"]:
            try:
                frame = capture_frame(c, _cfg["printer_id"], _cfg["kx_cam_exe"])
            except Exception:
                frame = None
        precheck(c, _cfg["printer_id"], _vision, frame)
        fid = c.upload_gcode(head["file"])
        mapping = _mapping(d, head["slots"])
        c.start_print(_cfg["printer_id"], fid, mapping)
        q.mark(head["id"], "running", cloud_file_id=fid)
        return {"action": "started", "id": head["id"], "cloud_file_id": fid}
    except PrecheckBlocked as e:
        return {"action": "blocked", "id": head["id"], "stage": e.stage, "msg": str(e)[:200]}
    except Exception as e:
        return {"action": "failed", "id": head["id"], "msg": str(e)[:200]}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
