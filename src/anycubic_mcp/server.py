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
from .gcode_verify import verify as verify_gcode_file
from .queue import Queue
from .safety import PrecheckBlocked, dup_check, precheck
from .vision import VisionChecker, from_config as vision_from_config

mcp = FastMCP("anycubic-mcp")

_cfg = cfgmod.load()
_cloud: AnycubicCloud | None = None
_vision: VisionChecker | None = vision_from_config(_cfg)
_queue = Queue(_cfg["queue_file"])

# how many physical slots the ACE unit exposes (KX = 4)
MAX_SLOTS = 4


def _slots_to0(slots: list[int]) -> list[int]:
    """Human 1-based slots (槽1 = first spool) -> Anycubic 0-based API indices.

    The 0/1-based mix caused two real incidents; the public API speaks 1-based,
    conversion to the 0-based wire format happens ONLY here.
    """
    out = []
    for s in slots or []:
        if not isinstance(s, int) or s < 1 or s > MAX_SLOTS:
            raise ValueError(
                f"槽位须为 1~{MAX_SLOTS} 的整数（1=第一个料槽，人类语义）；收到 {s!r}。"
                f"例如白色在第三个槽就传 3，不要传 2")
        out.append(s - 1)
    if not out:
        raise ValueError("slots 不能为空：至少给一个槽位（1 基，如 [1]）")
    return out


def _slot_map(slots0: list[int]) -> list[str]:
    """Human-readable echo of the resolved mapping (returned in every send result)."""
    return [f"槽{s + 1} -> 料位/T{s}" for s in slots0]


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
                   force: bool = False, dry_run: bool = False) -> dict:
    """Precheck -> upload -> start print. Safe default.

    slots use HUMAN 1-based numbering (槽1 = first spool); converted internally.
    dry_run=True runs every check (status/vision/dup/gcode-verify) and returns
    the checklist WITHOUT uploading or starting anything.
    """
    slots0 = _slots_to0(slots)
    c = _api()
    frame = None
    try:
        if _cfg["camera_enabled"] and _cfg["kx_cam_exe"]:
            frame = capture_frame(c, _cfg["printer_id"], _cfg["kx_cam_exe"])
    except Exception:
        frame = None  # vision infra down -> safety falls back to server flag
    pre = precheck(c, _cfg["printer_id"], _vision, frame,
                   skip_vision=skip_vision, force=force)
    dup = dup_check(c, file, _cfg["printer_id"])
    if dry_run:
        checks = {"状态": "空闲" if not force else "force 跳过",
                  "视觉": pre["msg"], "查重": dup["msg"]}
        try:
            v = verify_gcode_file(file)
            checks["首层验证"] = ("通过，" + ", ".join(v["t_sequence"]) +
                                f"，{v['tool_changes']} 次换料") if v["ok"] else \
                               ("FAIL: " + str(v.get("first_layer_tool_overlap") or v.get("overlaps")))
        except Exception as e:
            checks["首层验证"] = f"无法解析: {e}"
        return {"dry_run": True, "file": file, "slot_map": _slot_map(slots0),
                "checks": checks, "next": "确认无误后 dry_run=False 实发"}
    fid = c.upload_gcode(file)
    printers = c.printers_status([_cfg["printer_id"]])
    mapping = _mapping(printers[0], slots0)
    c.start_print(_cfg["printer_id"], fid, mapping)
    return {"ok": True, "cloud_file_id": fid, "precheck": pre["msg"],
            "slot_map": _slot_map(slots0)}


@mcp.tool()
def verify_gcode(file: str) -> dict:
    """First-layer scatter + tool-sequence sanity check BEFORE sending.

    Detects the stacked-plates incident (parts moved onto each other by
    move_object_to_plate) and wrong tool assignment. Run on every new G-code.
    """
    r = verify_gcode_file(file)
    r["action"] = ("ok，可发送" if r["ok"] else
                   "首层重叠/多工具同区，先核对摆盘再发（orca_layout check）")
    return r


@mcp.tool()
def cancel_print() -> dict:
    """Cancel the current print job."""
    return {"ok": True, "result": _api().cancel_print(_cfg["printer_id"])}


# ------------------------------------------------------- local queue ----

@mcp.tool()
def queue_add(file: str, slots: list[int], note: str = "") -> dict:
    """Append a job to the local queue (slots are HUMAN 1-based)."""
    global _queue
    slots0 = _slots_to0(slots)
    _queue = Queue(_cfg["queue_file"])
    t = _queue.add(file, slots0, note)
    return {"queued": t["id"], "file": file, "slot_map": _slot_map(slots0)}


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
        dup_check(c, head["file"], _cfg["printer_id"])
        fid = c.upload_gcode(head["file"])
        mapping = _mapping(d, head["slots"])
        c.start_print(_cfg["printer_id"], fid, mapping)
        q.mark(head["id"], "running", cloud_file_id=fid)
        return {"action": "started", "id": head["id"], "cloud_file_id": fid}
    except PrecheckBlocked as e:
        return {"action": "blocked", "id": head["id"], "stage": e.stage,
                "msg": str(e)[:200], "action_hint": e.action}
    except Exception as e:
        return {"action": "failed", "id": head["id"], "msg": str(e)[:200]}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
